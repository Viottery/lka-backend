"""Opt-in, bounded Matter attention capture; no model calls or external delivery."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domains.management import ManagementConflictError


def _instant(value=None):
    value = value if value is not None else datetime.now(UTC)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ManagementAttentionService:
    def __init__(self, conn_factory, *, matters, goals, management):
        self._conn = conn_factory
        self.matters, self.goals, self.management = matters, goals, management

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS manager_matter_attention_causes (
                    matter_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    cause_paths TEXT NOT NULL CHECK(json_valid(cause_paths) AND length(CAST(cause_paths AS BLOB))<=32768),
                    PRIMARY KEY(matter_id,revision)
                );
                CREATE TABLE IF NOT EXISTS manager_attention_policies (
                    session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), timezone TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL, scan_offset INTEGER NOT NULL DEFAULT 0
                );
            """)

    @staticmethod
    def _manager(conn, session_id):
        row = conn.execute("SELECT c.status FROM manager_controllers c JOIN agent_sessions s "
                           "ON s.session_id=c.session_id WHERE c.session_id=? "
                           "AND s.kind='manager' AND s.status!='deleted'", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return row["status"]

    @staticmethod
    def _horizon(value):
        if type(value) is not int or not 1 <= value <= 90:
            raise ValueError("horizon_days must be 1..90")
        return value

    def configure(self, session_id, expected_revision, enabled, timezone="Asia/Shanghai", horizon_days=7, *, authority_check=None):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be non-negative; use 0 for a new policy")
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if not isinstance(timezone, str) or not timezone or len(timezone) > 128:
            raise ValueError("timezone must be an IANA timezone name")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone name") from exc
        self._horizon(horizon_days)
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if authority_check is not None:
                authority_check(conn)
            self._manager(conn, session_id)
            prior = conn.execute("SELECT revision FROM manager_attention_policies WHERE session_id=?", (session_id,)).fetchone()
            if (prior[0] if prior else 0) != expected_revision:
                raise ManagementConflictError("Attention policy revision changed")
            conn.execute("INSERT INTO manager_attention_policies VALUES(?,?,?,?,?,0) "
                         "ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,"
                         "enabled=excluded.enabled,timezone=excluded.timezone,horizon_days=excluded.horizon_days,scan_offset=0",
                         (session_id, expected_revision + 1, int(enabled), timezone, horizon_days))
            if authority_check is not None:
                authority_check(conn)
            return self._policy(conn, session_id)

    @staticmethod
    def _policy(conn, session_id):
        row = conn.execute("SELECT * FROM manager_attention_policies WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item

    def get_policy(self, session_id):
        with closing(self._conn()) as conn:
            self._manager(conn, session_id)
            return self._policy(conn, session_id)

    get = get_policy

    @staticmethod
    def _candidates(matter, now, timezone, horizon_days):
        if matter.visibility != "user" or matter.status in {"done", "cancelled"}:
            return {}
        zone = ZoneInfo(timezone)
        today = now.astimezone(zone).date()
        phases = {}
        if matter.due_at is not None:
            due = matter.due_at
            if len(due) == 10:
                day = date.fromisoformat(due)
                overdue = day < today
                fact = due
            else:
                instant = datetime.fromisoformat(due).astimezone(UTC)
                day = instant.astimezone(zone).date()
                overdue = instant < now
                fact = instant.isoformat()
            if overdue:
                phases["overdue"] = _digest({"due_at": fact})
            elif day <= today + timedelta(days=horizon_days):
                phases["due_soon"] = _digest({"due_at": fact})
        scheduled = matter.scheduled_on == today.isoformat()
        if matter.scheduled_start:
            scheduled = scheduled or datetime.fromisoformat(matter.scheduled_start).astimezone(zone).date() == today
        if scheduled:
            facts = {"scheduled_on": matter.scheduled_on}
            for key in ("scheduled_start", "scheduled_end"):
                value = getattr(matter, key)
                facts[key] = datetime.fromisoformat(value).astimezone(UTC).isoformat() if value else None
            phases["scheduled_today"] = _digest(facts)
        return phases

    @staticmethod
    def _paths(value):
        if (not isinstance(value, dict) or len(value) > 64
                or any(not isinstance(key, str) or not key or len(key) > 256
                       or type(depth) is not int or depth < 0 for key, depth in value.items())):
            raise ManagementConflictError("Matter attention ancestry exceeds the bounded causal scope")
        return value

    def record_proposal_causes(self, conn, execution_id, session_id):
        """Record automatic Matter revisions inside the caller's final publication transaction."""
        if not conn.in_transaction:
            raise ValueError("Matter causal markers require a caller-owned transaction")
        source = conn.execute("SELECT r.record_payload FROM manager_executions e JOIN agent_runs r "
                              "ON r.run_id=e.run_id WHERE e.execution_id=? AND e.session_id=? "
                              "AND r.session_id=?", (execution_id, session_id, session_id)).fetchone()
        if source is None:
            raise ManagementConflictError("Management execution is unavailable")
        record = json.loads(source[0])
        if record.get("metadata", {}).get("manager_execution_id") != execution_id or record.get("parent_run_id"):
            raise ManagementConflictError("Matter causes require an autonomous management root")
        paths = self._paths(record.get("metadata", {}).get("manager_cause_paths", {}))
        encoded = json.dumps(paths, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > 32768:
            raise ManagementConflictError("Matter attention ancestry exceeds the bounded causal scope")
        rows = conn.execute("SELECT c.receipt FROM manager_work_proposals p JOIN management_commands c "
                            "ON c.manager_session_id=p.manager_session_id AND c.command_id=p.command_id "
                            "WHERE p.execution_id=? AND p.manager_session_id=? AND c.action='organize_work'",
                            (execution_id, session_id)).fetchall()
        for row in rows:
            receipt = json.loads(row[0])
            if receipt.get("status") != "applied" or receipt.get("execution_id") != execution_id:
                continue
            for result in receipt.get("results", []):
                if result.get("op") not in {"create", "update"} or type(result.get("revision")) is not int:
                    continue
                matter = self.matters.get_matter(matter_id=result["matter_id"], conn=conn)
                if matter.revision != result["revision"]:
                    continue
                conn.execute("INSERT INTO manager_matter_attention_causes VALUES(?,?,?) "
                             "ON CONFLICT(matter_id,revision) DO UPDATE SET cause_paths=excluded.cause_paths",
                             (matter.matter_id, matter.revision, encoded))

    def cause_paths(self, conn, event):
        payload = event["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        row = conn.execute("SELECT cause_paths FROM manager_matter_attention_causes WHERE matter_id=? AND revision=?",
                           (payload.get("matter_id"), payload.get("matter_revision"))).fetchone()
        if row is None:
            return None
        return {key: depth + 1 for key, depth in self._paths(json.loads(row[0])).items()}

    @staticmethod
    def _delivered(conn, session_id, matter_id, phase, facts_hash):
        return conn.execute("SELECT 1 FROM manager_inbox i "
                            "LEFT JOIN manager_executions e ON e.execution_id=i.consumed_execution_id AND e.session_id=i.session_id "
                            "LEFT JOIN manager_deliveries d ON d.execution_id=i.consumed_execution_id AND d.session_id=i.session_id "
                            "WHERE i.session_id=? AND i.kind='timer' "
                            "AND json_extract(i.payload,'$.matter_id')=? AND json_extract(i.payload,'$.phase')=? "
                            "AND json_extract(i.payload,'$.facts_hash')=? "
                            "AND (e.status='completed' OR d.delivery_id IS NOT NULL) LIMIT 1",
                            (session_id, matter_id, phase, facts_hash)).fetchone() is not None

    def _scan(self, conn, session_id, now, *, horizon_days=None, limit=50, offset=0, capture=True):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and offset non-negative")
        policy = self._policy(conn, session_id)
        if self._manager(conn, session_id) != "active" or not policy or not policy["enabled"]:
            return {"captured": 0, "scanned": 0, "next_offset": None}
        horizon = policy["horizon_days"] if horizon_days is None else self._horizon(horizon_days)
        if horizon > policy["horizon_days"]:
            raise ValueError("Scan horizon cannot exceed the saved attention policy")
        page = self.matters.list_matters(status="active", visibility="user", limit=limit, offset=offset, conn=conn)
        captured = 0
        for matter in page.matters:
            for phase, facts_hash in self._candidates(matter, now, policy["timezone"], horizon).items():
                if self._delivered(conn, session_id, matter.matter_id, phase, facts_hash):
                    continue
                key = "attention:" + _digest([session_id, matter.matter_id, phase, facts_hash]) + ":" + str(policy["revision"])
                if conn.execute("SELECT 1 FROM manager_inbox WHERE session_id=? AND dedup_key=?",
                                (session_id, key)).fetchone():
                    continue
                payload = {"attention_policy_revision": policy["revision"], "matter_id": matter.matter_id,
                           "matter_revision": matter.revision, "phase": phase, "facts_hash": facts_hash}
                captured += (self.goals._event(conn, session_id, key, "timer", payload, now.isoformat())
                             if capture else 1)
        return {"captured": captured, "scanned": len(page.matters), "next_offset": page.next_offset}

    def scan(self, session_id, now, horizon_days=None, limit=50, offset=0):
        instant = _instant(now)
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._scan(conn, session_id, instant, horizon_days=horizon_days, limit=limit, offset=offset)

    def tick(self, now=None):
        instant = _instant(now)
        with closing(self._conn()) as conn:
            ids = [row[0] for row in conn.execute("SELECT session_id FROM manager_attention_policies WHERE enabled=1 ORDER BY session_id")]
        captured = 0
        for session_id in ids:
            with closing(self._conn()) as conn, conn:
                conn.execute("BEGIN")
                policy = self._policy(conn, session_id)
                try:
                    active = self._manager(conn, session_id) == "active"
                except KeyError:
                    continue
                if not active or not policy or not policy["enabled"]:
                    continue
                preview = self._scan(conn, session_id, instant, offset=policy["scan_offset"], capture=False)
                if preview["captured"] == 0 and (preview["next_offset"] or 0) == policy["scan_offset"]:
                    continue
            with closing(self._conn()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                policy = self._policy(conn, session_id)
                # A deleted or paused controller retains its policy but does not advance the scan.
                try:
                    active = self._manager(conn, session_id) == "active"
                except KeyError:
                    continue
                if not active or not policy or not policy["enabled"]:
                    continue
                result = self._scan(conn, session_id, instant, offset=policy["scan_offset"])
                if (result["next_offset"] or 0) != policy["scan_offset"]:
                    conn.execute("UPDATE manager_attention_policies SET scan_offset=? WHERE session_id=? AND revision=?",
                                 (result["next_offset"] or 0, session_id, policy["revision"]))
                captured += result["captured"]
        return captured

    def validate_event(self, conn, event, *, now=None):
        """Recheck facts before use; edits to title/order alone do not invalidate an alert."""
        payload = event["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        policy = self._policy(conn, event["session_id"])
        if not policy or not policy["enabled"] or policy["revision"] != payload.get("attention_policy_revision"):
            return False
        try:
            matter = self.matters.get_matter(matter_id=payload["matter_id"], conn=conn)
        except (KeyError, PermissionError):
            return False
        phases = self._candidates(matter, _instant(now), policy["timezone"], policy["horizon_days"])
        return payload.get("phase") in phases and phases[payload["phase"]] == payload.get("facts_hash")


AttentionService = ManagementAttentionService
