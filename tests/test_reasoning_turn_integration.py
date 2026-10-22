"""Reasoning preferences reach actual service requests in both orchestrators."""
import asyncio
import json

import pytest

from app.core.llm import LLMResponse, LLMResponseMode, LLMStreamEvent, build_llm_service
from app.core.local_config import LLMClientConfig, LLMProviderConfig
from tests.test_unified_entry_quality import operation, runtime_for


def install_service(runtime, monkeypatch, outputs):
    monkeypatch.setenv('REASONING_INTEGRATION_KEY', 'isolated-test-key')
    service = build_llm_service(LLMProviderConfig(
        default_client='test', clients=[LLMClientConfig(
            name='test', api_key_env='REASONING_INTEGRATION_KEY',
            default_model='test-model', thinking_control='deepseek', context_window_tokens=262144,
        )],
    ))
    client = service.registry.get('test')
    captured = []
    responses = iter(outputs)

    async def complete(request):
        wire = json.loads(client._build_request(request, stream=False).data)
        captured.append((request, wire))
        return LLMResponse(provider='test', client_name='test', model='test-model',
                           status='completed', content=next(responses),
                           prompt_summary=request.prompt_summary)

    async def stream(request):
        wire = json.loads(client._build_request(request, stream=True).data)
        captured.append((request, wire))
        content = next(responses)
        yield LLMStreamEvent(event_type='llm_delta', stage=request.metadata['stage'],
                            client_name='test', provider='test', model='test-model', delta=content)
        yield LLMStreamEvent(event_type='llm_completed', stage=request.metadata['stage'],
                            client_name='test', provider='test', model='test-model',
                            content_snapshot=content,
                            metadata={'status': 'completed', 'partial': False, 'finish_reason': 'stop'})

    monkeypatch.setattr(client, 'complete', complete)
    monkeypatch.setattr(client, 'stream', stream)
    runtime.agent_llm_client = runtime.agent_turn_loop.llm_client = service
    return captured


@pytest.mark.parametrize('orchestrator', ['legacy', 'langgraph'])
@pytest.mark.parametrize('level', [None, 'off', 'max'])
@pytest.mark.parametrize('stream', [False, True])
def test_user_reasoning_reaches_transport_and_trace(tmp_path, monkeypatch, orchestrator, level, stream):
    runtime, _ = runtime_for(tmp_path, monkeypatch, orchestrator, [])
    calls = install_service(runtime, monkeypatch, [
        operation('final_answer', reason='A greeting needs no source lookup.'), 'Hello.',
    ])
    try:
        result = runtime.run_agent_turn(session_id='greeting', user_input='Say hello.',
            llm_reasoning_level=level,
            llm_response_mode=LLMResponseMode.STREAM if stream else LLMResponseMode.TEXT)
        expected = level or 'low'
        assert result.answer == 'Hello.'
        assert len(calls) == 2
        assert all(request.reasoning_level.value == expected for request, _ in calls)
        for request, wire in calls:
            assert request.metadata['reasoning_policy'] == 'user'
            assert wire['thinking']['type'] == ('disabled' if expected == 'off' else 'enabled')
            assert wire.get('reasoning_effort') == (None if expected == 'off' else expected)
        run = runtime.agent_run_manager.get_run(result.run_id)
        assert run.metadata['llm_reasoning_level'] == expected
        assert run.metadata['session_policy']['llm_reasoning_level'] == expected
        records = [event.payload['audit_record'] for event in runtime.agent_run_manager.list_events(result.run_id)
                   if event.type == 'llm_completed']
        assert len(records) == 2
        assert all(record['metadata']['reasoning']['requested'] == expected for record in records)
        assert all(record['metadata']['reasoning']['effective'] == expected for record in records)
    finally:
        runtime.stop()


@pytest.mark.parametrize('orchestrator', ['legacy', 'langgraph'])
def test_frozen_run_does_not_adopt_later_request_level(tmp_path, monkeypatch, orchestrator):
    runtime, _ = runtime_for(tmp_path, monkeypatch, orchestrator, [])
    calls = install_service(runtime, monkeypatch, [operation('final_answer'), 'Frozen.'])
    try:
        runner = runtime.agent_turn_runner if orchestrator == 'langgraph' else runtime.agent_turn_loop
        run = runner.create_run_for_turn(session_id='frozen', user_input='Reply.', llm_reasoning_level='max')
        result = runner.run(session_id='frozen', user_input='Reply.', existing_run_id=run.run_id,
                            llm_reasoning_level='off')
        assert result.answer == 'Frozen.'
        assert all(wire['reasoning_effort'] == 'max' for _, wire in calls)
    finally:
        runtime.stop()


def test_internal_purpose_does_not_change_business_preference(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch, 'legacy', [])
    calls = install_service(runtime, monkeypatch, ['{}', '{}', '{}'])
    try:
        from app.core.agent_turn import _turn_llm_reasoning_level
        token = _turn_llm_reasoning_level.set('max')
        try:
            for stage in ['route', 'safety_review', 'decision']:
                runtime.agent_turn_loop._complete_text_once(stage=stage, system_prompt='Test.',
                    user_prompt='{}', prompt_summary=stage, max_output_tokens=None,
                    response_mode=LLMResponseMode.TEXT)
        finally:
            _turn_llm_reasoning_level.reset(token)
        assert [request.metadata['reasoning']['effective'] for request, _ in calls] == ['off', 'low', 'max']
        assert calls[-1][1]['reasoning_effort'] == 'max'
    finally:
        runtime.stop()


def test_new_user_level_wins_over_legacy_decision_disable(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch, 'legacy', [])
    calls = install_service(runtime, monkeypatch, [operation('final_answer'), 'Hello.'])
    runtime.agent_turn_loop.decision_thinking_enabled = False
    try:
        result = runtime.run_agent_turn(session_id='legacy-flag', user_input='Say hello.',
                                        llm_reasoning_level='low')
        assert result.answer == 'Hello.'
        assert calls[0][1]['thinking'] == {'type': 'enabled'}
        assert calls[0][1]['reasoning_effort'] == 'low'
    finally:
        runtime.stop()


@pytest.mark.parametrize('structured', [False, True])
def test_stream_failure_keeps_reasoning_in_call_audit(tmp_path, monkeypatch, structured):
    from app.core.llm.errors import LLMClientError

    runtime, _ = runtime_for(tmp_path, monkeypatch, 'legacy', [])
    install_service(runtime, monkeypatch, [])
    client = runtime.agent_llm_client.registry.get('test')

    async def failed_stream(request):
        yield LLMStreamEvent(event_type='llm_failed', stage='decision', provider='test',
            client_name='test', model='test-model', error='Synthetic stream failure',
            metadata={'error_event': {'type': 'server_error', 'message': 'Synthetic failure'}}
                     if structured else {})

    monkeypatch.setattr(client, 'stream', failed_stream)
    loop = runtime.agent_turn_loop
    try:
        with pytest.raises(LLMClientError) as caught:
            loop._stream_text_once(stage='decision', system_prompt='Test.', user_prompt='{}',
                prompt_summary='synthetic failure', max_output_tokens=None, llm_call_id='test-failed')
        event = loop._llm_error_event(stage='decision', provider='test', attempt=1,
            system_prompt='Test.', user_prompt='{}', prompt_summary='synthetic failure',
            started_at='2026-10-21T00:00:00Z', duration_ms=1,
            llm_call_id='test-failed', exc=caught.value)
        assert event.metadata['reasoning']['requested'] == 'low'
        assert event.metadata['reasoning']['effective'] == 'low'
    finally:
        runtime.stop()


def test_fork_snapshot_roundtrip_keeps_user_level_and_legacy_absence():
    from app.core.context_driver import ContextDriver
    from app.core.multi_agent import ContextSnapshot
    from tests.test_context_driver import _request

    for level in [None, 'off', 'max']:
        derived = asyncio.run(ContextDriver().derive(_request(request_reasoning_level=level)))
        restored = ContextSnapshot.model_validate(derived.snapshot.model_dump(mode='json'))
        assert restored.inference_reasoning_level == level
