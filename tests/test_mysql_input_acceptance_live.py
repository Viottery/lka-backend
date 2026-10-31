"""MySQL hot-queue admission: ordering, deduplication and default-change fence."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Barrier, Event
from uuid import uuid4

import pytest

from app.core.session_inputs import SessionInputConflict
from tests.test_mysql_runtime_live import runtime as base_runtime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from base_runtime.__wrapped__(tmp_path, monkeypatch)


def manager(runtime):
    return runtime.management_service.create(
        title='Synthetic hot input '+uuid4().hex[:12], make_default=True,
    )['session']['session_id']


def submit(runtime, session_id, key):
    return runtime.session_inputs.submit(
        session_id, client_message_id=key, user_input='Synthetic '+key, attachment_ids=[],
    )


def test_concurrent_exact_duplicates_create_one_message_and_one_receipt(runtime):
    session_id = manager(runtime)
    barrier = Barrier(8)

    def accept(_index):
        barrier.wait(timeout=5)
        return submit(runtime, session_id, 'same-key')

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(accept, range(8)))
    assert len({row['input']['input_id'] for row in receipts}) == 1
    with runtime._conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM session_inputs WHERE session_id=?', (session_id,)).fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM agent_session_messages WHERE session_id=?', (session_id,)).fetchone()[0] == 1
    assert runtime.session_inputs.snapshot(session_id)['pending_count'] == 1


def test_hot_queue_capacity_and_sequences_remain_atomic(runtime):
    session_id = manager(runtime)
    runtime.session_inputs.capacity = 3
    barrier = Barrier(8)

    def accept(index):
        barrier.wait(timeout=5)
        try:
            return submit(runtime, session_id, 'key-'+str(index))
        except SessionInputConflict as exc:
            assert str(exc) == 'session_input_queue_full'
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = [item for item in pool.map(accept, range(8)) if item is not None]
    assert len(receipts) == 3
    assert sorted(item['input']['sequence'] for item in receipts) == [1, 2, 3]
    for item in receipts:
        assert submit(runtime, session_id, item['input']['client_message_id'])['input'] == item['input']
    with runtime._conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM agent_session_messages WHERE session_id=?', (session_id,)).fetchone()[0] == 3


def test_default_switch_waits_for_accepted_input_commit_and_preserves_exact_retry(runtime):
    session_id = manager(runtime)
    authorized, finish, switching = Event(), Event(), Event()
    original = runtime.session_inputs.authorize

    def authorize(conn, sid, *, accepting=False):
        original(conn, sid, accepting=accepting)
        if accepting:
            authorized.set()
            assert finish.wait(5)

    runtime.session_inputs.authorize = authorize

    def switch():
        switching.set()
        return manager(runtime)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = pool.submit(submit, runtime, session_id, 'before-switch')
            assert authorized.wait(5)
            changed = pool.submit(switch)
            assert switching.wait(2)
            try:
                with pytest.raises(FutureTimeout):
                    changed.result(timeout=0.1)
            finally:
                finish.set()
            receipt = accepted.result(timeout=5)
            assert changed.result(timeout=5) != session_id
        assert submit(runtime, session_id, 'before-switch')['input'] == receipt['input']
        with pytest.raises(SessionInputConflict, match='default_manager'):
            submit(runtime, session_id, 'after-switch')
    finally:
        finish.set()
        runtime.session_inputs.authorize = original


def test_compact_state_observes_mysql_queue_only_changes(runtime, monkeypatch):
    import app.core.sessions as sessions_module

    session_id = manager(runtime)
    clock = [10.0]
    monkeypatch.setattr(sessions_module, 'monotonic', lambda: clock[0])
    first = runtime.session_service.get_session_state(session_id=session_id)
    submit(runtime, session_id, 'opaque-revision')
    next_state = runtime.session_service.get_session_state(session_id=session_id)
    assert next_state.input_queue_revision != first.input_queue_revision
    assert 'Synthetic opaque-revision' not in next_state.model_dump_json()
    with runtime._conn() as conn:
        conn.execute('UPDATE session_input_queues SET paused=1 WHERE session_id=?', (session_id,))
    clock[0] += 1.1
    paused = runtime.session_service.get_session_state(session_id=session_id)
    assert paused.updated_at == next_state.updated_at
    assert paused.revision != next_state.revision
    assert paused.input_queue_revision != next_state.input_queue_revision
