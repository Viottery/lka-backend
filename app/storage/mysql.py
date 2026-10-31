"""Optional PyMySQL storage transport with bounded connection pooling.

The module is deliberately independent of application configuration/registry.
SQLite remains the default backend; callers opt in with `MySQLConfig`.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from app.storage.mysql_dialect import MySQLDialectError, split_script, translate_sql

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str
    pool_size: int = 8
    pool_wait_seconds: float = 5
    connect_timeout: float = 5
    read_timeout: float = 30
    lock_wait_seconds: int = 5
    transaction_seconds: float = 30
    pool_queue_capacity: int = 128

    def __post_init__(self) -> None:
        if not self.host or not self.user or not self.database:
            raise ValueError("MySQL host, user, and database are required")
        if not 1 <= self.port <= 65535:
            raise ValueError("MySQL port must be between 1 and 65535")
        if type(self.pool_size) is not int or self.pool_size < 1:
            raise ValueError("MySQL pool_size must be at least one")
        if type(self.pool_queue_capacity) is not int or self.pool_queue_capacity < 1:
            raise ValueError("MySQL pool_queue_capacity must be at least one")
        for name in ("pool_wait_seconds", "connect_timeout", "read_timeout", "transaction_seconds"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not math.isfinite(self.lock_wait_seconds) or self.lock_wait_seconds < 1:
            raise ValueError("lock_wait_seconds must be positive")


class MySQLStorageError(sqlite3.DatabaseError):
    """Normalized MySQL failure retaining the original vendor identifiers."""

    category = "database"

    def __init__(self, message: str, *, errno: int | None = None,
                 sqlstate: str | None = None, original: BaseException | None = None):
        super().__init__(message)
        self.mysql_errno = errno
        self.mysql_sqlstate = sqlstate
        self.original = original

    @property
    def error_category(self) -> str:
        """Stable payload-free category for API and background-job boundaries."""
        return f"mysql_{self.category}"


class MySQLOperationalError(MySQLStorageError, sqlite3.OperationalError):
    category = "operational"


class MySQLIntegrityError(MySQLStorageError, sqlite3.IntegrityError):
    category = "integrity"


class MySQLLockError(MySQLOperationalError):
    category = "lock_timeout"


class MySQLDeadlockError(MySQLOperationalError):
    category = "deadlock"


class MySQLPoolTimeout(MySQLOperationalError):
    category = "pool_timeout"


class MySQLTransactionTimeout(MySQLOperationalError):
    category = "transaction_timeout"


class MySQLStatementTimeout(MySQLOperationalError):
    category = "statement_timeout"


class MySQLCommitOutcomeUnknown(MySQLOperationalError):
    category = "commit_outcome_unknown"


class MySQLTransactionGuardError(MySQLOperationalError):
    category = "transaction_guard_rejected"


class MySQLLeaseInvalidatedError(MySQLOperationalError):
    category = "lease_invalidated"


class MySQLDependencyError(MySQLStorageError):
    category = "dependency_missing"


def _row_metadata(names: Sequence[str]):
    names = tuple(str(name) for name in names)
    first: dict[str, int] = {}
    for index, name in enumerate(names):
        first.setdefault(name, index)
    return names, MappingProxyType(first)


class MySQLRow:
    """Small `sqlite3.Row`-like result with positional and named access.

    sqlite3.Row iteration yields values (not keys); `dict(row)` uses its
    separate `keys()` protocol. Preserve both behaviors because application
    code unpacks selected rows positionally as well as converting `SELECT *`
    results into dictionaries.
    """

    __slots__ = ("_first_index", "_names", "_values")

    def __init__(self, names: Sequence[str], values: Sequence[Any] | Mapping[str, Any]):
        self._names, self._first_index = _row_metadata(names)
        self._set_values(values)

    @classmethod
    def _from_metadata(cls, metadata, values):
        row = cls.__new__(cls)
        row._names, row._first_index = metadata
        row._set_values(values)
        return row

    def _set_values(self, values):
        if isinstance(values, Mapping):
            self._values = tuple(values.get(name) for name in self._names)
        else:
            self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._first_index[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._names)

    def keys(self):
        return list(self._names)

    def items(self):
        return list(zip(self._names, self._values))

    def __repr__(self) -> str:
        return f"MySQLRow({dict(self)!r})"


def _load_driver():
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - optional dependency boundary
        raise MySQLDependencyError(
            "MySQL storage requires the optional PyMySQL dependency"
        ) from exc
    return pymysql


def _vendor_error(raw: BaseException) -> MySQLStorageError:
    errno = getattr(raw, "errno", None)
    if errno is None and getattr(raw, "args", None):
        errno = raw.args[0] if isinstance(raw.args[0], int) else None
    sqlstate = getattr(raw, "sqlstate", None)
    message = (
        f"MySQL operation failed (errno={errno})"
        if isinstance(errno, int) else "MySQL operation failed"
    )
    if errno == 1213:
        cls = MySQLDeadlockError
    elif errno == 1205:
        cls = MySQLLockError
    elif errno == 3024:
        cls = MySQLStatementTimeout
    elif errno in {1048, 1062, 1451, 1452, 3819} or sqlstate == "23000":
        cls = MySQLIntegrityError
    elif errno in {2006, 2013, 2055}:
        cls = MySQLOperationalError
    else:
        cls = MySQLStorageError
    error = cls(message, errno=errno, sqlstate=sqlstate, original=raw)
    error.__cause__ = raw
    return error


def _is_connection_error(error: MySQLStorageError) -> bool:
    return error.mysql_errno in {2006, 2013, 2055, 2003, 2002}


class _Pool:
    """Bounded connection slots; it does not serialize database transactions."""

    def __init__(self, config: MySQLConfig, read_only: bool):
        self.config = config
        self.read_only = read_only
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._waiters: deque[threading.Condition] = deque()
        self._idle: list[Any] = []
        self._created = 0
        self._closed = False

    def _connect_raw(self):
        driver = _load_driver()
        try:
            raw = driver.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                connect_timeout=self.config.connect_timeout,
                read_timeout=self.config.read_timeout,
                charset="utf8mb4",
                # Avoid PyMySQL's implicit read transactions. The wrapper
                # starts a transaction explicitly for DML or BEGIN, which
                # makes short read probes (including auth/schema checks) not
                # accidentally span a later write transaction.
                autocommit=True,
            )
        except Exception as exc:
            error = _vendor_error(exc)
            raise error from exc
        try:
            with raw.cursor() as cursor:
                cursor.execute("SET NAMES utf8mb4 COLLATE utf8mb4_0900_bin")
                cursor.execute("SET SESSION collation_connection = 'utf8mb4_0900_bin'")
                cursor.execute("SET SESSION time_zone = '+00:00'")
                cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
                cursor.execute("SET SESSION innodb_lock_wait_timeout = %s",
                               (int(self.config.lock_wait_seconds),))
                # MySQL rolls back idle open transactions when this session
                # timeout expires, even if the application thread vanishes
                # before it can hit our cooperative duration checks.
                cursor.execute(
                    "SET SESSION wait_timeout = %s",
                    (max(1, math.ceil(self.config.transaction_seconds)),),
                )
                cursor.execute("SET SESSION max_execution_time = %s",
                               (max(1, math.ceil(self.config.transaction_seconds * 1000)),))
                cursor.execute("SELECT @@SESSION.sql_mode")
                row = cursor.fetchone()
                modes = row[0] if row else ""
                required = {"STRICT_TRANS_TABLES", "ERROR_FOR_DIVISION_BY_ZERO", "NO_ENGINE_SUBSTITUTION"}
                modes = set(filter(None, str(modes).split(","))) | required
                cursor.execute("SET SESSION sql_mode = %s", (",".join(sorted(modes)),))
                if self.read_only:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
            raw.commit()
            return raw
        except Exception as exc:
            raw.close()
            error = _vendor_error(exc)
            raise error from exc

    def acquire(self):
        deadline = time.monotonic() + self.config.pool_wait_seconds
        while True:
            with self._condition:
                if self._closed:
                    raise MySQLOperationalError("MySQL connection pool is closed")
                if len(self._waiters) >= self.config.pool_queue_capacity:
                    raise MySQLPoolTimeout("MySQL connection pool waiting queue is full")
                ticket = threading.Condition(self._lock)
                self._waiters.append(ticket)
                try:
                    while True:
                        if self._closed:
                            raise MySQLOperationalError("MySQL connection pool is closed")
                        # New arrivals cannot take a returned connection before
                        # an older waiter. Waiting never owns a DB connection.
                        if self._waiters[0] is ticket:
                            if self._idle:
                                raw = self._idle.pop()
                                break
                            if self._created < self.config.pool_size:
                                self._created += 1
                                raw = None
                                break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise MySQLPoolTimeout("MySQL connection pool wait timed out")
                        ticket.wait(remaining)
                finally:
                    self._waiters.remove(ticket)
                    self._wake_waiter_locked()
            if raw is None:
                try:
                    return self._connect_raw()
                except BaseException:
                    with self._condition:
                        self._created -= 1
                        self._wake_waiter_locked()
                    raise
            try:
                raw.ping(reconnect=False)
                return raw
            except Exception:  # noqa: BLE001 - any ping failure invalidates this socket.
                self._discard(raw)

    def _wake_waiter_locked(self):
        if self._waiters:
            self._waiters[0].notify()

    @staticmethod
    def _connection_is_known_clean(raw) -> bool:
        """Trust only the driver's current, open autocommit/no-transaction state."""
        try:
            return (
                raw.open is True
                and raw.get_autocommit() is True
                and isinstance(raw.server_status, int)
                and not (raw.server_status & 0x0001)  # SERVER_STATUS_IN_TRANS
            )
        except Exception:  # noqa: BLE001 - uncertain state requires rollback.
            return False

    def release(self, raw, *, broken=False):
        reusable = not broken
        if reusable and not self._connection_is_known_clean(raw):
            try:
                # Unknown state or an active transaction still requires an
                # acknowledged rollback. Checkout always verifies the socket.
                raw.rollback()
            except Exception:  # noqa: BLE001 - any reset failure makes the socket unsafe.
                reusable = False
        if reusable:
            with self._condition:
                if self._closed:
                    reusable = False
                else:
                    self._idle.append(raw)
                    self._wake_waiter_locked()
        if not reusable:
            self._discard(raw)

    def _discard(self, raw):
        try:
            raw.close()
        except Exception:  # noqa: BLE001, S110 - failure to close is already being handled.
            pass
        with self._condition:
            self._created -= 1
            self._wake_waiter_locked()


_pool_lock = threading.Lock()
_pools: dict[tuple[MySQLConfig, bool], _Pool] = {}


def _pool_for(config: MySQLConfig, read_only: bool) -> _Pool:
    key = (config, read_only)
    with _pool_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = _Pool(config, read_only)
            _pools[key] = pool
        return pool


def close_mysql_pools(config: MySQLConfig | None = None) -> None:
    """Close currently idle pooled sockets, optionally for one config."""
    with _pool_lock:
        selected = [
            (key, pool) for key, pool in _pools.items()
            if config is None or key[0] == config
        ]
        for key, _ in selected:
            _pools.pop(key, None)
    for _, pool in selected:
        with pool._condition:
            pool._closed = True
            idle, pool._idle = pool._idle, []
            pool._created -= len(idle)
            pool._condition.notify_all()
            for waiter in pool._waiters:
                waiter.notify()
        for raw in idle:
            try:
                raw.close()
            except Exception:  # noqa: BLE001, S110 - failure to close is already being handled.
                pass


class MySQLCursor:
    def __init__(self, connection: MySQLConnection, raw_cursor, *, native=False):
        self.connection = connection
        self._cursor = raw_cursor
        self._native = native
        self._synthetic_rows = None
        self._synthetic_description = None
        self._row_metadata = None
        self._rowcount_override: int | None = None
        self._lastrowid_override: int | None = None
        self._has_lastrowid_override = False

    @property
    def rowcount(self):
        if self._rowcount_override is not None:
            return self._rowcount_override
        if self._synthetic_rows is not None:
            return len(self._synthetic_rows)
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        if self._has_lastrowid_override:
            return self._lastrowid_override
        return self._cursor.lastrowid

    @property
    def description(self):
        if self._synthetic_description is not None:
            return self._synthetic_description
        return self._cursor.description

    @property
    def arraysize(self):
        return self._cursor.arraysize

    @arraysize.setter
    def arraysize(self, value):
        self._cursor.arraysize = value

    def execute(self, statement, parameters=()):
        self._rowcount_override = None
        self._lastrowid_override = None
        self._has_lastrowid_override = False
        self._synthetic_rows = None
        self._synthetic_description = None
        self._row_metadata = None
        kind = self.connection._kind(statement)
        if (not self._native and self.connection._schema is not None
                and kind == "DDL"):
            if parameters:
                raise MySQLDialectError("schema DDL does not accept bind parameters")
            return self.connection._schema.execute_schema_statement(self.connection, statement)
        if not self._native:
            synthetic = self.connection._catalog_query(statement, parameters)
            if synthetic is not None:
                names, rows = synthetic
                self._synthetic_description = tuple((name, None, None, None, None, None, None) for name in names)
                self._synthetic_rows = list(rows)
                return self
        if (not self._native and self.connection._schema is not None
                and re.match(r"\s*INSERT\b", statement, re.IGNORECASE)
                and re.search(r"\bON\s+CONFLICT\b|\bINSERT\s+OR\s+IGNORE\b", statement, re.IGNORECASE)):
            from app.storage.mysql_upsert import execute_targeted_upsert
            try:
                outcome = execute_targeted_upsert(self.connection, statement, parameters)
            except MySQLStorageError as exc:
                self.connection._failed(exc)
                raise
            except MySQLDialectError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalize underlying PyMySQL errors.
                error = _vendor_error(exc)
                self.connection._failed(error)
                raise error
            if outcome is not None:
                self._rowcount_override = outcome.rowcount
                self._lastrowid_override = outcome.lastrowid
                self._has_lastrowid_override = True
                self.connection._record_changes(outcome.rowcount)
                return self
        sql = statement if self._native else translate_sql(
            statement, rowid_column=self.connection._rowid_column,
            conflict_target=self.connection._conflict_target,
            ignore_target=self.connection._ignore_target,
        )
        self.connection._before_statement(statement, kind=kind)
        try:
            self._cursor.execute(sql, parameters)
            self.connection._after_statement(statement, kind=kind)
            if kind == "DML":
                self.connection._record_changes(self._cursor.rowcount)
            return self
        except MySQLStorageError as exc:
            self.connection._failed(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - normalize all driver exceptions.
            error = _vendor_error(exc)
            self.connection._failed(error)
            raise error

    def executemany(self, statement, parameters: Iterable[Sequence[Any]]):
        self._rowcount_override = None
        self._lastrowid_override = None
        self._has_lastrowid_override = False
        self._row_metadata = None
        kind = self.connection._kind(statement)
        if (not self._native and self.connection._schema is not None
                and re.search(r"\bON\s+CONFLICT\b|\bINSERT\s+OR\s+IGNORE\b", statement, re.IGNORECASE)):
            total = 0
            lastrowid = None
            for row_parameters in parameters:
                cursor = self.connection.execute(statement, row_parameters)
                try:
                    total += max(0, int(cursor.rowcount))
                    if cursor.lastrowid is not None:
                        lastrowid = cursor.lastrowid
                finally:
                    cursor.close()
            self._rowcount_override = total
            self._lastrowid_override = lastrowid
            self._has_lastrowid_override = True
            return self
        if kind == "DML" and not self._native:
            # SQLite's changes() reports the last individual statement from
            # executemany(), while cursor.rowcount reports the aggregate. Run
            # each row through the normal adapter so both contracts remain
            # accurate without replaying a DML statement.
            total = 0
            lastrowid = None
            for row_parameters in parameters:
                cursor = self.connection.execute(statement, row_parameters)
                try:
                    total += max(0, int(cursor.rowcount))
                    if cursor.lastrowid is not None:
                        lastrowid = cursor.lastrowid
                finally:
                    cursor.close()
            self._rowcount_override = total
            self._lastrowid_override = lastrowid
            self._has_lastrowid_override = True
            return self
        sql = statement if self._native else translate_sql(
            statement, rowid_column=self.connection._rowid_column,
            conflict_target=self.connection._conflict_target,
            ignore_target=self.connection._ignore_target,
        )
        self.connection._before_statement(statement, kind=kind)
        try:
            self._cursor.executemany(sql, parameters)
            self.connection._after_statement(statement, kind=kind)
            if kind == "DML":
                self.connection._record_changes(self._cursor.rowcount)
            return self
        except MySQLStorageError as exc:
            self.connection._failed(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - normalize all driver exceptions.
            error = _vendor_error(exc)
            self.connection._failed(error)
            raise error

    def _wrap_row(self, row):
        if row is None:
            return None
        factory = self.connection.row_factory
        if factory is None:
            return tuple(row.values()) if isinstance(row, Mapping) else row
        if self._row_metadata is None:
            self._row_metadata = _row_metadata([column[0] for column in (self.description or ())])
        result = MySQLRow._from_metadata(self._row_metadata, row)
        if factory is sqlite3.Row:
            return result
        return factory(self, result)

    def fetchone(self):
        if self._synthetic_rows is not None:
            return self._wrap_row(self._synthetic_rows.pop(0) if self._synthetic_rows else None)
        return self._wrap_row(self._cursor.fetchone())

    def fetchmany(self, size=None):
        if self._synthetic_rows is not None:
            count = self._cursor.arraysize if size is None else size
            rows, self._synthetic_rows = self._synthetic_rows[:count], self._synthetic_rows[count:]
            return [self._wrap_row(row) for row in rows]
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._wrap_row(row) for row in rows]

    def fetchall(self):
        if self._synthetic_rows is not None:
            rows, self._synthetic_rows = self._synthetic_rows, []
            return [self._wrap_row(row) for row in rows]
        return [self._wrap_row(row) for row in self._cursor.fetchall()]

    def close(self):
        self._cursor.close()

    def __iter__(self):
        if self._synthetic_rows is not None:
            yield from self.fetchall()
            return
        for row in self._cursor:
            yield self._wrap_row(row)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class MySQLConnection:
    """DB-API-like lease over one pooled connection; no global write FIFO."""

    dialect = "mysql"

    def __init__(self, pool: _Pool, raw):
        self._pool = pool
        self._raw = raw
        self._closed = False
        self._broken = False
        self._in_transaction = False
        self._transaction_started: float | None = None
        self._transaction_guard: Callable[[MySQLConnection], None] | None = None
        self._guarded_transaction = False
        self._rowid_column: Callable[[str], str | None] | None = None
        self._conflict_target: Callable[[str, tuple[str, ...]], bool] | None = None
        self._ignore_target: Callable[[str], str | None] | None = None
        self._schema = None
        self._last_changes = 0
        self.row_factory = None
        self._lease_finalizer = weakref.finalize(self, pool.release, raw, broken=True)
        self._lease_finalizer.atexit = False

    @property
    def in_transaction(self):
        return self._in_transaction

    @property
    def closed(self):
        return self._closed

    def set_rowid_resolver(self, resolver: Callable[[str], str | None]) -> None:
        self._rowid_column = resolver

    def set_conflict_target_resolver(
        self, resolver: Callable[[str, tuple[str, ...]], bool],
    ) -> None:
        self._conflict_target = resolver

    def set_ignore_target_resolver(self, resolver: Callable[[str], str | None]) -> None:
        self._ignore_target = resolver

    def set_schema(self, schema) -> None:
        self._schema = schema
        connection = weakref.proxy(self)
        self._rowid_column = lambda table_name: schema.rowid_column(connection, table_name)
        self._conflict_target = lambda table_name, columns: schema.validate_conflict_target(
            connection, table_name, columns,
        )
        self._ignore_target = lambda table_name: schema.validate_ignore_target(
            connection, table_name,
        )

    def set_transaction_guard(
        self, callback: Callable[[MySQLConnection], None] | None,
    ) -> None:
        """Set the owner fence that runs exactly once after transaction start."""
        if self._in_transaction:
            raise MySQLOperationalError("transaction guard must be configured before a transaction starts")
        self._transaction_guard = callback

    def _run_transaction_guard(self) -> None:
        if self._guarded_transaction:
            return
        self._guarded_transaction = True
        if self._transaction_guard is None:
            return
        try:
            self._transaction_guard(self)
        except Exception as exc:
            try:
                self._raw.rollback()
            except Exception:  # noqa: BLE001 - failed rollback makes the socket unsafe.
                self._broken = True
            self._in_transaction = False
            self._transaction_started = None
            self._guarded_transaction = False
            raise MySQLTransactionGuardError(
                "MySQL transaction ownership fence rejected the transaction",
                errno=getattr(exc, "mysql_errno", None),
                sqlstate=getattr(exc, "mysql_sqlstate", None),
                original=exc,
            ) from exc

    def _catalog_query(self, statement, parameters):
        changes = re.fullmatch(
            r"\s*SELECT\s+changes\s*\(\s*\)"
            r"(?:\s+AS\s+(`?[\w$]+`?)|\s+(`?[\w$]+`))?\s*;?\s*",
            statement, re.IGNORECASE,
        )
        if changes:
            if parameters:
                raise MySQLDialectError("SELECT changes() does not accept bind parameters")
            alias = (changes.group(1) or changes.group(2) or "changes()").strip("`")
            return [alias], [(self._last_changes,)]
        if re.search(r"\bsqlite_(?:temp_)?master\b", statement, re.IGNORECASE):
            if self._schema is None:
                raise MySQLDialectError("SQLite catalog query requires attached MySQLSchema metadata")
            return self._schema.catalog_query(self, statement, parameters)
        if re.match(r"^\s*PRAGMA\s+table_info\b", statement, re.IGNORECASE):
            if self._schema is None:
                raise MySQLDialectError("PRAGMA table_info requires attached MySQLSchema metadata")
            return self._schema.pragma_table_info_query(self, statement, parameters)
        return None

    def _record_changes(self, rowcount) -> None:
        """Record the last successful logical DML count for SQLite changes()."""
        try:
            self._last_changes = max(0, int(rowcount))
        except (TypeError, ValueError):
            self._last_changes = 0

    def _check_open(self):
        if self._closed:
            raise MySQLOperationalError("MySQL connection lease is closed")
        if self._broken:
            raise MySQLLeaseInvalidatedError("MySQL connection lease is no longer usable")

    @staticmethod
    def _kind(statement: str) -> str:
        from app.storage.mysql_dialect import leading_keyword
        head = leading_keyword(statement)
        if head in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            return "DML"
        if head in {"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME"}:
            return "DDL"
        if head in {"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
            return "TX"
        if head == "WITH":
            # WITH can mutate; SQLGlot's parsed root tells us when available.
            from app.storage.mysql_dialect import _sqlglot
            try:
                node = _sqlglot().parse_one(statement, read="sqlite")
                key = str(node.key).upper()
            except Exception as exc:
                raise MySQLDialectError("cannot determine whether WITH statement writes") from exc
            return "DML" if key in {"INSERT", "UPDATE", "DELETE", "REPLACE"} else "READ"
        return "READ"

    def _before_statement(self, statement: str, *, kind: str | None = None):
        self._check_open()
        if kind is None:
            kind = self._kind(statement)
        if kind == "DDL" and self._in_transaction:
            raise MySQLOperationalError(
                "MySQL DDL implicitly commits; execute schema migration outside business transactions"
            )
        if (self._in_transaction and self._transaction_started is not None
                and time.monotonic() - self._transaction_started > self._pool.config.transaction_seconds):
            _logger.warning("MySQL transaction deadline exceeded before %s; rolling back", kind,
                            stack_info=True)
            self.rollback()
            self._broken = True
            raise MySQLTransactionTimeout("MySQL transaction exceeded configured duration")
        if kind == "DML" and not self._in_transaction:
            self._raw.begin()
            self._in_transaction = True
            self._transaction_started = time.monotonic()
            self._run_transaction_guard()

    def _after_statement(self, statement: str, *, kind: str | None = None):
        if kind is None:
            kind = self._kind(statement)
        if kind == "DML" and not self._in_transaction:
            self._in_transaction = True
            self._transaction_started = time.monotonic()
        elif kind == "TX":
            head = statement.lstrip().split(None, 1)[0].rstrip(";").upper()
            if head in {"BEGIN", "START"}:
                self._in_transaction = True
                self._transaction_started = time.monotonic()
                self._run_transaction_guard()
            elif head in {"COMMIT", "END", "ROLLBACK"}:
                self._in_transaction = False
                self._transaction_started = None
                self._guarded_transaction = False
        elif kind == "DDL":
            self._in_transaction = False
            self._transaction_started = None

    def _failed(self, error: MySQLStorageError):
        if _is_connection_error(error):
            self._broken = True
        # 1213 rolls back the entire InnoDB transaction. 1205 normally rolls
        # back only the statement, so explicitly roll back the transaction too:
        # callers may catch either error, and must never commit a partial unit.
        # In both cases invalidate the lease so a later statement cannot be
        # sent in autocommit mode without the transaction owner fence.
        if error.mysql_errno in {1205, 1213}:
            self._broken = True
            try:
                self._raw.rollback()
            except Exception:  # noqa: BLE001, S110 - invalidated lease is discarded by close.
                pass
            self._in_transaction = False
            self._transaction_started = None
            self._guarded_transaction = False

    def execute(self, statement, parameters=()):
        return self.cursor().execute(statement, parameters)

    def execute_native(self, statement, parameters=()):
        """Execute explicitly reviewed MySQL SQL (schema/metadata layer only)."""
        return MySQLCursor(self, self._raw.cursor(), native=True).execute(statement, parameters)

    def executemany(self, statement, parameters):
        return self.cursor().executemany(statement, parameters)

    def executemany_native(self, statement, parameters):
        """Batch explicitly reviewed MySQL SQL without dialect rewriting."""
        return MySQLCursor(self, self._raw.cursor(), native=True).executemany(statement, parameters)

    def executescript(self, script: str):
        """Execute parsed statements sequentially without implicit commit."""
        if self._schema is not None:
            return self._schema.execute_schema_script(self, script)
        cursor = None
        for statement in split_script(script):
            if cursor is not None:
                cursor.close()
            cursor = self.execute(statement)
        return cursor if cursor is not None else self.cursor()

    def cursor(self):
        self._check_open()
        return MySQLCursor(self, self._raw.cursor())

    def commit(self):
        self._check_open()
        if not self._in_transaction:
            return
        if self._transaction_started is not None and time.monotonic() - self._transaction_started > self._pool.config.transaction_seconds:
            _logger.warning("MySQL transaction deadline exceeded before commit; rolling back",
                            stack_info=True)
            self.rollback()
            self._broken = True
            raise MySQLTransactionTimeout("MySQL transaction exceeded configured duration")
        try:
            self._raw.commit()
        except Exception as exc:  # noqa: BLE001 - commit errors can make outcome unknown.
            self._broken = True
            self._closed = True
            self._lease_finalizer.detach()
            self._pool.release(self._raw, broken=True)
            error = _vendor_error(exc)
            unknown = MySQLCommitOutcomeUnknown(
                "MySQL commit outcome is unknown; operation was not retried",
                errno=error.mysql_errno, sqlstate=error.mysql_sqlstate, original=exc,
            )
            unknown.__cause__ = exc
            raise unknown
        self._in_transaction = False
        self._transaction_started = None
        self._guarded_transaction = False

    def rollback(self):
        # Rollback remains available after lease invalidation so context-manager
        # cleanup does not mask the original SQL error. The lease is discarded
        # by close() because _broken remains set.
        if self._closed:
            raise MySQLOperationalError("MySQL connection lease is closed")
        try:
            self._raw.rollback()
        except Exception as exc:  # noqa: BLE001 - reset failure invalidates this socket.
            error = _vendor_error(exc)
            self._failed(error)
            raise error
        self._in_transaction = False
        self._transaction_started = None
        self._guarded_transaction = False

    def close(self):
        if self._closed:
            return
        try:
            if self._in_transaction:
                self._raw.rollback()
        except Exception:  # noqa: BLE001 - rollback failure invalidates this socket.
            self._broken = True
        self._in_transaction = False
        self._transaction_started = None
        self._guarded_transaction = False
        self._closed = True
        self._lease_finalizer.detach()
        self._pool.release(self._raw, broken=self._broken)

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.commit()
            else:
                try:
                    self.rollback()
                except Exception:  # noqa: BLE001 - preserve the original operation error during cleanup.
                    # Cleanup cannot replace the business/validation failure
                    # that caused this exit. Never reuse a socket whose
                    # rollback outcome could not be acknowledged.
                    self._broken = True
                    if exc_value is not None and hasattr(exc_value, "add_note"):
                        exc_value.add_note("MySQL rollback cleanup failed; the connection was discarded.")
        finally:
            self.close()
        return False


def connect_mysql(config: MySQLConfig, *, read_only: bool = False) -> MySQLConnection:
    """Lease a pooled MySQL connection with explicit session safety settings."""
    if not isinstance(config, MySQLConfig):
        raise TypeError("config must be MySQLConfig")
    pool = _pool_for(config, bool(read_only))
    raw = pool.acquire()
    return MySQLConnection(pool, raw)
