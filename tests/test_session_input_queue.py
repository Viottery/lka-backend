"""Offline FIFO, recovery, admission, and local HTTP contracts for user inputs."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.session_inputs import router
from app.core.agent_runs import AgentRunStatus
from app.core.config import Settings
from app.core.llm import LLMResponseMode
from app.core.llm_workloads import current_workload
from app.core.local_config import LocalAppConfig
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.safety import SafetyReviewMode
from app.core.session_admission import SessionBusyError
from app.core.session_inputs import SessionInputConflict
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = LocalAppConfig()
    config.management.enabled = True
    config.agent.unified_entry_enabled = True
    config.agent.checkpoint_backend = 'sqlite'
    config.memory.enabled = False
    config.message_history.enabled = False
    monkeypatch.setattr(Settings, 'load_local_config', lambda _: config)
    instance = LocalKnowledgeAgentRuntime(Settings(LKA_DATA_DIR=tmp_path / 'data', LKA_WORKSPACE_ROOTS=str(tmp_path)))
    yield instance
    instance.stop()


def sid(runtime):
    return runtime.management_service.get_default()['session']['session_id']


def submit(runtime, session_id, key, text=None, **kwargs):
    return runtime.session_inputs.submit(session_id, client_message_id=key,
                                         user_input=text or key, attachment_ids=kwargs.pop('attachment_ids', []), **kwargs)


def input_run(runtime, session_id):
    snapshot = runtime.session_inputs.snapshot(session_id)
    return runtime.agent_run_manager.get_run(snapshot['active_run_id'])


def complete(runtime, run):
    runtime.agent_run_manager.mark_running(run.run_id)
    runtime.agent_run_manager.complete_run(run.run_id)


def test_acceptance_is_immediate_history_once_without_running_models(runtime):
    session_id = sid(runtime)
    client = ScriptedClient([])
    runtime.agent_turn_loop.llm_client = client
    a = submit(runtime, session_id, 'a')
    b = submit(runtime, session_id, 'b')
    assert [a['input']['sequence'], b['input']['sequence']] == [1, 2]
    assert a['input']['status'] == 'queued' and a['input']['run_id'] is None
    assert submit(runtime, session_id, 'a')['input'] == a['input']
    assert runtime.session_inputs.snapshot(session_id)['pending_count'] == 2
    assert client.calls == []
    messages = runtime.get_session(session_id=session_id).messages
    assert [m.content for m in messages] == ['a', 'b']
    assert [m.payload['client_message_id'] for m in messages] == ['a', 'b']
    with pytest.raises(SessionInputConflict, match='conflict'):
        submit(runtime, session_id, 'a', 'changed')
    assert len(runtime.get_session(session_id=session_id).messages) == 2


def test_legacy_retry_ignores_null_reasoning_field(runtime):
    session_id = sid(runtime)
    legacy = submit(runtime, session_id, 'legacy')
    replay = submit(runtime, session_id, 'legacy', llm={'response_mode': 'text', 'reasoning_level': None})
    assert replay['input'] == legacy['input']


def test_queue_batches_require_same_reasoning_level(runtime):
    session_id = sid(runtime)
    low = submit(runtime, session_id, 'low', llm={'response_mode': 'text', 'reasoning_level': 'low'})
    high = submit(runtime, session_id, 'high', llm={'response_mode': 'text', 'reasoning_level': 'high'})
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    run = runtime.session_inputs.freeze_for_dispatch(run, SimpleNamespace(check=lambda **_kwargs: None))
    batch = run.metadata['session_input_batch']
    assert [item['input_id'] for item in batch] == [low['input']['input_id']]
    assert low['input']['run_id'] is None and high['input']['run_id'] is None


def test_fifo_uses_root_admission_without_mid_run_injection(runtime):
    session_id = sid(runtime)
    first = runtime.create_agent_run(session_id=session_id, user_input='already running', resumable=True)
    runtime.agent_run_manager.mark_running(first.run_id)
    a, b = [submit(runtime, session_id, key) for key in ['a', 'b']]
    runtime.session_inputs.tick()
    assert input_run(runtime, session_id).run_id == first.run_id
    assert all(item['run_id'] is None for item in runtime.session_inputs.snapshot(session_id)['inputs'])
    complete(runtime, first)
    runtime.session_inputs.tick()
    next_run = input_run(runtime, session_id)
    assert next_run.user_input == 'a' and next_run.parent_run_id is None
    with pytest.raises(SessionBusyError):
        runtime.create_agent_run(session_id=session_id, user_input='ordinary admission unchanged')
    assert runtime.session_inputs.snapshot(session_id)['inputs'][1]['run_id'] is None
    complete(runtime, next_run)
    runtime.session_inputs.tick()
    assert input_run(runtime, session_id).user_input == 'b'
    assert a['input']['message_id'] != b['input']['message_id']


@pytest.mark.parametrize('status', ['paused', 'waiting_user', 'waiting_confirmation'])
def test_nonterminal_root_blocks_queue_even_after_queue_resume(runtime, status):
    session_id = sid(runtime)
    root = runtime.create_agent_run(session_id=session_id, user_input='blocker', resumable=True)
    runtime.agent_run_manager._update_run(root.run_id, status=AgentRunStatus(status))
    submit(runtime, session_id, 'later')
    runtime.session_inputs.resume(session_id)
    runtime.session_inputs.tick()
    assert runtime.session_inputs.snapshot(session_id)['inputs'][0]['run_id'] is None
    assert input_run(runtime, session_id).run_id == root.run_id


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'timed_out'])
@pytest.mark.parametrize('external', [False, True])
def test_failed_or_cancelled_root_pauses_without_retry_until_explicit_resume(runtime, status, external):
    session_id = sid(runtime)
    if external:
        run = runtime.create_agent_run(session_id=session_id, user_input='external', resumable=True)
    submit(runtime, session_id, 'a')
    submit(runtime, session_id, 'b')
    runtime.session_inputs.tick()
    if not external:
        run = input_run(runtime, session_id)
    runtime.agent_run_manager._update_run(run.run_id, status=AgentRunStatus(status))
    assert runtime.session_inputs.snapshot(session_id)['paused'] is True
    runtime.session_inputs.tick()
    runtime.session_inputs.tick()
    snap = runtime.session_inputs.snapshot(session_id)
    assert snap['paused'] and snap['active_run_id'] is None
    assert snap['inputs'][-1]['run_id'] is None
    runtime.session_inputs.resume(session_id)
    runtime.session_inputs.tick()
    next_run = input_run(runtime, session_id)
    assert next_run.run_id != run.run_id
    assert next_run.user_input == ('a' if external else 'b')
    assert runtime.agent_run_manager.get_run(run.run_id).status.value == status


def test_concurrent_acceptance_deduplicates_and_bind_has_one_root(runtime):
    session_id = sid(runtime)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: submit(runtime, session_id, 'same'), range(8)))
    assert len({item['input']['input_id'] for item in receipts}) == 1
    submit(runtime, session_id, 'second')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: runtime.session_inputs.tick(), range(8)))
    with closing(runtime._conn()) as conn:
        assert conn.execute('SELECT count(*) FROM agent_runs WHERE session_id=?', (session_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch'").fetchone()[0] == 1
    assert len(runtime.get_session(session_id=session_id).messages) == 2


def test_enqueue_failure_rolls_back_run_binding_and_outbox(runtime, monkeypatch):
    session_id = sid(runtime)
    submit(runtime, session_id, 'atomic')
    original = runtime.agent_dispatch.enqueue

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('injected rollback')

    monkeypatch.setattr(runtime.agent_dispatch, 'enqueue', fail)
    with pytest.raises(RuntimeError, match='rollback'):
        runtime.session_inputs.tick()
    assert runtime.session_inputs.snapshot(session_id)['inputs'][0]['run_id'] is None
    with closing(runtime._conn()) as conn:
        assert conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch'").fetchone()[0] == 0
    assert len(runtime.get_session(session_id=session_id).messages) == 1


def test_reopen_preserves_fifo_and_binding_without_recreating_runs(runtime):
    session_id = sid(runtime)
    first = submit(runtime, session_id, 'first')
    submit(runtime, session_id, 'second')
    runtime.session_inputs.tick()
    run_id = input_run(runtime, session_id).run_id
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        assert submit(reopened, session_id, 'first')['input']['message_id'] == first['input']['message_id']
        reopened.session_inputs.tick()
        assert input_run(reopened, session_id).run_id == run_id
        complete(reopened, input_run(reopened, session_id))
        reopened.session_inputs.tick()
        assert input_run(reopened, session_id).user_input == 'second'
        assert len(reopened.get_session(session_id=session_id).messages) == 2
    finally:
        reopened.stop()


@pytest.mark.parametrize('checkpoint', [False, True])
def test_recovery_of_running_input_uses_existing_dispatcher_checkpoint_policy(runtime, monkeypatch, checkpoint):
    session_id = sid(runtime)
    submit(runtime, session_id, 'first')
    submit(runtime, session_id, 'second')
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    runtime.agent_run_manager.mark_running(run.run_id)
    calls = []
    monkeypatch.setattr(runtime.agent_dispatch, 'has_checkpoint', lambda _: checkpoint)
    monkeypatch.setattr(runtime.agent_dispatch, 'execute', lambda *args: pytest.fail('unsafe replay'))

    def resume(record, guard):
        guard.check()
        calls.append(record.run_id)
        complete(runtime, record)

    monkeypatch.setattr(runtime.agent_dispatch, 'resume', resume)
    assert runtime.agent_dispatch.run_one()
    runtime.session_inputs.tick()
    if checkpoint:
        assert calls == [run.run_id]
        assert input_run(runtime, session_id).user_input == 'second'
    else:
        assert calls == []
        assert runtime.agent_run_manager.get_run(run.run_id).error_type == 'execution_uncertain'
        assert runtime.session_inputs.snapshot(session_id)['paused']


def test_bound_user_options_and_foreground_workload(runtime, monkeypatch):
    session_id = sid(runtime)
    monkeypatch.setattr(runtime.session_inputs, 'validate_attachments', lambda **kw: kw['attachment_ids'])
    receipt = submit(runtime, session_id, 'opts', attachment_ids=['file-a'], safety_review_mode='manual', llm={'response_mode': 'stream'})
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    seen = []

    def execute(**kwargs):
        seen.append((kwargs, current_workload().pool))
        complete(runtime, run)

    monkeypatch.setattr(runtime, 'run_agent_turn', execute)
    assert runtime.agent_dispatch.run_one()
    kwargs, pool = seen[0]
    assert kwargs['attachment_ids'] == ['file-a']
    assert kwargs['safety_review_mode'] == SafetyReviewMode.MANUAL
    assert kwargs['llm_response_mode'] == LLMResponseMode.STREAM
    assert pool == 'interactive'
    assert run.metadata['orchestrator'] == 'langgraph'
    assert run.metadata['session_input_message_id'] == receipt['input']['message_id']


def test_revoked_attachment_preflight_fails_and_pauses_without_stranding_root(runtime, monkeypatch):
    session_id = sid(runtime)
    monkeypatch.setattr(runtime.session_inputs, 'validate_attachments', lambda **kw: kw['attachment_ids'])
    submit(runtime, session_id, 'file', attachment_ids=['removed-file'])
    submit(runtime, session_id, 'later')
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(run.run_id).status.value == 'failed'
    runtime.session_inputs.tick()
    snap = runtime.session_inputs.snapshot(session_id)
    assert snap['paused'] and snap['active_run_id'] is None
    assert snap['inputs'][-1]['run_id'] is None


def test_real_graph_reuses_accepted_history_and_retains_trace_lookup(runtime):
    session_id = sid(runtime)
    first = submit(runtime, session_id, 'one', 'First question')
    client = ScriptedClient([operation('final_answer', reason='Answer first'), 'First answer',
                             operation('final_answer', reason='Answer second'), 'Second answer'])
    runtime.agent_turn_loop.llm_client = client
    runtime.session_inputs.tick()
    first_run = input_run(runtime, session_id)
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(first_run.run_id).status.value == 'completed'
    message = runtime.session_service.get_turn_user_message(session_id=session_id, trace_id=first_run.trace_id)
    assert message.message_id == first['input']['message_id']
    window = runtime.session_service.get_prompt_context_window(session_id=session_id)
    assert [m.content for m in window.recent_messages] == ['First question', 'First answer']
    submit(runtime, session_id, 'two', 'Second question')
    runtime.session_inputs.tick()
    second_run = input_run(runtime, session_id)
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(second_run.run_id).status.value == 'completed'
    messages = runtime.get_session(session_id=session_id).messages
    assert [m.content for m in messages if m.role == 'user'] == ['First question', 'Second question']
    assert [m.content for m in messages if m.role == 'agent'] == ['First answer', 'Second answer']
    assert all(item['status'] == 'completed' for item in runtime.session_inputs.snapshot(session_id)['inputs'])


def test_default_switch_preserves_accepted_original_queue_and_disallows_new_input(runtime):
    original = sid(runtime)
    receipt = submit(runtime, original, 'old')
    replacement = runtime.management_service.create(make_default=True)['session']['session_id']
    with pytest.raises(SessionInputConflict):
        submit(runtime, original, 'new')
    assert submit(runtime, original, 'old')['input'] == receipt['input']
    runtime.session_inputs.tick()
    assert input_run(runtime, original).session_id == original
    assert runtime.session_inputs.snapshot(replacement)['inputs'] == []


def test_deletion_permanently_cancels_pending_input_on_restore(runtime):
    session_id = sid(runtime)
    submit(runtime, session_id, 'pending')
    assert runtime.delete_session(session_id=session_id)
    runtime.session_inputs.tick()
    with pytest.raises(KeyError):
        runtime.session_inputs.snapshot(session_id)
    assert runtime.session_service.restore_session(session_id=session_id)
    runtime.session_inputs.resume(session_id)
    runtime.session_inputs.tick()
    snap = runtime.session_inputs.snapshot(session_id)
    assert snap['inputs'][0]['status'] == 'cancelled' and snap['active_run_id'] is None


def test_user_queue_has_priority_over_new_autonomous_wake(runtime):
    session_id = sid(runtime)
    config = runtime.local_app_config.management
    config.autonomous_enabled = True
    config.min_wake_interval_seconds = 0
    runtime.management_service.update(session_id, expected_revision=1, status='active')
    runtime.management_goals.create_goal(session_id, objective='Check', next_wake_at=datetime.now(UTC).isoformat())
    submit(runtime, session_id, 'user first')
    runtime.management_coordinator.tick()
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='manager_wake'").fetchone()[0] == 0
    runtime.session_inputs.tick()
    assert input_run(runtime, session_id).user_input == 'user first'


def test_capacity_and_exact_retry_at_capacity(runtime):
    session_id = sid(runtime)
    runtime.session_inputs.capacity = 2
    submit(runtime, session_id, 'a')
    submit(runtime, session_id, 'b')
    with pytest.raises(SessionInputConflict, match='full'):
        submit(runtime, session_id, 'c')
    assert submit(runtime, session_id, 'a')['input']['sequence'] == 1
    assert len(runtime.get_session(session_id=session_id).messages) == 2


def test_http_contract_and_server_owned_scope(runtime, monkeypatch):
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(router)
    session_id = sid(runtime)
    ordinary = runtime.session_service.create_session(metadata={'kind': 'manager'}).session.session_id
    nondefault = runtime.management_service.create()['session']['session_id']

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app, client=('127.0.0.1', 9999)), base_url='http://test') as client:
            path = f'/sessions/{session_id}/inputs'
            body = {'client_message_id': 'http', 'user_input': 'Hello', 'attachment_ids': []}
            response = await client.post(path, json=body)
            assert response.status_code == 202
            assert set(response.json()) == {'input', 'queue'}
            assert response.json()['input']['status'] == 'queued'
            assert (await client.get(path)).json()['pending_count'] == 1
            assert (await client.post(path + '/resume')).status_code == 200
            assert (await client.post(path, json=body | {'user_input': 'other'})).status_code == 409
            assert (await client.post(path, json=body | {'attachment_ids': None})).status_code == 422
            assert (await client.post(path, json=body | {'llm': {'client_name': 'invented'}})).status_code == 422
            for excluded in [ordinary, nondefault]:
                assert (await client.post(f'/sessions/{excluded}/inputs', json=body)).status_code == 409
            assert (await client.get('/sessions/missing/inputs')).status_code == 404
            runtime.local_app_config.management.enabled = False
            assert (await client.post(path, json=body)).status_code == 409
            runtime.local_app_config.management.enabled = True
            runtime.local_app_config.agent.checkpoint_backend = 'memory'
            assert (await client.post(path, json=body)).status_code == 409
            runtime.local_app_config.agent.checkpoint_backend = 'sqlite'
            monkeypatch.setenv('LKA_MEMORY_API_TOKEN', 'test-token')
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers={'Authorization': 'Bearer test-token'})).status_code == 200

    asyncio.run(exercise())


def test_new_active_blocker_replaces_watch_from_previously_drained_queue(runtime):
    session_id = sid(runtime)
    submit(runtime, session_id, 'first')
    runtime.session_inputs.tick()
    complete(runtime, input_run(runtime, session_id))
    blocker = runtime.create_agent_run(session_id=session_id, user_input='new blocker', resumable=True)
    submit(runtime, session_id, 'later')
    runtime.agent_run_manager.mark_cancelled(blocker.run_id)
    runtime.session_inputs.tick()
    snap = runtime.session_inputs.snapshot(session_id)
    assert snap['paused'] and snap['active_run_id'] is None
    assert snap['inputs'][-1]['run_id'] is None


def test_new_submission_cannot_erase_failure_pause_before_scheduler_observes_it(runtime):
    session_id = sid(runtime)
    submit(runtime, session_id, 'first')
    runtime.session_inputs.tick()
    failed = input_run(runtime, session_id)
    runtime.agent_run_manager.fail_run(failed.run_id, error_type='offline', error='offline failure')
    blocker = runtime.create_agent_run(session_id=session_id, user_input='other root', resumable=True)
    assert submit(runtime, session_id, 'later')['queue']['paused']
    complete(runtime, blocker)
    runtime.session_inputs.tick()
    assert runtime.session_inputs.snapshot(session_id)['paused']
    assert runtime.session_inputs.snapshot(session_id)['inputs'][-1]['run_id'] is None


def test_concurrent_distinct_inputs_have_persisted_fifo_attachment_bindings(runtime, monkeypatch):
    session_id = sid(runtime)
    monkeypatch.setattr(runtime.session_inputs, 'validate_attachments', lambda **kw: kw['attachment_ids'])
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda key: submit(runtime, session_id, key, attachment_ids=[key + '-file']), ['a', 'b', 'c', 'd']))
    assert sorted(r['input']['sequence'] for r in receipts) == [1, 2, 3, 4]
    ordered = sorted(receipts, key=lambda r: r['input']['sequence'])
    for receipt in ordered:
        runtime.session_inputs.tick()
        run = input_run(runtime, session_id)
        key = receipt['input']['client_message_id']
        assert run.user_input == key
        assert run.metadata['session_input_options']['attachment_ids'] == [key + '-file']
        complete(runtime, run)


def test_scheduler_drains_without_frontend_polling_or_status_requests(runtime, monkeypatch):
    from threading import Event

    session_id = sid(runtime)
    submit(runtime, session_id, 'a')
    submit(runtime, session_id, 'b')
    drained = Event()
    calls = []

    def execute(run, guard):
        guard.check()
        calls.append(run.user_input)
        complete(runtime, run)
        if len(calls) == 2:
            drained.set()

    monkeypatch.setattr(runtime.agent_dispatch, 'execute', execute)
    runtime.agent_dispatch.worker.poll_seconds = 0.01
    runtime.session_inputs.poll_seconds = 0.01
    runtime.session_inputs.start()
    runtime.agent_dispatch.start()
    assert drained.wait(5)
    assert calls == ['a', 'b']
    assert runtime.session_inputs.snapshot(session_id)['pending_count'] == 0


def test_delegate_remains_excluded_even_if_default_slot_is_stale(runtime):
    from tests.test_management_tasks import manager_context as context_fixture

    context = context_fixture.__wrapped__(runtime)
    delegate = runtime.management_delegates.delegate_work(context, command_id='delegate', objective='Inspect')
    delegate_sid = delegate['session_id']
    with closing(runtime._conn()) as conn, conn:
        conn.execute('INSERT INTO manager_defaults(slot,session_id) VALUES(1,?) '
                     'ON CONFLICT(slot) DO UPDATE SET session_id=excluded.session_id', (delegate_sid,))
    for call in [lambda: submit(runtime, delegate_sid, 'no'),
                 lambda: runtime.session_inputs.snapshot(delegate_sid),
                 lambda: runtime.session_inputs.resume(delegate_sid)]:
        with pytest.raises(SessionInputConflict, match='default_manager'):
            call()


def pause(runtime, run):
    runtime.agent_run_manager.mark_running(run.run_id)
    runtime.agent_run_manager.request_pause(run.run_id)
    assert runtime.agent_run_manager.acknowledge_pause(run.run_id).status == AgentRunStatus.PAUSED


@pytest.mark.parametrize('queued_source', [False, True])
def test_explicit_replacement_binds_receipt_before_preserved_fifo(runtime, queued_source):
    session_id = sid(runtime)
    if queued_source:
        submit(runtime, session_id, 'old')
        runtime.session_inputs.tick()
        old = input_run(runtime, session_id)
    else:
        old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    pause(runtime, old)
    later = [submit(runtime, session_id, key)['input'] for key in ['later-a', 'later-b']]
    # An earlier queue pause is cleared only by this explicit replacement.
    with closing(runtime._conn()) as conn, conn:
        conn.execute('UPDATE session_input_queues SET paused=1 WHERE session_id=?', (session_id,))
    receipt = submit(runtime, session_id, 'replacement', replace_run_id=old.run_id)
    new = input_run(runtime, session_id)
    assert receipt['input']['run_id'] == new.run_id != old.run_id
    assert receipt['queue']['paused'] is False
    assert new.metadata['replaces_run_id'] == old.run_id
    assert new.metadata['session_input_id'] == receipt['input']['input_id']
    assert new.metadata['session_input_message_id'] == receipt['input']['message_id']
    superseded = runtime.agent_run_manager.get_run(old.run_id)
    assert superseded.status == AgentRunStatus.CANCELLED
    assert superseded.metadata['cancel_reason'] == 'superseded'
    for waiting in later:
        item = next(i for i in runtime.session_inputs.snapshot(session_id)['inputs'] if i['input_id'] == waiting['input_id'])
        assert item == waiting
    runtime.session_inputs.tick()
    assert input_run(runtime, session_id).run_id == new.run_id
    for key in ['later-a', 'later-b']:
        complete(runtime, input_run(runtime, session_id))
        runtime.session_inputs.tick()
        assert input_run(runtime, session_id).user_input == key
        assert not runtime.session_inputs.snapshot(session_id)['paused']
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch' AND "
                            "json_extract(payload_json,'$.run_id')=?", (new.run_id,)).fetchone()[0] == 1


def test_replacement_dispatch_reuses_accepted_history_and_options(runtime, monkeypatch):
    session_id = sid(runtime)
    old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    pause(runtime, old)
    receipt = submit(runtime, session_id, 'replacement', 'New question', replace_run_id=old.run_id,
                     safety_review_mode='manual', llm={'response_mode': 'text'})
    new = input_run(runtime, session_id)
    assert new.metadata['session_input_options']['safety_review_mode'] == 'manual'
    assert new.metadata['session_input_options']['llm'] == {'response_mode': 'text'}
    client = ScriptedClient([operation('final_answer', reason='Answer'), 'New answer'])
    runtime.agent_turn_loop.llm_client = client
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(new.run_id).status == AgentRunStatus.COMPLETED
    message = runtime.session_service.get_turn_user_message(session_id=session_id, trace_id=new.trace_id)
    assert message.message_id == receipt['input']['message_id']
    messages = runtime.get_session(session_id=session_id).messages
    assert [m.content for m in messages if m.role == 'user'] == ['New question']
    assert [m.content for m in messages if m.role == 'agent'] == ['New answer']
    assert client.calls
    assert 'replacement_handoff' in client.calls[0]['user_prompt']


def test_replacement_full_request_retry_survives_completion_default_switch_and_restart(runtime, monkeypatch):
    session_id = sid(runtime)
    old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    pause(runtime, old)
    options = {'replace_run_id': old.run_id, 'attachment_ids': ['file-a'],
               'safety_review_mode': 'manual', 'llm': {'response_mode': 'stream'}}
    monkeypatch.setattr(runtime.session_inputs, 'validate_attachments', lambda **kw: kw['attachment_ids'])
    receipt = submit(runtime, session_id, 'replacement', **options)
    new = input_run(runtime, session_id)
    complete(runtime, new)
    runtime.management_service.create(make_default=True)
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        monkeypatch.setattr(reopened.session_inputs, 'validate_attachments', lambda **kw: pytest.fail('retry revalidation'))
        retry = submit(reopened, session_id, 'replacement', **options)
        assert retry['input'] == {**receipt['input'], 'status': 'completed'}
        for changed in [options | {'replace_run_id': new.run_id}, options | {'attachment_ids': []},
                        options | {'llm': {'response_mode': 'text'}}]:
            with pytest.raises(SessionInputConflict, match='client_message_id_conflict'):
                submit(reopened, session_id, 'replacement', **changed)
        assert len(reopened.get_session(session_id=session_id).messages) == 1
        with closing(reopened._conn()) as conn:
            assert conn.execute('SELECT count(*) FROM agent_runs WHERE session_id=?', (session_id,)).fetchone()[0] == 2
    finally:
        reopened.stop()


@pytest.mark.parametrize('status', ['queued', 'running', 'waiting_user', 'waiting_confirmation', 'completed', 'cancelled'])
def test_nonpaused_replacement_rejected_without_ghost_input(runtime, status):
    session_id = sid(runtime)
    old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    runtime.agent_run_manager._update_run(old.run_id, status=AgentRunStatus(status))
    with pytest.raises(SessionInputConflict):
        submit(runtime, session_id, 'replacement', replace_run_id=old.run_id)
    assert runtime.session_inputs.snapshot(session_id)['inputs'] == []
    assert runtime.get_session(session_id=session_id).messages == []
    assert runtime.agent_run_manager.get_run(old.run_id).status.value == status
    with closing(runtime._conn()) as conn:
        assert conn.execute('SELECT count(*) FROM session_input_queues').fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch'").fetchone()[0] == 0


def test_replacement_rejects_cross_session_and_child_before_callback(runtime, monkeypatch):
    session_id = sid(runtime)
    other = runtime.session_service.create_session().session.session_id
    root = runtime.create_agent_run(session_id=other, user_input='private', resumable=True)
    child = runtime.agent_run_manager.create_child_run(parent_run_id=root.run_id, plan_id='plan',
                                                      step_id='child', attempt=1, user_input='child', executor_kind='react')
    pause(runtime, child)
    pause(runtime, root)
    own_root = runtime.create_agent_run(session_id=session_id, user_input='own', resumable=True)
    own_child = runtime.agent_run_manager.create_child_run(parent_run_id=own_root.run_id, plan_id='plan',
                                                          step_id='child', attempt=1, user_input='child', executor_kind='react')
    monkeypatch.setattr(runtime.session_inputs, 'replace', lambda *a, **kw: pytest.fail('unauthorized replacement'))
    for run_id in [root.run_id, child.run_id, own_child.run_id, 'missing']:
        with pytest.raises(SessionInputConflict, match='same_session_root'):
            submit(runtime, session_id, 'replacement', replace_run_id=run_id)
    assert runtime.agent_run_manager.get_run(root.run_id).status == AgentRunStatus.PAUSED
    assert runtime.session_inputs.snapshot(session_id)['inputs'] == []


def test_replacement_transaction_failure_rolls_back_history_binding_watch_and_dispatch(runtime, monkeypatch):
    session_id = sid(runtime)
    submit(runtime, session_id, 'old')
    runtime.session_inputs.tick()
    old = input_run(runtime, session_id)
    pause(runtime, old)
    submit(runtime, session_id, 'later')
    before = runtime.session_inputs.snapshot(session_id)
    enqueue = runtime.agent_dispatch.enqueue

    def fail(*args, **kwargs):
        enqueue(*args, **kwargs)
        raise RuntimeError('replacement transaction rollback')

    monkeypatch.setattr(runtime.agent_dispatch, 'enqueue', fail)
    with pytest.raises(RuntimeError, match='rollback'):
        submit(runtime, session_id, 'replacement', replace_run_id=old.run_id)
    assert runtime.session_inputs.snapshot(session_id) == before
    assert runtime.agent_run_manager.get_run(old.run_id).status == AgentRunStatus.PAUSED
    assert not runtime.agent_run_manager.is_cancel_requested(old.run_id)
    assert [m.content for m in runtime.get_session(session_id=session_id).messages] == ['old', 'later']
    with closing(runtime._conn()) as conn:
        assert conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch'").fetchone()[0] == 1
        assert conn.execute('SELECT watched_run_id FROM session_input_queues').fetchone()[0] == old.run_id


@pytest.mark.parametrize('same_key', [False, True])
def test_concurrent_replacements_have_one_run_and_one_history_message(runtime, same_key):
    session_id = sid(runtime)
    old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    pause(runtime, old)

    def replace(index):
        try:
            return submit(runtime, session_id, 'same' if same_key else f'key-{index}', replace_run_id=old.run_id)
        except SessionInputConflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(replace, range(4)))
    accepted = [r for r in receipts if r is not None]
    assert len(accepted) == (4 if same_key else 1)
    assert len({r['input']['run_id'] for r in accepted}) == 1
    assert len(runtime.get_session(session_id=session_id).messages) == 1
    with closing(runtime._conn()) as conn:
        assert conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM session_inputs').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch'").fetchone()[0] == 1


def test_replacement_respects_execution_lease_and_retains_paused_children(runtime):
    session_id = sid(runtime)
    old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    child = runtime.agent_run_manager.create_child_run(parent_run_id=old.run_id, plan_id='plan',
                                                      step_id='child', attempt=1, user_input='child', executor_kind='react')
    pause(runtime, child)
    pause(runtime, old)
    lease = runtime._runner_for_turn(existing_run_id=old.run_id)._execution_lease(old.run_id)
    with lease, pytest.raises(SessionInputConflict, match='still executing'):
        submit(runtime, session_id, 'replacement', replace_run_id=old.run_id)
    assert runtime.session_inputs.snapshot(session_id)['inputs'] == []
    receipt = submit(runtime, session_id, 'replacement', replace_run_id=old.run_id)
    retired = runtime.agent_run_manager.get_run(child.run_id)
    assert retired.status == AgentRunStatus.CANCELLED
    assert retired.metadata['superseded_by_run_id'] == receipt['input']['run_id']
    assert runtime.agent_run_manager.get_run(old.run_id).child_run_ids == (child.run_id,)


def test_replacement_of_freshly_loaded_paused_root_without_queue_state(runtime):
    session_id = sid(runtime)
    old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    pause(runtime, old)
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        receipt = submit(reopened, session_id, 'replacement', replace_run_id=old.run_id)
        assert receipt['input']['run_id'] == input_run(reopened, session_id).run_id
        assert receipt['queue']['paused'] is False
        assert len(reopened.get_session(session_id=session_id).messages) == 1
    finally:
        reopened.stop()


def test_replacement_keeps_safety_budgets_and_dispatches_its_own_attachment_options(runtime, monkeypatch):
    session_id = sid(runtime)
    old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    budget = [{'scope_id': 'existing-cap', 'max_runs': 3}]
    policy = {**old.metadata['session_policy'], 'require_high_risk_approval': True}
    runtime.agent_run_manager._update_run(old.run_id, status=old.status,
                                         metadata_patch={'session_policy': policy, 'work_budget_scopes': budget})
    pause(runtime, old)
    monkeypatch.setattr(runtime.session_inputs, 'validate_attachments', lambda **kw: kw['attachment_ids'])
    submit(runtime, session_id, 'replacement', replace_run_id=old.run_id, attachment_ids=['new-file'],
           safety_review_mode='manual', llm={'response_mode': 'stream'})
    new = input_run(runtime, session_id)
    assert new.metadata['session_policy']['require_high_risk_approval'] is True
    assert new.metadata['work_budget_scopes'] == budget
    seen = []

    def execute(**kwargs):
        seen.append(kwargs)
        complete(runtime, new)

    monkeypatch.setattr(runtime, 'run_agent_turn', execute)
    assert runtime.agent_dispatch.run_one()
    assert seen[0]['attachment_ids'] == ['new-file']
    assert seen[0]['safety_review_mode'] == SafetyReviewMode.MANUAL
    assert seen[0]['llm_response_mode'] == LLMResponseMode.STREAM


def test_http_replacement_validation_conflicts_and_receipt(runtime):
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(router)
    session_id = sid(runtime)
    old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    pause(runtime, old)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app, client=('127.0.0.1', 9999)), base_url='http://test') as client:
            path = f'/sessions/{session_id}/inputs'
            body = {'client_message_id': 'replacement', 'user_input': 'New question',
                    'attachment_ids': [], 'replace_run_id': old.run_id}
            for invalid in ['', '   ', 'x' * 129]:
                response = await client.post(path, json=body | {'replace_run_id': invalid})
                assert response.status_code == 422
            assert runtime.session_inputs.snapshot(session_id)['inputs'] == []
            response = await client.post(path, json=body)
            assert response.status_code == 202
            receipt = response.json()
            assert receipt['input']['run_id'] and receipt['input']['status'] == 'queued'
            assert set(receipt) == {'input', 'queue'}
            assert (await client.post(path, json=body)).json() == receipt
            assert (await client.post(path, json=body | {'replace_run_id': 'other'})).status_code == 409
            assert (await client.post(path, json=body | {'client_message_id': 'stale'})).status_code == 409
            assert len(runtime.session_inputs.snapshot(session_id)['inputs']) == 1

    asyncio.run(exercise())


def test_session_input_replacement_uses_runtime_replacement_guard(runtime, monkeypatch):
    session_id = sid(runtime)
    old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    pause(runtime, old)

    def reject(run_id):
        assert run_id == old.run_id
        raise ValueError('custom replacement guard')

    monkeypatch.setattr(runtime.management_queue, 'guard_replacement', reject)
    with pytest.raises(SessionInputConflict, match='custom replacement guard'):
        submit(runtime, session_id, 'replacement', replace_run_id=old.run_id)
    assert runtime.agent_run_manager.get_run(old.run_id).status == AgentRunStatus.PAUSED
    assert runtime.session_inputs.snapshot(session_id)['inputs'] == []
    assert runtime.get_session(session_id=session_id).messages == []
