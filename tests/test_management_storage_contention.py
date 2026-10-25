"""Real SQLite contention without model calls or private data."""
import asyncio
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest

import app.core.agent_storage as storage
from app.api.routes.agent import get_agent_run
from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_tasks import counts
from tests.test_management_tasks import manager_context as context_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def manager_context(runtime):
    return context_fixture.__wrapped__(runtime)


def test_progress_lookup_uses_relational_scope_without_reading_large_run_json(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.sqlite3"
    store = SqliteAgentRunStore(path)
    manager = InMemoryAgentRunManager(durable_store=store)
    try:
        own = manager.create_run(session_id="own-session", user_input="synthetic")
        other = manager.create_run(session_id="other-session", user_input="synthetic")
        for identifier, run, kind, timestamp in (
            ("own-a", own, "session_progress", "2026-01-01T00:00:00+00:00"),
            ("own-b", own, "session_progress", "2026-01-02T00:00:00+00:00"),
            ("foreign", other, "session_progress", "2026-01-03T00:00:00+00:00"),
            ("not-progress", own, "tool_result", "2026-01-04T00:00:00+00:00"),
        ):
            store.put_artifact(artifact_id=identifier, run_id=run.run_id, kind=kind,
                               payload={"identifier": identifier}, summary="synthetic", created_at=timestamp)
        original = storage.connect

        def connection(db):
            conn = original(db)
            conn.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY
                                if action == sqlite3.SQLITE_READ and table == "agent_runs"
                                and column == "record_payload" else sqlite3.SQLITE_OK)
            return conn

        monkeypatch.setattr(storage, "connect", connection)
        assert store.list_session_progress("own-session", limit=1) == [{"identifier": "own-b"}]
        assert store.list_session_progress("own-session") == [
            {"identifier": "own-b"}, {"identifier": "own-a"}]
    finally:
        manager.close()


@pytest.mark.parametrize("release_after_first_wait", [True, False])
def test_task_admission_retries_only_before_writes_and_remains_idempotent(
    runtime, manager_context, monkeypatch, release_after_first_wait,
):
    service = runtime.managed_tasks
    before = counts(runtime)
    db = runtime.settings.data_dir / "lka.sqlite3"
    blocker = sqlite3.connect(db)
    attempts = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE":
                attempts.append(sql)
                if len(attempts) == 1:
                    blocker.execute("BEGIN IMMEDIATE")
                try:
                    return super().execute(sql, parameters)
                except sqlite3.OperationalError:
                    if release_after_first_wait:
                        blocker.rollback()
                    raise
            return super().execute(sql, parameters)

    def connection():
        conn = sqlite3.connect(db, timeout=0.02, factory=Connection)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(service, "_conn", connection)
    try:
        if release_after_first_wait:
            receipt = service.start_task(manager_context, command_id="busy-admission", purpose="synthetic")
            assert len(attempts) == 2
            after = counts(runtime)
            assert after["agent_runs"] == before["agent_runs"] + 1
            assert after["management_commands"] == before["management_commands"] + 1
            assert service.start_task(manager_context, command_id="busy-admission", purpose="synthetic") == receipt
            assert counts(runtime) == after
        else:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                service.start_task(manager_context, command_id="busy-admission", purpose="synthetic")
            assert len(attempts) == 2
            blocker.rollback()
            assert counts(runtime) == before
    finally:
        blocker.rollback()
        blocker.close()


def test_busy_after_task_rows_are_written_rolls_back_without_replaying(runtime, manager_context, monkeypatch):
    before = counts(runtime)
    calls = []

    def fail_enqueue(*_args, **_kwargs):
        calls.append(True)
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(runtime.managed_tasks, "enqueue", fail_enqueue)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        runtime.managed_tasks.start_task(manager_context, command_id="busy-after-write", purpose="synthetic")
    assert len(calls) == 1
    assert counts(runtime) == before


def test_committed_task_is_cached_without_rewriting_it(runtime, manager_context, monkeypatch):
    def reject_write(*_args, **_kwargs):
        pytest.fail("A committed task must not be written again merely to hydrate its cache")

    monkeypatch.setattr(runtime.agent_run_manager, "restore_run", reject_write)
    monkeypatch.setattr(runtime.agent_run_store, "save_run", reject_write)
    receipt = runtime.managed_tasks.start_task(manager_context, command_id="committed-read", purpose="synthetic")
    assert runtime.agent_run_manager.get_run(receipt["run_id"]) is not None
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM management_commands WHERE command_id='committed-read'").fetchone()[0] == 1


def test_run_endpoint_moves_potential_database_reads_off_the_event_loop(runtime, manager_context, monkeypatch):
    loop_thread = threading.get_ident()
    original = runtime.agent_run_manager.get_run

    def get_from_worker(run_id):
        assert threading.get_ident() != loop_thread
        return original(run_id)

    monkeypatch.setattr(runtime.agent_run_manager, "get_run", get_from_worker)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    response = asyncio.run(get_agent_run(manager_context.run_id, request))
    assert response.run_id == manager_context.run_id
