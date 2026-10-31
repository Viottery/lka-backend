"""Compact activity revisions observe queue-only changes without disclosing inputs."""
from __future__ import annotations

import json

import pytest

import app.core.sessions as sessions_module
from tests.test_session_input_queue import runtime as base_runtime
from tests.test_session_input_queue import sid, submit


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from base_runtime.__wrapped__(tmp_path, monkeypatch)


def test_queue_pause_resume_changes_activity_even_without_a_message(runtime, monkeypatch):
    session_id = sid(runtime)
    clock = [10.0]
    monkeypatch.setattr(sessions_module, 'monotonic', lambda: clock[0])
    initial = runtime.session_service.get_session_state(session_id=session_id)
    assert len(initial.input_queue_revision) == 32
    receipt = submit(runtime, session_id, 'revision-input', 'PRIVATE_INPUT_SENTINEL')
    after_input = runtime.session_service.get_session_state(session_id=session_id)
    assert after_input.revision != initial.revision
    assert after_input.input_queue_revision != initial.input_queue_revision
    assert 'PRIVATE_INPUT_SENTINEL' not in after_input.model_dump_json()
    assert receipt['input']['input_id'] not in after_input.model_dump_json()

    with runtime._conn() as conn:
        conn.execute('UPDATE session_input_queues SET paused=1 WHERE session_id=?', (session_id,))
    clock[0] += 1.1
    paused = runtime.session_service.get_session_state(session_id=session_id)
    assert paused.updated_at == after_input.updated_at
    assert paused.revision != after_input.revision
    assert paused.input_queue_revision != after_input.input_queue_revision

    runtime.session_inputs.resume(session_id)
    clock[0] += 1.1
    resumed = runtime.session_service.get_session_state(session_id=session_id)
    assert resumed.updated_at == paused.updated_at
    assert resumed.revision != paused.revision
    assert resumed.input_queue_revision != paused.input_queue_revision


def test_exact_input_retry_does_not_change_queue_activity_revision(runtime, monkeypatch):
    session_id = sid(runtime)
    clock = [10.0]
    monkeypatch.setattr(sessions_module, 'monotonic', lambda: clock[0])
    first = submit(runtime, session_id, 'stable-revision')
    original = runtime.session_service.get_session_state(session_id=session_id)
    again = submit(runtime, session_id, 'stable-revision')
    clock[0] += 1.1
    unchanged = runtime.session_service.get_session_state(session_id=session_id)
    assert first == again
    assert unchanged.revision == original.revision
    assert unchanged.input_queue_revision == original.input_queue_revision
    assert set(json.loads(unchanged.model_dump_json())) == {
        'session_id', 'status', 'updated_at', 'latest_root_run_id',
        'latest_root_run_status', 'latest_root_run_updated_at',
        'input_queue_revision', 'revision',
    }
