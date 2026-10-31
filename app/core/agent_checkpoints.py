"""Local checkpoint lifecycle for LangGraph Agent runs."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.storage.mysql_checkpoint_pool import MySQLCheckpointPool
from app.storage.sqlite_timing import connect_sqlite


class SqliteCheckpointRuntime:
    """Open short-lived savers over one durable local checkpoint database."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._setup_lock = threading.Lock()
        self._setup_complete = False

    @asynccontextmanager
    async def async_saver(self) -> AsyncIterator[AsyncSqliteSaver]:
        # Use aiosqlite's connector so opening and operations in its worker are timed.
        async with aiosqlite.Connection(lambda: connect_sqlite(str(self.path)), iter_chunk_size=64) as conn:
            saver = AsyncSqliteSaver(conn)
            if not self._setup_complete:
                # Sync and async savers can initialize the same runtime. Do not
                # block the event loop behind another initializer's thread lock.
                while not self._setup_lock.acquire(blocking=False):
                    await asyncio.sleep(0.01)
                try:
                    if not self._setup_complete:
                        await saver.setup()
                        self._setup_complete = True
                finally:
                    self._setup_lock.release()
            # The upstream flag is per saver, but WAL and these tables persist
            # across connections. Keep normal write transactions/commits intact.
            saver.is_setup = True
            yield saver

    @contextmanager
    def sync_saver(self) -> Iterator[SqliteSaver]:
        with closing(connect_sqlite(str(self.path), check_same_thread=False)) as conn:
            saver = SqliteSaver(conn)
            if not self._setup_complete:
                with self._setup_lock:
                    if not self._setup_complete:
                        saver.setup()
                        self._setup_complete = True
            saver.is_setup = True
            yield saver

    def close(self) -> None:
        """Saver scopes own connections; this runtime keeps no idle connection.

        A graph invocation can retain its saver connection across model waiting,
        but each checkpoint write still commits within the saver operation.
        """


def create_sqlite_checkpoint_runtime(path: Path) -> SqliteCheckpointRuntime:
    return SqliteCheckpointRuntime(path)


class MySQLCheckpointRuntime:
    """Open bounded, short-lived MySQL checkpoint savers.

    Schema creation is process-local and idempotently guarded. Applications
    should call ``setup``/``asetup`` during startup; saver contexts also guard
    it as a fallback for direct users and tests. Credentials are deliberately
    excluded from the object's representation.
    """

    def __init__(self, config_or_uri: object, *, max_connections: int | None = None) -> None:
        import asyncio

        self._asyncio = asyncio
        uri = self._connection_uri(config_or_uri)
        connect_args = self._parse_connect_args(uri)
        self._operation_timeout_seconds = float(connect_args.pop("_operation_timeout_seconds"))
        self._idle_timeout_seconds = int(connect_args.pop("_idle_timeout_seconds"))
        self._connect_args = connect_args
        configured_max = max_connections or self._config_value(
            config_or_uri,
            "max_connections",
            self._config_value(config_or_uri, "pool_size", 4),
        )
        self._max_connections = max(1, min(int(configured_max), 32))
        self._pool_wait_seconds = max(
            0.01,
            float(self._config_value(config_or_uri, "pool_wait_seconds", 5.0)),
        )
        # Logical saver objects share a bounded connection pool. Connections
        # are checked out only for individual checkpoint cursor transactions.
        self._checkpoint_pool = MySQLCheckpointPool(
            self._connect_args,
            sync_size=4,
            async_size=4,
            pool_wait_seconds=self._pool_wait_seconds,
            idle_timeout_seconds=self._idle_timeout_seconds,
        )
        self._setup_lock = __import__("threading").Lock()
        self._setup_complete = False
        self._closed = False
        self._owner = None

    def __repr__(self) -> str:
        return f"<MySQLCheckpointRuntime max_connections={self._max_connections}>"

    @staticmethod
    def _config_value(config: object, name: str, default: object = None) -> object:
        value = getattr(config, name, default)
        if callable(getattr(value, "get_secret_value", None)):
            return value.get_secret_value()
        return value

    @classmethod
    def _connection_uri(cls, config: object) -> str:
        from urllib.parse import quote, urlencode

        if isinstance(config, str):
            return config
        for name in ("connection_uri", "uri", "dsn"):
            uri = cls._config_value(config, name)
            if uri:
                return str(uri)
        host = str(cls._config_value(config, "host", "localhost"))
        port = int(cls._config_value(config, "port", 3306))
        user = cls._config_value(config, "username", cls._config_value(config, "user"))
        password = cls._config_value(config, "password", "")
        database = cls._config_value(config, "database", cls._config_value(config, "db"))
        if not user or not database:
            raise ValueError("MySQL checkpoint config requires username and database.")
        query: dict[str, str] = {}
        socket = cls._config_value(config, "unix_socket")
        if socket:
            query["unix_socket"] = str(socket)
        timeout = cls._config_value(config, "connect_timeout")
        if timeout is not None:
            query["connect_timeout"] = str(int(timeout))
        read_timeout = cls._config_value(config, "read_timeout")
        if read_timeout is not None:
            query["read_timeout"] = str(int(read_timeout))
        lock_wait_seconds = cls._config_value(config, "lock_wait_seconds")
        if lock_wait_seconds is not None:
            query["lock_wait_seconds"] = str(int(lock_wait_seconds))
        transaction_seconds = cls._config_value(config, "transaction_seconds")
        if transaction_seconds is not None:
            query["transaction_seconds"] = str(float(transaction_seconds))
        auth = quote(str(user), safe="")
        if password:
            auth += ":" + quote(str(password), safe="")
        suffix = "?" + urlencode(query) if query else ""
        return f"mysql://{auth}@{host}:{port}/{quote(str(database), safe='')}{suffix}"

    @classmethod
    def _parse_connect_args(cls, uri: str) -> dict[str, object]:
        import math
        from urllib.parse import parse_qsl, unquote, urlsplit

        parsed = urlsplit(uri)
        if parsed.scheme not in {"mysql", "mysql+pymysql", "mysql+aiomysql"}:
            raise ValueError("MySQL checkpoint URI must use mysql://.")
        database = unquote(parsed.path.lstrip("/"))
        if not parsed.hostname or not parsed.username or not database:
            raise ValueError("MySQL checkpoint URI requires host, username, and database.")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        try:
            lock_wait_seconds = int(query.get("lock_wait_seconds", "5"))
            transaction_seconds = float(query.get("transaction_seconds", "30"))
        except (TypeError, ValueError) as exc:
            raise ValueError("MySQL checkpoint timeout settings must be numeric") from exc
        if not 1 <= lock_wait_seconds <= 3600:
            raise ValueError("MySQL checkpoint lock_wait_seconds must be between 1 and 3600")
        if not math.isfinite(transaction_seconds) or not 0 < transaction_seconds <= 86400:
            raise ValueError("MySQL checkpoint transaction_seconds must be in (0, 86400]")
        operation_timeout = transaction_seconds
        # Saver connections span graph execution, including model calls. Keep
        # idle expiry above the default 180s model request timeout, while still
        # bounding a connection/transaction abandoned by a dead worker.
        idle_timeout = max(300, math.ceil(operation_timeout))
        init_command = (
            "SET SESSION innodb_lock_wait_timeout = " + str(lock_wait_seconds)
            + ", wait_timeout = " + str(idle_timeout)
            + ", max_execution_time = " + str(max(1, math.ceil(transaction_seconds * 1000)))
            + ", time_zone = '+00:00'"
        )
        args: dict[str, object] = {
            "host": parsed.hostname,
            "port": parsed.port or 3306,
            "user": unquote(parsed.username),
            "password": unquote(parsed.password or ""),
            "database": database,
            "autocommit": True,
            "connect_timeout": int(query.get("connect_timeout", "10")),
            "charset": query.get("charset", "utf8mb4"),
            "init_command": init_command,
            "_operation_timeout_seconds": operation_timeout,
            "_idle_timeout_seconds": idle_timeout,
        }
        if query.get("read_timeout") is not None:
            args["read_timeout"] = int(query["read_timeout"])
        if query.get("unix_socket"):
            args["unix_socket"] = query["unix_socket"]
        return args

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("MySQL checkpoint runtime is closed.")

    def set_coordinator_owner(self, owner) -> None:
        self._owner = owner

    def _sync_saver_type(self):
        from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

        owner = self._owner
        runtime = self

        class PooledSaver(PyMySQLSaver):
            @contextmanager
            def _cursor(self, *, pipeline=False):
                transactional = bool(pipeline or owner is not None)
                with self.lock, runtime._checkpoint_pool.sync_connection() as connection:
                    if transactional:
                        connection.begin()
                    try:
                        with self._get_cursor_from_connection(connection) as cursor:
                            if owner is not None:
                                owner.guard_checkpoint_cursor(cursor)
                            yield cursor
                        if transactional:
                            connection.commit()
                    except BaseException:
                        if transactional:
                            connection.rollback()
                        raise

        return PooledSaver

    def _async_saver_type(self):
        from langgraph.checkpoint.mysql.aio import AIOMySQLSaver

        owner = self._owner
        runtime = self

        class BoundedSaver(AIOMySQLSaver):
            @asynccontextmanager
            async def _cursor(self, *, pipeline=False):
                timeout = runtime._operation_timeout_seconds
                loop = runtime._asyncio.get_running_loop()
                deadline = loop.time() + timeout
                deadline_reached = False
                connection = None
                close_handle = None

                def close_connection_at_deadline() -> None:
                    nonlocal deadline_reached
                    deadline_reached = True
                    if connection is not None:
                        # Close the checked-out socket to interrupt driver I/O;
                        # aiomysql discards it when the lease is returned.
                        connection.close()

                try:
                    async with (
                        runtime._asyncio.timeout(timeout),
                        self.lock,
                        runtime._checkpoint_pool.async_connection() as connection,
                    ):
                        # Avoid holding a pool lease while queued on this
                        # saver instance's own operation lock.
                        close_handle = loop.call_later(
                            max(0.0, deadline - loop.time()),
                            close_connection_at_deadline,
                        )
                        transactional = bool(pipeline or owner is not None)
                        if transactional:
                            await connection.begin()
                        try:
                            async with self._get_cursor_from_connection(connection) as cursor:
                                if owner is not None:
                                    await owner.guard_async_checkpoint_cursor(cursor)
                                yield cursor
                            if transactional:
                                await connection.commit()
                        except BaseException:
                            if transactional:
                                await connection.rollback()
                            raise
                except BaseException as exc:
                    if deadline_reached:
                        if connection is not None:
                            connection.close()
                        raise TimeoutError(
                            "MySQL checkpoint cursor operation exceeded its deadline."
                        ) from exc
                    raise
                finally:
                    if close_handle is not None:
                        close_handle.cancel()

        return BoundedSaver

    def setup(self) -> None:
        """Create or migrate checkpoint tables once, outside graph-node calls."""
        self._ensure_open()
        if self._setup_complete:
            return
        with self._setup_lock:
            if self._setup_complete:
                return
            from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

            with __import__("pymysql").connect(**self._connect_args) as connection:
                PyMySQLSaver(connection).setup()
            self._setup_complete = True

    def verify_schema(self) -> None:
        """Verify an installed schema without giving the service DDL authority."""
        self._ensure_open()
        if self._setup_complete:
            return
        with self._setup_lock:
            if self._setup_complete:
                return
            import pymysql
            from langgraph.checkpoint.mysql.base import MIGRATIONS

            with pymysql.connect(**self._connect_args) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT MAX(v) FROM checkpoint_migrations")
                row = cursor.fetchone()
                if not row or row[0] != len(MIGRATIONS) - 1:
                    raise RuntimeError("MySQL checkpoints require the explicit schema migration command")
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    cursor.execute(f"SELECT 1 FROM `{table}` LIMIT 0")
            self._setup_complete = True

    async def asetup(self) -> None:
        """Create or migrate checkpoint tables without blocking the event loop."""
        self._ensure_open()
        if self._setup_complete:
            return
        while not self._setup_lock.acquire(blocking=False):
            await self._asyncio.sleep(0.01)
        try:
            if self._setup_complete:
                return
            import aiomysql
            from langgraph.checkpoint.mysql.aio import AIOMySQLSaver

            args = dict(self._connect_args)
            args["db"] = args.pop("database")
            args.pop("read_timeout", None)
            connection = await self._asyncio.wait_for(
                aiomysql.connect(**args), timeout=self._operation_timeout_seconds,
            )
            try:
                await self._asyncio.wait_for(
                    AIOMySQLSaver(connection).setup(), timeout=self._operation_timeout_seconds,
                )
            finally:
                connection.close()
            self._setup_complete = True
        finally:
            self._setup_lock.release()

    @contextmanager
    def sync_saver(self) -> Iterator[object]:
        self._ensure_open()
        self.setup()
        yield self._sync_saver_type()(self._checkpoint_pool)

    @asynccontextmanager
    async def async_saver(self) -> AsyncIterator[object]:
        self._ensure_open()
        await self.asetup()
        await self._checkpoint_pool._async_pool()
        yield self._async_saver_type()(self._checkpoint_pool)

    def close(self) -> None:
        """Prevent new connections; active saver scopes close their own handles."""
        self._closed = True
        self._checkpoint_pool.close()

    async def aclose(self) -> None:
        """Await async pool socket shutdown from an async application lifespan."""
        self._closed = True
        await self._checkpoint_pool.aclose()


def create_mysql_checkpoint_runtime(config_or_uri: object) -> MySQLCheckpointRuntime:
    """Create a MySQL checkpoint runtime without exposing credentials in repr."""
    return MySQLCheckpointRuntime(config_or_uri)
