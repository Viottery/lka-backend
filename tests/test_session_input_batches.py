"""Offline batching at the execution boundary, with independent user receipts."""
import sqlite3
from contextlib import closing

import pytest

from app.core.agent_runs import AgentRunStatus
from app.core.runtime import LocalKnowledgeAgentRuntime
from tests.test_session_input_queue import complete, input_run, pause, sid, submit
from tests.test_session_input_queue import runtime as queue_runtime
from tests.test_unified_entry_quality import ScriptedClient, operation

runtime = queue_runtime

def record_execution(runtime, monkeypatch, *, on_execute=None):
    calls = []

    def execute(**kwargs):
        run = runtime.agent_run_manager.get_run(kwargs['existing_run_id'])
        calls.append((kwargs, run))
        if on_execute:
            on_execute(run)
        complete(runtime, run)

    monkeypatch.setattr(runtime, 'run_agent_turn', execute)
    return calls


def test_first_dispatch_freezes_all_received_messages_and_late_input_waits(runtime, monkeypatch):
    session_id = sid(runtime)
    first = submit(runtime, session_id, 'a', 'First question')['input']
    runtime.session_inputs.tick()
    second = submit(runtime, session_id, 'b', 'Second question')['input']
    late = []
    calls = record_execution(runtime, monkeypatch, on_execute=lambda _: late.append(submit(runtime, session_id, 'late')['input']))
    assert runtime.agent_dispatch.run_one()
    kwargs, run = calls[0]
    assert kwargs['user_input'] == '[User message 1]\nFirst question\n\n[User message 2]\nSecond question'
    assert [item['message_id'] for item in run.metadata['session_input_batch']] == [first['message_id'], second['message_id']]
    assert all('user_input' not in item for item in run.metadata['session_input_batch'])
    assert 'user_input' not in run.metadata['session_input_options']
    snap = runtime.session_inputs.snapshot(session_id)
    assert [item['run_id'] for item in snap['inputs']] == [run.run_id, run.run_id, None]
    assert [item['status'] for item in snap['inputs']] == ['completed', 'completed', 'queued']
    for key, original in [('a', first), ('b', second)]:
        retry = submit(runtime, session_id, key, 'First question' if key == 'a' else 'Second question')['input']
        assert retry['input_id'] == original['input_id'] and retry['run_id'] == run.run_id
    runtime.session_inputs.tick()
    assert input_run(runtime, session_id).user_input == 'late'
    assert late[0]['message_id'] != first['message_id']


def test_real_graph_answers_one_batch_once_without_duplicate_or_synthetic_history(runtime):
    session_id = sid(runtime)
    receipts = [submit(runtime, session_id, key, text)['input'] for key, text in [('a', 'First question'), ('b', 'Second question')]]
    client = ScriptedClient([operation('final_answer', reason='Answer both'), 'Combined answer'])
    runtime.agent_turn_loop.llm_client = client
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    assert runtime.agent_dispatch.run_one()
    latest = runtime.agent_run_manager.get_run(run.run_id)
    assert latest.status == AgentRunStatus.COMPLETED
    messages = runtime.get_session(session_id=session_id).messages
    assert [message.content for message in messages] == ['First question', 'Second question', 'Combined answer']
    users = runtime.session_service.get_turn_user_messages(session_id=session_id, trace_id=run.trace_id)
    assert [message.message_id for message in users] == [item['message_id'] for item in receipts]
    window = runtime.session_service.get_prompt_context_window(session_id=session_id)
    assert [message.content for message in window.recent_messages] == [latest.user_input, 'Combined answer']
    assert len(client.calls) == 2
    assert all(text in client.calls[0]['user_prompt'] for text in ['First question', 'Second question'])


@pytest.mark.parametrize('different', [{'safety_review_mode': 'manual'}, {'llm': {'response_mode': 'stream'}}])
def test_incompatible_options_split_a_contiguous_prefix_without_skipping(runtime, monkeypatch, different):
    session_id = sid(runtime)
    submit(runtime, session_id, 'a')
    submit(runtime, session_id, 'b', **different)
    submit(runtime, session_id, 'c')
    calls = record_execution(runtime, monkeypatch)
    for expected in ['a', 'b', 'c']:
        runtime.session_inputs.tick()
        assert runtime.agent_dispatch.run_one()
        kwargs, run = calls[-1]
        assert kwargs['user_input'] == expected
        assert len(run.metadata['session_input_batch']) == 1
    if 'safety_review_mode' in different:
        assert calls[1][0]['safety_review_mode'].value == 'manual'
    else:
        assert calls[1][0]['llm_response_mode'].value == 'stream'


def test_attachment_deduplication_and_four_file_limit_do_not_split_a_message(runtime, monkeypatch):
    session_id = sid(runtime)
    monkeypatch.setattr(runtime.session_inputs, 'validate_attachments', lambda **kwargs: kwargs['attachment_ids'])
    monkeypatch.setattr(runtime.session_inputs, 'batch_attachments_fit', lambda *_: True)
    submit(runtime, session_id, 'a', attachment_ids=['one', 'two'])
    submit(runtime, session_id, 'b', attachment_ids=['two', 'three', 'four'])
    last = submit(runtime, session_id, 'c', attachment_ids=['five', 'six'])['input']
    calls = record_execution(runtime, monkeypatch)
    runtime.session_inputs.tick()
    assert runtime.agent_dispatch.run_one()
    kwargs, run = calls[0]
    assert kwargs['attachment_ids'] == ['one', 'two', 'three', 'four']
    assert [item['attachment_ids'] for item in run.metadata['session_input_batch']] == [['one', 'two'], ['two', 'three', 'four']]
    assert runtime.session_inputs.snapshot(session_id)['inputs'][-1] == last
    runtime.session_inputs.tick()
    assert runtime.agent_dispatch.run_one()
    assert calls[-1][0]['attachment_ids'] == ['five', 'six']


def test_combined_media_budget_splits_the_batch(runtime, monkeypatch):
    session_id = sid(runtime)
    monkeypatch.setattr(runtime.session_inputs, 'validate_attachments', lambda **kwargs: kwargs['attachment_ids'])
    monkeypatch.setattr(runtime.session_attachment_service, 'get', lambda **_: {'kind': 'image', 'size_bytes': 7 * 1024 * 1024})
    for key in ['a', 'b']:
        submit(runtime, session_id, key, attachment_ids=[key])
    calls = record_execution(runtime, monkeypatch)
    runtime.session_inputs.tick()
    assert runtime.agent_dispatch.run_one()
    assert calls[0][0]['attachment_ids'] == ['a']
    assert runtime.session_inputs.snapshot(session_id)['inputs'][-1]['run_id'] is None


def test_text_budget_stops_before_a_whole_message(runtime, monkeypatch):
    session_id = sid(runtime)
    submit(runtime, session_id, 'a', 'a' * 60000)
    submit(runtime, session_id, 'b', 'b' * 60000)
    calls = record_execution(runtime, monkeypatch)
    runtime.session_inputs.tick()
    assert runtime.agent_dispatch.run_one()
    assert calls[0][0]['user_input'] == 'a' * 60000
    assert runtime.session_inputs.snapshot(session_id)['inputs'][-1]['run_id'] is None


def test_frozen_batch_survives_restart_without_absorbing_new_arrivals(runtime, monkeypatch):
    session_id = sid(runtime)
    submit(runtime, session_id, 'a')
    submit(runtime, session_id, 'b')
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    # Simulate a crash after the atomic freeze, before graph entry.
    class Guard:
        def check(self, **kwargs):
            pass
    frozen = runtime.session_inputs.freeze_for_dispatch(run, Guard())
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        submit(reopened, session_id, 'late')
        calls = record_execution(reopened, monkeypatch)
        assert reopened.agent_dispatch.run_one()
        assert calls[0][0]['user_input'] == frozen.user_input
        assert calls[0][1].metadata['session_input_batch'] == frozen.metadata['session_input_batch']
        snap = reopened.session_inputs.snapshot(session_id)
        assert [item['status'] for item in snap['inputs']] == ['completed', 'completed', 'queued']
        assert snap['inputs'][-1]['run_id'] is None
    finally:
        reopened.stop()


@pytest.mark.parametrize('changed', ['content', 'attachments', 'trace'])
def test_frozen_batch_checks_each_original_history_source_before_models(runtime, changed):
    session_id = sid(runtime)
    submit(runtime, session_id, 'a')
    second = submit(runtime, session_id, 'b')['input']
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    class Guard:
        def check(self, **kwargs):
            pass
    runtime.session_inputs.freeze_for_dispatch(run, Guard())
    with closing(runtime._conn()) as conn, conn:
        if changed == 'content':
            conn.execute("UPDATE agent_session_messages SET content='changed' WHERE message_id=?", (second['message_id'],))
        else:
            path, value = ('$.attachment_ids', '["changed"]') if changed == 'attachments' else ('$.trace_id', '"different"')
            conn.execute('UPDATE agent_session_messages SET payload=json_set(payload,?,json(?)) WHERE message_id=?',
                         (path, value, second['message_id']))
    client = ScriptedClient([])
    runtime.agent_turn_loop.llm_client = client
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.FAILED
    assert client.calls == []
    assert runtime.session_inputs.snapshot(session_id)['paused']


def test_checkpoint_recovery_of_frozen_input_never_expands_batch(runtime, monkeypatch):
    session_id = sid(runtime)
    submit(runtime, session_id, 'a')
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    class Guard:
        def check(self, **kwargs):
            pass
    frozen = runtime.session_inputs.freeze_for_dispatch(run, Guard())
    runtime.agent_run_manager.mark_running(run.run_id)
    submit(runtime, session_id, 'late')
    monkeypatch.setattr(runtime.agent_dispatch, 'has_checkpoint', lambda _: True)
    monkeypatch.setattr(runtime.session_inputs, 'freeze_for_dispatch', lambda *_: pytest.fail('resume must not freeze'))
    seen = []
    def resume(run_id):
        seen.append(run_id)
        complete(runtime, runtime.agent_run_manager.get_run(run_id))
    monkeypatch.setattr(runtime, 'resume_agent_run', resume)
    assert runtime.agent_dispatch.run_one()
    assert seen == [run.run_id]
    assert runtime.agent_run_manager.get_run(run.run_id).metadata['session_input_batch'] == frozen.metadata['session_input_batch']
    assert runtime.session_inputs.snapshot(session_id)['inputs'][-1]['run_id'] is None


@pytest.mark.parametrize('status', ['paused', 'cancelled'])
def test_control_between_dispatch_selection_and_freeze_preserves_its_state(runtime, monkeypatch, status):
    session_id = sid(runtime)
    submit(runtime, session_id, 'a')
    submit(runtime, session_id, 'b')
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    freeze = runtime.session_inputs.freeze_for_dispatch
    def control_before_freeze(record, guard):
        if status == 'paused':
            pause(runtime, record)
        else:
            runtime.agent_run_manager.mark_cancelled(record.run_id)
        return freeze(record, guard)
    monkeypatch.setattr(runtime.session_inputs, 'freeze_for_dispatch', control_before_freeze)
    monkeypatch.setattr(runtime, 'run_agent_turn', lambda **_: pytest.fail('controlled run must not execute'))
    assert runtime.agent_dispatch.run_one()
    latest = runtime.agent_run_manager.get_run(run.run_id)
    assert latest.status.value == status
    assert not latest.metadata.get('session_input_batch_frozen')
    assert runtime.session_inputs.snapshot(session_id)['inputs'][1]['run_id'] is None
    runtime.session_inputs.tick()
    assert runtime.agent_run_manager.get_run(run.run_id).status.value == status
    if status == 'paused':
        assert runtime.session_inputs.snapshot(session_id)['active_run_id'] == run.run_id
    else:
        assert runtime.session_inputs.snapshot(session_id)['paused']


def test_freeze_failure_rolls_back_additional_bindings_and_never_replays_failed_input(runtime, monkeypatch):
    session_id = sid(runtime)
    submit(runtime, session_id, 'a')
    submit(runtime, session_id, 'b')
    runtime.session_inputs.tick()
    run = input_run(runtime, session_id)
    save = runtime.agent_run_store.save_run_in_transaction

    def fail(conn, record, **kwargs):
        save(conn, record, **kwargs)
        if record['metadata'].get('session_input_batch_frozen'):
            raise RuntimeError('freeze rollback')

    monkeypatch.setattr(runtime.agent_run_store, 'save_run_in_transaction', fail)
    monkeypatch.setattr(runtime, 'run_agent_turn', lambda **_: pytest.fail('must not execute'))
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.FAILED
    runtime.session_inputs.tick()
    snap = runtime.session_inputs.snapshot(session_id)
    assert snap['paused'] and snap['inputs'][1]['run_id'] is None
    second = runtime.get_session(session_id=session_id).messages[1]
    assert 'trace_id' not in second.payload
    runtime.session_inputs.tick()
    assert not runtime.agent_dispatch.run_one()


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'timed_out'])
def test_background_root_failure_never_pauses_user_queue(runtime, monkeypatch, status):
    session_id = sid(runtime)
    background = runtime.create_agent_run(session_id=session_id, user_input='autonomous', resumable=True)
    runtime.agent_run_manager._update_run(background.run_id, status=background.status,
                                         metadata_patch={'input_envelope': {'origin': 'background'}})
    submit(runtime, session_id, 'a')
    runtime.session_inputs.tick()
    with closing(runtime._conn()) as conn, conn:
        assert conn.execute('SELECT watched_run_id FROM session_input_queues WHERE session_id=?', (session_id,)).fetchone()[0] is None
        # Also support a watch persisted by the previous implementation.
        conn.execute('UPDATE session_input_queues SET watched_run_id=? WHERE session_id=?', (background.run_id, session_id))
    runtime.agent_run_manager._update_run(background.run_id, status=AgentRunStatus(status))
    assert not runtime.session_inputs.snapshot(session_id)['paused']
    runtime.session_inputs.tick()
    assert input_run(runtime, session_id).user_input == 'a'


def test_explicit_or_missing_source_pause_is_never_automatically_cleared(runtime):
    session_id = sid(runtime)
    submit(runtime, session_id, 'a')
    with closing(runtime._conn()) as conn, conn:
        conn.execute('UPDATE session_input_queues SET paused=1,watched_run_id=NULL WHERE session_id=?', (session_id,))
    runtime.session_inputs.tick()
    assert runtime.session_inputs.snapshot(session_id)['paused']
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE session_input_queues SET paused=0,watched_run_id='missing' WHERE session_id=?", (session_id,))
    runtime.session_inputs.tick()
    assert runtime.session_inputs.snapshot(session_id)['paused']
    assert runtime.session_inputs.snapshot(session_id)['inputs'][0]['run_id'] is None


def test_replacement_boundary_preserves_older_queue_and_can_include_newer_input(runtime, monkeypatch):
    session_id = sid(runtime)
    old = runtime.create_agent_run(session_id=session_id, user_input='old', resumable=True)
    pause(runtime, old)
    earlier = submit(runtime, session_id, 'earlier')['input']
    replacement = submit(runtime, session_id, 'replacement', replace_run_id=old.run_id)['input']
    newer = submit(runtime, session_id, 'newer')['input']
    calls = record_execution(runtime, monkeypatch)
    assert runtime.agent_dispatch.run_one()
    assert [item['message_id'] for item in calls[0][1].metadata['session_input_batch']] == [replacement['message_id'], newer['message_id']]
    assert runtime.session_inputs.snapshot(session_id)['inputs'][0] == earlier
    runtime.session_inputs.tick()
    assert runtime.agent_dispatch.run_one()
    assert calls[1][0]['user_input'] == 'earlier'


def test_legacy_unique_run_id_migration_preserves_receipts_indexes_and_other_uniqueness(runtime):
    session_id = sid(runtime)
    with closing(runtime._conn()) as conn, conn:
        conn.execute('DROP TABLE session_inputs')
        conn.execute('''CREATE TABLE session_inputs (
            input_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            client_message_id TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE,
            sequence INTEGER NOT NULL, request_json TEXT NOT NULL,
            run_id TEXT UNIQUE, created_at TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
            UNIQUE(session_id,client_message_id), UNIQUE(session_id,sequence))''')
        conn.execute('CREATE INDEX custom_input_index ON session_inputs(created_at)')
    for key in ['a', 'b']:
        submit(runtime, session_id, key)
    runtime.session_inputs.tick()
    with closing(runtime._conn()) as conn:
        before = [tuple(row) for row in conn.execute('SELECT * FROM session_inputs ORDER BY sequence')]
        history = [tuple(row) for row in conn.execute('SELECT * FROM agent_session_messages')]
    runtime.session_inputs.ensure_schema()
    runtime.session_inputs.ensure_schema()
    with closing(runtime._conn()) as conn, conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM session_inputs ORDER BY sequence')] == before
        assert [tuple(row) for row in conn.execute('SELECT * FROM agent_session_messages')] == history
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='custom_input_index'").fetchone()
        conn.execute('UPDATE session_inputs SET run_id=? WHERE sequence=2', (before[0][6],))
        for column in ['message_id', 'sequence', 'client_message_id']:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f'UPDATE session_inputs SET {column}=(SELECT {column} FROM session_inputs WHERE sequence=1) WHERE sequence=2')
