"""Long durable management requirements remain valid and fully readable."""
import json
from contextlib import closing
from datetime import UTC, datetime

import pytest

from app.core.turn_input import TurnInputEnvelope
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_coordinator import bind, enable, timer
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def dispatch_validated(runtime, run):
    envelope = TurnInputEnvelope.model_validate(run.metadata['input_envelope'])
    assert len(envelope.purpose) <= 16000
    assert runtime.agent_turn_loop._input_envelope_for_run(run.run_id) == envelope
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation('final_answer', reason='Input envelope validation probe'), 'Input accepted.',
    ])
    assert runtime.agent_dispatch.run_one()
    latest = runtime.agent_run_manager.get_run(run.run_id)
    assert latest.status.value == 'completed', latest.error


def read_complete_delegate(runtime, context, delegate_id):
    parts, start = [], 0
    while True:
        page = runtime.management_delegates.read_delegate(context, delegate_id=delegate_id, start_char=start)
        assert len(page['content']) <= 4000
        parts.append(page['content'])
        if page['next_char'] is None:
            return json.loads(''.join(parts))
        start = page['next_char']


def test_long_delegate_source_and_large_event_batch_dispatch_with_complete_continuations(runtime):
    enable(runtime)
    parent = context_fixture.__wrapped__(runtime)
    objective = 'O' * 15984 + 'OBJECTIVE_TAIL!!'
    quote = 'Q' * 7987 + 'QUOTE_TAIL!!!'
    assert len(objective) <= 16000 and len(quote) == 8000
    with closing(runtime._conn()) as conn, conn:
        conn.execute('UPDATE agent_session_messages SET content=? WHERE message_id=?', (quote, 'server-original'))
    line = runtime.management_delegates.delegate_work(parent, command_id='long-line', objective=objective)
    with closing(runtime._conn()) as conn, conn:
        delegate = runtime.management_delegates.get_for_session(conn, line['session_id'])
        # Internal legacy metadata is retained for accounting, never projected.
        envelope = delegate['kickoff_envelope']
        envelope['debug_info']['inherited_work_metadata'] = {'work_budget_scopes': [
            {'scope_id': 'private-inherited-budget', 'max_calls': 100},
        ]}
        conn.execute('UPDATE manager_delegates SET kickoff_envelope=? WHERE delegate_id=?',
                     (json.dumps(envelope), line['delegate_id']))
        for index in range(15):
            runtime.management_goals._event(conn, line['session_id'], f'large:{index}', 'timer',
                {'detail': '\x01' * 600 + f'EVENT_TAIL_{index}'}, datetime.now(UTC).isoformat())
    complete = read_complete_delegate(runtime, parent, line['delegate_id'])
    assert complete['objective'] == objective
    assert complete['source_envelope']['purpose'] == objective
    assert complete['source_envelope']['original_user_quotes'] == [quote]
    assert complete['source_envelope']['original_user_message_refs'] == ['server-original']
    assert 'debug_info' not in complete['source_envelope']
    assert 'private-inherited-budget' not in json.dumps(complete)
    assert runtime.management_coordinator.tick()['scheduled'] == 1
    run = bind(runtime)
    purpose = run.metadata['input_envelope']['purpose']
    assert 'management.read_delegate' in purpose and 'management.read_events' in purpose
    assert '截断预览' in purpose
    assert any(scope['scope_id'] == 'private-inherited-budget' for scope in run.metadata['work_budget_scopes'])
    with closing(runtime._conn()) as conn:
        bound_ids = json.loads(conn.execute('SELECT event_ids FROM manager_executions WHERE run_id=?',
                                           (run.run_id,)).fetchone()[0])
    assert len(bound_ids) == 16
    events, after = [], 0
    while True:
        page = runtime.management_goals.list_events(line['session_id'], after=after, limit=3)
        events.extend(page['events'])
        if page['next_after'] is None:
            break
        after = page['next_after']
    assert {event['event_id'] for event in events} == set(bound_ids)
    assert [event['payload']['detail'] for event in events[1:]] == [
        '\x01' * 600 + f'EVENT_TAIL_{index}' for index in range(15)
    ]
    dispatch_validated(runtime, run)


def test_large_manager_event_batch_and_full_goal_remain_readable(runtime):
    objective = 'G' * 15990 + 'GOAL_TAIL!'
    sid, goal = timer(runtime, objective=objective)
    with closing(runtime._conn()) as conn, conn:
        for index in range(16):
            runtime.management_goals._event(conn, sid, f'large:{index}', 'timer',
                {'detail': 'E' * 3800 + str(index)}, datetime.now(UTC).isoformat())
    assert runtime.management_coordinator.tick()['scheduled'] == 1
    run = bind(runtime)
    purpose = run.metadata['input_envelope']['purpose']
    assert 'management.read_events' in purpose and 'management.read_goal' in purpose
    # The prompt carries references only (since 70a22fe); the full goal stays
    # readable through management.read_goal, checked below.
    assert run.metadata['input_envelope']['debug_info']['event_count'] == 16
    assert runtime.management_goals.get_goal(sid, goal['goal_id'])['objective'] == objective
    events = runtime.management_goals.list_events(sid)['events']
    assert len(events) == 17
    assert events[15]['payload']['detail'].endswith('15')
    dispatch_validated(runtime, run)


def test_goal_and_event_tools_continue_complete_durable_requirements(runtime):
    parent = context_fixture.__wrapped__(runtime)
    objective = 'G' * 15990 + 'GOAL_TAIL!'
    goal = runtime.management_goals.create_goal(parent.session_id, objective=objective)
    parts, start = [], 0
    while True:
        page = runtime.managed_tasks.read_goal(parent, goal_id=goal['goal_id'], start_char=start)
        parts.append(page['objective'])
        if page['next_char'] is None:
            break
        start = page['next_char']
    assert ''.join(parts) == objective
    with closing(runtime._conn()) as conn, conn:
        for index in range(16):
            runtime.management_goals._event(conn, parent.session_id, f'large:{index}', 'timer',
                {'detail': 'E' * 3800 + str(index)}, datetime.now(UTC).isoformat())
    events, after = [], 0
    while True:
        page = runtime.managed_tasks.read_events(parent, after=after, limit=3)
        events.extend(page['events'])
        if page['next_after'] is None:
            break
        after = page['next_after']
    assert [event['payload']['detail'] for event in events] == ['E' * 3800 + str(i) for i in range(16)]


def test_long_goal_with_small_event_batch_explicitly_requires_continuation(runtime):
    sid, goal = timer(runtime, objective='G' * 16000)
    assert runtime.management_coordinator.tick()['scheduled'] == 1
    run = bind(runtime)
    assert run.session_id == sid
    purpose = run.metadata['input_envelope']['purpose']
    assert '截断预览' not in purpose
    assert 'management.read_goal' in purpose and 'next_char' in purpose and goal['goal_id'] in purpose
    dispatch_validated(runtime, run)
