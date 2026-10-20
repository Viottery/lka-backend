"""Logical session admission and root persistence share one SQLite transaction."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from app.core.agent_runs import AgentRunRecord, AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.session_admission import SessionBusyError
from app.core.sessions import SessionService
from app.storage.db import connect


@pytest.fixture
def state(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "runs.sqlite3")
    sessions = SessionService(lambda: connect(store.db_path))
    session = sessions.create_session(title="Real session").session
    manager = InMemoryAgentRunManager(durable_store=store)
    yield store, sessions, session.session_id, manager
    manager.close()


def slot(store, sid):
    with closing(connect(store.db_path)) as conn:
        row = conn.execute("SELECT run_id,generation FROM session_admissions WHERE session_id=?", (sid,)).fetchone()
        return tuple(row) if row else None


def test_concurrent_roots_have_one_winner_and_no_ghost(state):
    store, _, sid, manager = state
    barrier = Barrier(2)

    def create(index):
        barrier.wait(timeout=5)
        try:
            return manager.create_run(session_id=sid, user_input=f"Task {index}")
        except SessionBusyError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    winners = [item for item in results if isinstance(item, AgentRunRecord)]
    assert len(winners) == 1
    loser = next(item for item in results if isinstance(item, SessionBusyError))
    assert loser.session_id == sid and loser.run_id == winners[0].run_id
    assert list(manager._runs) == [winners[0].run_id]
    assert slot(store, sid) == (winners[0].run_id, 1)


def test_different_sessions_and_children_do_not_share_root_slot(state):
    store, sessions, sid, manager = state
    other = sessions.create_session(title="Another").session.session_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda session: manager.create_run(session_id=session, user_input="Task"), [sid, other]))
    manager.mark_running(runs[0].run_id)
    child = manager.create_child_run(parent_run_id=runs[0].run_id, plan_id="plan", step_id="child", attempt=1, user_input="Child")
    assert child.parent_run_id == runs[0].run_id
    assert slot(store, sid)[0] == runs[0].run_id
    assert slot(store, other)[0] == runs[1].run_id


def test_independent_managers_serialize_root_creation_in_sqlite(state):
    store, _, sid, first = state
    second = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    barrier = Barrier(2)

    def create(manager):
        barrier.wait(timeout=5)
        try:
            return manager.create_run(session_id=sid, user_input="Concurrent task")
        except SessionBusyError as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, [first, second]))
        assert sum(isinstance(item, SessionBusyError) for item in results) == 1
        assert len(first._runs) + len(second._runs) == 1
        winner = next(item for item in results if isinstance(item, AgentRunRecord))
        assert slot(store, sid)[0] == winner.run_id
    finally:
        second.close()


def test_child_record_in_same_session_does_not_block_root(state):
    store, _, sid, manager = state
    child = AgentRunRecord(run_id="same-session-child", session_id=sid, trace_id="trace",
                           parent_run_id="parent", user_input="Child", created_at="now", status=AgentRunStatus.QUEUED)
    store.save_run(child.model_dump(mode="json"), updated_at="now")
    assert slot(store, sid) is None
    root = manager.create_run(session_id=sid, user_input="Root")
    assert slot(store, sid)[0] == root.run_id


@pytest.mark.parametrize("status", ["paused", "waiting_user", "waiting_confirmation"])
def test_waiting_and_paused_keep_slot(state, status):
    store, _, sid, manager = state
    run = manager.create_run(session_id=sid, user_input="Wait")
    manager._update_run(run.run_id, status=AgentRunStatus(status))
    with pytest.raises(SessionBusyError):
        manager.create_run(session_id=sid, user_input="Cannot pass")
    assert slot(store, sid) == (run.run_id, 1)


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "timed_out"])
def test_terminal_update_releases_and_next_root_advances_generation(state, status):
    store, _, sid, manager = state
    run = manager.create_run(session_id=sid, user_input="First")
    manager._update_run(run.run_id, status=AgentRunStatus(status))
    assert slot(store, sid) == (None, 1)
    following = manager.create_run(session_id=sid, user_input="Next")
    assert slot(store, sid) == (following.run_id, 2)


def paused_root(manager, sid):
    run = manager.create_run(session_id=sid, user_input="Original")
    manager.request_pause(run.run_id)
    manager.acknowledge_pause(run.run_id)
    return manager.get_run(run.run_id)


def test_replace_transfers_slot_with_one_winner(state):
    store, _, sid, manager = state
    original = paused_root(manager, sid)
    barrier = Barrier(2)

    def replace(index):
        barrier.wait(timeout=5)
        try:
            return manager.replace_paused(original.run_id, user_input=f"New {index}", command_id=f"replace-{index}", request_fingerprint=str(index))
        except ValueError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace, range(2)))
    assert sum(isinstance(item, ValueError) for item in results) == 1
    replacement = next(item for item in results if isinstance(item, AgentRunRecord))
    assert manager.get_run(original.run_id).status == AgentRunStatus.CANCELLED
    assert slot(store, sid) == (replacement.run_id, 2)


def test_replace_failure_rolls_back_slot_runs_and_memory(state, monkeypatch):
    store, _, sid, manager = state
    original = paused_root(manager, sid)
    save = store.save_run_in_transaction

    def fail_after_new_run(conn, record, *, updated_at):
        save(conn, record, updated_at=updated_at)
        if record["run_id"] != original.run_id:
            raise OSError("Injected after transfer")

    monkeypatch.setattr(store, "save_run_in_transaction", fail_after_new_run)
    with pytest.raises(OSError, match="after transfer"):
        manager.replace_paused(original.run_id, user_input="New", command_id="replace", request_fingerprint="fingerprint")
    assert slot(store, sid) == (original.run_id, 1)
    assert manager.get_run(original.run_id).status == AgentRunStatus.PAUSED
    assert store.load_run(original.run_id)["status"] == "paused"
    assert len(manager._runs) == 1
    with closing(connect(store.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 1


def test_replace_callback_is_atomic_and_replay_does_not_call_it(state):
    store, _, sid, manager = state
    original = paused_root(manager, sid)
    calls = []

    def record_command(conn, replacement):
        assert isinstance(replacement, AgentRunRecord)
        calls.append(replacement.run_id)
        conn.execute("CREATE TABLE replacement_receipts(command_id TEXT PRIMARY KEY,run_id TEXT)")
        conn.execute("INSERT INTO replacement_receipts VALUES('command',?)", (replacement.run_id,))
        assert conn.execute("SELECT run_id FROM session_admissions WHERE session_id=?", (sid,)).fetchone()[0] == replacement.run_id

    envelope = {"origin": "manager", "input_id": "instruction"}
    replacement = manager.replace_paused(original.run_id, user_input="New", command_id="command", request_fingerprint="same",
                                         metadata_updates={"input_envelope": envelope}, on_created=record_command)
    replay = manager.replace_paused(original.run_id, user_input="New", command_id="command", request_fingerprint="same",
                                   metadata_updates={"input_envelope": envelope}, on_created=record_command)
    assert replay.run_id == replacement.run_id and calls == [replacement.run_id]
    assert replacement.metadata["input_envelope"] == envelope
    assert "input_envelope" not in manager.get_run(original.run_id).metadata
    with closing(connect(store.db_path)) as conn:
        assert conn.execute("SELECT run_id FROM replacement_receipts").fetchone()[0] == replacement.run_id


def test_replace_callback_failure_rolls_back_receipt_slot_and_control(state):
    store, _, sid, manager = state
    original = paused_root(manager, sid)
    with closing(connect(store.db_path)) as conn, conn:
        conn.execute("CREATE TABLE replacement_receipts(command_id TEXT PRIMARY KEY,run_id TEXT)")

    def fail(conn, replacement):
        conn.execute("INSERT INTO replacement_receipts VALUES('command',?)", (replacement.run_id,))
        raise OSError("receipt failure")

    with pytest.raises(OSError, match="receipt failure"):
        manager.replace_paused(original.run_id, user_input="New", command_id="command", request_fingerprint="same", on_created=fail)
    assert manager.get_run(original.run_id).status == AgentRunStatus.PAUSED
    assert store.load_run(original.run_id)["status"] == "paused"
    assert slot(store, sid) == (original.run_id, 1)
    assert len(manager._runs) == 1
    with closing(connect(store.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM replacement_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 1


def test_legacy_root_without_slot_blocks_new_and_is_adopted_on_restore(state):
    store, _, sid, manager = state
    run = manager.create_run(session_id=sid, user_input="Legacy")
    with closing(connect(store.db_path)) as conn, conn:
        conn.execute("DELETE FROM session_admissions WHERE session_id=?", (sid,))
    reopened = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    try:
        with pytest.raises(SessionBusyError):
            reopened.create_run(session_id=sid, user_input="Do not bypass legacy")
        assert not reopened._runs
        reopened.restore_run(AgentRunRecord.model_validate(store.load_run(run.run_id)))
        assert slot(store, sid) == (run.run_id, 1)
    finally:
        reopened.close()


def test_transactional_creation_rolls_back_run_slot_and_session(state):
    store, _, _, _ = state
    run = AgentRunRecord(run_id="atomic-run", session_id="atomic-session", trace_id="trace", user_input="Task", status=AgentRunStatus.QUEUED, created_at="now")
    with pytest.raises(OSError), closing(connect(store.db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO agent_sessions(session_id,title,status,metadata,created_at,updated_at) VALUES('atomic-session','Atomic','active','{}','now','now')")
        store.save_run_in_transaction(conn, run.model_dump(mode="json"), updated_at="now")
        assert conn.execute("SELECT run_id FROM session_admissions WHERE session_id='atomic-session'").fetchone()[0] == run.run_id
        raise OSError("Rollback caller transaction")
    assert store.load_run(run.run_id) is None
    assert slot(store, run.session_id) is None


def test_failed_terminal_write_does_not_release_memory_slot(state, monkeypatch):
    store, _, sid, manager = state
    run = manager.create_run(session_id=sid, user_input="Keep alive")
    monkeypatch.setattr(store, "save_run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        manager.complete_run(run.run_id)
    assert manager.get_run(run.run_id).status == AgentRunStatus.QUEUED
    assert slot(store, sid) == (run.run_id, 1)


def test_low_level_fake_sessions_keep_compatibility(state):
    store, _, _, manager = state
    first = manager.create_run(session_id="not-a-real-session", user_input="First")
    second = manager.create_run(session_id="not-a-real-session", user_input="Second")
    assert first.run_id != second.run_id
    assert slot(store, first.session_id) is None
