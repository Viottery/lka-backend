"""Bounded SQLite timing without SQL text, bindings, results or exception messages."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import sqlite3
import sys
import threading
import time
from collections import Counter
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from app.storage.sqlite_write_queue import QueuedConnection, QueuedCursor

_BOUNDS = (0.1, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
_OPERATIONS = frozenset({
    "SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "WITH", "PRAGMA", "BEGIN",
    "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "CREATE", "ALTER", "DROP",
})
_request: ContextVar[RequestTiming | None] = ContextVar("sqlite_request_timing", default=None)
_recorder: TimingRecorder | None = None
_SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _error_callsite():
    """Code coordinates only; never capture locals, SQL, arguments or messages."""
    frame = sys._getframe(1)
    try:
        for _ in range(24):
            if frame is None:
                break
            try:
                path = Path(frame.f_code.co_filename).resolve().relative_to(_SOURCE_ROOT)
            except (OSError, ValueError):
                path = None
            if path is not None and path.as_posix() not in {
                "app/storage/sqlite_timing.py", "app/storage/sqlite_retry.py",
                "app/storage/sqlite_write_queue.py",
            }:
                return {"file": path.as_posix(), "function": frame.f_code.co_name,
                        "line": frame.f_lineno}
            frame = frame.f_back
    finally:
        del frame
    return None


class RequestTiming:
    def __init__(self, sampled: bool):
        self.request_id = uuid4().hex
        self.sampled = sampled
        self.lock = threading.Lock()
        self.counts = Counter()
        self.totals = Counter()
        self.detail = sampled


class TimingRecorder:
    """Complete, fixed-size histograms plus sampled/slow/error detail records."""

    def __init__(self, path: Path, *, sample_rate: float = 0.01, slow_ms: float = 50):
        self.sample_rate = sample_rate
        self.slow_ms = slow_ms
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        self.lock = threading.Lock()
        self.histograms = {}
        self.last_summary = time.monotonic()
        self.closed = False

    def write(self, record):
        # Diagnostics must never change database or HTTP outcomes.
        if self.closed:
            return
        try:
            payload = {"timestamp": datetime.now(UTC).isoformat(), **record}
            self.handler.handle(logging.LogRecord(
                "lka.database_timing", logging.INFO, "", 0,
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")), (), None,
            ))
        except Exception:  # noqa: BLE001, S110 - logging failure must not affect the operation.
            pass

    def record(self, *, database, phase, operation, statement_id, elapsed_ms, error, details=None):
        context = _request.get()
        if context is not None:
            with context.lock:
                context.counts[phase] += 1
                context.totals[phase] += elapsed_ms
                if error is not None or elapsed_ms >= self.slow_ms:
                    context.detail = True
        bucket = next((i for i, bound in enumerate(_BOUNDS) if elapsed_ms <= bound), len(_BOUNDS))
        with self.lock:
            stats = self.histograms.setdefault((database, phase), {
                "count": 0, "errors": 0, "total_ms": 0.0, "max_ms": 0.0,
                "bucket_counts": [0] * (len(_BOUNDS) + 1),
            })
            stats["count"] += 1
            stats["errors"] += error is not None
            stats["total_ms"] += elapsed_ms
            stats["max_ms"] = max(stats["max_ms"], elapsed_ms)
            stats["bucket_counts"][bucket] += 1
        sampled = context.sampled if context else random.random() < self.sample_rate
        if sampled or error is not None or elapsed_ms >= self.slow_ms:
            self.write({
                "kind": "sqlite", "database": database, "phase": phase,
                "operation": operation, "statement_id": statement_id,
                "elapsed_ms": round(elapsed_ms, 4), "success": error is None,
                "sqlite_error": getattr(error, "sqlite_errorname", None),
                "sqlite_errorcode": getattr(error, "sqlite_errorcode", None),
                "thread_id": threading.get_ident(),
                "error_callsite": _error_callsite() if error is not None else None,
                "request_id": context.request_id if context else None,
                "sampled": sampled,
                **({"transaction": details} if details else {}),
            })
        self.flush_summary()

    def flush_summary(self, *, force=False):
        now = time.monotonic()
        with self.lock:
            if not force and now - self.last_summary < 60:
                return
            histograms, self.histograms = self.histograms, {}
            interval = now - self.last_summary
            self.last_summary = now
        for (database, phase), stats in histograms.items():
            percentiles = {}
            for percentile in (50, 95, 99):
                cumulative = 0
                for index, count in enumerate(stats["bucket_counts"]):
                    cumulative += count
                    if cumulative >= stats["count"] * percentile / 100:
                        percentiles[f"p{percentile}_upper_ms"] = (
                            _BOUNDS[index] if index < len(_BOUNDS) else None
                        )
                        break
            self.write({
                "kind": "sqlite_summary", "database": database, "phase": phase,
                "interval_seconds": round(interval, 3),
                "bucket_bounds_ms": [*_BOUNDS, None], **stats, **percentiles,
            })

    def close(self):
        if self.closed:
            return
        self.flush_summary(force=True)
        self.handler.close()
        self.closed = True


def configure_sqlite_timing(path: Path, *, enabled=True, sample_rate=0.01, slow_ms=50):
    global _recorder
    if _recorder is not None:
        _recorder.close()
    _recorder = None
    if enabled:
        try:
            _recorder = TimingRecorder(path, sample_rate=sample_rate, slow_ms=slow_ms)
        except OSError:
            logging.getLogger(__name__).warning("Database timing log unavailable; continuing without it")
    return _recorder


def close_sqlite_timing(recorder):
    global _recorder
    if _recorder is recorder:
        _recorder = None
    if recorder is not None:
        recorder.close()


def _statement(sql):
    # Do not log even normalized SQL: literals and comments can contain private content.
    parts = sql.lstrip().split(None, 2)
    operation = parts[0].rstrip(";").upper() if parts else "OTHER"
    if operation not in _OPERATIONS:
        operation = "OTHER"
    phase = "execute"
    if operation == "BEGIN" and len(parts) > 1 and parts[1].rstrip(";").upper() in {
        "IMMEDIATE", "EXCLUSIVE",
    }:
        phase = "acquire_write"
    elif operation in {"COMMIT", "END"}:
        phase = "commit"
    elif operation == "ROLLBACK":
        phase = "rollback"
    return phase, operation, hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def _measure(database, phase, operation, statement_id, call):
    recorder = _recorder
    if recorder is None:
        return call()
    started = time.perf_counter()
    error = None
    try:
        return call()
    except BaseException as exc:
        if not isinstance(exc, StopIteration):
            error = exc
        raise
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        try:
            recorder.record(database=database, phase=phase, operation=operation,
                            statement_id=statement_id, elapsed_ms=elapsed, error=error)
        except Exception:  # noqa: BLE001, S110 - diagnostics must preserve the original outcome.
            pass


class TimedCursor(QueuedCursor):
    _operation = "OTHER"
    _statement_id = None

    def execute(self, sql, parameters=()):
        phase, self._operation, self._statement_id = _statement(sql)
        return _measure(self.connection._timing_database, phase, self._operation,
                        self._statement_id, lambda: super(TimedCursor, self).execute(sql, parameters))

    def executemany(self, sql, parameters):
        phase, self._operation, self._statement_id = _statement(sql)
        return _measure(self.connection._timing_database, phase, self._operation,
                        self._statement_id,
                        lambda: super(TimedCursor, self).executemany(sql, parameters))

    def executescript(self, sql):
        _, self._operation, self._statement_id = _statement(sql)
        return _measure(self.connection._timing_database, "script", self._operation,
                        self._statement_id, lambda: super(TimedCursor, self).executescript(sql))

    def _fetch(self, call):
        return _measure(self.connection._timing_database, "fetch", self._operation,
                        self._statement_id, call)

    def fetchone(self):
        return self._fetch(super().fetchone)

    def fetchmany(self, *args, **kwargs):
        return self._fetch(lambda: super(TimedCursor, self).fetchmany(*args, **kwargs))

    def fetchall(self):
        return self._fetch(super().fetchall)

    def __next__(self):
        return self._fetch(super().__next__)


class TimedConnection(QueuedConnection):
    _timing_database = "other"

    def _observe_write_queue(self, phase, elapsed_ms, *, error=None, details=None):
        recorder = _recorder
        if recorder is not None:
            recorder.record(database=self._timing_database, phase=phase, operation="TRANSACTION",
                            statement_id=None, elapsed_ms=elapsed_ms, error=error, details=details)

    def cursor(self, factory=None):
        return super().cursor(factory or TimedCursor)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql):
        return self.cursor().executescript(sql)

    def commit(self):
        return _measure(self._timing_database, "commit", "COMMIT", None, super().commit)

    def rollback(self):
        return _measure(self._timing_database, "rollback", "ROLLBACK", None, super().rollback)

    def __exit__(self, exc_type, exc_value, traceback):
        # Measure the managed context exit, including native commit/rollback.
        self._check_live()
        call = lambda: super(TimedConnection, self).__exit__(exc_type, exc_value, traceback)
        if not self.in_transaction:
            return call()
        phase = "commit" if exc_type is None else "rollback"
        return _measure(self._timing_database, phase, phase.upper(), None, call)


def connect_sqlite(database, *args, **kwargs):
    """Coordinate application writes even when diagnostic timing is disabled.

    The watchdog needs serialized cross-thread cleanup. Statement caching is
    disabled so SQLite's authorizer sees every write, including repeated CTEs.
    Native sqlite3 itself is never patched; external writers still use its locks.
    """
    policy = kwargs.pop("write_policy", None)
    positional = list(args)

    def option(name, index, value):
        if len(positional) > index:
            positional[index] = value
            kwargs.pop(name, None)
        else:
            kwargs[name] = value

    factory = positional[4] if len(positional) > 4 else kwargs.get("factory", TimedConnection)
    if not isinstance(factory, type) or not issubclass(factory, QueuedConnection):
        raise TypeError("SQLite connection factory must subclass QueuedConnection")
    option("factory", 4, factory)
    option("check_same_thread", 3, False)
    option("cached_statements", 5, 0)
    name = Path(str(database).split("?", 1)[0]).name
    role = {"lka.sqlite3": "main", "agent_checkpoints.sqlite3": "checkpoints"}.get(name, "other")
    conn = _measure(role, "connect", "CONNECT", None,
                    lambda: sqlite3.connect(database, *positional, **kwargs))
    try:
        conn.configure_write_queue(database, policy=policy)
    except BaseException:
        sqlite3.Connection.close(conn)
        raise
    if isinstance(conn, TimedConnection):
        conn._timing_database = role
    return conn


class DatabaseTimingMiddleware:
    """Pure ASGI timing: streaming response duration is distinct from DB time."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        recorder = _recorder
        if scope["type"] != "http" or recorder is None:
            return await self.app(scope, receive, send)
        context = RequestTiming(random.random() < recorder.sample_rate)
        token = _request.set(context)
        started = time.perf_counter()
        headers_ms, status = None, 500
        failed = False

        async def timed_send(message):
            nonlocal headers_ms, status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers_ms = (time.perf_counter() - started) * 1000
                message = {**message, "headers": [*message.get("headers", []),
                           (b"x-request-id", context.request_id.encode("ascii"))]}
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        except BaseException:
            failed = True
            raise
        finally:
            _request.reset(token)
            elapsed = (time.perf_counter() - started) * 1000
            if context.detail or elapsed >= recorder.slow_ms or status >= 500 or failed:
                with context.lock:
                    phases = {phase: {"count": count, "total_ms": round(context.totals[phase], 4)}
                              for phase, count in context.counts.items()}
                route = scope.get("route")
                recorder.write({
                    "kind": "http", "request_id": context.request_id,
                    "method": scope["method"], "route": getattr(route, "path", "unmatched"),
                    "status": status, "failed": failed, "elapsed_ms": round(elapsed, 4),
                    "headers_ms": round(headers_ms, 4) if headers_ms is not None else None,
                    "database_phases": phases, "sampled": context.sampled,
                })
