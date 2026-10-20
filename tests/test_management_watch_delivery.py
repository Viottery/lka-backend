"""Synthetic SQLite checks: published references and independent delivery policy."""
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.sessions import SessionService
from app.domains.management import ManagementConflictError, ManagementService
from app.domains.management_delivery import DeliveryPolicyInput, ManagementDeliveryService
from app.domains.management_goals import ManagementGoalService
from app.domains.management_watches import ManagementWatchService, WatchSubscriptionInput
from app.domains.watch import BriefingInput, WatchInput, WatchPatch, WatchService
from app.storage.db import connect, init_db

NOW = datetime(2026, 10, 20, 2, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "watches.sqlite3"
    init_db(path)
    factory = lambda: connect(path)
    management = ManagementService(factory, SessionService(factory))
    management.ensure_schema()
    goals = ManagementGoalService(factory, management)
    goals.ensure_schema()
    sid = management.create()["session"]["session_id"]
    management.update(sid, expected_revision=1, status="active")
    watches = WatchService(factory)
    watches.initialize()
    watch = watches.create(WatchInput(title="Watch", goal="Observe", timezone="UTC", daily_time="00:00",
                                      scope={"source_ids": ["permitted-source"]}), now=NOW)
    service = ManagementWatchService(factory, watches=watches, goals=goals, tasks=None)
    service.ensure_schema()
    delivery = ManagementDeliveryService(factory)
    delivery.ensure_schema()
    return factory, management, goals, sid, watches, watch["watch_id"], service, delivery


def publish(state, index=0, changes=True):
    watches, wid = state[4:6]
    instant = NOW + timedelta(minutes=index)
    occurrence = watches.create_occurrence(wid, instant)
    watches.claim_occurrence(owner="worker", now=instant)
    return watches.complete_with_briefing(occurrence["occurrence_id"], owner="worker", now=instant,
        payload=BriefingInput(title="Secret", summary="private body " * 1000, changes=[{"claim": "private"}] if changes else []))


def events(state):
    return state[2].list_events(state[3])["events"]


def enable(state, **kwargs):
    return state[6].configure(state[3], "subscribe", state[5], **kwargs)


def test_default_off_and_baseline_only_future_published_refs(state):
    publish(state)
    service = state[6]
    assert service.tick(NOW) == 0
    enable(state)
    assert service.tick(NOW) == 0
    new = publish(state, 1)
    assert service.tick(NOW) == 1
    event = events(state)[0]
    assert event["payload"]["briefing_id"] == new["briefing_id"]
    assert "private" not in json.dumps(event) and "Secret" not in json.dumps(event)
    assert service.tick(NOW) == 0


def test_cas_command_retries_authority_and_strict_input(state):
    service, sid, wid = state[6], state[3], state[5]
    calls = []
    receipt = service.configure(sid, "same", wid, authority_check=lambda conn: calls.append(conn.in_transaction))
    assert receipt == service.configure(sid, "same", wid)
    assert calls == [True, True]
    with pytest.raises(ManagementConflictError):
        service.configure(sid, "same", wid, condition="changes")
    with pytest.raises(ManagementConflictError):
        service.configure(sid, "different", wid)
    with pytest.raises(ValidationError):
        WatchSubscriptionInput(watch_id=wid, enabled=1)
    with pytest.raises(ValidationError):
        WatchSubscriptionInput(watch_id=wid, arbitrary_sql="SELECT 1")


def test_changes_condition_advances_cursor_past_unmatched_results(state):
    enable(state, condition="changes")
    publish(state, changes=False)
    publish(state, 1)
    assert state[6].tick(NOW) == 1
    assert state[6].get(state[3], state[5])["cursor"] == 2
    assert state[6].tick(NOW) == 0


def test_persistent_cursor_reaches_tail_over_50_results(state):
    enable(state)
    for i in range(52):
        publish(state, i)
    assert state[6].tick(NOW) == 50
    reopened = ManagementWatchService(state[0], watches=state[4], goals=state[2], tasks=None)
    reopened.ensure_schema()
    assert reopened.tick(NOW) == 2
    assert reopened.tick(NOW) == 0
    assert len(state[2].list_events(state[3], limit=100)["events"]) == 52


def test_paused_manager_keeps_capture_and_deleted_manager_stops(state):
    enable(state)
    state[1].update(state[3], expected_revision=2, status="paused")
    publish(state)
    assert state[6].tick(NOW) == 1
    with closing(state[0]()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (state[3],))
    publish(state, 1)
    assert state[6].tick(NOW) == 0


def test_scope_change_blocks_old_events_and_requires_explicit_rebind(state):
    enable(state)
    publish(state)
    state[6].tick(NOW)
    event = events(state)[0]
    state[4].update(state[5], WatchPatch(scope={"source_ids": ["different-source"]}))
    publish(state, 1)
    with closing(state[0]()) as conn:
        assert state[6].validate_event(conn, event) is False
    assert state[6].tick(NOW) == 0
    policy = state[6].configure(state[3], "rebind", state[5], expected_revision=1)
    assert policy["scope_version"] == 2
    assert state[6].tick(NOW) == 0
    publish(state, 2)
    assert state[6].tick(NOW) == 1


def test_goal_scope_and_revision_are_revalidated(state):
    goal = state[2].create_goal(state[3], objective="Goal")
    enable(state, goal_id=goal["goal_id"])
    publish(state)
    state[6].tick(NOW)
    event = events(state)[0]
    with closing(state[0]()) as conn:
        assert state[6].validate_event(conn, event)
    state[2].update_goal(state[3], goal["goal_id"], expected_revision=1, status="paused")
    with closing(state[0]()) as conn:
        assert not state[6].validate_event(conn, event)
    publish(state, 1)
    assert state[6].tick(NOW) == 0


def test_notification_reads_use_scoped_existing_event_and_bounded_continuation(state, monkeypatch):
    enable(state)
    briefing = publish(state)
    state[6].tick(NOW)
    event = events(state)[0]
    reads = []
    original = state[4].get_briefing
    monkeypatch.setattr(state[4], "get_briefing", lambda ref: (reads.append(ref), original(ref))[1])
    page = state[6].read_notification(state[3], event_id=event["event_id"], max_chars=100)
    assert len(page["content"]) == 100 and page["next_char"] == 100
    tail = state[6].read_notification(state[3], briefing_id=briefing["briefing_id"], start_char=100, max_chars=100)
    assert tail["content"] != page["content"] and reads == [briefing["briefing_id"]] * 2
    other = state[1].create()["session"]["session_id"]
    with pytest.raises(PermissionError):
        state[6].read_notification(other, briefing_id=briefing["briefing_id"])
    state[4].set_paused(state[5], True)
    with pytest.raises(PermissionError):
        state[6].read_notification(state[3], event_id=event["event_id"])


def test_disable_invalidation_and_authority_failure_rolls_back(state):
    enable(state)
    publish(state)
    state[6].tick(NOW)
    event = events(state)[0]
    state[6].configure(state[3], "disable", state[5], expected_revision=1, enabled=False)
    with closing(state[0]()) as conn:
        assert not state[6].validate_event(conn, event)
    def reject(conn):
        raise PermissionError("revoked")
    with pytest.raises(PermissionError):
        state[6].configure(state[3], "restore", state[5], expected_revision=2, authority_check=reject)
    assert state[6].get(state[3], state[5])["revision"] == 2


def test_delivery_mute_snooze_decisions_and_no_consumption(state):
    enable(state)
    publish(state)
    state[6].tick(NOW)
    service, sid, event = state[7], state[3], events(state)[0]
    with closing(state[0]()) as conn:
        assert service.can_deliver(conn, sid, event, now=NOW)
    service.configure(sid, 0, muted=True)
    with closing(state[0]()) as conn:
        assert not service.can_deliver(conn, sid, event, now=NOW)
        decision = {**event, "payload": {"task_waiting": True}}
        assert service.can_deliver(conn, sid, decision, now=NOW)
        assert not conn.in_transaction
    service.configure(sid, 1, snooze_until=(NOW + timedelta(hours=1)).isoformat(), allow_decisions=False)
    with closing(state[0]()) as conn:
        assert not service.can_deliver(conn, sid, event, now=NOW)
        assert not service.can_deliver(conn, sid, decision, now=NOW)
        assert service.can_deliver(conn, sid, event, now=NOW + timedelta(hours=1))
    publish(state, 1)
    assert state[6].tick(NOW) == 1
    assert all(item["consumed_execution_id"] is None for item in events(state))
    with closing(state[0]()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (sid,))
    with closing(state[0]()) as conn:
        assert not service.can_deliver(conn, sid, event, now=NOW)


def test_delivery_cas_strict_aware_and_authority_rollback(state):
    service, sid = state[7], state[3]
    with pytest.raises(ValidationError):
        DeliveryPolicyInput(expected_revision=0, snooze_until="2026-10-20T00:00:00")
    with pytest.raises(ValidationError):
        DeliveryPolicyInput(expected_revision=0, muted="false")
    service.configure(sid, 0)
    with pytest.raises(ManagementConflictError):
        service.configure(sid, 0)
    def reject(conn):
        raise PermissionError("revoked")
    with pytest.raises(PermissionError):
        service.configure(sid, 1, muted=True, authority_check=reject)
    assert service.get(sid)["revision"] == 1 and not service.get(sid)["muted"]


def test_forged_cross_watch_refs_and_failed_occurrences_never_become_evidence(state):
    enable(state)
    briefing = publish(state)
    state[6].tick(NOW)
    event = events(state)[0]
    other = state[4].create(WatchInput(title="Other", goal="Other", timezone="UTC", daily_time="00:00",
                                      scope={"source_ids": ["other-source"]}), now=NOW)
    forged = {**event, "payload": {**event["payload"], "watch_id": other["watch_id"]}}
    with closing(state[0]()) as conn:
        assert not state[6].validate_event(conn, forged)
    with closing(state[0]()) as conn, conn:
        conn.execute("UPDATE watch_occurrences SET status='failed' WHERE occurrence_id=?", (briefing["occurrence_id"],))
    with closing(state[0]()) as conn:
        assert not state[6].validate_event(conn, event)
    with pytest.raises(PermissionError):
        state[6].read_notification(state[3], briefing_id=briefing["briefing_id"])


def test_available_watch_discovery_is_bounded_and_does_not_grant_source_scope(state):
    _, _, _, sid, watches, _, service, _ = state
    watches.create(WatchInput(title="Second watch", goal="Observe another bounded source", timezone="UTC",
                              daily_time="00:00", scope={"source_ids": ["other-private-source"]}), now=NOW)
    page = service.available_watches(sid, limit=1)
    assert len(page["watches"]) == 1 and page["next_offset"] == 1
    assert len(service.available_watches(sid, limit=1, offset=page["next_offset"])["watches"]) == 1
    assert "scope" not in page["watches"][0] and "source_ids" not in json.dumps(page)
    assert len(service.available_watches(sid, q="Second")["watches"]) == 1
    assert service.available_watches(sid, q="absent")["watches"] == []
    with pytest.raises(KeyError):
        service.available_watches("other-manager")
