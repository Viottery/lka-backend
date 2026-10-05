"""Scoped immutable web artifacts and bounded, synchronous acquisition coordination.

Network adapters and tool authorization live outside this deterministic service.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


class WebCacheError(RuntimeError):
    """A cache, lifecycle, or acquisition failure, never an empty search result."""


@dataclass
class _Flight:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: Exception | None = None


class WebCacheService:
    """Reuse the artifact table without widening generic observation permissions."""

    def __init__(
        self, conn_factory: Callable[[], sqlite3.Connection], *,
        ttl_seconds: int = 86400, max_entries: int = 256,
        max_bytes: int = 64_000_000, max_parallel: int = 4,
        max_pending: int = 16, request_timeout_seconds: float = 12,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.conn_factory = conn_factory
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_pending = max_pending
        self.request_timeout_seconds = request_timeout_seconds
        self.clock = clock
        self._semaphore = threading.BoundedSemaphore(max_parallel)
        self._lock = threading.Lock()
        self._flights: dict[tuple[str, str], _Flight] = {}
        self._pending = 0
        with self._connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS web_cache_index (
                artifact_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL,
                kind TEXT NOT NULL, source_key TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL,
                size_bytes INTEGER NOT NULL)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_web_cache_source
                ON web_cache_index(scope_key, kind, source_key, created DESC)""")

    @contextmanager
    def _connection(self):
        conn = self.conn_factory()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _cleanup(self, conn: sqlite3.Connection, now: float) -> None:
        # Only artifacts indexed by this service are eligible for eviction.
        conn.execute("""DELETE FROM agent_run_artifacts WHERE artifact_id IN
            (SELECT artifact_id FROM web_cache_index WHERE expires <= ?)""", (now,))
        conn.execute("DELETE FROM web_cache_index WHERE expires <= ?", (now,))

    def put(self, *, scope_key: str, run_id: str, kind: str, source_key: str,
            payload: dict[str, Any], check: Callable[[], None]) -> dict[str, Any]:
        check()
        now = self.clock()
        identity = f"web_{kind}_{uuid4().hex}"
        stored = {**payload, "snapshot_id": identity} if kind == "page" else {**payload, "search_id": identity}
        serialized = json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        size = len(serialized.encode("utf-8"))
        if size > self.max_bytes:
            raise WebCacheError("Web artifact exceeds cache capacity; no partial snapshot was saved.")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._cleanup(conn, now)
            # Global bounded capacity, FIFO eviction. Small limit; no unbounded payload scan.
            rows = conn.execute("SELECT artifact_id, size_bytes FROM web_cache_index ORDER BY created, rowid").fetchall()
            total = sum(row["size_bytes"] for row in rows)
            while rows and (len(rows) >= self.max_entries or total + size > self.max_bytes):
                row = rows.pop(0)
                conn.execute("DELETE FROM agent_run_artifacts WHERE artifact_id=?", (row["artifact_id"],))
                conn.execute("DELETE FROM web_cache_index WHERE artifact_id=?", (row["artifact_id"],))
                total -= row["size_bytes"]
            check()  # Cancellation during acquisition/serialization must not publish.
            conn.execute("""INSERT INTO agent_run_artifacts
                (artifact_id,run_id,kind,content_hash,summary,payload,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""", (
                    identity, run_id, f"web_{kind}", hashlib.sha256(serialized.encode()).hexdigest(),
                    "Scoped web source snapshot", serialized, str(now), str(now),
                ))
            conn.execute("INSERT INTO web_cache_index VALUES (?,?,?,?,?,?,?)", (
                identity, scope_key, kind, source_key, now, now + self.ttl_seconds, size,
            ))
        return stored

    def load(self, identity: str, *, scope_key: str, kind: str,
             max_age_seconds: float | None = None) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("""SELECT a.payload, a.content_hash, c.created, c.expires
                FROM web_cache_index c JOIN agent_run_artifacts a ON a.artifact_id=c.artifact_id
                WHERE c.artifact_id=? AND c.scope_key=? AND c.kind=? AND a.kind=?""",
                (identity, scope_key, kind, f"web_{kind}"),
            ).fetchone()
        if row is None:
            raise WebCacheError("Web reference is unavailable in the current scope; it may have been evicted.")
        now = self.clock()
        if row["expires"] <= now:
            raise WebCacheError("Web reference expired; explicitly reopen its URL. No network request was made.")
        age = max(0, now - row["created"])
        if max_age_seconds is not None and age > max_age_seconds:
            raise WebCacheError("Web reference exceeds max_age_seconds; explicitly refresh its URL.")
        if hashlib.sha256(row["payload"].encode()).hexdigest() != row["content_hash"]:
            raise WebCacheError("Web artifact integrity check failed.")
        value = json.loads(row["payload"])
        return {**value, "cache_hit": True, "cache_age_seconds": age,
                "cache_read_at": datetime.fromtimestamp(now, UTC).isoformat(),
                "cache_expires_at": datetime.fromtimestamp(row["expires"], UTC).isoformat()}

    def latest(self, *, scope_key: str, kind: str, source_key: str,
               max_age_seconds: float) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("""SELECT artifact_id FROM web_cache_index
                WHERE scope_key=? AND kind=? AND source_key=? AND expires>?
                ORDER BY created DESC, rowid DESC LIMIT 1""",
                (scope_key, kind, source_key, self.clock()),
            ).fetchone()
        if row is None:
            return None
        try:
            return self.load(row["artifact_id"], scope_key=scope_key, kind=kind,
                             max_age_seconds=max_age_seconds)
        except WebCacheError as exc:
            if "exceeds max_age_seconds" in str(exc):
                return None
            raise

    def _wait(self, *, check: Callable[[], None], deadline: float,
              ready: Callable[[float], bool]) -> None:
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebCacheError("Web acquisition deadline exceeded.")
            if ready(min(0.05, remaining)):
                return

    def acquire(self, *, scope_key: str, key: str, operation: Callable[[float], dict[str, Any]],
                check: Callable[[], None], coalesce: bool = True) -> dict[str, Any]:
        """Bound simultaneous network work; followers cancel independently of owner."""
        deadline = time.monotonic() + self.request_timeout_seconds
        flight_key = (scope_key, key if coalesce else f"{key}:{uuid4().hex}")
        check()
        with self._lock:
            flight = self._flights.get(flight_key)
            owner = flight is None
            if self._pending >= self.max_pending:
                raise WebCacheError("Web acquisition queue is full; retry after active work finishes.")
            self._pending += 1
            if owner:
                flight = _Flight()
                self._flights[flight_key] = flight
        try:
            if not owner:
                self._wait(check=check, deadline=deadline, ready=flight.event.wait)
                check()
                if flight.error is not None:
                    raise WebCacheError(f"Shared web acquisition failed: {flight.error}") from flight.error
                return {**flight.result, "cache_hit": True, "acquisition_shared": True}
            acquired = False
            try:
                self._wait(check=check, deadline=deadline,
                           ready=lambda seconds: self._semaphore.acquire(timeout=seconds))
                acquired = True
                check()
                result = operation(deadline)
                check()
                if time.monotonic() > deadline:
                    raise WebCacheError("Web acquisition deadline exceeded.")
                flight.result = result
                return result
            except Exception as exc:
                flight.error = exc
                raise
            finally:
                if acquired:
                    self._semaphore.release()
                flight.event.set()
                with self._lock:
                    self._flights.pop(flight_key, None)
        finally:
            with self._lock:
                self._pending -= 1

    def stats(self) -> dict[str, int]:
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) bytes FROM web_cache_index").fetchone()
        with self._lock:
            return {"entries": row["n"], "size_bytes": row["bytes"], "pending": self._pending}
