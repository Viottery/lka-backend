"""Explicit watch subscriptions over already published results; no new source reads."""
from __future__ import annotations

import json
from contextlib import closing
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domains.management import ManagementConflictError
from app.domains.management_goals import _instant
from app.storage.commit_signals import notify_after_commit


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WatchSubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    watch_id: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(default=0, ge=0)
    enabled: bool = True
    condition: Literal["published", "changes"] = "published"
    goal_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("watch_id", "goal_id")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("IDs must not be blank")
        return value


class ManagementWatchService:
    def __init__(self, conn_factory, *, watches, goals, tasks):
        self._conn, self.watches, self.goals, self.tasks = conn_factory, watches, goals, tasks

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS manager_watch_subscriptions (
                    session_id TEXT NOT NULL, watch_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), condition TEXT NOT NULL,
                    goal_id TEXT, scope_version INTEGER NOT NULL, cursor INTEGER NOT NULL,
                    PRIMARY KEY(session_id,watch_id));
                CREATE TABLE IF NOT EXISTS manager_watch_commands (
                    session_id TEXT NOT NULL, command_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    receipt TEXT NOT NULL, PRIMARY KEY(session_id,command_id));
            """)

    @staticmethod
    def _view(row):
        if row is None:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def _get(self, conn, session_id, watch_id):
        return self._view(conn.execute("SELECT * FROM manager_watch_subscriptions WHERE session_id=? AND watch_id=?",
                                       (session_id, watch_id)).fetchone())

    def get(self, session_id, watch_id):
        with closing(self._conn()) as conn:
            self.goals._manager(conn, session_id)
            return self._get(conn, session_id, watch_id)

    def list_subscriptions(self, session_id, *, limit=50, offset=0):
        self.goals._page(limit, offset)
        with closing(self._conn()) as conn:
            self.goals._manager(conn, session_id)
            rows = conn.execute("SELECT * FROM manager_watch_subscriptions WHERE session_id=? ORDER BY watch_id LIMIT ? OFFSET ?",
                                (session_id, limit + 1, offset)).fetchall()
            return {"subscriptions": [self._view(row) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}

    def available_watches(self, session_id, *, q="", limit=30, offset=0):
        """Discover watch identities without exposing source scope or raw configuration."""
        self.goals._page(limit, offset)
        if not isinstance(q, str) or len(q) > 500:
            raise ValueError("Watch query must contain at most 500 characters")
        with closing(self._conn()) as conn:
            self.goals._manager(conn, session_id)
            rows = conn.execute("SELECT watch_id FROM watches WHERE status!='deleted' "
                                "AND instr(lower(title),lower(?))>0 ORDER BY updated_at DESC,watch_id LIMIT ? OFFSET ?",
                                (q, limit + 1, offset)).fetchall()
            items = []
            for row in rows[:limit]:
                try:
                    watch = self.watches.get(row["watch_id"])
                except KeyError:
                    continue
                items.append({"watch_id": watch["watch_id"], "title": watch["title"][:500],
                              "goal_preview": watch["goal"][:300], "status": watch["status"],
                              "scope_version": watch["version"], "timezone": watch["timezone"],
                              "daily_time": watch["daily_time"]})
            return {"watches": items, "next_offset": offset + limit if len(rows) > limit else None}

    @notify_after_commit
    def configure(self, session_id, command_id, watch_id, expected_revision=0, enabled=True,
                  condition="published", goal_id=None, *, authority_check=None):
        args = WatchSubscriptionInput.model_validate({"watch_id": watch_id, "expected_revision": expected_revision,
                    "enabled": enabled, "condition": condition, "goal_id": goal_id})
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 128:
            raise ValueError("command_id must contain 1..128 nonblank characters")
        fingerprint = sha256(_json(args.model_dump()).encode()).hexdigest()
        # WatchService is the public authority for resolving this explicit watch.
        watch = self.watches.get(watch_id)
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if authority_check is not None:
                authority_check(conn)
            self.goals._manager(conn, session_id)
            command = conn.execute("SELECT * FROM manager_watch_commands WHERE session_id=? AND command_id=?",
                                   (session_id, command_id)).fetchone()
            if command:
                if command["fingerprint"] != fingerprint:
                    raise ManagementConflictError("Watch subscription command changed")
                return json.loads(command["receipt"])
            current = conn.execute("SELECT status,version FROM watches WHERE watch_id=?", (watch_id,)).fetchone()
            if current is None or current["status"] == "deleted" or current["version"] != watch["version"]:
                raise ManagementConflictError("Watch scope changed")
            if args.enabled and current["status"] != "active":
                raise ManagementConflictError("Activate the watch before subscribing")
            prior = self._get(conn, session_id, watch_id)
            if (prior["revision"] if prior else 0) != args.expected_revision:
                raise ManagementConflictError("Watch subscription revision changed")
            if args.goal_id is not None:
                goal = conn.execute("SELECT session_id FROM manager_goals WHERE goal_id=?", (args.goal_id,)).fetchone()
                if goal is None or goal["session_id"] != session_id:
                    raise ValueError("Goal must belong to this manager")
                if args.enabled and not self.goals.enabled_in_transaction(conn, args.goal_id):
                    raise ManagementConflictError("Goal must be active")
            # Each explicit configuration starts after currently published results.
            cursor = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM watch_briefings WHERE watch_id=?", (watch_id,)).fetchone()[0]
            conn.execute("INSERT INTO manager_watch_subscriptions VALUES(?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(session_id,watch_id) DO UPDATE SET revision=excluded.revision,enabled=excluded.enabled,"
                         "condition=excluded.condition,goal_id=excluded.goal_id,scope_version=excluded.scope_version,cursor=excluded.cursor",
                         (session_id, watch_id, args.expected_revision + 1, int(args.enabled), args.condition,
                          args.goal_id, current["version"], cursor))
            receipt = self._get(conn, session_id, watch_id)
            conn.execute("INSERT INTO manager_watch_commands VALUES(?,?,?,?)", (session_id, command_id, fingerprint, _json(receipt)))
            if authority_check is not None:
                authority_check(conn)
            return receipt

    def _current(self, conn, policy):
        try:
            self.goals._manager(conn, policy["session_id"])
        except KeyError:
            return False
        watch = conn.execute("SELECT status,version FROM watches WHERE watch_id=?", (policy["watch_id"],)).fetchone()
        if not policy["enabled"] or watch is None or watch["status"] != "active" or watch["version"] != policy["scope_version"]:
            return False
        if policy["goal_id"] is not None:
            goal = conn.execute("SELECT session_id FROM manager_goals WHERE goal_id=?", (policy["goal_id"],)).fetchone()
            if goal is None or goal["session_id"] != policy["session_id"] or not self.goals.enabled_in_transaction(conn, policy["goal_id"]):
                return False
        return True

    def tick(self, now=None):
        stamp = _instant(now).isoformat()
        captured = 0
        read_conn = self._conn()
        try:
            read_conn.execute("BEGIN")
            policies = read_conn.execute(
                "SELECT * FROM manager_watch_subscriptions WHERE enabled=1 ORDER BY session_id,watch_id"
            ).fetchall()
            has_candidates = any(
                self._current(read_conn, policy)
                and read_conn.execute(
                    "SELECT 1 FROM watch_briefings WHERE watch_id=? AND rowid>? LIMIT 1",
                    (policy["watch_id"], policy["cursor"]),
                ).fetchone() is not None
                for policy in policies
            )
            read_conn.commit()
        finally:
            read_conn.close()
        if not has_candidates:
            return 0
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            policies = conn.execute("SELECT * FROM manager_watch_subscriptions WHERE enabled=1 ORDER BY session_id,watch_id").fetchall()
            for policy in policies:
                if not self._current(conn, policy):
                    continue
                rows = conn.execute("SELECT b.rowid AS cursor,b.briefing_id,b.occurrence_id,"
                    "json_array_length(b.changes_payload) AS changes_count,o.scope_version,o.status "
                    "FROM watch_briefings b JOIN watch_occurrences o ON o.occurrence_id=b.occurrence_id AND o.watch_id=b.watch_id "
                    "WHERE b.watch_id=? AND b.rowid>? ORDER BY b.rowid LIMIT 50",
                    (policy["watch_id"], policy["cursor"])).fetchall()
                for row in rows:
                    if row["status"] != "succeeded" or row["scope_version"] != policy["scope_version"]:
                        continue
                    if policy["condition"] == "changes" and not row["changes_count"]:
                        continue
                    goal_revision = None
                    if policy["goal_id"]:
                        goal_revision = conn.execute("SELECT revision FROM manager_goals WHERE goal_id=?", (policy["goal_id"],)).fetchone()[0]
                    payload = {"watch_id": policy["watch_id"], "watch_subscription_revision": policy["revision"],
                               "scope_version": policy["scope_version"], "briefing_id": row["briefing_id"],
                               "occurrence_id": row["occurrence_id"]}
                    key = f"watch:{policy['watch_id']}:{policy['revision']}:{row['occurrence_id']}"
                    captured += self.goals._event(conn, policy["session_id"], key, "timer", payload, stamp,
                                                   policy["goal_id"], goal_revision)
                if rows:
                    conn.execute("UPDATE manager_watch_subscriptions SET cursor=? WHERE session_id=? AND watch_id=? AND revision=?",
                        (rows[-1]["cursor"], policy["session_id"], policy["watch_id"], policy["revision"]))
        return captured

    def validate_event(self, conn, event, *, now=None):
        payload = event["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        policy = self._get(conn, event["session_id"], payload.get("watch_id"))
        if policy is None or not self._current(conn, policy):
            return False
        if payload.get("watch_subscription_revision") != policy["revision"] or payload.get("scope_version") != policy["scope_version"]:
            return False
        if event.get("goal_id") != policy["goal_id"]:
            return False
        if policy["goal_id"]:
            goal = conn.execute("SELECT revision FROM manager_goals WHERE goal_id=?", (policy["goal_id"],)).fetchone()
            if goal["revision"] != event.get("goal_revision"):
                return False
        row = conn.execute("SELECT o.status,o.scope_version,json_array_length(b.changes_payload) AS changes_count "
            "FROM watch_briefings b JOIN watch_occurrences o ON o.occurrence_id=b.occurrence_id AND o.watch_id=b.watch_id "
            "WHERE b.watch_id=? AND b.briefing_id=? AND b.occurrence_id=?",
            (policy["watch_id"], payload.get("briefing_id"), payload.get("occurrence_id"))).fetchone()
        return bool(row and row["status"] == "succeeded" and row["scope_version"] == policy["scope_version"]
                    and (policy["condition"] == "published" or row["changes_count"]))

    def read_notification(self, session_id, *, event_id=None, briefing_id=None, start_char=0, max_chars=4000):
        if (event_id is None) == (briefing_id is None):
            raise ValueError("Specify exactly one event_id or briefing_id")
        if type(start_char) is not int or start_char < 0 or type(max_chars) is not int or not 1 <= max_chars <= 4000:
            raise ValueError("Invalid notification page")
        with closing(self._conn()) as conn:
            self.goals._manager(conn, session_id)
            if event_id is not None:
                if type(event_id) is not int or event_id < 1:
                    raise ValueError("event_id must be positive")
                row = conn.execute("SELECT * FROM manager_inbox WHERE session_id=? AND event_id=?", (session_id, event_id)).fetchone()
            else:
                row = conn.execute("SELECT * FROM manager_inbox WHERE session_id=? AND json_extract(payload,'$.briefing_id')=? "
                                   "AND json_type(payload,'$.watch_subscription_revision')='integer' ORDER BY event_id DESC LIMIT 1",
                                   (session_id, briefing_id)).fetchone()
            if row is None or not self.validate_event(conn, dict(row)):
                raise PermissionError("No current authorized watch notification")
            event = dict(row)
            payload = json.loads(event["payload"])
            # The public watch reader remains responsible for decoding the result.
            briefing = self.watches.get_briefing(payload["briefing_id"])
            if briefing["watch_id"] != payload["watch_id"] or briefing["occurrence_id"] != payload["occurrence_id"]:
                raise PermissionError("Watch notification scope changed")
            if not self.validate_event(conn, event):
                raise PermissionError("Watch notification scope changed")
            encoded = _json(briefing)
            end = min(start_char + max_chars, len(encoded))
            return {"event_id": event["event_id"], "briefing_id": briefing["briefing_id"],
                    "content": encoded[start_char:end], "next_char": end if end < len(encoded) else None,
                    "total_chars": len(encoded)}
