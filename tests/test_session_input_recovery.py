"""Explicit pending-input recovery retains receipts, history and execution fences."""
import asyncio
import json
from contextlib import closing

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.session_inputs import router
from app.core.session_inputs import SessionInputConflict
from tests.test_session_input_queue import input_run, sid, submit
from tests.test_session_input_queue import runtime as _runtime
from tests.test_unified_entry_quality import ScriptedClient, operation

runtime = _runtime


def pending_after_failure(runtime):
    session_id = sid(runtime)
    submit(runtime, session_id, 'failed')
    runtime.session_inputs.tick()
    failed = input_run(runtime, session_id)
    runtime.agent_run_manager.fail_run(failed.run_id, error_type='LLMTimeoutError', error='offline timeout')
    runtime.session_inputs.tick()
    assert runtime.agent_dispatch.run_one()  # Retire the failed run's already queued dispatch job.
    runtime.management_service.update(session_id, expected_revision=1, llm_answer_model='unavailable-writer')
    pending = submit(runtime, session_id, 'pending', 'Please reply.')['input']
    return session_id, failed, pending


def test_pending_recovery_uses_primary_without_changing_fingerprint_or_preferences(runtime):
    session_id, failed, pending = pending_after_failure(runtime)
    recovered = runtime.session_inputs.resume(session_id, input_id=pending['input_id'])
    assert not recovered['paused']
    assert submit(runtime, session_id, 'pending', 'Please reply.')['input']['input_id'] == pending['input_id']
    message = next(m for m in runtime.get_session(session_id=session_id).messages if m.message_id == pending['message_id'])
    assert message.payload['original_answer_model_selection']['llm_answer_model'] == 'unavailable-writer'
    assert message.payload['answer_model_selection'] == {'llm_answer_client_name': None, 'llm_answer_model': None}
    assert message.payload['answer_model_recovery']['source'] == 'explicit_resume'
    assert runtime.management_service.get(session_id)['controller']['llm_answer_model'] == 'unavailable-writer'
    client = ScriptedClient([operation('final_answer', reason='Answer the user.'), 'Recovered reply.'])
    runtime.agent_turn_loop.llm_client = client
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    assert run.metadata['session_policy']['llm_answer_model'] is None
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(run.run_id).status.value == 'completed'
    assert runtime.agent_run_manager.get_run(failed.run_id).status.value == 'failed'
    messages = runtime.get_session(session_id=session_id).messages
    assert [m.content for m in messages if m.role == 'agent'] == ['Recovered reply.']


@pytest.mark.parametrize('state', ['bound', 'cancelled', 'foreign'])
def test_recovery_cannot_change_started_cancelled_or_foreign_input(runtime, state):
    session_id, _, pending = pending_after_failure(runtime)
    if state == 'bound':
        runtime.session_inputs.resume(session_id)
        runtime.session_inputs.tick()
    elif state == 'cancelled':
        with closing(runtime._conn()) as conn, conn:
            conn.execute('UPDATE session_inputs SET cancelled=1 WHERE input_id=?', (pending['input_id'],))
    else:
        foreign = runtime.session_inputs.submit(session_id, client_message_id='other', user_input='Other.', attachment_ids=[])['input']
        with closing(runtime._conn()) as conn, conn:
            conn.execute('UPDATE session_inputs SET session_id=? WHERE input_id=?', ('another-session', foreign['input_id']))
        pending = foreign
    with pytest.raises(SessionInputConflict, match='not_pending'):
        runtime.session_inputs.resume(session_id, input_id=pending['input_id'])


def test_invalid_recovery_selection_rolls_back_and_plain_resume_is_compatible(runtime, monkeypatch):
    session_id, _, pending = pending_after_failure(runtime)
    def reject_selection(**kwargs):
        raise ValueError('Unknown answer LLM client')
    monkeypatch.setattr(runtime.session_inputs, 'validate_answer_selection', reject_selection)
    with pytest.raises(ValueError):
        runtime.session_inputs.resume(session_id, input_id=pending['input_id'], answer_client_name='unknown')
    assert runtime.session_inputs.snapshot(session_id)['paused']
    with closing(runtime._conn()) as conn:
        payload = json.loads(conn.execute('SELECT payload FROM agent_session_messages WHERE message_id=?', (pending['message_id'],)).fetchone()[0])
    assert payload['answer_model_selection']['llm_answer_model'] == 'unavailable-writer'
    assert 'answer_model_recovery' not in payload
    assert not runtime.session_inputs.resume(session_id)['paused']


def test_http_explicit_model_recovery_and_target_required(runtime):
    session_id, _, pending = pending_after_failure(runtime)
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(router)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app, client=('127.0.0.1', 9999)), base_url='http://test') as client:
            url = f'/sessions/{session_id}/inputs/resume'
            invalid = await client.post(url, json={'answer_model': None})
            assert invalid.status_code == 422
            response = await client.post(url, json={'input_id': pending['input_id'], 'answer_client_name': None, 'answer_model': None})
            assert response.status_code == 200, response.text
            assert response.json()['paused'] is False
            assert (await client.post(url)).status_code == 200

    asyncio.run(exercise())
