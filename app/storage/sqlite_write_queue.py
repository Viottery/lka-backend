"""In-process FIFO write admission for SQLite connections created by the app.

This is deliberately a connection wrapper, not a retry mechanism: a transaction
keeps its database permit until it commits, rolls back, or is poisoned by its
deadline. SQLite remains the authority across processes and unwrapped clients.
"""
from __future__ import annotations

import logging
import math
import ntpath
import os
import re
import sqlite3
import sys
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

_logger = logging.getLogger("lka.sqlite_write_queue")
_LOCAL_PRAGMAS = frozenset({
    "busy_timeout", "foreign_keys", "synchronous", "cache_size", "temp_store",
    "locking_mode", "recursive_triggers", "query_only", "defer_foreign_keys",
    "trusted_schema", "mmap_size", "wal_autocheckpoint",
})
_PERSISTENT_PRAGMAS = frozenset({
    "journal_mode", "user_version", "application_id", "schema_version",
    "page_size", "auto_vacuum", "secure_delete", "encoding",
})
_COMMAND_PRAGMAS = frozenset({"optimize", "wal_checkpoint", "incremental_vacuum"})
_SOURCE_PREFIX = os.path.normcase(str(Path(__file__).resolve().parents[2]) + os.sep)


def _write_callsite():
    frame = sys._getframe(1)
    try:
        for _ in range(24):
            if frame is None:
                break
            filename = os.path.normcase(frame.f_code.co_filename)
            if filename.startswith(_SOURCE_PREFIX):
                relative = filename[len(_SOURCE_PREFIX):].replace(os.sep, "/")
                if relative not in {"app/storage/sqlite_write_queue.py", "app/storage/sqlite_timing.py",
                                    "app/storage/sqlite_retry.py"}:
                    return {"file": relative, "function": frame.f_code.co_name, "line": frame.f_lineno}
            frame = frame.f_back
    finally:
        del frame
    return None


def _observe(conn, phase, elapsed_ms, *, error=None, details=None):
    try:
        observer = getattr(conn, "_observe_write_queue", None)
        if observer is not None:
            observer(phase, elapsed_ms, error=error, details=details)
    except Exception:  # noqa: BLE001, S110 - metrics cannot alter transaction outcomes.
        pass


def _log_queue_failure(conn, reason, age_seconds=None):
    with conn._write_queue.condition:
        writer_thread_id = conn._write_queue.owner_thread
        owner = dict(conn._write_queue.owner_details)
        owner_age = (time.monotonic() - conn._write_queue.owner_started
                     if conn._write_queue.owner_started is not None else None)
    _logger.warning("SQLite write queue %s database=%s thread_id=%s writer_thread_id=%s age_seconds=%s writer_age_seconds=%s owner=%s",
                    reason, getattr(conn, "_timing_database", "other"),
                    threading.get_ident(),
                    writer_thread_id,
                    round(age_seconds, 3) if age_seconds is not None else None,
                    round(owner_age, 3) if owner_age is not None else None, owner)


class SqliteWriteQueueError(sqlite3.OperationalError):
    """Application write admission or transaction safety limit was exceeded."""


class SqliteTransactionTimeoutError(SqliteWriteQueueError):
    """A managed write transaction exceeded its lease and was rolled back."""


@dataclass(frozen=True, slots=True)
class WriteQueuePolicy:
    capacity: int = 128
    wait_seconds: float = 5.0
    transaction_seconds: float = 30.0
    watchdog_seconds: float = 1.0

    def __post_init__(self):
        periods = (self.wait_seconds, self.transaction_seconds, self.watchdog_seconds)
        if (not isinstance(self.capacity, int) or self.capacity < 1
                or any(not math.isfinite(value) or value <= 0 for value in periods)):
            raise ValueError("SQLite write queue limits must be positive and finite")


_defaults_lock = threading.Lock()
_default_policy = WriteQueuePolicy()
_queues_lock = threading.Lock()
_queues: weakref.WeakValueDictionary[str, _DatabaseQueue] = weakref.WeakValueDictionary()
_connections: weakref.WeakSet = weakref.WeakSet()
_watchdog_started = False
_thread_holds: dict[int, set[str]] = {}
_thread_holds_lock = threading.Lock()


def configure_write_queue_defaults(policy: WriteQueuePolicy) -> None:
    global _default_policy
    if not isinstance(policy, WriteQueuePolicy):
        raise TypeError("policy must be WriteQueuePolicy")
    with _defaults_lock:
        _default_policy = policy


def _database_key(database) -> str:
    raw = os.fspath(database)
    if raw == ":memory:":
        return f":memory:{uuid4().hex}"
    if re.match(r"^/?[A-Za-z]:[/\\]", raw):
        winpath = ntpath.normcase(ntpath.normpath(raw.lstrip("/").replace("/", "\\")))
        return "windows:" + winpath.replace("\\", "/")
    if raw.startswith("file:"):
        parsed = urlsplit(raw)
        params = parse_qs(parsed.query)
        if parsed.path in {":memory:", "/:memory:"} or params.get("mode") == ["memory"]:
            normalized = "&".join(f"{k}={v}" for k, values in sorted(params.items()) for v in sorted(values))
            return f"file-memory:{parsed.netloc}{parsed.path}?{normalized}"
        path_text = unquote(parsed.path)
        if re.match(r"^/?[A-Za-z]:[/\\]", path_text):
            winpath = ntpath.normcase(ntpath.normpath(path_text.lstrip("/").replace("/", "\\")))
            return "windows:" + winpath.replace("\\", "/")
        path = Path(path_text).expanduser().resolve()
        return f"//{parsed.netloc}{path}" if parsed.netloc else str(path)
    return str(Path(raw).expanduser().resolve())


def _leading_sql_words(sql, count=2):
    """Read initial SQL keywords while skipping whitespace and SQL comments."""
    words = []
    index = 0
    length = len(sql)
    while len(words) < count:
        while index < length:
            if sql[index].isspace() or sql[index] == ";":
                index += 1
            elif sql.startswith("--", index):
                end = sql.find("\n", index + 2)
                index = length if end < 0 else end + 1
            elif sql.startswith("/*", index):
                end = sql.find("*/", index + 2)
                index = length if end < 0 else end + 2
            else:
                break
        if index >= length:
            break
        start = index
        while index < length and (sql[index].isalnum() or sql[index] == "_"):
            index += 1
        if start == index:
            break
        words.append(sql[start:index].upper())
    return words


@dataclass(slots=True)
class _WriteTicket:
    condition: threading.Condition


class _DatabaseQueue:
    def __init__(self, key, capacity):
        self.key = key
        self.capacity = capacity
        self._lock = threading.RLock()
        self.condition = threading.Condition(self._lock)
        self.waiters = deque()
        self.owner = None
        self.owner_thread = None
        self.owner_started = None
        self.owner_details = {}

    def _notify_head_locked(self):
        if self.waiters:
            self.waiters[0].condition.notify()

    def acquire(self, conn):
        with self.condition:
            if self.owner is conn:
                return
        with _thread_holds_lock:
            held = _thread_holds.get(threading.get_ident(), set())
            if self.key in held:
                raise SqliteWriteQueueError("nested SQLite write on another connection would deadlock")
        ticket = _WriteTicket(threading.Condition(self._lock))
        with self.condition:
            if len(self.waiters) >= self.capacity:
                _log_queue_failure(conn, "full")
                raise SqliteWriteQueueError("SQLite write queue is full")
            self.waiters.append(ticket)
        started = time.monotonic()
        deadline = started + conn._write_policy.wait_seconds
        details = {"transaction_id": uuid4().hex, "callsite": _write_callsite()}
        try:
            with self.condition:
                while self.owner is not None or self.waiters[0] is not ticket:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _log_queue_failure(conn, "wait_timeout")
                        raise SqliteWriteQueueError("SQLite write queue wait timed out")
                    ticket.condition.wait(remaining)
                self.waiters.popleft()
                self.owner = conn
                self.owner_thread = threading.get_ident()
                self.owner_started = time.monotonic()
                self.owner_details = details
                with conn._lifecycle_lock:
                    conn._lease_generation += 1
                    conn._write_started = time.monotonic()
                    conn._write_expired = False
                    conn._progress_deadline = conn._write_started + conn._write_policy.transaction_seconds
                with _thread_holds_lock:
                    _thread_holds.setdefault(threading.get_ident(), set()).add(self.key)
            _observe(conn, "queue_wait", (time.monotonic() - started) * 1000, details=details)
        except BaseException as exc:
            with self.condition:
                was_head = bool(self.waiters and self.waiters[0] is ticket)
                try:
                    self.waiters.remove(ticket)
                except ValueError:
                    pass
                if was_head and self.owner is None:
                    self._notify_head_locked()
            _observe(conn, "queue_wait", (time.monotonic() - started) * 1000, error=exc,
                     details=details)
            raise

    def release(self, conn):
        observed = None
        with self.condition:
            if self.owner is conn:
                observed = ((time.monotonic() - self.owner_started) * 1000, self.owner_details)
                self.owner = None
                self.owner_started = None
                self.owner_details = {}
                if self.owner_thread is not None:
                    with _thread_holds_lock:
                        held = _thread_holds.get(self.owner_thread)
                        if held is not None:
                            held.discard(self.key)
                            if not held:
                                _thread_holds.pop(self.owner_thread, None)
                self.owner_thread = None
                conn._progress_deadline = None
                self._notify_head_locked()
        if observed is not None:
            error = SqliteTransactionTimeoutError("expired") if conn._poisoned else None
            _observe(conn, "transaction", observed[0], error=error, details=observed[1])


def _watchdog():
    while True:
        with _queues_lock:
            interval = min((c._write_policy.watchdog_seconds for c in _connections), default=1.0)
        time.sleep(min(interval, 1.0))
        with _queues_lock:
            references = [weakref.ref(c) for c in _connections]
        now = time.monotonic()
        for reference in references:
            conn = reference()
            if conn is None:
                continue
            try:
                _watch_connection(conn, now)
            except Exception as exc:  # noqa: BLE001 - one failed cleanup must not stop the watchdog.
                _logger.error("SQLite watchdog cleanup failed type=%s", type(exc).__name__)
            finally:
                del conn


def _watch_connection(conn, now):
    if not conn._lifecycle_lock.acquire(blocking=False):
        return
    try:
        if conn._write_started is None or conn._closed_by_wrapper:
            return
        if not conn._write_expired:
            if now - conn._write_started < conn._write_policy.transaction_seconds:
                return
            conn._write_expired = True
            try:
                conn.interrupt()
            except sqlite3.Error:
                pass
        generation = conn._lease_generation
    finally:
        conn._lifecycle_lock.release()
    # Interrupt first, then make a nonblocking cleanup attempt. A stuck
    # callback on one connection must not stall other database queues.
    if not conn._connection_lock.acquire(blocking=False):
        return
    try:
        with conn._lifecycle_lock:
            if conn._lease_generation != generation or not conn._write_expired:
                return
        try:
            in_transaction = conn.in_transaction
        except sqlite3.Error:
            in_transaction = False
        if not in_transaction:
            # A concurrent commit may have completed before we acquired
            # the operation lock. That durable result is authoritative.
            if conn._write_started is not None and not conn._has_pending_cursors():
                if conn._write_expired:
                    conn._expire_locked()
                else:
                    conn._finish_write()
            elif conn._write_started is not None:
                conn._expire_locked()
            return
        conn._expire_locked()
    finally:
        conn._connection_lock.release()


def _ensure_watchdog():
    global _watchdog_started
    with _queues_lock:
        if not _watchdog_started:
            threading.Thread(target=_watchdog, name="sqlite-write-watchdog", daemon=True).start()
            _watchdog_started = True


class QueuedConnection(sqlite3.Connection):
    """sqlite3 connection with FIFO admission and serialized connection access."""

    def configure_write_queue(self, database, *, policy=None):
        with _defaults_lock:
            chosen = policy or _default_policy
        if not isinstance(chosen, WriteQueuePolicy):
            raise TypeError("policy must be WriteQueuePolicy")
        key = _database_key(database)
        with _queues_lock:
            q = _queues.get(key)
            if q is None:
                q = _DatabaseQueue(key, chosen.capacity)
                _queues[key] = q
            elif q.capacity != chosen.capacity:
                raise ValueError("connections for a database must use the same queue capacity")
            self._write_queue = q
            self._write_policy = chosen
            self._connection_lock = threading.RLock()
            self._write_started = None
            self._write_expired = False
            self._poisoned = False
            self._closed_by_wrapper = False
            self._authorizer_error = None
            self._pending_write_cursors = set()
            self._operation_wrote = False
            self._operation_pragma_write = False
            self._lifecycle_lock = threading.RLock()
            self._lease_generation = 0
            self._progress_deadline = None
            _connections.add(self)
        _ensure_watchdog()
        self.set_authorizer(self._authorize)
        self.set_progress_handler(self._progress, 1000)
        return self

    def _check_live(self):
        with self._connection_lock:
            if getattr(self, "_poisoned", False) or getattr(self, "_write_expired", False):
                raise SqliteTransactionTimeoutError("SQLite connection transaction lease expired")
            started = self._write_started
            if started is not None and time.monotonic() - started >= self._write_policy.transaction_seconds:
                self._expire_locked()
                raise SqliteTransactionTimeoutError("SQLite connection transaction lease expired")

    def _progress(self):
        deadline = self._progress_deadline
        return int(deadline is not None and time.monotonic() >= deadline)

    def _deadline_reached(self):
        return (self._write_expired or (self._write_started is not None and
                time.monotonic() - self._write_started >= self._write_policy.transaction_seconds))

    def _expire_locked(self):
        age = time.monotonic() - self._write_started if self._write_started is not None else None
        with self._lifecycle_lock:
            self._write_expired = True
            try:
                rollback_ok = not self.in_transaction
            except sqlite3.Error:
                rollback_ok = self._closed_by_wrapper
            close_ok = self._closed_by_wrapper
            if not self._closed_by_wrapper:
                try:
                    sqlite3.Connection.rollback(self)
                    rollback_ok = True
                except sqlite3.Error:
                    rollback_ok = False
                try:
                    sqlite3.Connection.close(self)
                    close_ok = True
                    # sqlite3 close rolls back any transaction still open.
                    rollback_ok = True
                except sqlite3.Error:
                    close_ok = False
                self._closed_by_wrapper = close_ok
            self._poisoned = True
        _log_queue_failure(self, "transaction_timeout", age)
        if not rollback_ok or not close_ok:
            return
        with self._lifecycle_lock:
            self._write_started = None
            self._lease_generation += 1
        self._clear_pending_cursors()
        self._write_queue.release(self)


    def _has_pending_cursors(self):
        self._pending_write_cursors = {ref for ref in self._pending_write_cursors if ref() is not None}
        return bool(self._pending_write_cursors)

    def _discard_pending_cursor(self, cursor):
        self._pending_write_cursors = {
            ref for ref in self._pending_write_cursors if ref() is not None and ref() is not cursor
        }

    def _clear_pending_cursors(self):
        for cursor_ref in list(self._pending_write_cursors):
            cursor = cursor_ref()
            if cursor is not None:
                cursor._holds_write = False
        self._pending_write_cursors.clear()

    def _authorize(self, action, arg1, arg2, dbname, source):
        # Actions which mutate database state. Reads and non-mutating PRAGMAs
        # remain concurrent; SQLite's own busy handler handles external writers.
        writes = {
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX,
            sqlite3.SQLITE_ANALYZE,
        }
        if action == sqlite3.SQLITE_ATTACH:
            self._authorizer_error = SqliteWriteQueueError("ATTACH is unsupported with managed write queue")
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            pragma = (arg1 or "").lower()
            if pragma in _LOCAL_PRAGMAS:
                return sqlite3.SQLITE_OK
            if pragma in _COMMAND_PRAGMAS or arg2 is not None and pragma in _PERSISTENT_PRAGMAS:
                self._operation_pragma_write = True
                try:
                    self._write_queue.acquire(self)
                except SqliteWriteQueueError as exc:
                    self._authorizer_error = exc
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_OK
        if action in writes:
            self._operation_wrote = True
            try:
                self._write_queue.acquire(self)
            except SqliteWriteQueueError as exc:
                self._authorizer_error = exc
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def cursor(self, factory=None):
        self._check_live()
        factory = factory or QueuedCursor
        if not issubclass(factory, QueuedCursor):
            raise TypeError("cursor factory must subclass QueuedCursor")
        return sqlite3.Connection.cursor(self, factory)

    def execute(self, sql, parameters=()):
        self._check_live()
        with self._connection_lock:
            self._check_live()
            return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        self._check_live()
        with self._connection_lock:
            self._authorizer_error = None
            try:
                cur = self.cursor()
                cur.executemany(sql, seq_of_parameters)
            except sqlite3.Error:
                if self._authorizer_error is not None:
                    error, self._authorizer_error = self._authorizer_error, None
                    raise error
                raise
            if not self.in_transaction and self._write_started is not None:
                self._finish_write()
            return cur

    def executescript(self, sql):
        self._check_live()
        with self._connection_lock:
            self._write_queue.acquire(self)
            try:
                cur = self.cursor()
                cur.executescript(sql)
            except BaseException:
                if not self.in_transaction:
                    self._finish_write()
                raise
            if not self.in_transaction:
                self._finish_write()
            return cur

    def _finish_write(self, *, force=False):
        if self._poisoned and not self._closed_by_wrapper:
            return
        if self._has_pending_cursors():
            if not force:
                return
            self._clear_pending_cursors()
        with self._lifecycle_lock:
            self._write_started = None
            self._write_expired = False
            self._progress_deadline = None
            self._lease_generation += 1
        self._write_queue.release(self)

    def commit(self):
        self._check_live()
        with self._connection_lock:
            self._check_live()
            try:
                sqlite3.Connection.commit(self)
            finally:
                if not self.in_transaction and not self._has_pending_cursors():
                    self._finish_write()

    def rollback(self):
        self._check_live()
        with self._connection_lock:
            self._check_live()
            try:
                sqlite3.Connection.rollback(self)
            finally:
                if not self.in_transaction:
                    self._finish_write(force=True)

    def close(self):
        with self._connection_lock:
            if self._poisoned:
                return
            with self._lifecycle_lock:
                sqlite3.Connection.close(self)
                self._closed_by_wrapper = True
            self._finish_write(force=True)

    def __exit__(self, exc_type, exc_value, traceback):
        self._check_live()
        with self._connection_lock:
            self._check_live()
            try:
                return sqlite3.Connection.__exit__(self, exc_type, exc_value, traceback)
            finally:
                try:
                    in_transaction = self.in_transaction
                except sqlite3.Error:
                    in_transaction = False
                if not in_transaction and not self._has_pending_cursors():
                    self._finish_write()


def _cursor_finalized(conn_ref, cursor_ref):
    conn = conn_ref()
    if conn is None:
        return
    try:
        with conn._connection_lock:
            conn._pending_write_cursors.discard(cursor_ref)
            if not conn.in_transaction and not conn._has_pending_cursors():
                conn._finish_write()
    except sqlite3.Error:
        return


class QueuedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        conn = self.connection
        conn._check_live()
        with conn._connection_lock:
            conn._check_live()
            conn._authorizer_error = None
            conn._operation_wrote = False
            conn._operation_pragma_write = False
            words = _leading_sql_words(sql)
            op = words[0] if words else ""
            if op == "BEGIN" and len(words) > 1 and words[1] in {"IMMEDIATE", "EXCLUSIVE"}:
                conn._write_queue.acquire(conn)
            try:
                result = sqlite3.Cursor.execute(self, sql, parameters)
            except sqlite3.Error:
                if conn._authorizer_error is not None:
                    error, conn._authorizer_error = conn._authorizer_error, None
                    raise error
                if conn._deadline_reached():
                    conn._expire_locked()
                    raise SqliteTransactionTimeoutError("SQLite connection transaction lease expired")
                if not conn.in_transaction and not conn._has_pending_cursors() and conn._write_started is not None:
                    conn._finish_write()
                raise
            if op in {"COMMIT", "END", "ROLLBACK", "RELEASE"} and not conn.in_transaction and not conn._has_pending_cursors():
                conn._finish_write(force=op == "ROLLBACK")
            self._is_write = conn._operation_wrote
            if not conn.in_transaction and self._is_write and not conn._operation_pragma_write and self.description is not None:
                self._holds_write = True
                conn_ref = weakref.ref(conn)
                cursor_ref = weakref.ref(
                    self, lambda ref, conn_ref=conn_ref: _cursor_finalized(conn_ref, ref),
                )
                conn._pending_write_cursors.add(cursor_ref)
            elif not conn.in_transaction and conn._write_started is not None and not conn._has_pending_cursors():
                conn._finish_write()
            return result

    def executemany(self, sql, seq_of_parameters):
        conn = self.connection
        conn._check_live()
        with conn._connection_lock:
            conn._check_live()
            conn._authorizer_error = None
            conn._operation_wrote = False
            conn._operation_pragma_write = False
            try:
                result = sqlite3.Cursor.executemany(self, sql, seq_of_parameters)
            except sqlite3.Error:
                if conn._authorizer_error is not None:
                    error, conn._authorizer_error = conn._authorizer_error, None
                    raise error
                if conn._deadline_reached():
                    conn._expire_locked()
                    raise SqliteTransactionTimeoutError("SQLite connection transaction lease expired")
                if not conn.in_transaction and not conn._has_pending_cursors() and conn._write_started is not None:
                    conn._finish_write()
                raise
            if not conn.in_transaction and conn._write_started is not None:
                conn._finish_write()
            return result

    def executescript(self, sql):
        conn = self.connection
        conn._check_live()
        with conn._connection_lock:
            conn._check_live()
            conn._write_queue.acquire(conn)
            try:
                result = sqlite3.Cursor.executescript(self, sql)
            except BaseException:
                if conn._deadline_reached():
                    conn._expire_locked()
                    raise SqliteTransactionTimeoutError("SQLite connection transaction lease expired")
                if not conn.in_transaction:
                    conn._finish_write()
                raise
            if not conn.in_transaction:
                conn._finish_write()
            return result

    def _fetch_call(self, method, *args):
        conn = self.connection
        conn._check_live()
        with conn._connection_lock:
            conn._check_live()
            try:
                result = method(*args)
            except sqlite3.Error:
                if conn._deadline_reached():
                    conn._expire_locked()
                    raise SqliteTransactionTimeoutError("SQLite connection transaction lease expired")
                raise
            if getattr(self, "_holds_write", False) and result is None:
                self._holds_write = False
                conn._discard_pending_cursor(self)
                conn._finish_write()
            return result

    def fetchone(self):
        return self._fetch_call(sqlite3.Cursor.fetchone, self)

    def fetchmany(self, size=None):
        return self._fetch_call(sqlite3.Cursor.fetchmany, self, size) if size is not None else self._fetch_call(sqlite3.Cursor.fetchmany, self)

    def fetchall(self):
        conn = self.connection
        conn._check_live()
        with conn._connection_lock:
            conn._check_live()
            try:
                result = sqlite3.Cursor.fetchall(self)
            except sqlite3.Error:
                if conn._deadline_reached():
                    conn._expire_locked()
                    raise SqliteTransactionTimeoutError("SQLite connection transaction lease expired")
                raise
            if getattr(self, "_holds_write", False):
                self._holds_write = False
                conn._discard_pending_cursor(self)
                conn._finish_write()
            return result

    def close(self):
        conn = self.connection
        with conn._connection_lock:
            sqlite3.Cursor.close(self)
            if getattr(self, "_holds_write", False):
                self._holds_write = False
                conn._discard_pending_cursor(self)
                conn._finish_write()

    def __next__(self):
        conn = self.connection
        conn._check_live()
        with conn._connection_lock:
            conn._check_live()
            try:
                return sqlite3.Cursor.__next__(self)
            except sqlite3.Error:
                if conn._deadline_reached():
                    conn._expire_locked()
                    raise SqliteTransactionTimeoutError("SQLite connection transaction lease expired")
                raise
            except StopIteration:
                if getattr(self, "_holds_write", False):
                    self._holds_write = False
                    conn._discard_pending_cursor(self)
                    conn._finish_write()
                raise
