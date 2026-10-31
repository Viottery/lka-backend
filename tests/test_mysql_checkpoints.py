"""MySQL checkpoint lifecycle contracts, with driver connections isolated."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from types import ModuleType, SimpleNamespace

import pytest

from app.core.agent_checkpoints import (
    MySQLCheckpointRuntime,
    SqliteCheckpointRuntime,
    create_mysql_checkpoint_runtime,
    create_sqlite_checkpoint_runtime,
)
from app.storage.mysql import MySQLConfig


class _Connection:
    def __init__(self) -> None:
        self.closed = False
        self.autocommit = True
        self.server_status = 0
        self.rollback_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        self.closed = True

    def close(self) -> None:
        self.closed = True

    def ping(self, reconnect=False):
        if self.closed:
            raise RuntimeError("closed")

    def begin(self):
        self.server_status |= 0x0001

    def commit(self):
        self.server_status &= ~0x0001

    def rollback(self):
        self.rollback_count += 1
        self.server_status &= ~0x0001

    def get_autocommit(self):
        return self.autocommit

    def cursor(self, *_args):
        return _SyncCursor()


class _SyncCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return None


class _AsyncCursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return None


class _AsyncConnection(_Connection):
    async def begin(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def cursor(self, *_args):
        return _AsyncCursor()


class _FakeAsyncPool:
    def __init__(self, connections, args):
        self.connections = connections
        self.args = args
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self.active = 0
        self.maximum_active = 0
        self.clear_calls = 0
        self._slots = asyncio.Semaphore(int(args["maxsize"]))

    @asynccontextmanager
    async def acquire(self):
        await self._slots.acquire()
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        connection = _AsyncConnection()
        self.connections.append(connection)
        try:
            yield connection
        finally:
            self.active -= 1
            self._slots.release()

    def close(self):
        self.closed = True

    def terminate(self):
        self.closed = True

    async def clear(self):
        self.clear_calls += 1

    async def wait_closed(self):
        return None


def _install_fake_mysql(monkeypatch):
    counts = {"connect": 0, "setup": 0}
    connections: list[_Connection] = []
    connect_args: list[dict[str, object]] = []

    def connect(**kwargs):
        counts["connect"] += 1
        connect_args.append(kwargs)
        connection = _Connection()
        connections.append(connection)
        return connection

    def install(name: str, module: ModuleType) -> None:
        monkeypatch.setitem(sys.modules, name, module)

    mysql_package = ModuleType("langgraph.checkpoint.mysql")
    mysql_package.__path__ = []
    install("langgraph.checkpoint.mysql", mysql_package)

    sync_module = ModuleType("langgraph.checkpoint.mysql.pymysql")

    class FakePyMySQLSaver:
        def __init__(self, connection):
            self.lock = threading.Lock()
            self.connection = connection
            self.conn = connection

        def setup(self):
            counts["setup"] += 1

        @staticmethod
        def _get_cursor_from_connection(connection):
            return connection.cursor()

    sync_module.PyMySQLSaver = FakePyMySQLSaver
    install("langgraph.checkpoint.mysql.pymysql", sync_module)

    async_module = ModuleType("langgraph.checkpoint.mysql.aio")

    class FakeAIOMySQLSaver:
        def __init__(self, connection):
            self.lock = asyncio.Lock()
            self.connection = connection
            self.conn = connection

        async def setup(self):
            counts["setup"] += 1

        @staticmethod
        def _get_cursor_from_connection(connection):
            return connection.cursor()

        @asynccontextmanager
        async def _cursor(self, *, pipeline=False):
            yield object()

    async_module.AIOMySQLSaver = FakeAIOMySQLSaver
    install("langgraph.checkpoint.mysql.aio", async_module)

    driver = ModuleType("pymysql")
    driver.__path__ = []
    driver.cursors = ModuleType("pymysql.cursors")
    driver.cursors.DictCursor = object
    driver.connect = connect
    install("pymysql.cursors", driver.cursors)
    install("pymysql", driver)

    async_driver = ModuleType("aiomysql")
    async_driver.__path__ = []
    async_driver.cursors = ModuleType("aiomysql.cursors")
    async_driver.cursors.DictCursor = object

    async def async_connect(**kwargs):
        connect_args.append(kwargs)
        connection = _Connection()
        connections.append(connection)
        return connection

    async def create_pool(**kwargs):
        connect_args.append(kwargs)
        return _FakeAsyncPool(connections, kwargs)

    async_driver.connect = async_connect
    async_driver.create_pool = create_pool
    install("aiomysql.cursors", async_driver.cursors)
    install("aiomysql", async_driver)
    return counts, connections, connect_args, FakeAIOMySQLSaver


def test_mysql_config_uri_is_accepted_without_leaking_credentials_in_repr():
    runtime = create_mysql_checkpoint_runtime(
        "mysql://checkpoint-user:very-secret@db.internal:3307/lka_checkpoints"
    )

    assert isinstance(runtime, MySQLCheckpointRuntime)
    assert "very-secret" not in repr(runtime)
    assert runtime._connect_args == {
        "host": "db.internal",
        "port": 3307,
        "user": "checkpoint-user",
        "password": "very-secret",
        "database": "lka_checkpoints",
        "autocommit": True,
        "connect_timeout": 10,
        "charset": "utf8mb4",
        "init_command": (
            "SET SESSION innodb_lock_wait_timeout = 5, wait_timeout = 300, "
            "max_execution_time = 30000, time_zone = '+00:00'"
        ),
    }
    assert runtime._operation_timeout_seconds == 30
    assert runtime._idle_timeout_seconds == 300


def test_mysql_config_object_is_accepted_and_uri_parts_are_escaped():
    runtime = create_mysql_checkpoint_runtime(
        SimpleNamespace(
            host="127.0.0.1", port=3306, username="checkpoint user",
            password="p@ss/word", database="checkpoints", max_connections=2,
        )
    )

    assert runtime._connect_args["user"] == "checkpoint user"
    assert runtime._connect_args["password"] == "p@ss/word"
    assert runtime._connect_args["database"] == "checkpoints"
    assert runtime._max_connections == 2


def test_mysql_runtime_honors_storage_config_pool_and_timeout_fields():
    runtime = create_mysql_checkpoint_runtime(
        SimpleNamespace(
            host="db.internal", port=3306, user="svc", password="secret",
            database="lka", pool_size=8, pool_wait_seconds=2,
            connect_timeout=3, read_timeout=9, lock_wait_seconds=2,
            transaction_seconds=7.5,
        )
    )

    assert runtime._max_connections == 8
    assert runtime._pool_wait_seconds == 2
    assert runtime._connect_args["connect_timeout"] == 3
    assert runtime._connect_args["read_timeout"] == 9
    assert runtime._connect_args["init_command"] == (
        "SET SESSION innodb_lock_wait_timeout = 2, wait_timeout = 300, "
        "max_execution_time = 7500, time_zone = '+00:00'"
    )
    assert runtime._operation_timeout_seconds == 7.5
    assert "secret" not in repr(runtime)


def test_mysql_runtime_accepts_application_mysql_config():
    runtime = create_mysql_checkpoint_runtime(
        MySQLConfig(
            host="db.internal", port=3306, user="lka-checkpointer",
            password="private-password", database="lka", pool_size=3,
            pool_wait_seconds=2, connect_timeout=4, read_timeout=12,
            lock_wait_seconds=4, transaction_seconds=15,
        )
    )

    assert runtime._max_connections == 3
    assert runtime._pool_wait_seconds == 2
    assert runtime._connect_args["user"] == "lka-checkpointer"
    assert runtime._connect_args["connect_timeout"] == 4
    assert runtime._connect_args["read_timeout"] == 12
    assert runtime._connect_args["init_command"] == (
        "SET SESSION innodb_lock_wait_timeout = 4, wait_timeout = 300, "
        "max_execution_time = 15000, time_zone = '+00:00'"
    )
    assert "private-password" not in repr(runtime)


@pytest.mark.parametrize("uri", ["sqlite:///tmp/checkpoints.db", "mysql://user@host/"])
def test_mysql_runtime_rejects_invalid_uri(uri):
    with pytest.raises(ValueError):
        create_mysql_checkpoint_runtime(uri)


def test_sync_setup_is_thread_safe_and_only_runs_schema_setup_once(monkeypatch):
    counts, connections, connect_args, _async_saver = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")
    barrier = threading.Barrier(8)

    def setup():
        barrier.wait()
        runtime.setup()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: setup(), range(8)))

    assert counts["setup"] == 1
    assert counts["connect"] == 1
    assert connections[0].closed
    assert connect_args[0]["autocommit"] is True


def test_sync_saver_is_logical_and_does_not_hold_a_connection(monkeypatch):
    counts, connections, _args, _async_saver = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")

    with runtime.sync_saver() as saver:
        assert type(saver).__name__ == "PooledSaver"
        assert saver.conn is runtime._checkpoint_pool
        assert len(connections) == 1  # one-time schema setup only

    assert counts["setup"] == 1
    assert connections[0].closed
    runtime.close()
    with pytest.raises(RuntimeError, match="closed"), runtime.sync_saver():
        pass


def test_async_saver_is_logical_and_keeps_pool_loop_local(monkeypatch):
    counts, connections, args, async_saver_type = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")

    async def use_saver():
        async with runtime.async_saver() as saver:
            assert isinstance(saver, async_saver_type)
            assert saver.conn is runtime._checkpoint_pool
            assert len(connections) == 1  # setup only; pool minsize is zero

    asyncio.run(use_saver())

    assert len(connections) == 1
    assert connections[0].closed
    assert counts["setup"] == 1
    async_args = args[-1]
    assert async_args["db"] == "checkpoints"
    assert async_args["autocommit"] is True
    assert "read_timeout" not in async_args  # aiomysql.connect has no read_timeout argument.
    assert "innodb_lock_wait_timeout = 5" in async_args["init_command"]
    assert "wait_timeout = 300" in async_args["init_command"]
    assert "max_execution_time = 30000" in async_args["init_command"]
    assert "time_zone = '+00:00'" in async_args["init_command"]


def test_many_sync_logical_savers_borrow_only_during_cursor(monkeypatch):
    _counts, connections, _args, _async_saver = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")

    with ExitStack() as stack:
        savers = [stack.enter_context(runtime.sync_saver()) for _ in range(8)]
        assert len(connections) == 1  # schema setup; no saver holds a connection
        for saver in savers:
            with saver._cursor(pipeline=True):
                pass
            assert len(runtime._checkpoint_pool._sync_idle) == 1
    runtime.close()


def test_sync_pool_skips_rollback_only_for_known_clean_autocommit_connection(monkeypatch):
    _counts, _connections, _args, _async_saver = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")
    pool = runtime._checkpoint_pool

    with pool.sync_connection() as clean:
        assert clean.get_autocommit() is True
        assert clean.server_status == 0
    assert clean.rollback_count == 0

    with pool.sync_connection() as dirty:
        dirty.server_status |= 0x0001  # SERVER_STATUS_IN_TRANS
    assert dirty.rollback_count == 1
    assert dirty.server_status == 0

    with pool.sync_connection() as uncertain:
        del uncertain.server_status
    assert uncertain.rollback_count == 2  # dirty reset plus unknown-state fallback
    runtime.close()


def test_sync_pool_discards_connection_after_failed_operation(monkeypatch):
    _counts, connections, _args, _async_saver = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")
    pool = runtime._checkpoint_pool

    with (
        pytest.raises(ValueError, match="synthetic operation failure"),
        pool.sync_connection() as failed,
    ):
        raise ValueError("synthetic operation failure")

    assert failed.closed
    assert failed.rollback_count == 0
    assert pool._sync_created == 0
    with pool.sync_connection() as fresh:
        assert fresh is not failed
    assert len(connections) >= 2
    runtime.close()


def test_many_async_logical_savers_share_loop_pool_and_release_each_operation(monkeypatch):
    _counts, connections, _args, _async_saver = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")

    async def scenario():
        async with AsyncExitStack() as stack:
            savers = [await stack.enter_async_context(runtime.async_saver()) for _ in range(8)]
            pool = await runtime._checkpoint_pool._async_pool()
            assert len(connections) == 1  # schema setup only; pool is lazy

            async def operation(saver):
                async with saver._cursor(pipeline=True):
                    await asyncio.sleep(0.01)

            await asyncio.gather(*(operation(saver) for saver in savers))
            assert pool.maximum_active <= 4
            assert pool.active == 0
            assert runtime._checkpoint_pool._async_checkout_budget._active == 0
            await asyncio.sleep(0.01)  # external/model wait cannot retain a cursor lease
            assert pool.active == 0

    asyncio.run(scenario())
    runtime.close()


def test_async_cursor_cancellation_returns_connection_and_budget(monkeypatch):
    _counts, _connections, _args, _async_saver = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")

    async def scenario():
        async with runtime.async_saver() as saver:
            pool = await runtime._checkpoint_pool._async_pool()
            stopped_loop = asyncio.new_event_loop()
            other_pool = SimpleNamespace(terminated=False)
            other_pool.terminate = lambda: setattr(other_pool, "terminated", True)

            async def blocked():
                async with saver._cursor():
                    # Model a second loop pool being registered while this
                    # operation holds the shared permit.
                    runtime._checkpoint_pool._async_pools[stopped_loop] = other_pool
                    await asyncio.Event().wait()

            task = asyncio.create_task(blocked())
            while pool.active == 0:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert pool.active == 0
            assert runtime._checkpoint_pool._async_checkout_budget._active == 0
            assert pool.clear_calls >= 1
            assert other_pool.terminated
            assert stopped_loop not in runtime._checkpoint_pool._async_pools
            stopped_loop.close()

    asyncio.run(scenario())
    runtime.close()


def test_async_connection_failure_still_prunes_cross_loop_idle_pools(monkeypatch):
    _counts, _connections, _args, _async_saver = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")

    async def scenario():
        pool = await runtime._checkpoint_pool._async_pool()
        stopped_loop = asyncio.new_event_loop()
        other_pool = SimpleNamespace(terminated=False)
        other_pool.terminate = lambda: setattr(other_pool, "terminated", True)
        with pytest.raises(ValueError, match="synthetic failure"):
            async with runtime._checkpoint_pool.async_connection():
                runtime._checkpoint_pool._async_pools[stopped_loop] = other_pool
                raise ValueError("synthetic failure")
        assert pool.clear_calls == 1
        assert other_pool.terminated
        assert stopped_loop not in runtime._checkpoint_pool._async_pools
        assert runtime._checkpoint_pool._async_checkout_budget._active == 0
        stopped_loop.close()

    asyncio.run(scenario())
    runtime.close()


def test_async_checkpoint_pools_are_not_shared_between_event_loops(monkeypatch):
    _counts, _connections, _args, _async_saver = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")
    pools = []

    async def get_pool():
        pools.append(await runtime._checkpoint_pool._async_pool())

    asyncio.run(get_pool())
    asyncio.run(get_pool())

    assert pools[0] is not pools[1]
    assert pools[0].closed  # stopped-loop pool is closed before the next is made
    runtime.close()


def test_async_saver_cursor_operation_has_a_finite_deadline(monkeypatch):
    _counts, connections, _args, async_saver_type = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")
    runtime._operation_timeout_seconds = 0.01

    async def use_saver():
        async with runtime.async_saver() as saver:
            assert isinstance(saver, async_saver_type)
            with pytest.raises(TimeoutError):
                async with saver._cursor():
                    await asyncio.sleep(0.05)

    asyncio.run(use_saver())
    assert connections[-1].closed


def test_async_setup_does_not_block_event_loop(monkeypatch):
    _counts, _connections, _args, async_saver = _install_fake_mysql(monkeypatch)
    runtime = create_mysql_checkpoint_runtime("mysql://user:pw@host/checkpoints")

    async def slow_setup(self):
        await asyncio.sleep(0.05)

    async_saver.setup = slow_setup

    async def verify_responsive() -> None:
        started = time.monotonic()
        task = asyncio.create_task(runtime.asetup())
        await asyncio.sleep(0.005)
        assert time.monotonic() - started < 0.04
        await task

    asyncio.run(verify_responsive())


def test_sqlite_checkpoint_factory_remains_sqlite(tmp_path):
    runtime = create_sqlite_checkpoint_runtime(tmp_path / "checkpoints.sqlite3")

    assert isinstance(runtime, SqliteCheckpointRuntime)
    assert runtime.path == tmp_path / "checkpoints.sqlite3"


def test_mysql_savers_implement_installed_langgraph_interface():
    pytest.importorskip("langgraph.checkpoint.mysql.pymysql")
    pytest.importorskip("langgraph.checkpoint.mysql.aio")
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.checkpoint.mysql.aio import AIOMySQLSaver
    from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

    assert issubclass(PyMySQLSaver, BaseCheckpointSaver)
    assert issubclass(AIOMySQLSaver, BaseCheckpointSaver)
