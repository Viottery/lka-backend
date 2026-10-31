"""Accepted failures release their session; overload never creates an orphan."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.routes.agent import stream_agent_turn
from app.api.schemas import AgentTurnRequest
from app.core.config import get_settings
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def app(tmp_path, monkeypatch):
    config = tmp_path / 'local.toml'
    config.write_text('[llm]\nprovider="mock"\n[agent]\nunified_entry_enabled=true\norchestrator="langgraph"\ncheckpoint_backend="sqlite"\n[memory]\nenabled=false\nbackground_enabled=false\n[message_history]\nenabled=false\nbackground_enabled=false\n', encoding='utf-8')
    monkeypatch.setenv('LKA_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('LKA_LOCAL_CONFIG', str(config))
    get_settings.cache_clear()
    instance = create_app()
    try:
        yield instance
    finally:
        instance.state.runtime.stop()
        get_settings.cache_clear()


def _persisted_runs(runtime, sid):
    from app.core.agent_runs import AgentRunRecord

    with runtime._conn() as conn:
        return [AgentRunRecord.model_validate_json(row[0]) for row in conn.execute(
            'SELECT record_payload FROM agent_runs WHERE session_id=? ORDER BY created_at', (sid,),
        ).fetchall()]


def test_http_capacity_refusal_precedes_persistence_and_recovery_succeeds(app):
    runtime = app.state.runtime
    sid = runtime.create_session(title='HTTP capacity').session.session_id
    leases = [runtime.execution_admission.try_acquire() for _ in range(4)]
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            rejected = client.post('/agent/turn', json={'session_id': sid, 'user_input': 'synthetic',
                                                      'client_request_id': 'capacity-correlation'})
            assert rejected.status_code == 503
            assert rejected.json() == {'detail': 'agent_execution_capacity_full',
                                       'accepted': False, 'retry_safe': True}
            assert _persisted_runs(runtime, sid) == []
            for lease in leases:
                lease.close()
            runtime.agent_turn_loop.llm_client = ScriptedClient([operation('final_answer', reason='test'), 'OK', '{"faithful":true,"hallucination_free":true,"request_aligned":true,"decision":"pass","issues":[],"revision_instructions":""}'])
            accepted = client.post('/agent/turn', json={'session_id': sid, 'user_input': 'synthetic'})
            assert accepted.status_code == 200
            assert accepted.json()['answer'] == 'OK'
    finally:
        for lease in leases:
            lease.close()


def test_http_checkpoint_entry_failure_does_not_keep_session_busy(app, monkeypatch):
    runtime = app.state.runtime
    sid = runtime.create_session(title='Checkpoint startup fault').session.session_id
    checkpoints = runtime.agent_turn_runner.checkpoint_runtime
    original = checkpoints.sync_saver

    @contextmanager
    def fail_saver():
        raise TimeoutError('injected checkpoint capacity fault')
        yield  # pragma: no cover - make this a context manager

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(checkpoints, 'sync_saver', fail_saver)
        failed = client.post('/agent/turn', json={'session_id': sid, 'user_input': 'first',
                                                'client_request_id': 'accepted-failure'})
        assert failed.status_code == 500
        runs = _persisted_runs(runtime, sid)
        assert len(runs) == 1 and runs[0].status.value == 'failed'
        assert runs[0].error_type == 'execution_start_failed'
        with runtime._conn() as conn:
            assert conn.execute('SELECT run_id FROM session_admissions WHERE session_id=?', (sid,)).fetchone()[0] is None
        assert runtime.execution_admission.snapshot()['interactive'] == 0
        monkeypatch.setattr(checkpoints, 'sync_saver', original)
        runtime.agent_turn_loop.llm_client = ScriptedClient([operation('final_answer', reason='test'), 'RECOVERED', '{"faithful":true,"hallucination_free":true,"request_aligned":true,"decision":"pass","issues":[],"revision_instructions":""}'])
        recovered = client.post('/agent/turn', json={'session_id': sid, 'user_input': 'second'})
        assert recovered.status_code == 200
        assert recovered.json()['answer'] == 'RECOVERED'


def test_stream_closed_before_start_releases_reservation_and_session(app):
    runtime = app.state.runtime
    sid = runtime.create_session(title='Unused stream').session.session_id

    async def scenario():
        response = await stream_agent_turn(
            AgentTurnRequest(session_id=sid, user_input='never started'),
            SimpleNamespace(app=app),
        )
        assert runtime.execution_admission.snapshot()['interactive'] == 1
        await response.background()
        assert runtime.execution_admission.snapshot()['interactive'] == 0
        runs = _persisted_runs(runtime, sid)
        assert len(runs) == 1 and runs[0].status.value == 'failed'
        assert runtime.create_agent_run(session_id=sid, user_input='next').run_id != runs[0].run_id

    asyncio.run(scenario())


def test_stream_header_send_failure_releases_unused_permit(app):
    from starlette.requests import ClientDisconnect

    runtime = app.state.runtime
    sid = runtime.create_session(title='Header transport failure').session.session_id

    async def scenario():
        response = await stream_agent_turn(
            AgentTurnRequest(session_id=sid, user_input='headers never sent'),
            SimpleNamespace(app=app),
        )

        async def send(_message):
            raise OSError('injected transport failure before body')

        async def receive():
            return {'type': 'http.disconnect'}

        with pytest.raises(ClientDisconnect):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        assert runtime.execution_admission.snapshot()['interactive'] == 0
        runs = _persisted_runs(runtime, sid)
        assert len(runs) == 1 and runs[0].status.value == 'failed'

    asyncio.run(scenario())
