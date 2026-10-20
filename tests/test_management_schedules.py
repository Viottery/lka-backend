"""Offline schedule configuration, wall-clock DST and durable capture contracts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from pydantic import ValidationError

from app.domains.management import ManagementConflictError
from app.domains.management_schedules import (
    ManagementScheduleInput,
    ManagementScheduleService,
    next_occurrence,
)
from tests.test_management_goals import state as state_fixture

START = "2026-10-20T08:00:00+00:00"


@pytest.fixture
def state(tmp_path):
    factory, sessions, management, goals, sid = state_fixture.__wrapped__(tmp_path)
    management.update(sid, expected_revision=1, status="active")
    service = ManagementScheduleService(factory, goals, management)
    service.ensure_schema()
    goal = goals.create_goal(sid, objective="Weekly report")
    return factory, sessions, management, goals, sid, service, goal


def fields(goal, **changes):
    return {"goal_id": goal["goal_id"], "timezone": "UTC", "kind": "daily", "local_time": "09:00", **changes}


def test_configuration_idempotency_cas_reopen_and_bounded_lists(state):
    factory, _, management, goals, sid, service, goal = state
    original = fields(goal)
    receipt = service.configure(sid, "daily", now=START, **original)
    schedule = receipt["schedule"]
    assert schedule["revision"] == 1 and schedule["next_run_at"] == "2026-10-20T09:00:00+00:00"
    assert service.configure(sid, "daily", now="2026-10-22T08:00:00Z", **original) == receipt
    with pytest.raises(ManagementConflictError):
        service.configure(sid, "daily", now=START, **fields(goal, local_time="10:00"))
    with pytest.raises(ManagementConflictError, match="revision"):
        service.configure(sid, "stale", now=START, **fields(goal, schedule_id=schedule["schedule_id"]))
    changed = service.configure(sid, "later", now=START, **fields(goal, schedule_id=schedule["schedule_id"], expected_revision=1, local_time="10:00"))
    assert changed["schedule"]["revision"] == 2
    reopened = ManagementScheduleService(factory, goals, management)
    reopened.ensure_schema()
    assert reopened.get(sid, schedule["schedule_id"]) == changed["schedule"]
    reopened.configure(sid, "another", now=START, **original)
    assert reopened.list_schedules(sid, limit=1)["next_offset"] == 1
    assert reopened.list_schedules(sid, limit=1, offset=1)["next_offset"] is None
    with pytest.raises(ValueError):
        reopened.list_schedules(sid, limit=101)


@pytest.mark.parametrize("changes", [
    {"timezone": "Not/AZone"}, {"local_time": "9:00"}, {"local_time": "24:00"},
    {"weekday": 2}, {"kind": "weekly"}, {"kind": "one_shot"},
    {"run_at": "2026-10-20T09:00:00"}, {"enabled": "yes"}, {"weekday": True},
    {"unexpected": "ignored?"}, {"expires_at": "2026-10-20"},
])
def test_strict_bounded_configuration_rejects_unknown_or_incoherent_fields(changes):
    with pytest.raises(ValidationError):
        ManagementScheduleInput.model_validate(fields({"goal_id": "goal"}, **changes))


@pytest.mark.parametrize("kind,weekday,expected", [("daily", None, "2026-03-09T06:30:00+00:00"), ("weekly", 6, "2026-03-15T06:30:00+00:00")])
def test_dst_gap_skips_invalid_wall_time(kind, weekday, expected):
    policy = ManagementScheduleInput(goal_id="g", timezone="America/New_York", kind=kind, local_time="02:30", weekday=weekday)
    assert next_occurrence(policy, "2026-03-08T05:00:00Z").isoformat() == expected


def test_dst_ambiguous_fold_zero_and_weekly_timezone():
    policy = ManagementScheduleInput(goal_id="g", timezone="America/New_York", kind="daily", local_time="01:30")
    assert next_occurrence(policy, "2026-11-01T04:00:00Z").isoformat() == "2026-11-01T05:30:00+00:00"
    # The second 01:30 is not another occurrence.
    assert next_occurrence(policy, "2026-11-01T05:31:00Z").isoformat() == "2026-11-02T06:30:00+00:00"
    weekly = ManagementScheduleInput(goal_id="g", timezone="Asia/Shanghai", kind="weekly", local_time="09:00", weekday=0)
    assert next_occurrence(weekly, START).isoformat() == "2026-10-26T01:00:00+00:00"


def test_one_shot_capture_once_and_replay_is_historical_not_current(state):
    factory, _, _, goals, sid, service, goal = state
    args = fields(goal, kind="one_shot", local_time=None, run_at="2026-10-20T09:00:00+00:00")
    receipt = service.configure(sid, "once", now=START, **args)
    assert service.tick("2026-10-20T09:01:00Z") == 1
    assert service.tick("2026-10-23T09:01:00Z") == 0
    assert service.get(sid, receipt["schedule"]["schedule_id"])["next_run_at"] is None
    assert service.configure(sid, "once", now="2026-10-23T09:01:00Z", **args) == receipt
    event = goals.list_events(sid)["events"][0]
    assert event["goal_revision"] == goal["revision"]
    assert event["payload"]["occurrence"] == "2026-10-20T09:00:00+00:00"
    with closing(factory()) as conn:
        assert service.validate_event(conn, event, now="2026-10-20T09:01:00Z")


def test_latest_only_catchup_is_atomic_concurrent_and_survives_restart(state):
    factory, _, management, goals, sid, service, goal = state
    receipt = service.configure(sid, "daily", now=START, **fields(goal))
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: service.tick("2026-11-20T12:00:00Z"), range(2)))
    assert sum(counts) == 1
    event = goals.list_events(sid)["events"][0]
    assert event["payload"]["occurrence"] == "2026-11-20T09:00:00+00:00"
    reopened = ManagementScheduleService(factory, goals, management)
    assert reopened.tick("2026-11-20T12:00:00Z") == 0
    record = reopened.get(sid, receipt["schedule"]["schedule_id"])
    assert record["next_run_at"] == "2026-11-21T09:00:00+00:00" and record["last_outcome"] == "captured"


def test_paused_controller_and_goal_skip_without_backlog_and_disable_resets(state):
    _, _, management, goals, sid, service, goal = state
    receipt = service.configure(sid, "daily", now=START, **fields(goal))
    schedule_id = receipt["schedule"]["schedule_id"]
    management.update(sid, expected_revision=2, status="paused")
    assert service.tick("2026-10-21T12:00:00Z") == 0
    assert service.get(sid, schedule_id)["last_outcome"] == "skipped"
    management.update(sid, expected_revision=3, status="active")
    assert service.tick("2026-10-21T13:00:00Z") == 0
    goals.update_goal(sid, goal["goal_id"], expected_revision=1, status="paused")
    assert service.tick("2026-10-22T12:00:00Z") == 0
    goals.update_goal(sid, goal["goal_id"], expected_revision=2, status="active")
    service.configure(sid, "off", now="2026-10-22T12:00:00Z", **fields(goal, schedule_id=schedule_id, expected_revision=1, enabled=False))
    assert service.tick("2026-10-25T12:00:00Z") == 0
    service.configure(sid, "on", now="2026-10-25T12:00:00Z", **fields(goal, schedule_id=schedule_id, expected_revision=2))
    assert service.tick("2026-10-25T13:00:00Z") == 0
    assert service.tick("2026-10-26T09:00:00Z") == 1


def test_event_fence_revision_disabled_expiry_goal_terminal_and_deleted_manager(state):
    factory, _, _, goals, sid, service, goal = state
    receipt = service.configure(sid, "daily", now=START, **fields(goal, expires_at="2026-10-22T10:00:00Z"))
    assert service.tick("2026-10-20T09:01:00Z") == 1
    event = goals.list_events(sid)["events"][0]
    with closing(factory()) as conn:
        assert service.validate_event(conn, event, now="2026-10-20T09:02:00Z")
        assert not service.validate_event(conn, event, now="2026-10-22T10:00:00Z")
    service.configure(sid, "off", now="2026-10-20T10:00:00Z", **fields(goal, schedule_id=receipt["schedule"]["schedule_id"], expected_revision=1, enabled=False))
    with closing(factory()) as conn:
        assert not service.validate_event(conn, event, now="2026-10-20T10:00:00Z")
    service.configure(sid, "new", now=START, **fields(goal))
    goals.update_goal(sid, goal["goal_id"], expected_revision=1, status="cancelled")
    assert service.tick("2026-10-21T10:00:00Z") == 0
    with closing(factory()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (sid,))
    with pytest.raises(KeyError):
        service.get(sid, receipt["schedule"]["schedule_id"])
    assert service.tick("2026-10-22T10:00:00Z") == 0


def test_authority_checks_inside_transaction_even_replay_and_atomic_rollback(state, monkeypatch):
    factory, _, _, goals, sid, service, goal = state
    checks = []
    def allow(conn):
        assert conn.in_transaction
        checks.append(True)
    receipt = service.configure(sid, "daily", now=START, authority_check=allow, **fields(goal))
    service.configure(sid, "daily", now=START, authority_check=allow, **fields(goal))
    assert len(checks) == 4
    def deny(_):
        raise PermissionError("lease_lost")
    with pytest.raises(PermissionError):
        service.configure(sid, "daily", now=START, authority_check=deny, **fields(goal))
    checks.clear()
    def late_deny(conn):
        checks.append(conn.in_transaction)
        if len(checks) == 2:
            raise PermissionError("lease_lost")
    with pytest.raises(PermissionError):
        service.configure(sid, "rollback", now=START, authority_check=late_deny, **fields(goal))
    assert len(service.list_schedules(sid)["schedules"]) == 1
    original = goals._event
    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("interrupt commit")
    monkeypatch.setattr(goals, "_event", fail_after_insert)
    with pytest.raises(OSError):
        service.tick("2026-10-20T10:00:00Z")
    assert goals.list_events(sid)["events"] == []
    assert service.get(sid, receipt["schedule"]["schedule_id"])["next_run_at"] == "2026-10-20T09:00:00+00:00"
    with closing(factory()) as conn:
        assert conn.execute("SELECT count(*) FROM manager_schedule_commands WHERE command_id='rollback'").fetchone()[0] == 0


def test_cross_manager_goal_and_schedule_targets_fail_closed(state):
    _, _, management, goals, sid, service, goal = state
    receipt = service.configure(sid, "own", now=START, **fields(goal))
    other = management.create()["session"]["session_id"]
    foreign_goal = goals.create_goal(other, objective="Other")
    with pytest.raises(ValueError, match="belong"):
        service.configure(sid, "foreign-goal", now=START, **fields(foreign_goal))
    with pytest.raises(KeyError):
        service.configure(other, "foreign-schedule", now=START, **fields(foreign_goal, schedule_id=receipt["schedule"]["schedule_id"]))
    assert service.get(sid, receipt["schedule"]["schedule_id"])["goal_id"] == goal["goal_id"]
    goals.update_goal(sid, goal["goal_id"], expected_revision=1, status="paused")
    with pytest.raises(ManagementConflictError, match="not enabled"):
        service.configure(sid, "paused-goal", now=START, **fields(goal))


def test_bound_matter_authority_is_consulted_on_capture_and_event_validation(state, monkeypatch):
    _, _, _, goals, sid, service, goal = state
    service.configure(sid, "daily", now=START, **fields(goal))
    assert service.tick("2026-10-20T10:00:00Z") == 1
    event = goals.list_events(sid)["events"][0]
    monkeypatch.setattr(goals, "enabled_in_transaction", lambda conn, goal_id: False)
    assert service.tick("2026-10-21T10:00:00Z") == 0
    with closing(service._conn()) as conn:
        assert not service.validate_event(conn, event, now=START)


def test_expired_configuration_and_old_one_shot_not_replayed(state):
    _, _, _, goals, sid, service, goal = state
    with pytest.raises(ValueError, match="expired"):
        service.configure(sid, "expired", now=START, **fields(goal, expires_at="2026-10-19T10:00:00Z"))
    with pytest.raises(ValueError, match="past"):
        service.configure(sid, "old", now=START, **fields(goal, kind="one_shot", local_time=None, run_at="2026-10-19T10:00:00Z"))
    service.configure(sid, "expires", now=START, **fields(goal, expires_at="2026-10-20T10:00:00Z"))
    assert service.tick("2026-10-20T11:00:00Z") == 0 and goals.list_events(sid)["events"] == []
    assert service.list_schedules(sid)["schedules"][0]["last_outcome"] == "skipped"
