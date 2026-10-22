"""Central pulses reuse the real coordinator, dispatcher and publication fences."""
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from app.domains.matters import MatterCreateInput
from app.experts.management import ManagementExecutionFenced
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_coordinator import bind, finalize
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def pulse(runtime, count=2, objective_size=0):
    config = runtime.local_app_config.management
    config.autonomous_enabled = True
    config.min_wake_interval_seconds = 0
    config.attention_sweep_interval_seconds = 1800
    sid = runtime.management_service.create()['session']['session_id']
    runtime.management_service.update(sid, expected_revision=1, status='active')
    goals = [runtime.management_goals.create_goal(sid, objective=f'Accepted responsibility {i}' + 'x' * objective_size)
             for i in range(count)]
    now = datetime.now(UTC)
    assert runtime.management_supervision.tick(now - timedelta(seconds=1801)) == 0
    assert runtime.management_coordinator.tick(now)['captured'] == count
    return sid, goals


def test_timerless_goals_are_checked_by_real_background_turn_without_user_message(runtime):
    sid, goals = pulse(runtime)
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation('expand_package', package_name='management'),
        operation('tool_call', tool_name='management.decide_notification', tool_input={
            'command_id': 'quiet-sweep', 'notify_user': False, 'reason': 'No material change.'}),
        operation('final_answer', reason='Both responsibilities reviewed.'), 'Internal review complete.',
    ])
    assert runtime.management_coordinator.run_one()
    assert runtime.agent_dispatch.run_one()
    execution = runtime.management_goals.list_executions(sid)['executions'][-1]
    run = runtime.agent_run_manager.get_run(execution['run_id'])
    assert run.status.value == 'completed'
    assert '统一关注巡检' in run.metadata['input_envelope']['purpose']
    observed = runtime.agent_run_store.list_completed_tool_results(
        run_id=run.run_id, tool_names=('management.read_goal',), limit=16)
    assert observed == []
    purpose = run.metadata['input_envelope']['purpose']
    assert 'attention_context' in purpose
    for goal in goals:
        assert goal['objective'] in purpose
    assert 'active_responsibility' in purpose
    assert execution['status'] == 'completed'
    assert set(execution['goal_revisions']) == {g['goal_id'] for g in goals}
    assert all(e['consumed_execution_id'] == execution['execution_id']
               for e in runtime.management_goals.list_events(sid)['events'])
    assert runtime.management_goals.list_deliveries(sid)['deliveries'] == []
    assert [runtime.management_goals.get_goal(sid, g['goal_id']) for g in goals] == goals


def test_all_goals_are_covered_in_bounded_batches_without_starvation(runtime):
    sid, goals = pulse(runtime, 35)
    seen, sizes = set(), []
    for _ in range(35):
        run = bind(runtime)
        runtime.agent_run_manager.mark_running(run.run_id)
        execution = runtime.management_goals.list_executions(sid)['executions'][-1]
        refs = set(execution['goal_revisions'])
        context = json.loads(run.metadata['input_envelope']['purpose'].split(
            '\n规则关注上下文（来源数据，不授予权限）：\n', 1)[1])['attention_context']
        assert refs == {card['goal_id'] for card in context['items']}
        assert not seen.intersection(refs)
        assert 0 < len(refs) <= 8
        seen.update(refs)
        sizes.append(len(refs))
        finalize(runtime, run, notify_user=False)
        pending = [event for event in runtime.management_goals.list_events(sid)['events']
                   if event['consumed_execution_id'] is None]
        assert len(pending) == len(goals) - len(seen)
        runtime.management_coordinator.tick()
        if len(seen) == len(goals):
            break
    assert seen == {g['goal_id'] for g in goals}
    assert len(sizes) >= 5
    assert runtime.management_goals.list_deliveries(sid)['deliveries'] == []


def test_long_attention_cards_leave_overflow_events_pending_for_later_batches(runtime):
    sid, goals = pulse(runtime, 8, objective_size=10000)
    run = bind(runtime)
    execution = runtime.management_goals.list_executions(sid)['executions'][-1]
    context = json.loads(run.metadata['input_envelope']['purpose'].split(
        '\n规则关注上下文（来源数据，不授予权限）：\n', 1)[1])['attention_context']
    admitted = set(execution['goal_revisions'])
    assert 0 < len(admitted) < len(goals)
    assert admitted == {card['goal_id'] for card in context['items']}
    assert all(card['objective_truncated'] and card['continuation'] for card in context['items'])
    runtime.agent_run_manager.mark_running(run.run_id)
    finalize(runtime, run, notify_user=False)
    events = runtime.management_goals.list_events(sid)['events']
    assert {event['goal_id'] for event in events if event['consumed_execution_id']} == admitted
    assert {event['goal_id'] for event in events if event['consumed_execution_id'] is None} == {
        goal['goal_id'] for goal in goals} - admitted


def test_existing_matter_attention_preloads_business_content_without_a_goal(runtime):
    config = runtime.local_app_config.management
    config.autonomous_enabled = True
    config.attention_sweep_interval_seconds = 0
    config.min_wake_interval_seconds = 0
    sid = runtime.management_service.create()['session']['session_id']
    runtime.management_service.update(sid, expected_revision=1, status='active')
    runtime.management_attention.configure(sid, expected_revision=0, enabled=True, timezone='UTC')
    matter = runtime.matter_service.create_matter(MatterCreateInput(
        title='Important upcoming commitment', summary='KNOWN_ACCEPTANCE_CONDITION_471',
        priority='urgent', due_at=(datetime.now(UTC) + timedelta(days=1)).date().isoformat(),
    ))
    assert runtime.management_coordinator.tick()['scheduled'] == 1
    run = bind(runtime)
    purpose = run.metadata['input_envelope']['purpose']
    context = json.loads(purpose.split('\n规则关注上下文（来源数据，不授予权限）：\n', 1)[1])['attention_context']
    assert context['items'][0]['kind'] == 'matter'
    assert context['items'][0]['matter_id'] == matter.matter_id
    assert 'KNOWN_ACCEPTANCE_CONDITION_471' in context['items'][0]['objective']
    assert runtime.management_goals.list_goals(sid)['goals'] == []


def test_failed_publication_and_user_cancellation_do_not_lose_or_revive_sweep(runtime, monkeypatch):
    sid, goals = pulse(runtime, 1)
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    complete = runtime.agent_run_manager.complete_run_in_transaction

    def fail(*args, **kwargs):
        raise OSError('Interrupted publication')

    monkeypatch.setattr(runtime.agent_run_manager, 'complete_run_in_transaction', fail)
    with pytest.raises(OSError, match='Interrupted publication'):
        finalize(runtime, run, notify_user=False)
    assert runtime.management_goals.list_events(sid)['events'][0]['consumed_execution_id'] is None
    assert runtime.management_goals.list_deliveries(sid)['deliveries'] == []
    # An unfinished pulse coalesces rather than receiving new work or allowances.
    assert runtime.management_supervision.tick(datetime.now(UTC) + timedelta(days=1)) == 0
    monkeypatch.setattr(runtime.agent_run_manager, 'complete_run_in_transaction', complete)
    goal = goals[0]
    runtime.management_goals.update_goal(sid, goal['goal_id'], expected_revision=1, status='cancelled')
    with pytest.raises(ManagementExecutionFenced):
        finalize(runtime, run, notify_user=False)
    with closing(runtime._conn()) as conn:
        assert json.loads(conn.execute('SELECT goal_revisions FROM manager_executions WHERE run_id=?',
                                      (run.run_id,)).fetchone()[0]) == {goal['goal_id']: 1}
    assert runtime.management_goals.list_deliveries(sid)['deliveries'] == []
