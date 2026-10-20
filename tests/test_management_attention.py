"""Disposable SQLite coverage for opt-in, paged and deduplicated attention."""
import json
from contextlib import closing
from datetime import UTC, datetime

import pytest

from app.core.sessions import SessionService
from app.domains.management import ManagementConflictError, ManagementService
from app.domains.management_attention import ManagementAttentionService
from app.domains.management_goals import ManagementGoalService
from app.domains.matters import MatterCreateInput, MatterService, MatterUpdateInput
from app.storage.db import connect, init_db

NOW = datetime(2026, 10, 20, 2, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "attention.sqlite3"
    init_db(path)
    factory = lambda: connect(path)
    management = ManagementService(factory, SessionService(factory))
    management.ensure_schema()
    goals = ManagementGoalService(factory, management)
    goals.ensure_schema()
    matters = MatterService(factory, internal_items_enabled=lambda: True)
    service = ManagementAttentionService(factory, matters=matters, goals=goals, management=management)
    service.ensure_schema()
    sid = management.create()["session"]["session_id"]
    management.update(sid, expected_revision=1, status="active")
    return factory, management, goals, matters, service, sid


def make(state, **fields):
    return state[3].create_matter(MatterCreateInput(**{"title": "PRIVATE TITLE", **fields}))


def events(state):
    with closing(state[0]()) as conn:
        rows = conn.execute("SELECT * FROM manager_inbox ORDER BY event_id").fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def enable(state, **values):
    return state[4].configure(state[5], expected_revision=0, enabled=True, **values)


def test_default_policy_is_off_and_configuration_is_cas_not_a_schedule(state):
    _, _, _, _, service, sid = state
    make(state, due_at="2026-10-19")
    assert service.get(sid) is None
    assert service.scan(sid, NOW) == {"captured": 0, "scanned": 0, "next_offset": None}
    assert service.tick(NOW) == 0
    policy = enable(state)
    assert policy["revision"] == 1 and policy["enabled"] is True
    with pytest.raises(ManagementConflictError):
        service.configure(sid, expected_revision=0, enabled=True)
    assert service.tick(NOW) == 1


def test_due_and_local_scheduling_phases_exclude_internal_and_finished(state):
    _, _, _, _, service, sid = state
    enable(state)
    make(state, due_at="2026-10-19")
    make(state, due_at="2026-10-20")
    make(state, due_at="2026-10-23T15:00:00+08:00")
    make(state, scheduled_on="2026-10-20")
    make(state, scheduled_start="2026-10-19T19:00:00Z", scheduled_end="2026-10-19T20:00:00Z")
    make(state, due_at="2026-11-20")
    make(state, due_at="2026-10-19", visibility="internal")
    make(state, due_at="2026-10-19", status="done")
    make(state, due_at="2026-10-19", status="cancelled")
    result = service.scan(sid, NOW)
    assert result["captured"] == 5 and result["scanned"] == 6
    phases = [event["payload"]["phase"] for event in events(state)]
    assert phases.count("overdue") == 1 and phases.count("due_soon") == 2 and phases.count("scheduled_today") == 2
    assert all(event["kind"] == "timer" and event["goal_id"] is None for event in events(state))
    assert "PRIVATE TITLE" not in json.dumps(events(state)) and "timer_generation" not in json.dumps(events(state))


def test_title_sort_edits_do_not_repeat_or_invalidate_same_business_fact(state):
    factory, _, _, matters, service, _ = state
    record = make(state, due_at="2026-10-21")
    enable(state)
    assert service.tick(NOW) == 1
    event = events(state)[0]
    matters.update_matter(matter_id=record.matter_id,
                          payload=MatterUpdateInput(title="Changed", sort_order=200, expected_revision=1))
    assert service.tick(NOW) == 0
    with closing(factory()) as conn:
        assert service.validate_event(conn, event, now=NOW) is True
        assert not conn.in_transaction
    assert len(events(state)) == 1


@pytest.mark.parametrize("changes", [
    {"due_at": None}, {"due_at": "2026-11-20"}, {"status": "done"},
    {"status": "cancelled"}, {"visibility": "internal"},
])
def test_date_status_and_visibility_changes_invalidate_pending_events(state, changes):
    factory, _, _, matters, service, _ = state
    record = make(state, due_at="2026-10-21")
    enable(state)
    service.tick(NOW)
    event = events(state)[0]
    matters.update_matter(matter_id=record.matter_id, payload=MatterUpdateInput(expected_revision=1, **changes))
    with closing(factory()) as conn:
        assert service.validate_event(conn, event, now=NOW) is False


def test_day_transition_invalidates_old_phase_and_captures_overdue_once(state):
    factory, _, _, _, service, _ = state
    make(state, due_at="2026-10-20")
    enable(state)
    assert service.tick(NOW) == 1
    old = events(state)[0]
    later = datetime(2026, 10, 21, 2, tzinfo=UTC)
    with closing(factory()) as conn:
        assert service.validate_event(conn, old, now=later) is False
    assert service.tick(later) == 1
    assert service.tick(later) == 0
    assert {event["payload"]["phase"] for event in events(state)} == {"due_soon", "overdue"}


def test_policy_revision_disable_and_reenable_recaptures_undelivered_old_facts(state):
    factory, _, _, _, service, sid = state
    make(state, due_at="2026-10-21")
    enable(state)
    service.tick(NOW)
    event = events(state)[0]
    service.configure(sid, expected_revision=1, enabled=False)
    with closing(factory()) as conn:
        assert service.validate_event(conn, event, now=NOW) is False
    assert service.tick(NOW) == 0
    service.configure(sid, expected_revision=2, enabled=True)
    assert service.get(sid)["scan_offset"] == 0
    assert service.tick(NOW) == 1 and len(events(state)) == 2
    assert events(state)[-1]["payload"]["attention_policy_revision"] == 3


def test_tick_persists_page_continuation_across_service_reopen_and_completes_tail(state):
    factory, management, goals, matters, service, sid = state
    for i in range(55):
        make(state, title=str(i), due_at="2026-10-19")
    enable(state)
    assert service.tick(NOW) == 50
    assert service.get(sid)["scan_offset"] == 50
    reopened = ManagementAttentionService(factory, matters=matters, goals=goals, management=management)
    reopened.ensure_schema()
    assert reopened.tick(NOW) == 5
    assert reopened.get(sid)["scan_offset"] == 0
    assert reopened.tick(NOW) == 0
    assert len(events(state)) == 55


def test_explicit_scan_can_continue_beyond_first_page(state):
    _, _, _, _, service, sid = state
    for i in range(4):
        make(state, title=str(i), due_at="2026-10-19")
    enable(state)
    first = service.scan(sid, NOW, limit=2)
    second = service.scan(sid, NOW, limit=2, offset=first["next_offset"])
    assert first == {"captured": 2, "scanned": 2, "next_offset": 2}
    assert second == {"captured": 2, "scanned": 2, "next_offset": None}


def test_pause_skips_scanning_without_disabling_policy_or_losing_offset(state):
    factory, management, _, _, service, sid = state
    make(state, due_at="2026-10-19")
    enable(state)
    with closing(factory()) as conn, conn:
        conn.execute("UPDATE manager_attention_policies SET scan_offset=50 WHERE session_id=?", (sid,))
    management.update(sid, expected_revision=2, status="paused")
    assert service.tick(NOW) == 0
    assert service.get(sid)["enabled"] is True and service.get(sid)["scan_offset"] == 50
    management.update(sid, expected_revision=3, status="active")
    assert service.tick(NOW) == 0  # Exhausted stale offset wraps to the beginning.
    assert service.tick(NOW) == 1


def test_configuration_authority_rechecked_at_commit_and_rolls_back(state):
    _, _, _, _, service, sid = state
    calls = []
    def authority(conn):
        assert conn.in_transaction
        calls.append(1)
        if len(calls) == 2:
            raise ManagementConflictError("Cancelled before commit")
    with pytest.raises(ManagementConflictError, match="Cancelled"):
        service.configure(sid, expected_revision=0, enabled=True, authority_check=authority)
    assert len(calls) == 2 and service.get(sid) is None


def test_capture_rolls_back_all_events_and_cursor_on_error(state, monkeypatch):
    factory, _, goals, _, service, sid = state
    make(state, due_at="2026-10-19")
    make(state, due_at="2026-10-18")
    enable(state)
    original = goals._event
    calls = 0
    def failure(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Injected event failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(goals, "_event", failure)
    with pytest.raises(OSError):
        service.tick(NOW)
    assert events(state) == [] and service.get(sid)["scan_offset"] == 0
    with closing(factory()) as conn:
        assert conn.execute("SELECT count(*) FROM manager_goals").fetchone()[0] == 0


def test_source_permission_is_revalidated_through_matter_service(state, monkeypatch):
    factory, _, _, matters, service, _ = state
    make(state, due_at="2026-10-19")
    enable(state)
    service.tick(NOW)
    def denied(**kwargs):
        raise PermissionError("Source permission changed")
    monkeypatch.setattr(matters, "get_matter", denied)
    with closing(factory()) as conn:
        assert service.validate_event(conn, events(state)[0], now=NOW) is False


@pytest.mark.parametrize("values", [{"timezone": "Unknown/NoSuchPlace"}, {"horizon_days": 0}, {"enabled": "true"}])
def test_invalid_configuration_is_rejected_without_policy(state, values):
    _, _, _, _, service, sid = state
    args = {"expected_revision": 0, "enabled": True, **values}
    with pytest.raises(ValueError):
        service.configure(sid, **args)
    assert service.get(sid) is None


def test_scan_rejects_naive_time_and_cannot_expand_policy_scope(state):
    _, _, _, _, service, sid = state
    enable(state, horizon_days=2)
    with pytest.raises(ValueError, match="timezone-aware"):
        service.scan(sid, NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="exceed"):
        service.scan(sid, NOW, horizon_days=3)
    assert service.scan(sid, NOW, horizon_days=1)["captured"] == 0


def seed_execution(state, execution_id="execution-attention", *, paths=None, results=(), receipt_status="applied", status="bound"):
    factory, management, goals, matters, _, sid = state
    from app.domains.management_work import ManagementWorkService
    work = ManagementWorkService(factory, matters=matters, tasks=None, management=management, goals=goals,
                                 run_store=None, run_manager=None)
    work.ensure_schema()
    run_id = "run-" + execution_id
    metadata = {"manager_execution_id": execution_id,
                "manager_cause_paths": paths if paths is not None else {"event:seed": 3}}
    payload = {"run_id": run_id, "session_id": sid, "parent_run_id": None, "metadata": metadata}
    receipt = {"action": "organize_work", "execution_id": execution_id, "status": receipt_status, "results": list(results)}
    with closing(factory()) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS management_commands (manager_session_id TEXT,command_id TEXT,"
                     "fingerprint TEXT,action TEXT,receipt TEXT,created_at TEXT,PRIMARY KEY(manager_session_id,command_id))")
        conn.execute("INSERT INTO agent_runs VALUES(?,?,?,?,?,?)", (run_id, sid, "running", json.dumps(payload), "now", "now"))
        conn.execute("INSERT INTO manager_executions VALUES(?,?,?,?,2,'[]','{}','now','now')",
                     (execution_id, sid, run_id, status))
        conn.execute("INSERT INTO management_commands VALUES(?,?,?,'organize_work',?,'now')",
                     (sid, execution_id, "fingerprint", json.dumps(receipt)))
        conn.execute("INSERT INTO manager_work_proposals VALUES(?,?,?,'{}')", (execution_id, execution_id, sid))
    return execution_id


def test_same_fact_delivered_before_policy_update_is_not_delivered_again(state):
    factory, _, _, _, service, sid = state
    make(state, due_at="2026-10-21")
    enable(state)
    assert service.tick(NOW) == 1
    execution = seed_execution(state, status="completed")
    with closing(factory()) as conn, conn:
        conn.execute("UPDATE manager_inbox SET consumed_execution_id=?", (execution,))
    service.configure(sid, expected_revision=1, enabled=True)
    assert service.tick(NOW) == 0
    assert len(events(state)) == 1


def test_obsolete_undelivered_event_allows_new_policy_generation_once(state):
    factory, _, _, _, service, sid = state
    make(state, due_at="2026-10-21")
    enable(state)
    service.tick(NOW)
    with closing(factory()) as conn, conn:
        conn.execute("UPDATE manager_inbox SET consumed_execution_id='obsolete'")
    service.configure(sid, expected_revision=1, enabled=True, horizon_days=5)
    assert service.tick(NOW) == 1
    assert service.tick(NOW) == 0
    assert len(events(state)) == 2


def test_failed_execution_is_not_evidence_of_delivered_fact(state):
    factory, _, _, _, service, sid = state
    make(state, due_at="2026-10-21")
    enable(state)
    service.tick(NOW)
    execution = seed_execution(state, status="failed")
    with closing(factory()) as conn, conn:
        conn.execute("UPDATE manager_inbox SET consumed_execution_id=?", (execution,))
    service.configure(sid, expected_revision=1, enabled=True)
    assert service.tick(NOW) == 1


def test_date_changes_inherit_causal_depth_without_inlining_paths_in_event(state):
    factory, _, _, matters, service, sid = state
    record = make(state, due_at="2026-10-21")
    enable(state)
    service.tick(NOW)
    with closing(factory()) as conn:
        assert service.cause_paths(conn, events(state)[0]) is None
    updated = matters.update_matter(matter_id=record.matter_id, payload=MatterUpdateInput(due_at="2026-10-22", expected_revision=1))
    execution = seed_execution(state, results=[{"op": "update", "matter_id": record.matter_id, "revision": updated.revision}])
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        service.record_proposal_causes(conn, execution, sid)
    assert service.tick(NOW) == 1
    event = events(state)[-1]
    assert "cause_paths" not in event["payload"]
    with closing(factory()) as conn:
        assert service.cause_paths(conn, event) == {"event:seed": 4}
    matters.update_matter(matter_id=record.matter_id, payload=MatterUpdateInput(title="Renamed", sort_order=12, expected_revision=2))
    assert service.tick(NOW) == 0
    with closing(factory()) as conn:
        assert service.cause_paths(conn, event) == {"event:seed": 4}
        assert service.validate_event(conn, event, now=NOW)


def test_causal_markers_include_only_applied_current_create_update_results(state):
    factory, _, _, matters, service, sid = state
    current = make(state, due_at="2026-10-21")
    link_only = make(state, due_at="2026-10-22")
    stale = make(state, due_at="2026-10-23")
    matters.update_matter(matter_id=stale.matter_id, payload=MatterUpdateInput(title="Changed", expected_revision=1))
    execution = seed_execution(state, results=[
        {"op": "create", "matter_id": current.matter_id, "revision": 1},
        {"op": "bind_goal", "matter_id": link_only.matter_id, "revision": 1},
        {"op": "update", "matter_id": stale.matter_id, "revision": 1},
        {"op": "link", "matter_id": link_only.matter_id, "revision": 1},
    ])
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        service.record_proposal_causes(conn, execution, sid)
        service.record_proposal_causes(conn, execution, sid)
        rows = conn.execute("SELECT matter_id,revision FROM manager_matter_attention_causes").fetchall()
        assert [(row[0], row[1]) for row in rows] == [(current.matter_id, 1)]
    pending = seed_execution(state, "pending", receipt_status="proposed", results=[{"op": "update", "matter_id": link_only.matter_id, "revision": 1}])
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        service.record_proposal_causes(conn, pending, sid)
        assert conn.execute("SELECT count(*) FROM manager_matter_attention_causes").fetchone()[0] == 1


def test_causal_marker_rollback_and_bounded_ancestry(state):
    factory, _, _, _, service, sid = state
    record = make(state, due_at="2026-10-21")
    result = {"op": "update", "matter_id": record.matter_id, "revision": 1}
    execution = seed_execution(state, results=[result], paths={f"event:{i}": 4 for i in range(64)})
    with closing(factory()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        service.record_proposal_causes(conn, execution, sid)
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM manager_matter_attention_causes").fetchone()[0] == 0
    with closing(factory()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        service.record_proposal_causes(conn, execution, sid)
        paths = service.cause_paths(conn, {"payload": {"matter_id": record.matter_id, "matter_revision": 1}})
        assert len(paths) == 64 and set(paths.values()) == {5}
    too_many = seed_execution(state, "excessive", results=[result], paths={f"event:{i}": 0 for i in range(65)})
    with closing(factory()) as conn, conn, pytest.raises(ManagementConflictError, match="bounded causal"):
        conn.execute("BEGIN IMMEDIATE")
        service.record_proposal_causes(conn, too_many, sid)
    with closing(factory()) as conn, pytest.raises(ValueError, match="caller-owned"):
        service.record_proposal_causes(conn, execution, sid)
