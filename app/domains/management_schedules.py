"""Explicit, durable wall-clock schedules. No model calls or autonomous writes."""
from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domains.management import ManagementConflictError
from app.domains.management_goals import _instant
from app.storage.commit_signals import notify_after_commit


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ManagementScheduleInput(BaseModel):
    """Full replacement configuration; `now` is server-only and not part of this schema."""
    model_config = ConfigDict(extra="forbid", strict=True)
    schedule_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=1)
    goal_id: str = Field(min_length=1, max_length=256)
    timezone: str = Field(min_length=1, max_length=128)
    kind: Literal["one_shot", "daily", "weekly"]
    local_time: str | None = Field(default=None, max_length=5)
    weekday: int | None = Field(default=None, ge=0, le=6)
    run_at: str | None = Field(default=None, max_length=80)
    expires_at: str | None = Field(default=None, max_length=80)
    enabled: bool = True

    @field_validator("goal_id", "schedule_id")
    @classmethod
    def nonblank_id(cls, value):
        if value is not None and not value.strip():
            raise ValueError("IDs must not be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    @field_validator("local_time")
    @classmethod
    def wall_time(cls, value):
        if value is not None:
            if not re.fullmatch(r"\d{2}:\d{2}", value):
                raise ValueError("local_time must be HH:MM")
            time.fromisoformat(value)
        return value

    @field_validator("run_at", "expires_at")
    @classmethod
    def aware_time(cls, value):
        return _instant(value).isoformat() if value is not None else None

    @model_validator(mode="after")
    def coherent_shape(self):
        if self.kind == "one_shot":
            if self.run_at is None or self.local_time is not None or self.weekday is not None:
                raise ValueError("one_shot requires run_at, without local_time or weekday")
            if self.expires_at is not None and _instant(self.expires_at) <= _instant(self.run_at):
                raise ValueError("expires_at must be later than run_at")
        elif self.local_time is None or self.run_at is not None:
            raise ValueError("daily/weekly require local_time and no run_at")
        if self.kind == "weekly" and self.weekday is None:
            raise ValueError("weekly requires weekday (Monday=0)")
        if self.kind == "daily" and self.weekday is not None:
            raise ValueError("daily does not accept weekday")
        return self


def _wall_occurrence(policy, day):
    zone = ZoneInfo(policy["timezone"])
    local = datetime.combine(day, time.fromisoformat(policy["local_time"]), tzinfo=zone).replace(fold=0)
    instant = local.astimezone(UTC)
    # A nonexistent wall time cannot round-trip. Ambiguous times consistently
    # choose fold=0, never a second occurrence from the repeated clock hour.
    if instant.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        return None
    return instant


def _occurrence(policy, pivot, *, future, inclusive):
    pivot = _instant(pivot)
    if policy["kind"] == "one_shot":
        candidate = _instant(policy["run_at"])
        candidates = [candidate]
    else:
        local_day = pivot.astimezone(ZoneInfo(policy["timezone"])).date()
        direction = 1 if future else -1
        candidates = []
        # Fifteen local days cover the next/previous weekly time even when a
        # DST gap skips that week's wall time; no historical backlog loop.
        for offset in range(15):
            day = local_day + timedelta(days=offset * direction)
            if policy["kind"] == "weekly" and day.weekday() != policy["weekday"]:
                continue
            candidate = _wall_occurrence(policy, day)
            if candidate is not None:
                candidates.append(candidate)
    expiry = _instant(policy["expires_at"]) if policy.get("expires_at") else None
    for candidate in candidates:
        valid = candidate >= pivot if future and inclusive else candidate > pivot if future else candidate <= pivot if inclusive else candidate < pivot
        if valid and (expiry is None or candidate < expiry):
            return candidate
    return None


def next_occurrence(policy, after, *, inclusive=False):
    """Return one UTC instant or None; accepts a validated model or stored row."""
    if isinstance(policy, ManagementScheduleInput):
        policy = policy.model_dump(mode="json")
    return _occurrence(policy, after, future=True, inclusive=inclusive)


class ManagementScheduleService:
    def __init__(self, conn_factory, goals, management):
        self._conn, self.goals, self.management = conn_factory, goals, management

    def ensure_schema(self):
        self.goals.ensure_schema()
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS manager_schedules (
                    schedule_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, goal_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision>=1), timezone TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('one_shot','daily','weekly')),
                    local_time TEXT, weekday INTEGER, run_at TEXT, expires_at TEXT,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), next_run_at TEXT,
                    last_occurrence_at TEXT, last_outcome TEXT CHECK(last_outcome IN ('captured','skipped')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS manager_schedules_due ON manager_schedules(next_run_at);
                CREATE TABLE IF NOT EXISTS manager_schedule_commands (
                    session_id TEXT NOT NULL, command_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    receipt TEXT NOT NULL, PRIMARY KEY(session_id,command_id)
                );
            """)

    @staticmethod
    def _view(row):
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    @notify_after_commit
    def configure(self, session_id, command_id, *, now=None, authority_check=None, **fields):
        """Explicit configuration only; caller owns authorization and tool review."""
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 128:
            raise ValueError("command_id must contain 1..128 characters")
        args = ManagementScheduleInput.model_validate(fields)
        data = args.model_dump(mode="json")
        fingerprint = sha256(_json(data).encode()).hexdigest()
        instant = _instant(now)
        stamp = instant.isoformat()
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if authority_check is not None:
                authority_check(conn)
            self.goals._manager(conn, session_id)
            prior = conn.execute("SELECT fingerprint,receipt FROM manager_schedule_commands WHERE session_id=? AND command_id=?", (session_id, command_id)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ManagementConflictError("Schedule command changed")
                if authority_check is not None:
                    authority_check(conn)
                return json.loads(prior["receipt"])
            goal = conn.execute("SELECT session_id FROM manager_goals WHERE goal_id=?", (args.goal_id,)).fetchone()
            if goal is None or goal["session_id"] != session_id:
                raise ValueError("Goal must belong to this management session")
            if args.enabled and not self.goals.enabled_in_transaction(conn, args.goal_id):
                raise ManagementConflictError("Goal is not enabled")
            schedule_id = args.schedule_id or "schedule_" + sha256(_json([session_id, command_id]).encode()).hexdigest()[:32]
            old = conn.execute("SELECT * FROM manager_schedules WHERE schedule_id=?", (schedule_id,)).fetchone()
            if old is not None and old["session_id"] != session_id:
                raise KeyError(schedule_id)
            if old is None and args.expected_revision is not None:
                raise ManagementConflictError("Schedule is unavailable")
            if old is not None and args.expected_revision != old["revision"]:
                raise ManagementConflictError("Schedule revision changed")
            if args.enabled and args.expires_at is not None and _instant(args.expires_at) <= instant:
                raise ValueError("Schedule has expired")
            if args.enabled and args.kind == "one_shot" and _instant(args.run_at) < instant:
                raise ValueError("one_shot run_at must not be in the past")
            revision = old["revision"] + 1 if old else 1
            upcoming = next_occurrence(data, instant, inclusive=True) if args.enabled else None
            values = (schedule_id, session_id, args.goal_id, revision, args.timezone, args.kind,
                      args.local_time, args.weekday, args.run_at, args.expires_at, int(args.enabled),
                      upcoming.isoformat() if upcoming else None, old["created_at"] if old else stamp, stamp)
            conn.execute("INSERT INTO manager_schedules(schedule_id,session_id,goal_id,revision,timezone,kind,local_time,weekday,run_at,expires_at,enabled,next_run_at,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(schedule_id) DO UPDATE SET "
                         "goal_id=excluded.goal_id,revision=excluded.revision,timezone=excluded.timezone,kind=excluded.kind,local_time=excluded.local_time,weekday=excluded.weekday,run_at=excluded.run_at,expires_at=excluded.expires_at,enabled=excluded.enabled,next_run_at=excluded.next_run_at,last_occurrence_at=NULL,last_outcome=NULL,updated_at=excluded.updated_at",
                         values)
            result = {"command_id": command_id, "status": "configured", "snapshot_kind": "command_commit",
                      "schedule": self._view(conn.execute("SELECT * FROM manager_schedules WHERE schedule_id=?", (schedule_id,)).fetchone())}
            conn.execute("INSERT INTO manager_schedule_commands VALUES(?,?,?,?)", (session_id, command_id, fingerprint, _json(result)))
            if authority_check is not None:
                authority_check(conn)
            return result

    def get(self, session_id, schedule_id):
        with closing(self._conn()) as conn:
            self.goals._manager(conn, session_id)
            row = conn.execute("SELECT * FROM manager_schedules WHERE schedule_id=? AND session_id=?", (schedule_id, session_id)).fetchone()
            if row is None:
                raise KeyError(schedule_id)
            return self._view(row)

    def list_schedules(self, session_id, *, limit=50, offset=0):
        self.goals._page(limit, offset)
        with closing(self._conn()) as conn:
            self.goals._manager(conn, session_id)
            rows = conn.execute("SELECT * FROM manager_schedules WHERE session_id=? ORDER BY created_at,schedule_id LIMIT ? OFFSET ?", (session_id, limit + 1, offset)).fetchall()
            return {"schedules": [self._view(row) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}

    def validate_event(self, conn, event, *, now=None):
        payload = event["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        row = conn.execute("SELECT * FROM manager_schedules WHERE schedule_id=?", (payload.get("schedule_id"),)).fetchone()
        if (row is None or not row["enabled"] or row["revision"] != payload.get("schedule_revision")
                or row["session_id"] != event["session_id"] or row["goal_id"] != event["goal_id"]
                or (row["expires_at"] is not None and _instant(row["expires_at"]) <= _instant(now))):
            return False
        goal = conn.execute("SELECT revision FROM manager_goals WHERE goal_id=?", (row["goal_id"],)).fetchone()
        return bool(goal and goal["revision"] == event["goal_revision"] and self.goals.enabled_in_transaction(conn, row["goal_id"]))

    def tick(self, now=None):
        """Capture at most the latest due occurrence and atomically advance its watermark."""
        instant = _instant(now)
        count = 0
        with closing(self._conn()) as probe:
            due_exists = probe.execute(
                "SELECT 1 FROM manager_schedules WHERE enabled=1 AND next_run_at IS NOT NULL "
                "AND next_run_at<=? LIMIT 1", (instant.isoformat(),),
            ).fetchone() is not None
        if not due_exists:
            return 0
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT p.*,c.status AS controller_status,s.status AS session_status FROM manager_schedules p "
                                "JOIN manager_controllers c ON c.session_id=p.session_id JOIN agent_sessions s ON s.session_id=p.session_id "
                                "WHERE p.enabled=1 AND p.next_run_at IS NOT NULL AND p.next_run_at<=?", (instant.isoformat(),)).fetchall()
            for row in rows:
                policy = dict(row)
                occurrence = _occurrence(policy, instant, future=False, inclusive=True)
                upcoming = next_occurrence(policy, instant)
                goal = conn.execute("SELECT * FROM manager_goals WHERE goal_id=? AND session_id=?", (row["goal_id"], row["session_id"])).fetchone()
                expired = row["expires_at"] is not None and _instant(row["expires_at"]) <= instant
                eligible = (not expired and row["controller_status"] == "active" and row["session_status"] != "deleted"
                            and goal is not None and self.goals.enabled_in_transaction(conn, row["goal_id"]))
                outcome = "skipped"
                if eligible and occurrence is not None and occurrence >= _instant(row["next_run_at"]):
                    occurrence_id = occurrence.isoformat()
                    payload = {"schedule_id": row["schedule_id"], "schedule_revision": row["revision"],
                               "occurrence": occurrence_id, "timer_generation": goal["timer_generation"]}
                    count += self.goals._event(conn, row["session_id"], f"schedule:{row['schedule_id']}:{row['revision']}:{occurrence_id}",
                                               "timer", payload, instant.isoformat(), row["goal_id"], goal["revision"])
                    outcome = "captured"
                conn.execute("UPDATE manager_schedules SET next_run_at=?,last_occurrence_at=?,last_outcome=?,updated_at=? WHERE schedule_id=?",
                             (upcoming.isoformat() if upcoming else None, occurrence.isoformat() if occurrence else None,
                              outcome, instant.isoformat(), row["schedule_id"]))
        return count
