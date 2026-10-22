"""Deterministic attention cards preserve source authority and strict size limits."""
import json
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.sessions import SessionService
from app.domains.management import ManagementService
from app.domains.management_attention_context import ManagementAttentionContext
from app.domains.management_goals import ManagementGoalService
from app.domains.matters import MatterCreateInput, MatterService
from app.storage.db import connect, init_db

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "attention-context.sqlite3"
    init_db(path)
    factory = lambda: connect(path)
    sessions = SessionService(factory)
    management = ManagementService(factory, sessions)
    goals = ManagementGoalService(factory, management)
    goals.ensure_schema()
    matters = MatterService(factory)
    goals.work_service = SimpleNamespace(matters=matters)
    sid = management.create()["session"]["session_id"]
    with closing(factory()) as conn, conn:
        conn.execute("CREATE TABLE manager_goal_matter_links(goal_id TEXT PRIMARY KEY,manager_session_id TEXT,matter_id TEXT)")
        conn.execute("CREATE TABLE managed_session_links(manager_session_id TEXT,task_session_id TEXT,run_id TEXT)")
    return factory, sessions, management, goals, matters, sid


def build(state, refs, **options):
    with closing(state[0]()) as conn:
        return ManagementAttentionContext(state[3], **options).build(conn, state[5], refs, now=NOW)


def bind(state, *, title, **values):
    factory, _, _, goals, matters, sid = state
    matter = matters.create_matter(MatterCreateInput(title=title, **values))
    goal = goals.create_goal(sid, objective="Original bound goal")
    with closing(factory()) as conn, conn:
        conn.execute("INSERT INTO manager_goal_matter_links VALUES(?,?,?)", (goal["goal_id"], sid, matter.matter_id))
    return goal, matter


def task(state, status, *, owner=None, child=False, deleted=False):
    factory, sessions, _, _, _, sid = state
    session = sessions.create_session().session.session_id
    run = "run_" + session
    record = {"run_id": run, "session_id": session, "status": status,
              "parent_run_id": "parent" if child else None,
              "metadata": {"pending_user_question": {"question": "PRIVATE QUESTION"}}}
    with closing(factory()) as conn, conn:
        conn.execute("INSERT INTO agent_runs VALUES(?,?,?,?,?,?)",
                     (run, session, status, json.dumps(record), NOW.isoformat(), NOW.isoformat()))
        conn.execute("INSERT INTO managed_session_links VALUES(?,?,?)", (owner or sid, session, run))
        if deleted:
            conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (session,))
    return run


def test_ranking_deadline_priority_and_authorized_matter_projection(state):
    low, _ = bind(state, title="Ordinary", priority="low")
    urgent, matter = bind(state, title="Overdue approval", priority="urgent", due_at="2026-10-21",
                          summary="Authorized business summary", status="waiting")
    result = build(state, {low["goal_id"]: 1, urgent["goal_id"]: 1})
    card = result["items"][0]
    assert card["goal_id"] == urgent["goal_id"]
    assert card["matter"]["matter_id"] == matter.matter_id
    assert card["matter"]["due_at"] == "2026-10-21"
    assert card["objective"].startswith("Overdue approval")
    assert {"matter_deadline_overdue", "matter_priority_urgent", "matter_waiting"} <= set(card["attention_reasons"])
    assert card["severity"] == "high"
    assert card["source_snapshot_at"] == NOW.isoformat()


@pytest.mark.parametrize("status,reason", [("failed", "owned_task_failed"),
    ("timed_out", "owned_task_timed_out"), ("waiting_user", "owned_task_waiting_user"),
    ("waiting_confirmation", "owned_task_waiting_confirmation"),
    ("completed", "owned_task_terminal_needs_business_review")])
def test_owned_task_actual_status_without_claiming_business_completion(state, status, reason):
    run = task(state, status)
    goal = state[3].create_goal(state[5], objective="Pending acceptance", wait_run_id=run)
    result = build(state, {goal["goal_id"]: 1})
    card = result["items"][0]
    assert card["wait_run"]["run_id"] == run
    assert card["wait_run"]["status"] == status
    assert reason in card["attention_reasons"]
    assert "PRIVATE QUESTION" not in json.dumps(result)
    assert "business completion are not established" in result["note"]


@pytest.mark.parametrize("options", [{"owner": "foreign-manager"}, {"deleted": True}, {"child": True}])
def test_foreign_deleted_and_child_task_references_do_not_leak(state, options):
    run = task(state, "failed", **options)
    # Persist the relationship directly to exercise defensive handling of stale
    # or legacy references that normal goal-write validation would reject.
    goal = state[3].create_goal(state[5], objective="Owned objective")
    with closing(state[0]()) as conn, conn:
        conn.execute("UPDATE manager_goals SET wait_run_id=? WHERE goal_id=?", (run, goal["goal_id"]))
    result = build(state, {goal["goal_id"]: 1})
    assert "wait_run" not in result["items"][0]
    assert run not in json.dumps(result)
    assert "owned_task_failed" not in json.dumps(result)


def test_bound_source_denial_omits_goal_and_reader_handle(state, monkeypatch):
    goal, matter = bind(state, title="SECRET SOURCE TITLE", summary="SECRET SOURCE BODY")
    original = state[4].get_matter
    seen = []
    def guarded(*, matter_id, conn=None):
        seen.append(conn)
        if matter_id == matter.matter_id:
            raise PermissionError("Source access denied")
        return original(matter_id=matter_id, conn=conn)
    monkeypatch.setattr(state[4], "get_matter", guarded)
    result = build(state, {goal["goal_id"]: 1})
    assert result["items"] == [] and result["omitted_refs"] == []
    assert "SECRET" not in json.dumps(result)
    assert goal["goal_id"] not in json.dumps(result)
    assert seen and all(conn is not None for conn in seen)


def test_long_objectives_bounds_and_read_more_are_exact_and_deterministic(state):
    goals = [state[3].create_goal(state[5], objective="跨轮责任与来源" * 1000) for _ in range(16)]
    refs = {goal["goal_id"]: 1 for goal in goals}
    result = build(state, refs)
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) <= 4500
    assert 0 < len(result["items"]) <= 8
    assert result["omitted_count"] == 16 - len(result["items"])
    card = result["items"][0]
    assert card["objective_truncated"] is True
    assert card["continuation"] == {"tool": "management.read_goal", "input": {
        "goal_id": card["goal_id"], "start_char": len(card["objective"])}}
    assert result == build(state, dict(reversed(list(refs.items()))))
    assert [item["goal_id"] for item in result["items"]] == sorted(item["goal_id"] for item in result["items"])
    for limit in (2, 40, 100, 500, 1000):
        assert len(json.dumps(build(state, refs, max_chars=limit), ensure_ascii=False,
                              separators=(',', ':'))) <= limit


def test_json_expansion_cannot_drop_the_last_fitting_responsibility(state):
    goal = state[3].create_goal(state[5], objective='x' + '\x00' * 15999)
    result = build(state, {goal['goal_id']: 1})
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) <= 4500
    assert len(result['items']) == 1
    card = result['items'][0]
    assert card['goal_id'] == goal['goal_id']
    assert card['objective_truncated'] and card['details_truncated']
    assert card['continuation']['input']['start_char'] == len(card['objective'])


def test_frozen_revision_session_scope_and_explicit_due_and_age(state):
    goal = state[3].create_goal(state[5], objective="Accepted work", next_wake_at="2026-10-21T00:00:00+00:00")
    foreign = state[2].create()["session"]["session_id"]
    foreign_goal = state[3].create_goal(foreign, objective="FOREIGN OBJECTIVE")
    with closing(state[0]()) as conn, conn:
        conn.execute("UPDATE manager_goals SET updated_at='2026-10-01T00:00:00+00:00' WHERE goal_id=?", (goal["goal_id"],))
    result = build(state, {goal["goal_id"]: 1, foreign_goal["goal_id"]: 1})
    assert len(result["items"]) == 1
    assert {"explicit_check_due", "goal_record_not_updated_for_seven_days"} <= set(result["items"][0]["attention_reasons"])
    assert "FOREIGN" not in json.dumps(result)
    assert build(state, {goal["goal_id"]: 2})["items"] == []
    state[3].update_goal(state[5], goal["goal_id"], expected_revision=1, status="cancelled")
    assert build(state, {goal["goal_id"]: 2})["items"] == []


def test_constructor_and_batch_bounds(state):
    for options in ({"max_chars": 1}, {"max_items": 9}, {"max_items": 0}):
        with pytest.raises(ValueError):
            ManagementAttentionContext(state[3], **options)
    with pytest.raises(ValueError):
        build(state, {f"goal-{i}": 1 for i in range(17)})


def test_standalone_matter_event_preloads_without_a_goal(state):
    matter = state[4].create_matter(MatterCreateInput(title="Release awaiting external validation",
        summary="Keep the already accepted release condition", due_at="2026-10-21", priority="high"))
    with closing(state[0]()) as conn:
        result = ManagementAttentionContext(state[3]).build(
            conn, state[5], {}, now=NOW, matter_revisions={matter.matter_id: matter.revision})
    assert len(result["items"]) == 1
    card = result["items"][0]
    assert card["kind"] == "matter" and card["matter_id"] == matter.matter_id
    assert "goal_id" not in card
    assert card["objective"] == matter.title + "\n" + matter.summary
    assert "matter_deadline_overdue" in card["attention_reasons"]
    assert result["omitted_count"] == 0
    assert state[3].list_goals(state[5])["goals"] == []


def test_standalone_matter_requires_current_authorized_nonterminal_source(state, monkeypatch):
    matter = state[4].create_matter(MatterCreateInput(title="SOURCE SECRET"))
    with closing(state[0]()) as conn:
        projection = ManagementAttentionContext(state[3])
        stale = projection.build(conn, state[5], {}, now=NOW, matter_revisions={matter.matter_id: 99})
        assert stale["items"] == [] and stale["omitted_refs"] == []
    def denied(**kwargs):
        assert kwargs["conn"] is not None
        raise PermissionError("Denied source")
    monkeypatch.setattr(state[4], "get_matter", denied)
    with closing(state[0]()) as conn:
        result = projection.build(conn, state[5], {}, now=NOW, matter_revisions={matter.matter_id: 1})
    assert result["items"] == [] and result["omitted_refs"] == []
    assert "SECRET" not in json.dumps(result) and matter.matter_id not in json.dumps(result)


@pytest.mark.parametrize("status,deleted", [("done", False), ("cancelled", False), ("open", True)])
def test_standalone_matter_terminal_or_deleted_data_is_not_preloaded(state, status, deleted):
    matter = state[4].create_matter(MatterCreateInput(title="Finished or deleted", status=status))
    if deleted:
        with closing(state[0]()) as conn, conn:
            conn.execute("UPDATE matters SET deleted_at=? WHERE matter_id=?", (NOW.isoformat(), matter.matter_id))
    with closing(state[0]()) as conn:
        result = ManagementAttentionContext(state[3]).build(
            conn, state[5], {}, now=NOW, matter_revisions={matter.matter_id: matter.revision})
    assert result["items"] == []


def test_bound_and_standalone_same_matter_coalesce_and_include_saved_management(state):
    from app.domains.management_work import ManagementWorkService
    goal, matter = bind(state, title="Accepted dependency", status="waiting")
    state[3].work_service._sidecar = ManagementWorkService._sidecar
    with closing(state[0]()) as conn, conn:
        conn.execute("CREATE TABLE matter_management(matter_id TEXT,manager_session_id TEXT,next_action TEXT,"
                     "responsible_party TEXT,wait_condition TEXT,management_state TEXT)")
        conn.execute("INSERT INTO matter_management VALUES(?,?,?,?,?,?)", (
            matter.matter_id, state[5], "Check the approved external validation" * 40, "external",
            json.dumps({"type": "external", "description": "Wait for validation", "until": "2026-10-23T00:00:00Z",
                        "reference": "DO_NOT_EXPOSE_RAW_REFERENCE"}), "active"))
        result = ManagementAttentionContext(state[3]).build(conn, state[5], {goal["goal_id"]: 1}, now=NOW,
                                                           matter_revisions={matter.matter_id: 1})
    assert len(result["items"]) == 1 and result["items"][0]["kind"] == "goal"
    assert result["omitted_count"] == 0
    saved = result["items"][0]["management"]
    assert saved["responsible_party"] == "external" and len(saved["next_action"]) == 400
    assert saved["wait_condition"]["type"] == "external"
    assert "DO_NOT_EXPOSE_RAW_REFERENCE" not in json.dumps(result)
    with closing(state[0]()) as conn:
        standalone = ManagementAttentionContext(state[3]).build(conn, state[5], {}, now=NOW,
                                                                matter_revisions={matter.matter_id: 1})
    assert standalone["items"][0]["management"] == saved


def test_standalone_matter_truncation_and_typed_omitted_reader(state):
    long = state[4].create_matter(MatterCreateInput(title="Long source", summary="长期来源依据" * 1000))
    high, _ = bind(state, title="Urgent existing responsibility", priority="urgent", due_at="2026-10-21")
    with closing(state[0]()) as conn:
        standalone = ManagementAttentionContext(state[3]).build(conn, state[5], {}, now=NOW,
                                                                matter_revisions={long.matter_id: 1})
        omitted = ManagementAttentionContext(state[3], max_items=1).build(conn, state[5], {high["goal_id"]: 1}, now=NOW,
                                                                         matter_revisions={long.matter_id: 1})
    card = standalone["items"][0]
    assert len(card["objective"]) == 1200 and card["objective_truncated"]
    assert card["continuation"] == {"tool": "management.inspect_work", "input": {"matter_id": long.matter_id}}
    assert len(json.dumps(standalone, ensure_ascii=False, separators=(",", ":"))) <= 4500
    ref = omitted["omitted_refs"][0]
    assert ref["kind"] == "matter" and ref["matter_id"] == long.matter_id
    assert ref["reader"] == card["continuation"]
