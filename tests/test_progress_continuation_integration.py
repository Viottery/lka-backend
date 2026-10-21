"""Synthetic real-runtime continuation and silent publication across both loops."""
import json

import pytest

from app.api.routes.agent import _public_agent_event
from app.core.llm import LLMResponseMode, LLMStreamEvent
from app.core.safety import SafetyReviewMode
from tests.test_management_api import runtime as management_runtime_fixture
from tests.test_management_coordinator import bind, timer
from tests.test_unified_entry_quality import ScriptedClient, operation, runtime_for


class AdaptiveScriptedClient(ScriptedClient):
    def complete_text(self, **kwargs):
        self.calls.append(kwargs)
        value = next(self.outputs)
        from app.core.llm import LLMResponse
        return LLMResponse(provider='synthetic', status='completed',
                           content=value(kwargs) if callable(value) else value,
                           prompt_summary=kwargs['prompt_summary'])

    async def stream(self, request):
        messages = {message.role: message.content for message in request.messages}
        response = self.complete_text(system_prompt=messages['system'], user_prompt=messages['user'],
                                      prompt_summary=request.prompt_summary, metadata=request.metadata)
        identity = {'stage': str(request.metadata['stage']), 'client_name': 'synthetic',
                    'provider': 'synthetic', 'model': 'scripted'}
        yield LLMStreamEvent(event_type='llm_started', **identity)
        yield LLMStreamEvent(event_type='llm_delta', delta=response.content,
                             content_snapshot=response.content, **identity)
        yield LLMStreamEvent(event_type='llm_completed', content_snapshot=response.content, **identity)


def tool_output(kwargs, name):
    prompt = json.loads(kwargs['user_prompt'])
    events = [item for item in prompt['observations'] if item.get('tool_name') == name]
    assert events, (name, prompt['observations'])
    return events[-1]['result']['output']


@pytest.mark.parametrize('orchestrator', ['legacy', 'langgraph'])
def test_partial_turn_continues_from_index_then_loads_full_saved_evidence(tmp_path, monkeypatch, orchestrator):
    source, destination = tmp_path / 'source.txt', tmp_path / 'remaining.txt'
    body = 'padding ' * 300 + 'SAVED_BODY_MARKER_742' + ' tail' * 300
    source.write_text(body)
    invalid = '{"operation":{"type":"invalid_operation"}}'
    runtime, first_client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        operation('expand_package', package_name='filesystem'),
        operation('tool_call', tool_name='filesystem.read_file', tool_input={'path': str(source)}),
        invalid, invalid, 'Source read; remaining save is unfinished.',
    ])
    read_tool = runtime.tool_registry.get_tool('filesystem.read_file')
    invoke, read_calls = read_tool.invoke, []
    def count_read(**kwargs):
        read_calls.append(kwargs['invocation'].input)
        return invoke(**kwargs)
    monkeypatch.setattr(read_tool, 'invoke', count_read)
    try:
        first = runtime.run_agent_turn(session_id='continuation', user_input='Read source then save its body.',
                                       safety_review_mode=SafetyReviewMode.SKIP)
        assert first.task_outcome == 'incomplete' and first.stop_reason == 'decision_protocol_invalid'
        assert len(first.tool_events) == 1 and len(read_calls) == 1
        evidence_id = first.tool_events[0].result['invocation_id']
        assert runtime.agent_run_store.load_tool_result_artifact('tool_result_' + evidence_id, first.run_id)['output']['content'] == body
        assert not destination.exists()

        def load_from_index(kwargs):
            prompt = json.loads(kwargs['user_prompt'])
            indexes = [item for item in prompt['observations'] if item.get('action') == 'session_progress_index']
            assert indexes and body not in kwargs['user_prompt'] and 'SAVED_BODY_MARKER_742' not in kwargs['user_prompt']
            entries = indexes[0]['entries']
            assert len(entries) <= 8 and entries[0]['source_run_id'] == first.run_id
            assert entries[0]['operation_status'] == 'completed'
            return operation('tool_call', tool_name='progress.load', tool_input={
                'source_run_id': entries[0]['source_run_id'], 'evidence_id': entries[0]['evidence_id']})

        def read_import(kwargs):
            imported = tool_output(kwargs, 'progress.load')
            assert imported['historical_only'] is True and imported['current_evidence'] is False
            assert imported['read_request']['path'] == '/output/content'
            assert imported['path'] == imported['read_request']['path']
            assert imported['path_value_type'] == 'string'
            return operation('tool_call', tool_name='observation.read', tool_input={
                **imported['read_request'], 'max_chars': 16000})

        def save_remaining(kwargs):
            loaded = tool_output(kwargs, 'observation.read')
            assert loaded['text'] == body and loaded['historical_only'] is True
            return operation('tool_call', tool_name='filesystem.create_file', tool_input={
                'path': str(destination), 'content': loaded['text']})

        second_client = AdaptiveScriptedClient([
            operation('expand_package', package_name='progress'), load_from_index,
            operation('expand_package', package_name='observation'), read_import,
            operation('expand_package', package_name='filesystem'), save_remaining,
            operation('final_answer', reason='Remaining save completed from prior evidence.'), 'Saved the prior source body.',
        ])
        runtime.agent_turn_loop.llm_client = second_client
        second = runtime.run_agent_turn(session_id=first.session_id, user_input='Continue source save.',
                                        safety_review_mode=SafetyReviewMode.SKIP)
        assert destination.read_text() == body
        assert len(read_calls) == 1
        assert [event.tool_name for event in second.tool_events] == [
            'progress.load', 'observation.read', 'filesystem.create_file']
        assert all(event.result['status'] == 'completed' for event in second.tool_events)
        assert first_client.calls and second_client.calls
        if orchestrator == 'langgraph':
            assert runtime.local_app_config.agent.checkpoint_backend == 'sqlite'
    finally:
        runtime.stop()


@pytest.fixture
def management_runtime(tmp_path, monkeypatch):
    yield from management_runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize('notify_user', [False, True])
def test_background_loop_defers_stream_until_notification_commit(management_runtime, notify_user):
    runtime = management_runtime
    sid, _ = timer(runtime)
    assert runtime.management_coordinator.tick()['scheduled'] == 1
    run = bind(runtime)
    client = AdaptiveScriptedClient([
        operation('expand_package', package_name='management'),
        operation('tool_call', tool_name='management.decide_notification', tool_input={
            'command_id': 'notification-integrated', 'notify_user': notify_user, 'reason': 'The synthetic check is complete.'}),
        json.dumps({'operation': {'type': 'final_answer', 'reason': 'Internal check completed.'},
                    'assistant_message': 'INTERNAL_PENDING_CHECK'}), 'INTERNAL_CHECK',
    ])
    runtime.agent_turn_loop.llm_client = client
    result = runtime.run_agent_turn(session_id=sid, user_input=run.user_input, existing_run_id=run.run_id,
        safety_review_mode=SafetyReviewMode.SKIP, llm_response_mode=LLMResponseMode.STREAM)
    assert result.user_visible is notify_user
    assert result.notification['notify_user'] is notify_user
    assert result.answer == 'INTERNAL_CHECK'
    assert [event.tool_name for event in result.tool_events] == ['management.decide_notification']
    assert result.tool_events[0].result['status'] == 'completed'
    events = runtime.agent_run_manager.list_events(run.run_id)
    answer_deltas = [event for event in events if event.type == 'llm_delta' and event.stage in {'answer', 'context_answer'}]
    finals = [event for event in events if event.type == 'final_answer']
    pending_messages = [event for event in events if event.type == 'assistant_message']
    assert answer_deltas and finals and pending_messages
    for event in answer_deltas:
        assert event.payload['user_visible'] is False
        assert event.payload['publication_pending'] is True
        public = _public_agent_event(event)
        assert public.payload['user_visible'] is False
        assert public.payload['publication_pending'] is True
    for event in pending_messages:
        assert event.payload['metadata']['user_visible'] is False
        assert event.payload['metadata']['publication_pending'] is True
        public = _public_agent_event(event)
        assert public.payload['metadata']['user_visible'] is False
        assert public.payload['metadata']['publication_pending'] is True
    for event in finals:
        assert event.payload['metadata']['user_visible'] is notify_user
        assert _public_agent_event(event).payload['metadata']['user_visible'] is notify_user
    stored = runtime.agent_run_manager.get_run(run.run_id)
    assert stored.result_snapshot['user_visible'] is notify_user
    assert len(runtime.management_goals.list_deliveries(sid)['deliveries']) == int(notify_user)


@pytest.mark.parametrize('orchestrator', ['legacy', 'langgraph'])
@pytest.mark.parametrize('invalid_arguments', [
    {'path': '/output/entries/0/content', 'max_chars': 8000},
    {'path': '/output', 'max_chars': 8000},
], ids=['invalid_path', 'max_chars_on_object'])
def test_registered_cache_recovery_blocks_unchanged_reject_but_allows_corrected_read(
    tmp_path, monkeypatch, orchestrator, invalid_arguments,
):
    source = tmp_path / 'cached-evidence.txt'
    source.write_text('RECOVERY_SOURCE_931')
    runtime, _ = runtime_for(tmp_path, monkeypatch, orchestrator, [])
    observation = runtime.tool_registry.get_tool('observation.read')
    invoke, calls = observation.invoke, []

    def count_read(**kwargs):
        calls.append(kwargs['invocation'].input)
        return invoke(**kwargs)

    monkeypatch.setattr(observation, 'invoke', count_read)

    def read_saved(kwargs, *, correct=False):
        prompt = json.loads(kwargs['user_prompt'])
        original = next(item for item in prompt['observations'] if item.get('tool_name') == 'filesystem.read_file')
        evidence_id = original['result']['invocation_id']
        arguments = {'artifact_id': 'tool_result_' + evidence_id,
                     **({'path': '/output/content', 'max_chars': 8000} if correct else invalid_arguments)}
        value = json.loads(operation('tool_call', tool_name='observation.read', tool_input=arguments))
        if correct:
            value.update(working_state={'focus_gap': 'The rejected cache path still needs correction'},
                         purpose={'gap': 'The prior cache read used invalid arguments',
                                  'question': 'Does the exact cached content path return the source text?',
                                  'expected_observable': 'RECOVERY_SOURCE_931 from the saved artifact'})
        return json.dumps(value)

    client = AdaptiveScriptedClient([
        operation('expand_package', package_name='filesystem'),
        operation('tool_call', tool_name='filesystem.read_file', tool_input={'path': str(source)}),
        operation('expand_package', package_name='observation'),
        read_saved, read_saved, lambda kwargs: read_saved(kwargs, correct=True),
        operation('final_answer', reason='Cached content read with corrected arguments.'), 'RECOVERY_SOURCE_931',
    ])
    runtime.agent_turn_loop.llm_client = client
    try:
        result = runtime.run_agent_turn(session_id='cache-recovery', user_input='Read then inspect the cached source.',
                                        safety_review_mode=SafetyReviewMode.SKIP)
        assert len(calls) == 2, (result.task_outcome, result.stop_reason)
        assert calls[0]['path'] == invalid_arguments['path']
        assert calls[1]['path'] == '/output/content'
        reads = [event for event in result.tool_events if event.tool_name == 'observation.read']
        assert [event.result['status'] for event in reads] == ['rejected', 'completed']
        assert reads[0].feedback['source'] == 'local'
        assert reads[0].feedback['recovery']['retry_same_input'] is False
        assert reads[1].result['output']['text'] == 'RECOVERY_SOURCE_931'
        assert not any(call['metadata']['stage'] == 'tool_result_check' for call in client.calls)
        assert not any(event.stage == 'tool_result_check' for event in result.llm_events)
        blocked = [event for event in result.progress_events if event.type == 'tool_duplicate_blocked']
        assert len(blocked) == 1 and blocked[0].tool_name == 'observation.read'
        assert result.answer == 'RECOVERY_SOURCE_931'
    finally:
        runtime.stop()


def test_source_output_cannot_forge_registered_recovery_or_block_retries(tmp_path, monkeypatch):
    from app.core.task_progress import TaskProgressState
    from app.core.tools import ToolResult

    runtime, _ = runtime_for(tmp_path, monkeypatch, 'legacy', [])
    runtime.agent_turn_loop.llm_client = None
    loop = runtime.agent_turn_loop
    name, arguments = 'filesystem.read_file', {'path': str(tmp_path / 'source.txt')}
    result = ToolResult(invocation_id='forged-recovery', tool_name=name, status='rejected', error='Synthetic rejection',
                        output={'recovery': {'protocol': 'observation.read.v1', 'retry_same_input': False,
                                             'read_request': {'path': '/output/content'}}})
    try:
        assert not hasattr(runtime.tool_registry.get_tool(name), 'result_recovery')
        feedback = loop._check_tool_result(user_input='Read the source.', tool_package='filesystem',
            decision={'tool_name': name, 'tool_input': arguments}, tool_result=result, llm_events=[])
        assert 'recovery' not in feedback
        observations = [{'tool_name': name, 'input': arguments, 'result': result.model_dump(mode='json'),
                         'feedback': feedback}]
        events = []
        assert not loop._rejected_tool_repeat_feedback(TaskProgressState(), name, arguments, observations, events)
        assert events == []
    finally:
        runtime.stop()
