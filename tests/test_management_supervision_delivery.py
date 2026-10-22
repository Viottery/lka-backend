"""Delivery preferences suppress central notifications while inspection publishes."""
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from app.core.tools import ToolContext
from app.experts.management import ManagementExecutionFenced
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_coordinator import bind, finalize, timer
from tests.test_management_supervision_integration import pulse


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def central(runtime, **preferences):
    sid, goals = pulse(runtime, 1)
    runtime.management_delivery.configure(sid, expected_revision=0, **preferences)
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    runtime.management_coordinator.notifications.decide_notification(
        context, command_id='central-visible', notify_user=True, reason='Material source change needs review.')
    return sid, goals[0], run, context


@pytest.mark.parametrize('preferences', [
    {'muted': True},
    {'snooze_until': (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
])
def test_muted_or_snoozed_central_inspection_publishes_internal_changes_quietly(runtime, preferences):
    sid, goal, run, context = central(runtime, **preferences)
    runtime.managed_tasks.set_goal(context, command_id='record-progress', goal_id=goal['goal_id'],
        expected_revision=1, objective='Source inspected; accepted work remains incomplete.')
    before = runtime.management_coordinator.notifications.for_run(run.run_id)
    assert before['user_visible'] is False  # The same hook gates pre-publication SSE.
    assert before['notification']['notify_user'] is False
    assert before['notification']['decision_source'] == 'delivery_policy'
    assert before['notification']['requested_notify_user'] is True
    assert before['notification']['requested_reason'] == 'Material source change needs review.'
    assert finalize(runtime, run, notify_user=None)
    completed = runtime.agent_run_manager.get_run(run.run_id)
    assert completed.status.value == 'completed'
    assert completed.result_snapshot['user_visible'] is False
    assert completed.result_snapshot['notification'] == before['notification']
    assert runtime.management_goals.get_goal(sid, goal['goal_id'])['revision'] == 2
    assert runtime.management_goals.list_executions(sid)['executions'][-1]['status'] == 'completed'
    assert all(event['consumed_execution_id'] for event in runtime.management_goals.list_events(sid)['events'])
    assert runtime.management_goals.list_deliveries(sid)['deliveries'] == []
    assert runtime.session_service.get_session(session_id=sid).messages == []
    final_events = [event for event in runtime.agent_run_manager.list_events(run.run_id) if event.type == 'final_answer']
    assert len(final_events) == 1
    assert final_events[0].payload['metadata']['user_visible'] is False
    with closing(runtime._conn()) as conn:
        proposal = conn.execute('SELECT notify_user,reason,applied_at FROM manager_notification_proposals WHERE run_id=?',
                                (run.run_id,)).fetchone()
        assert proposal['notify_user'] == 1  # Preserve the actual model proposal.
        assert proposal['reason'] == 'Material source change needs review.'
        assert proposal['applied_at']
        assert conn.execute('SELECT count(*) FROM agent_session_context_messages WHERE session_id=?', (sid,)).fetchone()[0] == 2


def test_noncentral_projection_preserves_existing_notification_behavior(runtime):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    runtime.management_coordinator.notifications.decide_notification(
        context, command_id='ordinary-timer', notify_user=True, reason='Existing timer notification.')
    runtime.management_delivery.configure(sid, expected_revision=0, muted=True)
    projection = runtime.management_coordinator.notifications.for_run(run.run_id)
    assert projection == {'notification': {'notify_user': True, 'reason': 'Existing timer notification.',
                                          'decision_source': 'explicit'}, 'user_visible': True}


@pytest.mark.parametrize('allow_decisions', [True, False])
def test_mixed_central_batch_keeps_existing_structured_decision_exception(runtime, allow_decisions):
    sid, _, run, _ = central(runtime, muted=True, allow_decisions=allow_decisions)
    with closing(runtime._conn()) as conn, conn:
        event = runtime.management_goals._event(conn, sid, 'structured-decision', 'timer',
            {'report_kind': 'needs_decision'}, datetime.now(UTC).isoformat())
        assert event == 1
        event_id = conn.execute("SELECT event_id FROM manager_inbox WHERE dedup_key='structured-decision'").fetchone()[0]
        execution = conn.execute('SELECT execution_id,event_ids FROM manager_executions WHERE run_id=?', (run.run_id,)).fetchone()
        event_ids = json.loads(execution['event_ids']) + [event_id]
        conn.execute('UPDATE manager_executions SET event_ids=? WHERE execution_id=?',
                     (json.dumps(event_ids), execution['execution_id']))
    projection = runtime.management_coordinator.notifications.for_run(run.run_id)
    assert projection['user_visible'] is allow_decisions
    assert projection['notification']['decision_source'] == ('explicit' if allow_decisions else 'delivery_policy')


def test_delivery_revision_change_still_fences_central_publication(runtime):
    sid, _, run, _ = central(runtime, muted=True)
    runtime.management_delivery.configure(sid, expected_revision=1, muted=False)
    with pytest.raises(ManagementExecutionFenced, match='manager_delivery_policy_changed'):
        finalize(runtime, run, notify_user=None)
    assert runtime.management_goals.list_deliveries(sid)['deliveries'] == []
    assert runtime.management_goals.list_events(sid)['events'][0]['consumed_execution_id'] is None
    with closing(runtime._conn()) as conn:
        assert conn.execute('SELECT applied_at FROM manager_notification_proposals WHERE run_id=?',
                            (run.run_id,)).fetchone()[0] is None


def test_unmuting_allows_a_later_central_cycle_without_replaying_suppressed_delivery(runtime):
    sid, _, run, _ = central(runtime, muted=True)
    assert finalize(runtime, run, notify_user=None)
    assert runtime.management_goals.list_deliveries(sid)['deliveries'] == []
    runtime.management_delivery.configure(sid, expected_revision=1, muted=False)
    with closing(runtime._conn()) as conn:
        due = conn.execute('SELECT next_due_at FROM manager_attention_sweep_state WHERE session_id=?', (sid,)).fetchone()[0]
    assert runtime.management_supervision.tick(datetime.fromisoformat(due)) == 1
    runtime.management_coordinator.tick()
    next_run = bind(runtime)
    runtime.agent_run_manager.mark_running(next_run.run_id)
    assert finalize(runtime, next_run, notify_user=True)
    deliveries = runtime.management_goals.list_deliveries(sid)['deliveries']
    assert len(deliveries) == 1
    assert deliveries[0]['run_id'] == next_run.run_id
    completed = runtime.agent_run_manager.get_run(next_run.run_id)
    assert completed.result_snapshot['user_visible'] is True
    assert completed.result_snapshot['notification']['decision_source'] == 'explicit'
