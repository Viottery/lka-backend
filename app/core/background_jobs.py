"""Durable, generic SQLite queue for small background memory jobs.

Payloads are deliberately restricted to identifiers and bounded scalar options. The
store never logs payloads or exception text; callers should persist domain output
separately and use the lease epoch as its compare-and-swap fence.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.llm.errors import (
    LLMClientError,
    LLMContextCapacityError,
    LLMNetworkError,
    LLMProviderHTTPError,
    LLMResponseParseError,
    LLMTimeoutError,
)
from app.core.llm_workloads import BackgroundBudgetDeferred, BackgroundTaskBudgetExceeded

STATUSES = ("queued", "running", "retry_wait", "succeeded", "failed", "cancelled")
_TERMINAL = {"succeeded", "failed", "cancelled"}
_MAX_PAYLOAD_BYTES = 4096
_ID_KEYS = frozenset({
    "id", "*_id", "*_ids", "revision", "*_revision", "*_epoch", "*_seq", "*_version", "limit", "offset",
    "mode", "*_mode", "force", "include_*", "*_count",
})


def _utc(value: datetime | str | None, *, default: datetime | None = None) -> datetime | None:
    if value is None:
        return default
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _payload_json(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    if len(payload) > 32:
        raise ValueError("payload has too many fields")
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ValueError("payload keys must be short strings")
        lowered = key.lower()
        allowed = any(
            pattern == key
            or (pattern.startswith("*") and key.endswith(pattern[1:]))
            or (pattern.endswith("*") and key.startswith(pattern[:-1]))
            for pattern in _ID_KEYS
        )
        if any(word in lowered for word in ("body", "content", "prompt", "secret", "token", "email", "text", "credential")):
            allowed = False
        if not allowed:
            raise ValueError(f"payload field is not an allowed identifier or bounded option: {key}")
        if (
            isinstance(value, bool)
            or isinstance(value, int) and -(2**63) <= value < 2**63
            or isinstance(value, str) and value and len(value) <= 256
            or isinstance(value, list) and len(value) <= 100 and all(
                isinstance(item, str) and item and len(item) <= 128 for item in value
            )
        ):
            clean[key] = value
        else:
            raise ValueError("payload values must be bounded IDs or scalar options")
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds 4096 bytes")
    return encoded


class BackgroundJobStore:
    """SQLite backed durable queue. Each public operation uses its own connection."""

    def __init__(self, db_path: Path | str, *, max_pending_jobs: int = 1024) -> None:
        self.db_path = str(db_path)
        self.max_pending_jobs = max(1, max_pending_jobs)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS background_jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','retry_wait','succeeded','failed','cancelled')),
                    priority INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    deadline TEXT,
                    error_class TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_recovery_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(kind, scope_id, idempotency_key)
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_background_jobs_ready ON background_jobs(status, available_at, priority DESC, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_background_jobs_lease ON background_jobs(status, lease_expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_background_jobs_finished ON background_jobs(status,finished_at)")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(background_jobs)")}
            if "lease_recovery_count" not in columns:
                conn.execute(
                    "ALTER TABLE background_jobs ADD COLUMN lease_recovery_count INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute("""CREATE TABLE IF NOT EXISTS background_job_inputs(
                kind TEXT NOT NULL,scope_id TEXT NOT NULL,input_id TEXT NOT NULL,
                job_id TEXT NOT NULL,PRIMARY KEY(kind,scope_id,input_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS background_pending_inputs(
                kind TEXT NOT NULL,scope_id TEXT NOT NULL,input_id TEXT NOT NULL,
                created_at TEXT NOT NULL,PRIMARY KEY(kind,scope_id,input_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS background_pending_watermarks(
                kind TEXT NOT NULL,scope_id TEXT NOT NULL,watermark_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,priority INTEGER NOT NULL,available_at TEXT NOT NULL,
                max_attempts INTEGER NOT NULL,deadline TEXT,watermark_order INTEGER,updated_at TEXT NOT NULL,
                PRIMARY KEY(kind,scope_id))""")
            pending_columns = {row[1] for row in conn.execute("PRAGMA table_info(background_pending_watermarks)")}
            if "watermark_order" not in pending_columns:
                conn.execute("ALTER TABLE background_pending_watermarks ADD COLUMN watermark_order INTEGER")
            conn.execute("""CREATE TABLE IF NOT EXISTS background_watermark_heads(
                kind TEXT NOT NULL,scope_id TEXT NOT NULL,watermark_order INTEGER NOT NULL,
                watermark_id TEXT NOT NULL,updated_at TEXT NOT NULL,
                PRIMARY KEY(kind,scope_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS background_input_progress(
                job_id TEXT NOT NULL,input_id TEXT NOT NULL,completed_at TEXT NOT NULL,
                PRIMARY KEY(job_id,input_id))""")

    def completed_inputs(self, job_id: str) -> set[str]:
        with self._connect() as conn:
            return {row[0] for row in conn.execute("SELECT input_id FROM background_input_progress WHERE job_id=?", (job_id,))}

    def complete_input(self, job_id: str, owner: str, epoch: int, input_id: str) -> bool:
        now = _stamp(datetime.now(UTC))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            valid = conn.execute("SELECT 1 FROM background_jobs WHERE job_id=? AND lease_owner=? AND lease_epoch=? "
                                 "AND status='running' AND lease_expires_at>? AND (deadline IS NULL OR deadline>?)",
                                 (job_id, owner, epoch, now, now)).fetchone()
            if valid is not None:
                conn.execute("INSERT OR IGNORE INTO background_input_progress VALUES(?,?,?)", (job_id, input_id, now))
            conn.commit()
            return valid is not None

    def compact_history(self, *, retention_days: int = 30, limit: int = 500) -> int:
        """Compact old terminal payloads, retaining idempotency/provenance IDs."""
        if retention_days < 1 or not 1 <= limit <= 1000:
            raise ValueError("invalid history compaction bounds")
        before = _stamp(datetime.now(UTC) - timedelta(days=retention_days))
        with self._connect() as conn:
            cursor = conn.execute("UPDATE background_jobs SET payload_json='{}' WHERE job_id IN "
                                  "(SELECT job_id FROM background_jobs WHERE status IN ('succeeded','failed','cancelled') "
                                  "AND finished_at<? AND payload_json!='{}' ORDER BY finished_at LIMIT ?)", (before, limit))
            return cursor.rowcount

    def enqueue_grouped(self, kind: str, scope_id: str, input_id: str, *, conn: sqlite3.Connection,
                        debounce_seconds: float = 0, limit: int = 8) -> dict[str, Any]:
        if not math.isfinite(debounce_seconds) or debounce_seconds < 0 or not 1 <= limit <= 8:
            raise ValueError("invalid grouping limits")
        if not all(isinstance(value, str) and 0 < len(value) <= 128 for value in (kind, scope_id, input_id)):
            raise ValueError("grouped inputs must be bounded identifiers")
        factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            return self._enqueue_grouped(kind, scope_id, input_id, conn=conn,
                                         debounce_seconds=debounce_seconds, limit=limit)
        finally:
            conn.row_factory = factory

    def _enqueue_grouped(self, kind: str, scope_id: str, input_id: str, *, conn: sqlite3.Connection,
                         debounce_seconds: float = 0, limit: int = 8) -> dict[str, Any]:
        """Coalesce a burst of source IDs without losing per-input idempotency."""
        existing = conn.execute(
            "SELECT job_id FROM background_job_inputs WHERE kind=? AND scope_id=? AND input_id=?",
            (kind, scope_id, input_id),
        ).fetchone()
        if existing:
            row = conn.execute("SELECT * FROM background_jobs WHERE job_id=?", (existing[0],)).fetchone()
            return self._public(row)
        # Adopt jobs created before grouped delivery was introduced.
        row = conn.execute("SELECT * FROM background_jobs WHERE kind=? AND scope_id=? AND idempotency_key=?", (kind,scope_id,input_id)).fetchone()
        if row is None:
            candidates = conn.execute("SELECT * FROM background_jobs WHERE kind=? AND scope_id=? AND status='queued' ORDER BY created_at DESC LIMIT 8", (kind,scope_id)).fetchall()
            for candidate in candidates:
                payload = json.loads(candidate["payload_json"])
                ids = payload.get("message_ids", [payload["message_id"]] if "message_id" in payload else [])
                if not ids or len(ids) >= limit:
                    continue
                conn.execute("UPDATE background_jobs SET payload_json=?,updated_at=? WHERE job_id=? AND status='queued'", (
                    _payload_json({"message_ids": [*ids, input_id]}), _stamp(datetime.now(UTC)), candidate["job_id"],
                ))
                row = candidate
                break
        if row is None:
            count = conn.execute("SELECT COUNT(*) FROM background_jobs WHERE status IN ('queued','running','retry_wait')").fetchone()[0]
            if count >= self.max_pending_jobs:
                conn.execute("INSERT OR IGNORE INTO background_pending_inputs VALUES(?,?,?,?)",
                             (kind, scope_id, input_id, _stamp(datetime.now(UTC))))
                return {"job_id": None, "status": "backpressured"}
            result = self.enqueue(kind, scope_id, input_id, {"message_ids": [input_id]},
                                  available_at=datetime.now(UTC) + timedelta(seconds=debounce_seconds),
                                  max_attempts=10, conn=conn)
            job_id = result["job_id"]
        else:
            job_id = row["job_id"]
        conn.execute("INSERT OR IGNORE INTO background_job_inputs VALUES(?,?,?,?)", (kind,scope_id,input_id,job_id))
        return self._public(conn.execute("SELECT * FROM background_jobs WHERE job_id=?", (job_id,)).fetchone())

    def enqueue_latest_watermark(
        self, kind: str, scope_id: str, watermark_id: str, payload: Mapping[str, Any], *,
        conn: sqlite3.Connection, priority: int = 0,
        available_at: datetime | str | None = None, max_attempts: int = 3,
        deadline: datetime | str | None = None, watermark_order: int | None = None,
    ) -> dict[str, Any]:
        """Persist the latest per-scope watermark and coalesce only unclaimed work.

        Callers may pass their enclosing transaction connection so source state and
        the raw latest watermark commit or roll back together.
        """
        if (not isinstance(kind, str) or not kind or len(kind) > 256
            or not isinstance(scope_id, str) or not scope_id or len(scope_id) > 256
            or not isinstance(watermark_id, str) or not watermark_id or len(watermark_id) > 240):
            raise ValueError("kind, scope_id and watermark_id must be bounded identifiers")
        encoded = _payload_json(payload)
        available = _stamp(_utc(available_at, default=datetime.now(UTC)))
        end = _utc(deadline)
        if end is not None and end <= datetime.now(UTC):
            raise ValueError("deadline must be in the future")
        if not isinstance(priority, int) or not -(2**31) <= priority < 2**31:
            raise ValueError("priority must be a 32-bit integer")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        if watermark_order is not None and (
            isinstance(watermark_order, bool) or not isinstance(watermark_order, int)
            or not -(2**63) <= watermark_order < 2**63
        ):
            raise ValueError("watermark_order must be a signed 64-bit integer")
        old_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            now = _stamp(datetime.now(UTC))
            head = conn.execute(
                "SELECT watermark_order FROM background_watermark_heads WHERE kind=? AND scope_id=?",
                (kind, scope_id),
            ).fetchone()
            if head is not None and watermark_order is None:
                current = conn.execute(
                    "SELECT * FROM background_jobs WHERE kind=? AND scope_id=? "
                    "AND status IN ('queued','retry_wait','running') ORDER BY created_at DESC LIMIT 1",
                    (kind, scope_id),
                ).fetchone()
                return self._public(current, include_payload=True) or {
                    "job_id": None, "status": "stale_watermark_ignored",
                }
            if watermark_order is not None:
                if head is not None and watermark_order < head["watermark_order"]:
                    current = conn.execute(
                        "SELECT * FROM background_jobs WHERE kind=? AND scope_id=? "
                        "AND status IN ('queued','retry_wait','running') ORDER BY created_at DESC LIMIT 1",
                        (kind, scope_id),
                    ).fetchone()
                    return self._public(current, include_payload=True) or {
                        "job_id": None, "status": "stale_watermark_ignored",
                    }
                conn.execute(
                    """INSERT INTO background_watermark_heads
                       (kind,scope_id,watermark_order,watermark_id,updated_at) VALUES(?,?,?,?,?)
                       ON CONFLICT(kind,scope_id) DO UPDATE SET watermark_order=excluded.watermark_order,
                       watermark_id=excluded.watermark_id,updated_at=excluded.updated_at
                       WHERE excluded.watermark_order>=background_watermark_heads.watermark_order""",
                    (kind, scope_id, watermark_order, watermark_id, now),
                )
            conn.execute(
                """INSERT INTO background_pending_watermarks
                   (kind,scope_id,watermark_id,payload_json,priority,available_at,max_attempts,deadline,watermark_order,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,scope_id) DO UPDATE SET
                   watermark_id=excluded.watermark_id,payload_json=excluded.payload_json,
                   priority=excluded.priority,available_at=excluded.available_at,
                   max_attempts=excluded.max_attempts,deadline=excluded.deadline,
                   watermark_order=excluded.watermark_order,updated_at=excluded.updated_at""",
                (kind, scope_id, watermark_id, encoded, priority, available, max_attempts,
                 _stamp(end) if end else None, watermark_order, now),
            )
            self._materialize_latest_watermarks(conn, limit=8)
            row = conn.execute(
                "SELECT * FROM background_jobs WHERE kind=? AND scope_id=? "
                "AND status IN ('queued','retry_wait','running') ORDER BY created_at DESC LIMIT 1",
                (kind, scope_id),
            ).fetchone()
            pending = conn.execute(
                "SELECT 1 FROM background_pending_watermarks WHERE kind=? AND scope_id=?",
                (kind, scope_id),
            ).fetchone()
            if row is not None:
                return self._public(row, include_payload=True)
            return {"job_id": None, "status": "backpressured" if pending else "queued"}
        finally:
            conn.row_factory = old_factory

    def _materialize_latest_watermarks(self, conn: sqlite3.Connection, *, limit: int) -> None:
        rows = conn.execute(
            "SELECT * FROM background_pending_watermarks ORDER BY updated_at LIMIT ?", (limit,)
        ).fetchall()
        for item in rows:
            if item["deadline"] is not None and item["deadline"] <= _stamp(datetime.now(UTC)):
                conn.execute(
                    "DELETE FROM background_pending_watermarks WHERE kind=? AND scope_id=?",
                    (item["kind"], item["scope_id"]),
                )
                continue
            mutable = conn.execute(
                "SELECT * FROM background_jobs WHERE kind=? AND scope_id=? "
                "AND status IN ('queued','retry_wait') ORDER BY created_at DESC LIMIT 1",
                (item["kind"], item["scope_id"]),
            ).fetchone()
            now = _stamp(datetime.now(UTC))
            if mutable is not None:
                conn.execute(
                    "UPDATE background_jobs SET payload_json=?,priority=?,updated_at=? WHERE job_id=?",
                    (item["payload_json"], item["priority"], now, mutable["job_id"]),
                )
                conn.execute("DELETE FROM background_pending_watermarks WHERE kind=? AND scope_id=?",
                             (item["kind"], item["scope_id"]))
                continue
            running = conn.execute(
                "SELECT 1 FROM background_jobs WHERE kind=? AND scope_id=? AND status='running' LIMIT 1",
                (item["kind"], item["scope_id"]),
            ).fetchone()
            if running is not None:
                continue
            count = conn.execute(
                "SELECT COUNT(*) FROM background_jobs WHERE status IN ('queued','running','retry_wait')"
            ).fetchone()[0]
            if count >= self.max_pending_jobs:
                continue
            key = f"watermark:{item['watermark_id']}"
            created = self.enqueue(
                item["kind"], item["scope_id"], key, json.loads(item["payload_json"]),
                priority=item["priority"], available_at=item["available_at"],
                max_attempts=item["max_attempts"], deadline=item["deadline"], conn=conn,
            )
            if created["status"] in _TERMINAL:
                self.enqueue(
                    item["kind"], item["scope_id"], f"{key}:{uuid4().hex}",
                    json.loads(item["payload_json"]), priority=item["priority"],
                    available_at=item["available_at"], max_attempts=item["max_attempts"],
                    deadline=item["deadline"], conn=conn,
                )
            conn.execute("DELETE FROM background_pending_watermarks WHERE kind=? AND scope_id=?",
                         (item["kind"], item["scope_id"]))

    def enqueue(
        self,
        kind: str,
        scope_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        priority: int = 0,
        available_at: datetime | str | None = None,
        max_attempts: int = 3,
        deadline: datetime | str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        if not all(isinstance(v, str) and v and len(v) <= 256 for v in (kind, scope_id, idempotency_key)):
            raise ValueError("kind, scope_id and idempotency_key must be non-empty strings up to 256 chars")
        if not isinstance(priority, int) or not -(2**31) <= priority < 2**31:
            raise ValueError("priority must be a 32-bit integer")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        encoded = _payload_json(payload)
        now = datetime.now(UTC)
        available = _utc(available_at, default=now)
        end = _utc(deadline)
        if end is not None and end <= now:
            raise ValueError("deadline must be in the future")
        own = conn is None
        db = conn if conn is not None else self._connect()
        previous_factory = db.row_factory
        if not own and db.row_factory is None:
            db.row_factory = sqlite3.Row
        try:
            if own:
                db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO background_jobs(job_id,kind,scope_id,idempotency_key,payload_json,status,
                    priority,available_at,max_attempts,deadline,created_at,updated_at)
                    VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?)
                    ON CONFLICT(kind,scope_id,idempotency_key) DO NOTHING""",
                (uuid4().hex, kind, scope_id, idempotency_key, encoded, priority,
                 _stamp(available), max_attempts, _stamp(end) if end else None, _stamp(now), _stamp(now)),
            )
            row = db.execute(
                "SELECT * FROM background_jobs WHERE kind=? AND scope_id=? AND idempotency_key=?",
                (kind, scope_id, idempotency_key),
            ).fetchone()
            if own:
                db.commit()
            return self._public(row)
        except Exception:
            if own:
                db.rollback()
            raise
        finally:
            if own:
                db.close()
            else:
                db.row_factory = previous_factory

    @staticmethod
    def _public(row: sqlite3.Row | Mapping[str, Any] | None, *, include_payload: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        result = {key: row[key] for key in row.keys() if key != "payload_json"}  # noqa: SIM118 - sqlite3.Row iterates values.
        if include_payload:
            result["payload"] = json.loads(row["payload_json"])
        return result

    def claim(
        self, owner: str, lease_seconds: float, *, kinds: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise ValueError("owner must be a non-empty string up to 256 chars")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if kinds is not None:
            if not kinds:
                return None
            if len(kinds) > 128 or any(not isinstance(kind, str) or not kind or len(kind) > 256 for kind in kinds):
                raise ValueError("kinds must contain bounded job kind names")
        kind_filter = "" if kinds is None else " AND j.kind IN (" + ",".join("?" for _ in kinds) + ")"
        now = datetime.now(UTC)
        stamp = _stamp(now)
        expiry = _stamp(now + timedelta(seconds=lease_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Preserve overflow as cheap, durable source IDs. Materialize only
            # a bounded batch when execution capacity returns; no raw data lost.
            pending = conn.execute("SELECT * FROM background_pending_inputs ORDER BY created_at LIMIT 8").fetchall()
            for item in pending:
                result = self.enqueue_grouped(item["kind"], item["scope_id"], item["input_id"], conn=conn)
                if result.get("job_id") is None:
                    break
                conn.execute("DELETE FROM background_pending_inputs WHERE kind=? AND scope_id=? AND input_id=?",
                             (item["kind"], item["scope_id"], item["input_id"]))
            conn.execute(
                """UPDATE background_jobs SET status='failed', error_class='deadline_exceeded',
                   finished_at=?, updated_at=?, lease_owner=NULL, lease_expires_at=NULL
                   WHERE status IN ('queued','retry_wait','running') AND deadline IS NOT NULL AND deadline<=?""",
                (stamp, stamp, stamp),
            )
            self._materialize_latest_watermarks(conn, limit=8)
            row = conn.execute(
                """SELECT j.job_id FROM background_jobs j
                   WHERE ((j.status IN ('queued','retry_wait') AND j.available_at<=?)
                      OR (j.status='running' AND j.lease_expires_at<=?))
                   AND NOT EXISTS (SELECT 1 FROM background_jobs active
                       WHERE active.kind=j.kind AND active.scope_id=j.scope_id
                       AND active.job_id!=j.job_id AND active.status='running'
                       AND active.lease_expires_at>?)""" + kind_filter + """
                   ORDER BY j.priority + MIN(10, CAST((julianday(?) - julianday(j.created_at))*1440 AS INTEGER)) DESC,
                       j.available_at, j.created_at LIMIT 1""",
                (stamp, stamp, stamp, *(kinds or ()), stamp),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """UPDATE background_jobs SET status='running', lease_owner=?, lease_expires_at=?,
                   lease_epoch=lease_epoch+1, attempts=attempts+1,
                   lease_recovery_count=lease_recovery_count + CASE WHEN status='running' THEN 1 ELSE 0 END,
                   started_at=COALESCE(started_at,?), updated_at=? WHERE job_id=?""",
                (owner, expiry, stamp, stamp, row["job_id"]),
            )
            claimed = conn.execute("SELECT * FROM background_jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            conn.commit()
            return self._public(claimed, include_payload=True)

    def heartbeat(self, job_id: str, owner: str, epoch: int, lease_seconds: float = 60) -> bool:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            expired = conn.execute(
                """UPDATE background_jobs SET status='failed',error_class='deadline_exceeded',
                   finished_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL
                   WHERE job_id=? AND status='running' AND lease_owner=? AND lease_epoch=?
                     AND deadline IS NOT NULL AND deadline<=?""",
                (_stamp(now), _stamp(now), job_id, owner, epoch, _stamp(now)),
            )
            if expired.rowcount:
                conn.commit()
                return False
            cursor = conn.execute(
                """UPDATE background_jobs SET lease_expires_at=?, updated_at=?
                   WHERE job_id=? AND status='running' AND lease_owner=? AND lease_epoch=?
                     AND lease_expires_at>? AND (deadline IS NULL OR deadline>?)""",
                (_stamp(now + timedelta(seconds=lease_seconds)), _stamp(now), job_id, owner, epoch,
                 _stamp(now), _stamp(now)),
            )
            conn.commit()
            return cursor.rowcount == 1

    def complete(self, job_id: str, owner: str, epoch: int) -> bool:
        return self._finish(job_id, owner, epoch, status="succeeded")

    def _finish(self, job_id: str, owner: str, epoch: int, *, status: str,
                error_class: str | None = None, available_at: datetime | None = None) -> bool:
        now = datetime.now(UTC)
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE background_jobs SET status=?, error_class=?, available_at=COALESCE(?,available_at),
                   finished_at=?, updated_at=?, lease_owner=NULL, lease_expires_at=NULL
                   WHERE job_id=? AND status='running' AND lease_owner=? AND lease_epoch=?
                     AND lease_expires_at>? AND (deadline IS NULL OR deadline>?)""",
                (status, error_class, _stamp(available_at) if available_at else None,
                 _stamp(now) if status in _TERMINAL else None, _stamp(now), job_id, owner, epoch,
                 _stamp(now), _stamp(now)),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    """UPDATE background_jobs SET status='failed',error_class='deadline_exceeded',
                       finished_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL
                       WHERE job_id=? AND status='running' AND lease_owner=? AND lease_epoch=?
                         AND deadline IS NOT NULL AND deadline<=?""",
                    (_stamp(now), _stamp(now), job_id, owner, epoch, _stamp(now)),
                )
            return cursor.rowcount == 1

    def fail(self, job_id: str, owner: str, epoch: int, error_class: str, retryable: bool) -> bool:
        if not isinstance(error_class, str) or not error_class or len(error_class) > 100:
            raise ValueError("error_class must be a short classification, not an exception message")
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts,max_attempts,deadline FROM background_jobs WHERE job_id=? AND status='running' AND lease_owner=? AND lease_epoch=? AND lease_expires_at>? AND (deadline IS NULL OR deadline>?)",
                (job_id, owner, epoch, _stamp(now), _stamp(now)),
            ).fetchone()
            if row is None:
                conn.execute(
                    """UPDATE background_jobs SET status='failed',error_class='deadline_exceeded',
                       finished_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL
                       WHERE job_id=? AND status='running' AND lease_owner=? AND lease_epoch=?
                         AND deadline IS NOT NULL AND deadline<=?""",
                    (_stamp(now), _stamp(now), job_id, owner, epoch, _stamp(now)),
                )
                conn.commit()
                return False
            should_retry = bool(retryable and row["attempts"] < row["max_attempts"]
                                and (row["deadline"] is None or _utc(row["deadline"]) > now))
            if should_retry:
                delay = min(3600.0, 2 ** min(row["attempts"] - 1, 12))
                delay *= random.uniform(0.8, 1.2)
                next_at = now + timedelta(seconds=delay)
                if row["deadline"] and next_at >= _utc(row["deadline"]):
                    should_retry = False
            else:
                next_at = None
            cursor = conn.execute(
                """UPDATE background_jobs SET status=?, error_class=?, available_at=COALESCE(?,available_at),
                   finished_at=?, updated_at=?, lease_owner=NULL, lease_expires_at=NULL
                   WHERE job_id=? AND status='running' AND lease_owner=? AND lease_epoch=?""",
                ("retry_wait" if should_retry else "failed", error_class,
                 _stamp(next_at) if next_at else None,
                 None if should_retry else _stamp(now), _stamp(now), job_id, owner, epoch),
            )
            conn.commit()
            return cursor.rowcount == 1

    def cancel(self, job_id: str) -> bool:
        now = _stamp(datetime.now(UTC))
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE background_jobs SET status='cancelled', finished_at=?, updated_at=?,
                   lease_owner=NULL, lease_expires_at=NULL
                   WHERE job_id=? AND status IN ('queued','retry_wait','running')""",
                (now, now, job_id),
            )
            return cursor.rowcount == 1

    def control_status(self, job_id: str) -> dict[str, Any] | None:
        """Read the small payload-free state needed for a user control CAS."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT job_id,kind,status,updated_at,deadline FROM background_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def retry_controlled(self, job_id: str, expected_updated_at: str,
                         allowed_kinds: set[str]) -> tuple[str, dict[str, Any] | None]:
        """Requeue an eligible terminal job without replacing its identity/checkpoints.

        The payload is intentionally retained internally and never returned. Attempt
        count and completed input progress are preserved; the next claim spends the
        existing attempt budget.
        """
        now = datetime.now(UTC)
        stamp = _stamp(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM background_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                conn.commit()
                return "missing", None
            public = self._public(row)
            if row["updated_at"] != expected_updated_at:
                conn.commit()
                return "conflict", public
            if row["kind"] not in allowed_kinds:
                conn.commit()
                return "unsupported", public
            if row["status"] not in {"failed", "cancelled"}:
                conn.commit()
                return "conflict", public
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = None
            if not isinstance(payload, dict) or not payload:
                conn.commit()
                return "expired", public
            if row["deadline"] is not None and row["deadline"] <= stamp:
                conn.commit()
                return "expired", public
            # A user initiated retry grants one additional attempt while preserving
            # the existing attempt ledger and all completed input checkpoints.
            max_attempts = row["max_attempts"] + (1 if row["attempts"] >= row["max_attempts"] else 0)
            cursor = conn.execute(
                """UPDATE background_jobs SET status='queued',available_at=?,error_class=NULL,
                   finished_at=NULL,started_at=NULL,lease_owner=NULL,lease_expires_at=NULL,
                   lease_epoch=lease_epoch+1,max_attempts=?,updated_at=?
                   WHERE job_id=? AND updated_at=? AND status IN ('failed','cancelled')""",
                (stamp, max_attempts, stamp, job_id, expected_updated_at),
            )
            updated = conn.execute("SELECT * FROM background_jobs WHERE job_id=?", (job_id,)).fetchone()
            conn.commit()
            return ("retried" if cursor.rowcount == 1 else "conflict", self._public(updated))

    def cancel_controlled(self, job_id: str, expected_updated_at: str) -> tuple[str, dict[str, Any] | None]:
        """Cancel with CAS; changing lease_epoch fences any running publisher."""
        now = _stamp(datetime.now(UTC))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM background_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                conn.commit()
                return "missing", None
            if row["status"] == "cancelled":
                result = self._public(row)
                conn.commit()
                return "cancelled", result
            if row["updated_at"] != expected_updated_at:
                conn.commit()
                return "conflict", self._public(row)
            if row["status"] not in {"queued", "retry_wait", "running"}:
                conn.commit()
                return "conflict", self._public(row)
            conn.execute(
                """UPDATE background_jobs SET status='cancelled',finished_at=?,updated_at=?,
                   lease_owner=NULL,lease_expires_at=NULL,lease_epoch=lease_epoch+1
                   WHERE job_id=? AND updated_at=? AND status IN ('queued','retry_wait','running')""",
                (now, now, job_id, expected_updated_at),
            )
            updated = conn.execute("SELECT * FROM background_jobs WHERE job_id=?", (job_id,)).fetchone()
            conn.commit()
            return "cancelled", self._public(updated)

    def defer(self, job_id: str, owner: str, epoch: int, *, delay_seconds: float = 60) -> bool:
        """Resource throttling waits without spending the provider retry allowance."""
        now = datetime.now(UTC)
        next_at = _stamp(now + timedelta(seconds=delay_seconds))
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE background_jobs SET status='retry_wait',error_class='background_budget_deferred',
                   available_at=?,attempts=MAX(0,attempts-1),updated_at=?,lease_owner=NULL,lease_expires_at=NULL
                   WHERE job_id=? AND status='running' AND lease_owner=? AND lease_epoch=?
                   AND lease_expires_at>? AND (deadline IS NULL OR deadline>?)""",
                (next_at, _stamp(now), job_id, owner, epoch, _stamp(now), _stamp(now)),
            )
            return cursor.rowcount == 1

    def get(self, job_id: str, *, include_payload: bool = False) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._public(conn.execute("SELECT * FROM background_jobs WHERE job_id=?", (job_id,)).fetchone(), include_payload=include_payload)

    def status(self, job_id: str) -> dict[str, Any] | None:
        return self.get(job_id)

    def list(self, *, status: str | None = None, scope_id: str | None = None,
             kind: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        if status is not None and status not in STATUSES:
            raise ValueError("invalid job status")
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("limit must be 1..500 and offset non-negative")
        clauses, args = [], []
        for field, value in (("status", status), ("scope_id", scope_id), ("kind", kind)):
            if value is not None:
                clauses.append(f"{field}=?")
                args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM background_jobs{where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            return [self._public(row) for row in rows]

    def health_metrics(self, *, recent_failure_limit: int = 20) -> dict[str, Any]:
        """Return bounded, payload-free aggregate queue and execution health."""
        if not isinstance(recent_failure_limit, int) or not 1 <= recent_failure_limit <= 100:
            raise ValueError("recent_failure_limit must be between 1 and 100")
        now = datetime.now(UTC)
        with self._connect() as conn:
            counts = {row["status"]: row["count"] for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM background_jobs GROUP BY status"
            )}
            pending_count = conn.execute("SELECT COUNT(*) FROM background_pending_inputs").fetchone()[0]
            pending_watermark_count = conn.execute(
                "SELECT COUNT(*) FROM background_pending_watermarks"
            ).fetchone()[0]
            oldest = conn.execute(
                "SELECT created_at FROM background_jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            running = conn.execute(
                "SELECT COUNT(*) AS count FROM background_jobs WHERE status='running' AND lease_expires_at<=?",
                (_stamp(now),),
            ).fetchone()["count"]
            durations = conn.execute(
                """SELECT started_at,finished_at FROM background_jobs
                   WHERE status='succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL
                   ORDER BY finished_at DESC LIMIT 1000"""
            ).fetchall()
            failures = conn.execute(
                """SELECT error_class,COUNT(*) AS count FROM (
                       SELECT error_class FROM background_jobs
                       WHERE error_class IS NOT NULL ORDER BY updated_at DESC LIMIT ?
                   ) GROUP BY error_class ORDER BY count DESC,error_class LIMIT 20""",
                (recent_failure_limit,),
            ).fetchall()
            recovered = conn.execute(
                "SELECT COALESCE(SUM(lease_recovery_count),0) AS count FROM background_jobs"
            ).fetchone()["count"]
        elapsed = max(0.0, (now - _utc(oldest["created_at"])).total_seconds()) if oldest else None
        seconds = sorted(max(0.0, (_utc(row["finished_at"]) - _utc(row["started_at"])).total_seconds())
                         for row in durations)
        return {
            "backpressured_input_count": pending_count,
            "pending_watermark_count": pending_watermark_count,
            "queue_depth_by_status": {status: counts.get(status, 0) for status in STATUSES},
            "oldest_queued_age_seconds": elapsed,
            "expired_running_lease_count": running,
            "lease_recovery_count": recovered,
            "recent_failure_categories": {row["error_class"]: row["count"] for row in failures},
            "execution_duration_seconds": {
                "count": len(seconds),
                "average_seconds": sum(seconds) / len(seconds) if seconds else None,
                "max_seconds": max(seconds) if seconds else None,
            },
        }


class BackgroundJobYielded(Exception):
    """A handler atomically persisted progress and released its lease to queued."""


class BackgroundJobFailure(Exception):
    """Handler-owned, payload-free failure code; no domain policy in the worker."""

    def __init__(self, error_class: str, *, retryable: bool = False):
        if not error_class or len(error_class) > 80 or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in error_class
        ):
            raise ValueError("invalid_background_failure_code")
        super().__init__(error_class)
        self.error_class, self.retryable = error_class, retryable


class BackgroundJobWorker:
    """Small bounded thread pool runner; handlers run outside queue transactions."""

    def __init__(self, store: BackgroundJobStore, handlers: Mapping[str, Callable[[dict[str, Any]], None]],
                 *, worker_count: int = 1, lease_seconds: float = 60, poll_seconds: float = 1) -> None:
        if worker_count < 1 or worker_count > 32:
            raise ValueError("worker_count must be between 1 and 32")
        self.store = store
        self.handlers = dict(handlers)
        self.worker_count = worker_count
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._owner = f"background-{uuid4().hex}"

    def run_one(self, owner: str | None = None) -> bool:
        worker_owner = owner or self._owner
        # Independent domain workers may share the durable queue. Never take
        # a job this worker cannot execute (including during lease recovery).
        job = self.store.claim(worker_owner, self.lease_seconds, kinds=tuple(self.handlers))
        if job is None:
            return False
        handler = self.handlers.get(job["kind"])
        try:
            if handler is None:
                self.store.fail(job["job_id"], worker_owner, job["lease_epoch"], "unknown_job_kind", False)
            else:
                handler(job)
                self.store.complete(job["job_id"], worker_owner, job["lease_epoch"])
        except BackgroundJobYielded:
            # The handler's transaction already moved the same job to queued.
            # This is neither success nor a failed/retried provider attempt.
            pass
        except BackgroundBudgetDeferred:
            self.store.defer(job["job_id"], worker_owner, job["lease_epoch"])
        except Exception as exc:  # noqa: BLE001 - worker boundary classifies failures.
            error_class, retryable = _classify_worker_error(exc)
            self.store.fail(job["job_id"], worker_owner, job["lease_epoch"], error_class, retryable)
        return True

    def start(self) -> None:
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        if self._threads:
            return
        self._stop.clear()
        self.store.compact_history()
        for index in range(self.worker_count):
            thread = threading.Thread(target=self._loop, args=(f"{self._owner}-{index}",), daemon=True,
                                      name=f"lka-background-{index}")
            thread.start()
            self._threads.append(thread)

    def _loop(self, owner: str) -> None:
        while not self._stop.is_set():
            try:
                if not self.run_one(owner):
                    self._stop.wait(self.poll_seconds)
            except Exception:  # noqa: BLE001 - keep worker alive after unexpected errors.
                self._stop.wait(self.poll_seconds)

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=max(0, timeout))
        # A timed-out handler cannot be killed safely. Retain its thread and
        # stop event so an immediate start cannot revive it beside a new pool.
        self._threads = [thread for thread in self._threads if thread.is_alive()]


def _classify_worker_error(exc: Exception) -> tuple[str, bool]:
    """Return a stable, payload-free failure class and retry policy."""
    if isinstance(exc, BackgroundJobFailure):
        return exc.error_class, exc.retryable
    if isinstance(exc, BackgroundBudgetDeferred):
        return "background_budget_deferred", True
    if isinstance(exc, BackgroundTaskBudgetExceeded):
        return getattr(exc, "error_category", "background_task_budget_exceeded"), False
    if isinstance(exc, LLMContextCapacityError):
        return exc.error_category, False
    if isinstance(exc, LLMProviderHTTPError):
        status = exc.status_code
        retryable = (
            exc.is_retriable
            if exc.is_retriable is not None
            else status in {408, 429} or 500 <= status < 600
        )
        return f"provider_http_{status}", retryable
    if isinstance(exc, LLMTimeoutError):
        return "provider_timeout", True
    if isinstance(exc, LLMNetworkError):
        return "provider_network", True
    if isinstance(exc, LLMResponseParseError):
        return "model_output_invalid", False
    if isinstance(exc, LLMClientError) and getattr(exc, "error_category", None) == "incomplete_generation":
        # Immediate generation recovery has already been exhausted. Repeating
        # the same job would merely reset its two-call recovery allowance.
        return "incomplete_generation", False
    if isinstance(exc, TimeoutError):
        return "timeout", True
    if isinstance(exc, ConnectionError):
        return "connection_unavailable", True
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            return "database_busy", True
        return "database_operational_error", False
    return "handler_error", False
