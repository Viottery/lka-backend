"""Bound Matter is business authority; manager goal remains a scheduling record."""
from contextlib import closing
from datetime import UTC, datetime

import pytest

from app.domains.management import ManagementConflictError
from app.domains.management_work import ManagementWorkService
from app.domains.matters import MatterCreateInput, MatterService, MatterUpdateInput
from tests.test_management_goals import state as state_fixture
from tests.test_management_goals import task


@pytest.fixture
def state(tmp_path):
    return state_fixture.__wrapped__(tmp_path)


@pytest.fixture
def bound(state):
    factory, _, management, goals, sid = state
    matters = MatterService(factory, internal_items_enabled=lambda: True)
    work = ManagementWorkService(factory, matters=matters, tasks=None, management=management,
                                 goals=goals, run_store=None, run_manager=None)
    work.ensure_schema()
    goals.work_service = work
    matter = matters.create_matter(MatterCreateInput(title="Current responsibility", summary="Current evidence", visibility="internal"))
    goal = goals.create_goal(sid, objective="Legacy intent", status="waiting")
    with closing(factory()) as conn, conn:
        conn.execute("INSERT INTO manager_goal_matter_links VALUES(?,?,?)", (goal["goal_id"], sid, matter.matter_id))
    return factory, goals, matters, sid, goal, matter


def test_get_and_list_project_current_matter_without_rewriting_scheduler(bound):
    factory, goals, matters, sid, goal, matter = bound
    changed = matters.update_matter(matter_id=matter.matter_id,
                                   payload=MatterUpdateInput(title="Refined title", summary="New evidence", status="in_progress", expected_revision=1))
    projected = goals.get_goal(sid, goal["goal_id"])
    assert projected["objective"] == "Refined title\nNew evidence"
    assert projected["status"] == "waiting" and projected["revision"] == 1
    assert (projected["matter_id"], projected["matter_revision"], projected["matter_status"]) == (matter.matter_id, changed.revision, "in_progress")
    assert goals.list_goals(sid)["goals"] == [projected]
    with closing(factory()) as conn:
        raw = conn.execute("SELECT objective,status,revision FROM manager_goals WHERE goal_id=?", (goal["goal_id"],)).fetchone()
        assert tuple(raw) == ("Legacy intent", "waiting", 1)


@pytest.mark.parametrize("change", [{"objective": "Shadow objective"}, {"status": "completed"}, {"status": "waiting"}])
def test_bound_business_writes_rejected_even_same_status_and_rollback(bound, change):
    factory, goals, _, sid, goal, _ = bound
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ManagementConflictError, match="belong to Matter"):
            goals.update_goal_in_transaction(conn, sid, goal["goal_id"], expected_revision=1, **change)
        assert conn.in_transaction
    assert goals.get_goal(sid, goal["goal_id"])["revision"] == 1


def test_bound_timer_wait_updates_allowed_in_caller_transaction(bound, state):
    factory, goals, _, sid, goal, matter = bound
    run_id, _ = task(state)
    with closing(factory()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = goals.update_goal_in_transaction(conn, sid, goal["goal_id"], expected_revision=1,
                                                   next_wake_at="2026-10-20T10:00:00+08:00", wait_run_id=run_id)
        assert updated["matter_id"] == matter.matter_id and updated["objective"] == "Current responsibility\nCurrent evidence"
        assert updated["revision"] == updated["timer_generation"] == 2 and conn.in_transaction
        assert updated["next_wake_at"] == "2026-10-20T02:00:00+00:00"
        conn.rollback()
    assert goals.get_goal(sid, goal["goal_id"])["wait_run_id"] is None
    unbound = goals.create_goal(sid, objective="Unbound")
    changed = goals.update_goal(sid, unbound["goal_id"], expected_revision=1, objective="Still supported", status="paused")
    assert changed["status"] == "paused" and changed["objective"] == "Still supported"


@pytest.mark.parametrize("blocking", ["done", "cancelled", "management_paused"])
def test_capture_fences_bound_timer_and_terminal_but_preserves_evidence_for_resume(bound, state, blocking):
    factory, goals, matters, sid, goal, matter = bound
    run_id, _ = task(state)
    goals.update_goal(sid, goal["goal_id"], expected_revision=1, next_wake_at=datetime.now(UTC).isoformat(), wait_run_id=run_id)
    if blocking == "management_paused":
        with closing(factory()) as conn, conn:
            conn.execute("INSERT INTO matter_management VALUES(?,?, 'goal','agent','Next',NULL,'paused',1)", (matter.matter_id, sid))
    else:
        matters.update_matter(matter_id=matter.matter_id, payload=MatterUpdateInput(status=blocking))
    with closing(factory()) as conn:
        conn.execute("BEGIN")
        assert goals.enabled_in_transaction(conn, goal["goal_id"]) is False
    assert goals.capture_events() == 0
    assert goals.get_goal(sid, goal["goal_id"])["next_wake_at"] is not None
    assert goals.list_events(sid)["events"] == []
    if blocking == "management_paused":
        with closing(factory()) as conn, conn:
            conn.execute("UPDATE matter_management SET management_state='active'")
    else:
        matters.update_matter(matter_id=matter.matter_id, payload=MatterUpdateInput(status="open"))
    assert goals.capture_events() == 2
    assert goals.capture_events() == 0
    events = goals.list_events(sid)["events"]
    assert {row["kind"] for row in events} == {"timer", "task_terminal"}
    assert all(row["goal_revision"] == 2 for row in events)


def test_bound_reader_uses_domain_authority_same_transaction_and_fails_closed(bound, monkeypatch):
    factory, goals, matters, sid, goal, _ = bound
    seen = []
    def forbidden(*, matter_id, conn):
        assert conn.in_transaction
        seen.append(matter_id)
        raise PermissionError("source_scope_denied")
    monkeypatch.setattr(matters, "get_matter", forbidden)
    with pytest.raises(PermissionError, match="source_scope_denied"):
        goals.get_goal(sid, goal["goal_id"])
    with pytest.raises(PermissionError, match="source_scope_denied"):
        goals.list_goals(sid)
    with closing(factory()) as conn:
        conn.execute("BEGIN")
        assert goals.enabled_in_transaction(conn, goal["goal_id"]) is False
        with pytest.raises(PermissionError, match="source_scope_denied"):
            goals.update_goal_in_transaction(conn, sid, goal["goal_id"], expected_revision=1, next_wake_at=None)
        assert conn.execute("SELECT revision FROM manager_goals WHERE goal_id=?", (goal["goal_id"],)).fetchone()[0] == 1
    assert len(seen) == 4
    goals.work_service = None
    with pytest.raises(ManagementConflictError, match="unavailable"):
        goals.get_goal(sid, goal["goal_id"])


def test_unbound_schema_is_optional_and_scheduler_status_still_applies(state):
    factory, _, _, goals, sid = state
    goal = goals.create_goal(sid, objective="Legacy goal")
    assert goals.get_goal(sid, goal["goal_id"])["objective"] == "Legacy goal"
    assert "matter_id" not in goals.list_goals(sid)["goals"][0]
    with closing(factory()) as conn:
        conn.execute("BEGIN")
        assert goals.enabled_in_transaction(conn, goal["goal_id"])
        assert not goals.enabled_in_transaction(conn, "missing")
    goals.update_goal(sid, goal["goal_id"], expected_revision=1, status="paused")
    with closing(factory()) as conn:
        assert not goals.enabled_in_transaction(conn, goal["goal_id"])
