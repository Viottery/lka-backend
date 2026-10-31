"""Low-cardinality, in-memory storage metrics for isolated workload comparisons.

Importing this file has no application side effects. Call ``install`` after
application startup, then bracket only the measurement window with ``start``
and ``stop``. SQL text and bind values are inspected only to derive a fixed
operation label and are never retained.
"""
from __future__ import annotations

import collections
import contextlib
import os
import pathlib
import sqlite3
import threading
import time
import weakref

_BUCKETS_MS = (0.1, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
_ROLES = frozenset({"main_db", "checkpoint_db", "aux_db"})
_VIEWS = frozenset({"sqlite_timing", "mysql_dbapi", "sqlite_trace", "mysql_com_query", "pool"})
_PHASES = frozenset({
    "execute", "fetch", "script", "connect", "app_call", "driver_sql", "queue_wait",
    "transaction", "commit", "rollback", "pool_checkout", "close", "other", "acquire_write",
})
_OPERATIONS = frozenset({
    "SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "WITH", "PRAGMA",
    "BEGIN", "START", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE",
    "CREATE", "ALTER", "DROP", "TRUNCATE", "SET", "OTHER",
})


def _operation(statement) -> str:
    """Return a fixed SQL verb label without retaining the statement."""
    if isinstance(statement, bytes):
        statement = statement[:128].decode("ascii", errors="ignore")
    elif isinstance(statement, str):
        statement = statement[:128]
    else:
        return "OTHER"
    parts = statement.lstrip().split(None, 1)
    verb = parts[0].rstrip(";`).").upper() if parts else "OTHER"
    return verb if verb in _OPERATIONS else "OTHER"


def _role_from_path(value) -> str:
    try:
        name = pathlib.Path(os.fspath(value).split("?", 1)[0]).name.lower()
    except (TypeError, ValueError):
        return "aux_db"
    if name in {"lka.sqlite3", "lka.sqlite", "lka.db"}:
        return "main_db"
    if name in {"agent_checkpoints.sqlite3", "checkpoints.sqlite3"}:
        return "checkpoint_db"
    return "aux_db"


class _Aggregate:
    __slots__ = ("buckets", "count", "errors", "max_ms", "total_ms")

    def __init__(self):
        self.count = 0
        self.errors = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.buckets = [0] * (len(_BUCKETS_MS) + 1)

    def add(self, elapsed_ms=0.0, *, error=False):
        elapsed_ms = max(0.0, float(elapsed_ms))
        self.count += 1
        self.errors += bool(error)
        self.total_ms += elapsed_ms
        self.max_ms = max(self.max_ms, elapsed_ms)
        for index, boundary in enumerate(_BUCKETS_MS):
            if elapsed_ms <= boundary:
                self.buckets[index] += 1
                break
        else:
            self.buckets[-1] += 1

    def as_dict(self):
        def percentile(p):
            if not self.count:
                return None
            target = self.count * p / 100
            seen = 0
            for index, value in enumerate(self.buckets):
                seen += value
                if seen >= target:
                    return _BUCKETS_MS[index] if index < len(_BUCKETS_MS) else None

        return {
            "count": self.count,
            "errors": self.errors,
            "total_ms": round(self.total_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "p50_upper_ms": percentile(50),
            "p95_upper_ms": percentile(95),
            "p99_upper_ms": percentile(99),
            "bucket_bounds_ms": [*_BUCKETS_MS, None],
            "bucket_counts": list(self.buckets),
        }


class Probe:
    """Runtime hooks with fixed metric dimensions and no SQL/body storage."""

    def __init__(self, data_dir):
        self.data_dir = pathlib.Path(data_dir)
        self._lock = threading.RLock()
        self._active = False
        self._closed = False
        self._events = collections.defaultdict(_Aggregate)
        self._leases = {role: {"acquired": 0, "released": 0, "active": 0, "peak": 0}
                        for role in sorted(_ROLES)}
        self._raw_roles = {}
        # Observing a connection must not keep it alive past normal GC.
        self._sqlite_connections = weakref.WeakValueDictionary()
        self._sqlite_finalizers = {}
        self._window_token = None
        self._config_roles = []
        self._connections_module = None
        self._patches = []
        self._executemany_local = threading.local()

    @staticmethod
    def _safe_role(role):
        return role if role in _ROLES else "aux_db"

    def _record(self, view, role, phase, operation="OTHER", elapsed_ms=0.0, *, error=False):
        if not self._active:
            return
        role = self._safe_role(role)
        view = view if view in _VIEWS else "pool"
        phase = phase if phase in _PHASES else "other"
        operation = operation if operation in _OPERATIONS else "OTHER"
        with self._lock:
            self._events[(view, role, phase, operation)].add(elapsed_ms, error=error)

    def _lease(self, role, delta):
        if not self._active:
            return
        role = self._safe_role(role)
        with self._lock:
            stats = self._leases[role]
            if delta > 0:
                stats["acquired"] += 1
                stats["active"] += 1
                stats["peak"] = max(stats["peak"], stats["active"])
            elif stats["active"] > 0:
                stats["released"] += 1
                stats["active"] -= 1

    def _patch_method(self, target, name, make_wrapper):
        original = getattr(target, name)
        wrapper = make_wrapper(original)
        setattr(target, name, wrapper)
        self._patches.append((target, name, wrapper, original))
        return original

    def _role_for_config(self, config):
        targets = getattr(self._connections_module, "_targets", {})
        for path, configured in targets.items():
            if configured == config:
                return _role_from_path(path)
        for configured, role in self._config_roles:
            if configured == config:
                return role
        return "aux_db"

    def _install_sqlite(self):
        try:
            from app.storage import sqlite_timing
            from app.storage.sqlite_write_queue import QueuedConnection
        except ImportError:
            return

        if hasattr(sqlite_timing, "TimingRecorder"):
            def timing_wrapper(original):
                def record(recorder, *, database, phase, operation, statement_id,
                           elapsed_ms, error, details=None):
                    role = {"main": "main_db", "checkpoints": "checkpoint_db"}.get(database, "aux_db")
                    self._record("sqlite_timing", role, phase,
                                 operation if operation in _OPERATIONS else "OTHER", elapsed_ms,
                                 error=error is not None)
                    return original(recorder, database=database, phase=phase, operation=operation,
                                    statement_id=statement_id, elapsed_ms=elapsed_ms, error=error,
                                    details=details)
                return record
            self._patch_method(sqlite_timing.TimingRecorder, "record", timing_wrapper)

        def connect_wrapper(original):
            def connect(database, *args, **kwargs):
                role = _role_from_path(database)
                started = time.perf_counter()
                connection = original(database, *args, **kwargs)
                if isinstance(connection, QueuedConnection):
                    connection._lka_metrics_role = role
                    connection._lka_metrics_tracked = self._active
                    token = self._window_token if self._active else None
                    connection._lka_metrics_window = token
                    if self._active:
                        self._lease(role, 1)
                self._record("sqlite_trace", role, "connect", "OTHER",
                             (time.perf_counter() - started) * 1000)
                try:
                    connection.set_trace_callback(
                        lambda statement, _role=role: self._record(
                            "sqlite_trace", _role, "driver_sql", _operation(statement), 0.0,
                        )
                    )
                    if isinstance(connection, QueuedConnection):
                        identity = id(connection)
                        with self._lock:
                            self._sqlite_connections[identity] = connection
                            self._sqlite_finalizers[identity] = weakref.finalize(
                                connection, self._sqlite_collected, identity, role, token,
                            )
                except sqlite3.Error:
                    self._record("sqlite_trace", role, "driver_sql", "OTHER", error=True)
                return connection
            return connect
        self._patch_method(sqlite_timing.sqlite3, "connect", connect_wrapper)

        def close_wrapper(original):
            def close(connection, *args, **kwargs):
                role = getattr(connection, "_lka_metrics_role", "aux_db")
                was_open = not getattr(connection, "_closed_by_wrapper", False)
                was_tracked = (getattr(connection, "_lka_metrics_window", None) is self._window_token)
                started = time.perf_counter()
                with self._lock:
                    self._sqlite_connections.pop(id(connection), None)
                    finalizer = self._sqlite_finalizers.pop(id(connection), None)
                    if finalizer is not None:
                        finalizer.detach()
                try:
                    connection.set_trace_callback(None)
                except sqlite3.Error:
                    self._record("sqlite_trace", role, "close", "OTHER", error=True)
                result = original(connection, *args, **kwargs)
                if was_open and was_tracked:
                    self._lease(role, -1)
                    self._record("sqlite_timing", role, "close", "OTHER",
                                 (time.perf_counter() - started) * 1000)
                return result
            return close
        if hasattr(QueuedConnection, "close"):
            self._patch_method(QueuedConnection, "close", close_wrapper)

    def _sqlite_collected(self, identity, role, token):
        with self._lock:
            self._sqlite_finalizers.pop(identity, None)
            if token is not None and token is self._window_token:
                self._lease(role, -1)

    def _install_mysql(self):
        try:
            from app.storage import connections, mysql
        except ImportError:
            return
        self._connections_module = connections

        for path, config in getattr(connections, "_targets", {}).items():
            self._config_roles.append((config, _role_from_path(path)))

        def pool_acquire_wrapper(original):
            def acquire(pool):
                role = self._role_for_config(pool.config)
                started = time.perf_counter()
                error = False
                try:
                    return original(pool)
                except BaseException:
                    error = True
                    raise
                finally:
                    if self._active:
                        # Includes connect/ping when a physical socket is new or stale.
                        self._record("pool", role, "pool_checkout", "OTHER",
                                     (time.perf_counter() - started) * 1000, error=error)
            return acquire
        self._patch_method(mysql._Pool, "acquire", pool_acquire_wrapper)

        def execute_wrapper(original):
            def execute(cursor, statement, parameters=()):
                if getattr(self._executemany_local, "depth", 0):
                    return original(cursor, statement, parameters)
                role = self._raw_roles.get(id(cursor.connection._raw), "main_db")
                operation = _operation(statement)
                started = time.perf_counter()
                error = False
                try:
                    return original(cursor, statement, parameters)
                except BaseException:
                    error = True
                    raise
                finally:
                    self._record("mysql_dbapi", role, "execute", operation,
                                 (time.perf_counter() - started) * 1000, error=error)
            return execute
        self._patch_method(mysql.MySQLCursor, "execute", execute_wrapper)

        def executemany_wrapper(original):
            def executemany(cursor, statement, parameters):
                role = self._raw_roles.get(id(cursor.connection._raw), "main_db")
                operation = _operation(statement)
                started = time.perf_counter()
                error = False
                self._executemany_local.depth = getattr(self._executemany_local, "depth", 0) + 1
                try:
                    return original(cursor, statement, parameters)
                except BaseException:
                    error = True
                    raise
                finally:
                    self._executemany_local.depth -= 1
                    self._record("mysql_dbapi", role, "executemany", operation,
                                 (time.perf_counter() - started) * 1000, error=error)
            return executemany
        self._patch_method(mysql.MySQLCursor, "executemany", executemany_wrapper)

        def commit_wrapper(original):
            def commit(connection):
                role = self._raw_roles.get(id(connection._raw), "aux_db")
                had_transaction = connection.in_transaction
                transaction_started = getattr(connection, "_lka_metrics_transaction_started", None)
                started = time.perf_counter()
                error = False
                try:
                    return original(connection)
                except BaseException:
                    error = True
                    raise
                finally:
                    elapsed = (time.perf_counter() - started) * 1000
                    self._record("mysql_dbapi", role, "commit", "COMMIT", elapsed, error=error)
                    if had_transaction and transaction_started is not None:
                        self._record("mysql_dbapi", role, "transaction", "BEGIN",
                                     (time.perf_counter() - transaction_started) * 1000,
                                     error=error)
                    connection._lka_metrics_transaction_started = None
            return commit
        self._patch_method(mysql.MySQLConnection, "commit", commit_wrapper)

        def rollback_wrapper(original):
            def rollback(connection):
                role = self._raw_roles.get(id(connection._raw), "aux_db")
                had_transaction = connection.in_transaction
                transaction_started = getattr(connection, "_lka_metrics_transaction_started", None)
                started = time.perf_counter()
                error = False
                try:
                    return original(connection)
                except BaseException:
                    error = True
                    raise
                finally:
                    elapsed = (time.perf_counter() - started) * 1000
                    self._record("mysql_dbapi", role, "rollback", "ROLLBACK", elapsed, error=error)
                    if had_transaction and transaction_started is not None:
                        self._record("mysql_dbapi", role, "transaction", "BEGIN",
                                     (time.perf_counter() - transaction_started) * 1000,
                                     error=True)
                    connection._lka_metrics_transaction_started = None
            return rollback
        self._patch_method(mysql.MySQLConnection, "rollback", rollback_wrapper)

        def observe_transaction(connection, was_active):
            if (self._active and not was_active and connection.in_transaction
                    and getattr(connection, "_lka_metrics_transaction_started", None) is None):
                connection._lka_metrics_transaction_started = time.perf_counter()

        def connect_wrapper(original):
            def connect(config, *, read_only=False):
                role = self._role_for_config(config)
                connection = original(config, read_only=read_only)
                raw_id = id(connection._raw)
                self._raw_roles[raw_id] = role
                connection._lka_metrics_transaction_started = None
                connection._lka_metrics_tracked = self._active
                if self._active:
                    self._lease(role, 1)
                return connection
            return connect
        self._patch_method(mysql, "connect_mysql", connect_wrapper)

        # Explicit BEGIN and implicit first-DML transactions both pass through
        # MySQLCursor.execute; observe their logical state transition once.
        execute_hook = mysql.MySQLCursor.execute
        def transaction_aware_execute(cursor, statement, parameters=()):
            was_active = cursor.connection.in_transaction
            try:
                return execute_hook(cursor, statement, parameters)
            finally:
                observe_transaction(cursor.connection, was_active)
        self._patch_method(mysql.MySQLCursor, "execute", lambda original: transaction_aware_execute)

        executemany_hook = mysql.MySQLCursor.executemany
        def transaction_aware_executemany(cursor, statement, parameters):
            was_active = cursor.connection.in_transaction
            try:
                return executemany_hook(cursor, statement, parameters)
            finally:
                observe_transaction(cursor.connection, was_active)
        self._patch_method(mysql.MySQLCursor, "executemany", lambda original: transaction_aware_executemany)

        def close_wrapper(original):
            def close(connection):
                was_open = not connection._closed
                role = self._raw_roles.get(id(connection._raw), "aux_db")
                transaction_started = getattr(connection, "_lka_metrics_transaction_started", None)
                try:
                    return original(connection)
                finally:
                    if was_open:
                        self._raw_roles.pop(id(connection._raw), None)
                        if getattr(connection, "_lka_metrics_tracked", False):
                            self._lease(role, -1)
                        if transaction_started is not None:
                            self._record("mysql_dbapi", role, "transaction", "BEGIN",
                                         (time.perf_counter() - transaction_started) * 1000,
                                         error=True)
                        connection._lka_metrics_transaction_started = None
            return close
        self._patch_method(mysql.MySQLConnection, "close", close_wrapper)

        try:
            import pymysql
            from pymysql.constants import COMMAND

            def wire_wrapper(original):
                def execute_command(connection, command, sql):
                    if command != COMMAND.COM_QUERY:
                        return original(connection, command, sql)
                    role = self._raw_roles.get(id(connection), "main_db")
                    operation = _operation(sql)
                    started = time.perf_counter()
                    error = False
                    try:
                        return original(connection, command, sql)
                    except BaseException:
                        error = True
                        raise
                    finally:
                        self._record("mysql_com_query", role, "driver_sql", operation,
                                     (time.perf_counter() - started) * 1000, error=error)
                return execute_command
            self._patch_method(pymysql.connections.Connection, "_execute_command", wire_wrapper)

            try:
                import aiomysql
                async_connection = aiomysql.connection.Connection
            except ImportError:
                async_connection = None
            if async_connection is not None:
                def async_wire_wrapper(original):
                    async def execute_command(connection, command, sql):
                        if command != COMMAND.COM_QUERY:
                            return await original(connection, command, sql)
                        role = self._raw_roles.get(id(connection), "checkpoint_db")
                        operation = _operation(sql)
                        started = time.perf_counter()
                        error = False
                        try:
                            return await original(connection, command, sql)
                        except BaseException:
                            error = True
                            raise
                        finally:
                            self._record("mysql_com_query", role, "driver_sql", operation,
                                         (time.perf_counter() - started) * 1000, error=error)
                    return execute_command
                self._patch_method(async_connection, "_execute_command", async_wire_wrapper)
        except ImportError:
            pass

        try:
            from app.storage.mysql_checkpoint_pool import MySQLCheckpointPool
        except ImportError:
            return

        def sync_context_wrapper(original):
            @contextlib.contextmanager
            def sync_connection(pool):
                started = time.perf_counter()
                try:
                    with original(pool) as raw:
                        role = "checkpoint_db"
                        tracked = self._active
                        self._raw_roles[id(raw)] = role
                        if tracked:
                            self._lease(role, 1)
                            self._record("pool", role, "pool_checkout", "OTHER",
                                         (time.perf_counter() - started) * 1000)
                        try:
                            yield raw
                        finally:
                            self._raw_roles.pop(id(raw), None)
                            if tracked:
                                self._lease(role, -1)
                except BaseException:
                    self._record("pool", "checkpoint_db", "pool_checkout", "OTHER",
                                 (time.perf_counter() - started) * 1000, error=True)
                    raise
            return sync_connection
        self._patch_method(MySQLCheckpointPool, "sync_connection", sync_context_wrapper)

        def async_context_wrapper(original):
            @contextlib.asynccontextmanager
            async def async_connection(pool):
                started = time.perf_counter()
                try:
                    async with original(pool) as raw:
                        role = "checkpoint_db"
                        tracked = self._active
                        self._raw_roles[id(raw)] = role
                        if tracked:
                            self._lease(role, 1)
                            self._record("pool", role, "pool_checkout", "OTHER",
                                         (time.perf_counter() - started) * 1000)
                        try:
                            yield raw
                        finally:
                            self._raw_roles.pop(id(raw), None)
                            if tracked:
                                self._lease(role, -1)
                except BaseException:
                    self._record("pool", "checkpoint_db", "pool_checkout", "OTHER",
                                 (time.perf_counter() - started) * 1000, error=True)
                    raise
            return async_connection
        self._patch_method(MySQLCheckpointPool, "async_connection", async_context_wrapper)

    def start(self):
        if self._closed:
            raise RuntimeError("workload metrics probe is stopped")
        with self._lock:
            self._window_token = object()
            self._events.clear()
            self._leases = {role: {"acquired": 0, "released": 0, "active": 0, "peak": 0}
                            for role in sorted(_ROLES)}
            self._active = True
        return self

    def snapshot(self):
        with self._lock:
            events = [
                {"view": key[0], "role": key[1], "phase": key[2], "operation": key[3],
                 **stats.as_dict()}
                for key, stats in sorted(self._events.items())
            ]
            leases = {role: dict(values) for role, values in self._leases.items()}
        return {
            "events": events,
            "leases": leases,
            "metric_contract": {
                "sqlite_timing": "SQLite TimingRecorder operations; compare its execute phase only with mysql_dbapi execute calls, never add fetch/connect/close phases.",
                "mysql_dbapi": "MySQL wrapper execute/executemany and transaction methods; execute and executemany are separate application call counts.",
                "driver_sql": "sqlite_trace counts SQLite trace callbacks; mysql_com_query counts MySQL COM_QUERY calls. They are full driver statement counts for their respective drivers, but not identical protocol work units.",
                "pool_checkout": "Lease acquisition wall time; includes connect/ping when a new or stale socket is used.",
                "leases": "Checked-out application leases; this is not a physical socket count.",
                "privacy": "No SQL text, bind values, result rows, exception messages, database names, or credentials are retained.",
                "window": "Call start after startup/warmup. Counts are complete during the active window; histograms use fixed buckets.",
            },
        }

    def stop(self):
        if self._closed:
            return self.snapshot()
        with self._lock:
            self._active = False
            connections = tuple(self._sqlite_connections.values())
            finalizers = tuple(self._sqlite_finalizers.values())
            self._sqlite_finalizers.clear()
        for finalizer in finalizers:
            finalizer.detach()
        for connection in connections:
            try:
                connection.set_trace_callback(None)
            except sqlite3.Error:
                self._record("sqlite_trace", "aux_db", "close", "OTHER", error=True)
        self._sqlite_connections.clear()
        for target, name, wrapper, original in reversed(self._patches):
            if getattr(target, name, None) is wrapper:
                setattr(target, name, original)
        self._patches.clear()
        self._closed = True
        return self.snapshot()


def install(data_dir) -> Probe:
    """Install optional runtime hooks without starting a measurement window."""
    probe = Probe(data_dir)
    # App imports are intentionally lazy: callers can load this module from an
    # old source export before switching sys.path to the candidate checkout.
    try:
        probe._install_sqlite()
        probe._install_mysql()
    except BaseException:
        probe.stop()
        raise
    return probe
