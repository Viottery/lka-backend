"""Unified attention sweeps use disposable SQLite, without models or workers."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.local_config import ManagementConfig
from app.core.sessions import SessionService
from app.domains.management import ManagementService
from app.domains.management_goals import ManagementGoalService
from app.domains.management_supervision import ManagementSupervisionService
from app.storage.db import connect, init_db

NOW = datetime(2026, 10, 22, tzinfo=UTC)
INTERVAL = timedelta(seconds=60)


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "supervision.sqlite3"
    init_db(path)
    factory = lambda: connect(path)
    sessions = SessionService(factory)
    management = ManagementService(factory, sessions)
    goals = ManagementGoalService(factory, management)
    config = ManagementConfig(enabled=True, autonomous_enabled=True, attention_sweep_interval_seconds=60)
    service = ManagementSupervisionService(factory, goals=goals, management=management, config=config)
    service.ensure_schema()
    sid = management.create()["session"]["session_id"]
    management.update(sid, expected_revision=1, status="active")
    return factory, management, goals, config, service, sid


def events(state):
    with closing(state[0]()) as conn:
        rows = conn.execute("SELECT * FROM manager_inbox ORDER BY event_id").fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def test_first_tick_delays_then_captures_every_timerless_goal_without_mutation(state):
    _, _, goals, _, service, sid = state
    original = [goals.create_goal(sid, objective=f"Accepted unfinished responsibility {i}",
                                 status="waiting" if i % 2 else "active") for i in range(121)]
    assert service.tick(NOW) == 0
    assert service.tick(NOW + INTERVAL - timedelta(microseconds=1)) == 0
    assert service.tick(NOW + INTERVAL) == 121
    captured = events(state)
    assert {e["goal_id"] for e in captured} == {g["goal_id"] for g in original}
    assert all(e["session_id"] == sid and e["goal_revision"] == 1 and e["kind"] == "timer"
               and e["payload"] == {"attention_sweep": True, "generation": 1} for e in captured)
    assert [goals.get_goal(sid, g["goal_id"]) for g in original] == original
    with closing(state[0]()) as conn:
        assert all(service.validate_event(conn, event) for event in captured)


def test_pending_events_coalesce_across_restart_and_missed_periods(state):
    factory, management, goals, config, service, sid = state
    goal = goals.create_goal(sid, objective="Continue accepted work")
    service.tick(NOW)
    assert service.tick(NOW + INTERVAL) == 1
    before = events(state)
    reopened = ManagementSupervisionService(factory, goals=goals, management=management, config=config)
    reopened.ensure_schema()
    assert reopened.tick(NOW + timedelta(days=2)) == 0
    assert events(state) == before
    with closing(factory()) as conn, conn:
        assert reopened.validate_event(conn, before[0])
        conn.execute("UPDATE manager_inbox SET consumed_execution_id='published' WHERE event_id=?",
                     (before[0]["event_id"],))
    assert reopened.tick(NOW + timedelta(days=2)) == 0
    assert reopened.tick(NOW + timedelta(days=2) + INTERVAL) == 1
    after = events(state)
    assert after[0] == {**before[0], "consumed_execution_id": "published"}
    assert after[1]["goal_id"] == goal["goal_id"]
    assert after[1]["payload"]["generation"] == 3


def test_concurrent_ticks_capture_once(state):
    _, _, goals, _, service, sid = state
    goals.create_goal(sid, objective="One pulse")
    service.tick(NOW)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.tick(NOW + INTERVAL), range(8)))
    assert sum(results) == 1
    assert len(events(state)) == 1


@pytest.mark.parametrize("setting,value", [("enabled", False), ("autonomous_enabled", False),
                                           ("attention_sweep_interval_seconds", 0)])
def test_disabled_policy_neither_captures_nor_validates(state, setting, value):
    factory, _, goals, config, service, sid = state
    goals.create_goal(sid, objective="Accepted responsibility")
    service.tick(NOW)
    service.tick(NOW + INTERVAL)
    event = events(state)[0]
    setattr(config, setting, value)
    assert service.tick(NOW + INTERVAL * 2) == 0
    assert len(events(state)) == 1
    with closing(factory()) as conn:
        assert not service.validate_event(conn, event)


def test_paused_controllers_capture_but_terminal_and_disabled_goals_do_not(state, monkeypatch):
    _, management, goals, _, service, sid = state
    active = goals.create_goal(sid, objective="Keep while controller paused")
    blocked = goals.create_goal(sid, objective="Disabled bound responsibility")
    for status in ("paused", "completed", "cancelled"):
        goals.create_goal(sid, objective=status, status=status)
    enabled = goals.enabled_in_transaction
    monkeypatch.setattr(goals, "enabled_in_transaction", lambda conn, gid:
                        gid != blocked["goal_id"] and enabled(conn, gid))
    management.update(sid, expected_revision=2, status="paused")
    service.tick(NOW)
    assert service.tick(NOW + INTERVAL) == 1
    assert [event["goal_id"] for event in events(state)] == [active["goal_id"]]


def test_managers_are_scoped_and_delegate_and_deleted_sessions_excluded(state):
    factory, management, goals, _, service, sid = state
    other = management.create()["session"]["session_id"]
    delegated = management.create()["session"]["session_id"]
    deleted = management.create()["session"]["session_id"]
    for manager in (sid, other, delegated, deleted):
        goals.create_goal(manager, objective="Same text does not merge ownership")
    # Only session_id determines delegate exclusion, including inactive lines.
    with closing(factory()) as conn, conn:
        conn.execute("CREATE TABLE manager_delegates(delegate_id TEXT,session_id TEXT,state TEXT)")
        conn.execute("INSERT INTO manager_delegates VALUES('line',?,'cancelled')", (delegated,))
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (deleted,))
    assert service.tick(NOW) == 0
    assert service.tick(NOW + INTERVAL) == 2
    assert {event["session_id"] for event in events(state)} == {sid, other}
    event = events(state)[0]
    with closing(factory()) as conn, conn:
        assert not service.validate_event(conn, {**event, "session_id": delegated})
        assert not service.validate_event(conn, {**event, "session_id": deleted})
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (event["session_id"],))
        assert not service.validate_event(conn, event)


def test_new_goal_revision_gets_fresh_event_without_mutating_old_event(state):
    _, _, goals, _, service, sid = state
    goal = goals.create_goal(sid, objective="Original requirement")
    service.tick(NOW)
    service.tick(NOW + INTERVAL)
    before = events(state)[0]
    revised = goals.update_goal(sid, goal["goal_id"], expected_revision=1, objective="User correction")
    assert service.tick(NOW + INTERVAL * 2) == 1
    assert events(state)[0] == before
    assert events(state)[1]["goal_revision"] == revised["revision"]


def test_marker_and_time_validation_fail_closed(state):
    factory, _, goals, _, service, sid = state
    goals.create_goal(sid, objective="Follow through")
    with pytest.raises(ValueError):
        service.tick(NOW.replace(tzinfo=None))
    service.tick(NOW)
    service.tick(NOW + INTERVAL)
    event = events(state)[0]
    with closing(factory()) as conn:
        for payload in ({"attention_sweep": 1, "generation": 1},
                        {"attention_sweep": True, "generation": True},
                        {"attention_sweep": True, "generation": 0},
                        {"attention_sweep": True, "generation": 99}):
            assert not service.validate_event(conn, {**event, "payload": payload})
        assert not service.validate_event(conn, {**event, "kind": "task_terminal"})
        assert not service.validate_event(conn, {**event, "goal_id": None})


def test_interval_config_defaults_bounds_and_explicit_disable():
    assert ManagementConfig().attention_sweep_interval_seconds == 1800
    assert ManagementConfig(attention_sweep_interval_seconds=0).attention_sweep_interval_seconds == 0
    assert ManagementConfig(attention_sweep_interval_seconds=1).attention_sweep_interval_seconds == 1
    for value in (-1, 86401):
        with pytest.raises(ValidationError):
            ManagementConfig(attention_sweep_interval_seconds=value)
