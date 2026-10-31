"""Checkpoint schema reuse must retain per-write durability and recovery."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import aiosqlite
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.core import agent_checkpoints
from app.core.agent_checkpoints import SqliteCheckpointRuntime


@pytest.fixture
def statements(monkeypatch):
    statements = []
    lock = threading.Lock()
    connect = agent_checkpoints.connect_sqlite

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)

        def trace(sql):
            with lock:
                statements.append(sql.strip().upper())

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(agent_checkpoints, "connect_sqlite", traced_connect)
    return statements


def _schema_count(statements):
    return sum(sql.startswith("CREATE TABLE IF NOT EXISTS CHECKPOINTS") for sql in statements)


def test_sync_hot_savers_skip_schema_but_commit_checkpoints_and_pending_writes(tmp_path, statements):
    path = tmp_path / "checkpoints.sqlite3"
    runtime = SqliteCheckpointRuntime(path)
    config = {"configurable": {"thread_id": "sync", "checkpoint_ns": ""}}
    for index in range(3):
        with runtime.sync_saver() as saver:
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"value": index}
            config = saver.put(config, checkpoint, {"source": "input", "step": index}, {})
            saver.put_writes(config, [("value", index)], "synthetic-task")
            # Visibility from another connection before the saver closes proves
            # neither checkpoint nor pending-write commits were deferred.
            with sqlite3.connect(path) as observer:
                assert observer.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == index + 1
                assert observer.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == index + 1
                assert observer.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert not saver.conn.in_transaction
    assert _schema_count(statements) == 1
    assert sum(sql.startswith("PRAGMA JOURNAL_MODE=WAL") for sql in statements) == 1
    with runtime.sync_saver() as saver:
        saved = saver.get_tuple(config)
        assert saved.checkpoint["channel_values"] == {"value": 2}
        assert saved.pending_writes == [("synthetic-task", "value", 2)]
    assert _schema_count(statements) == 1


def test_async_hot_savers_share_successful_sync_setup_and_commit(tmp_path, statements):
    path = tmp_path / "checkpoints.sqlite3"
    runtime = SqliteCheckpointRuntime(path)
    with runtime.sync_saver():
        pass

    async def scenario():
        config = {"configurable": {"thread_id": "async", "checkpoint_ns": ""}}
        for index in range(3):
            async with runtime.async_saver() as saver:
                checkpoint = empty_checkpoint()
                checkpoint["channel_values"] = {"value": index}
                config = await saver.aput(config, checkpoint, {"source": "input", "step": index}, {})
                await saver.aput_writes(config, [("value", index)], "synthetic-task")
                with sqlite3.connect(path) as observer:
                    assert observer.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == index + 1
                    assert observer.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == index + 1
                assert not saver.conn.in_transaction
        async with runtime.async_saver() as saver:
            saved = await saver.aget_tuple(config)
            assert saved.checkpoint["channel_values"] == {"value": 2}
            assert saved.pending_writes == [("synthetic-task", "value", 2)]

    asyncio.run(scenario())
    assert _schema_count(statements) == 1


def test_sync_first_use_from_threads_prepares_schema_once(tmp_path, statements):
    runtime = SqliteCheckpointRuntime(tmp_path / "checkpoints.sqlite3")
    barrier = threading.Barrier(8)

    def read(_index):
        barrier.wait(timeout=5)
        with runtime.sync_saver() as saver:
            assert saver.get_tuple({"configurable": {"thread_id": "absent"}}) is None

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(read, range(8)))
    assert _schema_count(statements) == 1


def test_async_first_use_prepares_schema_once(tmp_path, statements):
    runtime = SqliteCheckpointRuntime(tmp_path / "checkpoints.sqlite3")

    async def read():
        async with runtime.async_saver() as saver:
            assert await saver.aget_tuple({"configurable": {"thread_id": "absent"}}) is None

    async def scenario():
        await asyncio.gather(*(read() for _ in range(8)))

    asyncio.run(scenario())
    with runtime.sync_saver():
        pass
    assert _schema_count(statements) == 1


@pytest.mark.parametrize("async_api", [False, True])
def test_failed_schema_setup_remains_retryable(tmp_path, statements, monkeypatch, async_api):
    runtime = SqliteCheckpointRuntime(tmp_path / "checkpoints.sqlite3")
    saver_type = agent_checkpoints.AsyncSqliteSaver if async_api else agent_checkpoints.SqliteSaver
    original = saver_type.setup
    calls = 0

    def fail_once(saver):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("synthetic setup failure")
        return original(saver)

    async def async_fail_once(saver):
        return await fail_once(saver)

    monkeypatch.setattr(saver_type, "setup", async_fail_once if async_api else fail_once)

    async def attempt():
        async with runtime.async_saver():
            pass

    def run_attempt():
        if async_api:
            asyncio.run(attempt())
        else:
            with runtime.sync_saver():
                pass

    with pytest.raises(sqlite3.OperationalError, match="synthetic setup failure"):
        run_attempt()
    assert not runtime._setup_complete
    assert not runtime._setup_lock.locked()
    run_attempt()
    assert runtime._setup_complete
    assert _schema_count(statements) == 1


def test_async_wait_for_sync_initializer_keeps_loop_responsive(tmp_path, monkeypatch):
    runtime = SqliteCheckpointRuntime(tmp_path / "checkpoints.sqlite3")
    started = threading.Event()
    release = threading.Event()
    original = agent_checkpoints.SqliteSaver.setup

    def slow_setup(saver):
        started.set()
        assert release.wait(timeout=5)
        original(saver)

    monkeypatch.setattr(agent_checkpoints.SqliteSaver, "setup", slow_setup)

    def sync_init():
        with runtime.sync_saver():
            pass

    async def async_init():
        async with runtime.async_saver():
            pass

    async def scenario():
        sync_task = asyncio.create_task(asyncio.to_thread(sync_init))
        assert await asyncio.to_thread(started.wait, 5)
        async_task = asyncio.create_task(async_init())
        try:
            await asyncio.sleep(0.03)
            assert not async_task.done()
        finally:
            release.set()
        await asyncio.gather(sync_task, async_task)

    asyncio.run(scenario())


def test_async_schema_commit_failure_remains_retryable(tmp_path, statements, monkeypatch):
    runtime = SqliteCheckpointRuntime(tmp_path / "checkpoints.sqlite3")
    original = aiosqlite.Connection.commit
    calls = 0

    async def fail_commit_once(connection):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("synthetic setup commit failure")
        await original(connection)

    monkeypatch.setattr(aiosqlite.Connection, "commit", fail_commit_once)

    async def scenario():
        with pytest.raises(sqlite3.OperationalError, match="synthetic setup commit failure"):
            async with runtime.async_saver():
                pytest.fail("failed initialization must not yield a saver")
        assert not runtime._setup_complete
        assert not runtime._setup_lock.locked()
        async with runtime.async_saver() as saver:
            assert await saver.aget_tuple({"configurable": {"thread_id": "absent"}}) is None

    asyncio.run(scenario())
    assert runtime._setup_complete
    assert _schema_count(statements) == 2


def test_cancelled_async_initialization_releases_lock_for_retry(tmp_path, monkeypatch):
    runtime = SqliteCheckpointRuntime(tmp_path / "checkpoints.sqlite3")
    original = agent_checkpoints.AsyncSqliteSaver.setup

    async def scenario():
        started = asyncio.Event()

        async def interrupted_setup(_saver):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(agent_checkpoints.AsyncSqliteSaver, "setup", interrupted_setup)

        async def initialize():
            async with runtime.async_saver():
                pytest.fail("cancelled initialization must not yield a saver")

        task = asyncio.create_task(initialize())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runtime._setup_complete
        assert not runtime._setup_lock.locked()
        monkeypatch.setattr(agent_checkpoints.AsyncSqliteSaver, "setup", original)
        async with runtime.async_saver():
            pass
        assert runtime._setup_complete

    asyncio.run(scenario())


@pytest.mark.parametrize("async_api", [False, True])
def test_new_runtime_resumes_durable_interrupt_without_replaying_completed_node(tmp_path, async_api):
    path = tmp_path / "checkpoints.sqlite3"
    completed = []

    def increment(value):
        completed.append(value)
        return value + 1

    def gate(value):
        return value + interrupt("synthetic confirmation")

    builder = StateGraph(int)
    builder.add_node("increment", increment)
    builder.add_node("gate", gate)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", "gate")
    builder.add_edge("gate", END)
    config = {"configurable": {"thread_id": "resume"}}

    async def scenario():
        async with SqliteCheckpointRuntime(path).async_saver() as saver:
            await builder.compile(checkpointer=saver).ainvoke(0, config)
        async with SqliteCheckpointRuntime(path).async_saver() as saver:
            graph = builder.compile(checkpointer=saver)
            assert await graph.ainvoke(Command(resume=2), config) == 3
            assert (await graph.aget_state(config)).next == ()

    if async_api:
        asyncio.run(scenario())
    else:
        with SqliteCheckpointRuntime(path).sync_saver() as saver:
            builder.compile(checkpointer=saver).invoke(0, config)
        with SqliteCheckpointRuntime(path).sync_saver() as saver:
            graph = builder.compile(checkpointer=saver)
            assert graph.invoke(Command(resume=2), config) == 3
            assert graph.get_state(config).next == ()
    assert completed == [0]
