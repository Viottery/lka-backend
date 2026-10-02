"""Persistent daily watches, occurrence leases, and local briefings."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware(value, "timestamp").isoformat()


def _decode(value: str) -> Any:
    return json.loads(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WatchInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=2000)
    timezone: str
    daily_time: str
    categories: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    importance_rules: dict[str, Any] = Field(default_factory=dict)
    delivery_policy: dict[str, Any] = Field(default_factory=lambda: {"channel": "session"})
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class WatchPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, min_length=1, max_length=2000)
    timezone: str | None = None
    daily_time: str | None = None
    categories: list[str] | None = None
    scope: dict[str, Any] | None = None
    importance_rules: dict[str, Any] | None = None
    delivery_policy: dict[str, Any] | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class BriefingInput(BaseModel):
    title: str
    summary: str
    changes: list[dict[str, Any]] = Field(default_factory=list)
    unchanged: list[dict[str, Any]] = Field(default_factory=list)
    unconfirmed: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    content_fingerprint: str | None = None


class WatchService:
    """A connection-factory based persistence service; it performs no scheduling."""

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]) -> None:
        self._conn_factory = conn_factory

    def initialize(self) -> None:
        """Create watch tables without coupling this service to global db init."""
        conn = self._conn_factory()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    watch_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    categories TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    daily_time TEXT NOT NULL,
                    starts_at TEXT,
                    ends_at TEXT,
                    importance_rules TEXT NOT NULL,
                    delivery_policy TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','paused','deleted')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_watches_status ON watches(status, updated_at);
                CREATE TABLE IF NOT EXISTS watch_occurrences (
                    occurrence_id TEXT PRIMARY KEY,
                    watch_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','cancelled')),
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    run_id TEXT,
                    error_category TEXT,
                    evidence_refs TEXT NOT NULL DEFAULT '[]',
                    content_fingerprint TEXT,
                    scope_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(watch_id, scheduled_for),
                    FOREIGN KEY(watch_id) REFERENCES watches(watch_id)
                );
                CREATE INDEX IF NOT EXISTS idx_watch_occurrences_claim
                    ON watch_occurrences(status, scheduled_for, lease_expires_at);
                CREATE TABLE IF NOT EXISTS watch_briefings (
                    briefing_id TEXT PRIMARY KEY,
                    watch_id TEXT NOT NULL,
                    occurrence_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    changes_payload TEXT NOT NULL,
                    unchanged_payload TEXT NOT NULL,
                    unconfirmed_payload TEXT NOT NULL,
                    decisions_payload TEXT NOT NULL,
                    evidence_refs TEXT NOT NULL,
                    content_fingerprint TEXT,
                    created_at TEXT NOT NULL,
                    read_at TEXT,
                    FOREIGN KEY(watch_id) REFERENCES watches(watch_id),
                    FOREIGN KEY(occurrence_id) REFERENCES watch_occurrences(occurrence_id)
                );
                CREATE INDEX IF NOT EXISTS idx_watch_briefings_inbox
                    ON watch_briefings(read_at, created_at DESC);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(watch_occurrences)")}
            if "scope_version" not in columns:
                conn.execute(
                    "ALTER TABLE watch_occurrences ADD COLUMN scope_version INTEGER NOT NULL DEFAULT 1"
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _validate_schedule(timezone: str, daily_time: str) -> tuple[ZoneInfo, time]:
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        try:
            parsed_time = time.fromisoformat(daily_time)
        except (TypeError, ValueError) as exc:
            raise ValueError("daily_time must be an ISO local time (HH:MM[:SS])") from exc
        if parsed_time.tzinfo is not None:
            raise ValueError("daily_time must be a local wall-clock time")
        return zone, parsed_time

    @staticmethod
    def _validate_capabilities(
        categories: list[str],
        scope: dict[str, Any],
        delivery_policy: dict[str, Any],
    ) -> None:
        if delivery_policy.get("channel", "session") != "session":
            raise ValueError("Only session delivery is currently supported")
        for field in ("source_ids", "account_ids", "workspace_paths"):
            values = scope.get(field, [])
            if (
                not isinstance(values, list)
                or len(values) > 50
                or any(not isinstance(value, str) or not value.strip() for value in values)
            ):
                raise ValueError(f"scope.{field} must be a list of at most 50 non-empty strings")
        for field in (
            "web_enabled",
            "matter_enabled",
            "knowledge_enabled",
            "allow_mixed_private_external",
        ):
            if field in scope and not isinstance(scope[field], bool):
                raise ValueError(f"scope.{field} must be a boolean")
        has_web = scope.get("web_enabled", "web" in categories or "news" in categories)
        has_sources = bool(scope.get("source_ids"))
        if not has_web and not has_sources and not scope.get("workspace_paths"):
            raise ValueError("Watch needs web/news access, source_ids, or workspace_paths")
        if has_web and scope.get("account_ids") and not scope.get("allow_mixed_private_external"):
            raise ValueError(
                "Mixed private account and external web access needs "
                "scope.allow_mixed_private_external=true"
            )

    @staticmethod
    def _next_run_from(
        *,
        timezone: str,
        daily_time: str,
        after: datetime,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> datetime | None:
        zone, local_time = WatchService._validate_schedule(timezone, daily_time)
        after_utc = _aware(after, "after")
        start_utc = _aware(starts_at, "starts_at") if starts_at else None
        end_utc = _aware(ends_at, "ends_at") if ends_at else None
        local_after = after_utc.astimezone(zone)
        day = local_after.date()
        for _ in range(3700):
            candidate = datetime.combine(day, local_time, tzinfo=zone)
            candidate_utc = candidate.astimezone(UTC)
            if candidate_utc > after_utc and (start_utc is None or candidate_utc >= start_utc):
                if end_utc is not None and candidate_utc > end_utc:
                    return None
                return candidate_utc
            day += timedelta(days=1)
        return None

    def create(self, payload: WatchInput, *, now: datetime | None = None) -> dict[str, Any]:
        self._validate_schedule(payload.timezone, payload.daily_time)
        self._validate_capabilities(payload.categories, payload.scope, payload.delivery_policy)
        if not payload.title.strip() or not payload.goal.strip():
            raise ValueError("title and goal must not be blank")
        if (
            payload.starts_at
            and payload.ends_at
            and _aware(payload.ends_at, "ends_at") < _aware(payload.starts_at, "starts_at")
        ):
            raise ValueError("ends_at must not precede starts_at")
        current = _aware(now or _now(), "now")
        watch_id, stamp = f"watch_{uuid.uuid4().hex}", _iso(current)
        conn = self._conn_factory()
        try:
            conn.execute(
                """INSERT INTO watches(watch_id,title,goal,categories,scope,timezone,daily_time,
                   starts_at,ends_at,importance_rules,delivery_policy,status,version,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',1,?,?)""",
                (
                    watch_id,
                    payload.title.strip(),
                    payload.goal.strip(),
                    _json(payload.categories),
                    _json(payload.scope),
                    payload.timezone,
                    payload.daily_time,
                    _iso(payload.starts_at) if payload.starts_at else None,
                    _iso(payload.ends_at) if payload.ends_at else None,
                    _json(payload.importance_rules),
                    _json(payload.delivery_policy),
                    stamp,
                    stamp,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get(watch_id)

    def _watch(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("categories", "scope", "importance_rules", "delivery_policy"):
            result[key] = _decode(result[key])
        result["next_run"] = (
            self._next_run_from(
                timezone=result["timezone"],
                daily_time=result["daily_time"],
                after=_now(),
                starts_at=datetime.fromisoformat(result["starts_at"])
                if result["starts_at"]
                else None,
                ends_at=datetime.fromisoformat(result["ends_at"]) if result["ends_at"] else None,
            )
            if result["status"] == "active"
            else None
        )
        return result

    def get(self, watch_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
        conn = self._conn_factory()
        try:
            row = conn.execute("SELECT * FROM watches WHERE watch_id=?", (watch_id,)).fetchone()
        finally:
            conn.close()
        if row is None or (row["status"] == "deleted" and not include_deleted):
            raise KeyError(watch_id)
        return self._watch(row)

    def is_current_scope(self, watch_id: str, version: int) -> bool:
        """True only while the exact active watch authorization version remains current."""
        conn = self._conn_factory()
        try:
            row = conn.execute(
                "SELECT status,version FROM watches WHERE watch_id=?", (watch_id,)
            ).fetchone()
        finally:
            conn.close()
        return bool(row and row["status"] == "active" and row["version"] == version)

    def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if status not in (None, "active", "paused", "deleted"):
            raise ValueError("invalid watch status")
        conn = self._conn_factory()
        try:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM watches WHERE status != 'deleted' ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM watches WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
        finally:
            conn.close()
        return [self._watch(row) for row in rows]

    def update(self, watch_id: str, patch: WatchPatch) -> dict[str, Any]:
        changes = patch.model_dump(exclude_unset=True)
        if not changes:
            return self.get(watch_id)
        current = self.get(watch_id)
        merged = {**current, **changes}
        self._validate_capabilities(
            merged["categories"],
            merged["scope"],
            merged["delivery_policy"],
        )
        if not str(merged["title"]).strip() or not str(merged["goal"]).strip():
            raise ValueError("title and goal must not be blank")
        self._validate_schedule(merged["timezone"], merged["daily_time"])
        start, end = merged.get("starts_at"), merged.get("ends_at")
        # Persisted records expose ISO strings; supplied patch dates are datetimes.
        if isinstance(start, str):
            start = datetime.fromisoformat(start)
        if isinstance(end, str):
            end = datetime.fromisoformat(end)
        if start and end and _aware(end, "ends_at") < _aware(start, "starts_at"):
            raise ValueError("ends_at must not precede starts_at")
        mapping = {
            "title": "title",
            "goal": "goal",
            "categories": "categories",
            "scope": "scope",
            "timezone": "timezone",
            "daily_time": "daily_time",
            "starts_at": "starts_at",
            "ends_at": "ends_at",
            "importance_rules": "importance_rules",
            "delivery_policy": "delivery_policy",
        }
        assignments, params = [], []
        for key, value in changes.items():
            assignments.append(f"{mapping[key]}=?")
            if key in {"categories", "scope", "importance_rules", "delivery_policy"}:
                value = _json(value)
            elif key in {"starts_at", "ends_at"}:
                value = _iso(value) if value else None
            elif key in {"title", "goal"}:
                value = value.strip()
            params.append(value)
        assignments.extend(("version=version+1", "updated_at=?"))
        params.extend((_iso(_now()), watch_id))
        conn = self._conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                f"UPDATE watches SET {', '.join(assignments)} WHERE watch_id=? AND status!='deleted'",
                params,
            )
            if cursor.rowcount == 0:
                raise KeyError(watch_id)
            conn.execute(
                "UPDATE watch_occurrences SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE watch_id=? AND status IN ('pending','running')",
                (_iso(_now()), watch_id),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get(watch_id)

    def set_paused(self, watch_id: str, paused: bool) -> dict[str, Any]:
        conn = self._conn_factory()
        try:
            cursor = conn.execute(
                "UPDATE watches SET status=?,version=version+1,updated_at=? WHERE watch_id=? AND status IN ('active','paused')",
                ("paused" if paused else "active", _iso(_now()), watch_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(watch_id)
            if paused:
                conn.execute(
                    "UPDATE watch_occurrences SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE watch_id=? AND status IN ('pending','running')",
                    (_iso(_now()), watch_id),
                )
            conn.commit()
        finally:
            conn.close()
        return self.get(watch_id)

    def delete(self, watch_id: str) -> dict[str, Any]:
        conn = self._conn_factory()
        try:
            stamp = _iso(_now())
            cursor = conn.execute(
                "UPDATE watches SET status='deleted',deleted_at=?,updated_at=?,version=version+1 WHERE watch_id=? AND status!='deleted'",
                (stamp, stamp, watch_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(watch_id)
            conn.execute(
                "UPDATE watch_occurrences SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE watch_id=? AND status IN ('pending','running')",
                (stamp, watch_id),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get(watch_id, include_deleted=True)

    def next_run(self, watch_id: str, *, after: datetime | None = None) -> datetime | None:
        watch = self.get(watch_id)
        if watch["status"] != "active":
            return None
        return self._next_run_from(
            timezone=watch["timezone"],
            daily_time=watch["daily_time"],
            after=after or _now(),
            starts_at=datetime.fromisoformat(watch["starts_at"]) if watch["starts_at"] else None,
            ends_at=datetime.fromisoformat(watch["ends_at"]) if watch["ends_at"] else None,
        )

    def create_occurrence(self, watch_id: str, scheduled_for: datetime) -> dict[str, Any]:
        scheduled = _iso(scheduled_for)
        stamp, occurrence_id = _iso(_now()), f"occ_{uuid.uuid4().hex}"
        conn = self._conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            watch = conn.execute(
                "SELECT status,version FROM watches WHERE watch_id=?", (watch_id,)
            ).fetchone()
            if watch is None or watch["status"] != "active":
                raise KeyError(watch_id)
            conn.execute(
                "INSERT OR IGNORE INTO watch_occurrences(occurrence_id,watch_id,scheduled_for,status,evidence_refs,scope_version,created_at,updated_at) VALUES(?,?,?,'pending','[]',?,?,?)",
                (occurrence_id, watch_id, scheduled, watch["version"], stamp, stamp),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_occurrence(watch_id, scheduled_for)

    def get_occurrence(self, watch_id: str, scheduled_for: datetime) -> dict[str, Any]:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                "SELECT * FROM watch_occurrences WHERE watch_id=? AND scheduled_for=?",
                (watch_id, _iso(scheduled_for)),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise KeyError((watch_id, scheduled_for))
        result = dict(row)
        result["evidence_refs"] = _decode(result["evidence_refs"])
        result["session_id"] = f"watch_session_{result['occurrence_id']}"
        return result

    def list_occurrences(self, watch_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        self.get(watch_id)
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                "SELECT * FROM watch_occurrences WHERE watch_id=? ORDER BY scheduled_for DESC LIMIT ?",
                (watch_id, limit),
            ).fetchall()
        finally:
            conn.close()
        result = [dict(row) for row in rows]
        for item in result:
            item["evidence_refs"] = _decode(item["evidence_refs"])
            item["session_id"] = f"watch_session_{item['occurrence_id']}"
        return result

    def claim_occurrence(
        self,
        *,
        owner: str,
        now: datetime | None = None,
        lease_for: timedelta = timedelta(minutes=5),
    ) -> dict[str, Any] | None:
        if not owner.strip() or lease_for.total_seconds() <= 0:
            raise ValueError("owner and positive lease_for are required")
        current = _aware(now or _now(), "now")
        expiry = current + lease_for
        conn = self._conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT o.* FROM watch_occurrences o JOIN watches w USING(watch_id)
                   WHERE w.status='active' AND o.scheduled_for<=? AND (
                     o.status='pending' OR (o.status='running' AND o.lease_expires_at<=?)
                   ) ORDER BY o.scheduled_for,o.created_at LIMIT 1""",
                (_iso(current), _iso(current)),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            stamp = _iso(current)
            conn.execute(
                "UPDATE watch_occurrences SET status='running',lease_owner=?,lease_expires_at=?,started_at=COALESCE(started_at,?),updated_at=? WHERE occurrence_id=?",
                (owner, _iso(expiry), stamp, stamp, row["occurrence_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        conn = self._conn_factory()
        try:
            claimed = conn.execute(
                "SELECT * FROM watch_occurrences WHERE occurrence_id=?", (row["occurrence_id"],)
            ).fetchone()
        finally:
            conn.close()
        result = dict(claimed)
        result["evidence_refs"] = _decode(result["evidence_refs"])
        result["session_id"] = f"watch_session_{result['occurrence_id']}"
        return result

    def renew_lease(
        self,
        occurrence_id: str,
        *,
        owner: str,
        lease_for: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> bool:
        if lease_for.total_seconds() <= 0:
            raise ValueError("lease_for must be positive")
        current = _aware(now or _now(), "now")
        conn = self._conn_factory()
        try:
            cursor = conn.execute(
                """UPDATE watch_occurrences SET lease_expires_at=?,updated_at=?
                   WHERE occurrence_id=? AND lease_owner=? AND status='running'
                   AND lease_expires_at>? AND EXISTS (
                       SELECT 1 FROM watches WHERE watch_id=watch_occurrences.watch_id
                       AND status='active')""",
                (_iso(current + lease_for), _iso(current), occurrence_id, owner, _iso(current)),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def complete_with_briefing(
        self,
        occurrence_id: str,
        *,
        owner: str,
        payload: BriefingInput,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically claim success and publish one briefing under the active lease."""
        current = _aware(now or _now(), "now")
        stamp = _iso(current)
        conn = self._conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT o.watch_id FROM watch_occurrences o
                   JOIN watches w ON w.watch_id=o.watch_id
                   WHERE o.occurrence_id=? AND o.lease_owner=? AND o.status='running'
                   AND o.lease_expires_at>? AND w.status='active' AND o.scope_version=w.version""",
                (occurrence_id, owner, stamp),
            ).fetchone()
            if row is None:
                raise ValueError("occurrence lease is lost or watch is inactive")
            evidence = _json(payload.evidence)
            existing = conn.execute(
                "SELECT briefing_id FROM watch_briefings WHERE occurrence_id=?",
                (occurrence_id,),
            ).fetchone()
            briefing_id = existing["briefing_id"] if existing else f"brief_{uuid.uuid4().hex}"
            if existing is None:
                conn.execute(
                    """INSERT INTO watch_briefings(
                       briefing_id,watch_id,occurrence_id,title,summary,changes_payload,
                       unchanged_payload,unconfirmed_payload,decisions_payload,evidence_refs,
                       content_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        briefing_id,
                        row["watch_id"],
                        occurrence_id,
                        payload.title,
                        payload.summary,
                        _json(payload.changes),
                        _json(payload.unchanged),
                        _json(payload.unconfirmed),
                        _json(payload.decisions),
                        evidence,
                        payload.content_fingerprint,
                        stamp,
                    ),
                )
            conn.execute(
                """UPDATE watch_occurrences SET status='succeeded',lease_owner=NULL,
                   lease_expires_at=NULL,finished_at=?,updated_at=?,run_id=?,
                   evidence_refs=?,content_fingerprint=? WHERE occurrence_id=?""",
                (stamp, stamp, run_id, evidence, payload.content_fingerprint, occurrence_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_briefing(briefing_id)

    def finish_occurrence(
        self,
        occurrence_id: str,
        *,
        owner: str,
        succeeded: bool,
        run_id: str | None = None,
        error_category: str | None = None,
        evidence_refs: Sequence[Mapping[str, Any]] = (),
        content_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _aware(now or _now(), "now")
        stamp = _iso(current)
        conn = self._conn_factory()
        try:
            cursor = conn.execute(
                """UPDATE watch_occurrences SET status=?,lease_owner=NULL,lease_expires_at=NULL,
                finished_at=?,updated_at=?,run_id=?,error_category=?,evidence_refs=?,content_fingerprint=?
                WHERE occurrence_id=? AND status='running' AND lease_owner=? AND lease_expires_at>?""",
                (
                    "succeeded" if succeeded else "failed",
                    stamp,
                    stamp,
                    run_id,
                    error_category,
                    _json(list(evidence_refs)),
                    content_fingerprint,
                    occurrence_id,
                    owner,
                    _iso(current),
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("occurrence is not leased by this owner or its lease expired")
            conn.commit()
            row = conn.execute(
                "SELECT watch_id,scheduled_for FROM watch_occurrences WHERE occurrence_id=?",
                (occurrence_id,),
            ).fetchone()
        finally:
            conn.close()
        return self.get_occurrence(row["watch_id"], datetime.fromisoformat(row["scheduled_for"]))

    def get_briefing(self, briefing_id: str) -> dict[str, Any]:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                "SELECT * FROM watch_briefings WHERE briefing_id=?", (briefing_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise KeyError(briefing_id)
        result = dict(row)
        result["session_id"] = f"watch_session_{result['occurrence_id']}"
        for key, column in (
            ("changes", "changes_payload"),
            ("unchanged", "unchanged_payload"),
            ("unconfirmed", "unconfirmed_payload"),
            ("decisions", "decisions_payload"),
            ("evidence", "evidence_refs"),
        ):
            result[key] = _decode(result.pop(column))
        return result

    def list_briefings(
        self, *, unread_only: bool = False, watch_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses, params = [], []
        if unread_only:
            clauses.append("read_at IS NULL")
        if watch_id:
            clauses.append("watch_id=?")
            params.append(watch_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"SELECT briefing_id FROM watch_briefings{where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()
        return [self.get_briefing(row["briefing_id"]) for row in rows]

    def mark_briefing_read(
        self, briefing_id: str, *, read: bool = True, now: datetime | None = None
    ) -> dict[str, Any]:
        conn = self._conn_factory()
        try:
            cursor = conn.execute(
                "UPDATE watch_briefings SET read_at=? WHERE briefing_id=?",
                (_iso(now or _now()) if read else None, briefing_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(briefing_id)
            conn.commit()
        finally:
            conn.close()
        return self.get_briefing(briefing_id)
