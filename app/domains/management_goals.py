"""Deterministic management goals and bounded durable event capture."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from app.domains.management import ManagementConflictError

UNSET = object()
_UNSET = UNSET
_STATUSES = {"active", "waiting", "paused", "completed", "cancelled"}
_TERMINAL = {"completed", "failed", "cancelled", "timed_out"}


def _instant(value=None):
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Time must be an aware ISO timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Time must include a timezone")
    return value.astimezone(UTC)


class ManagementGoalService:
    def __init__(self, conn_factory, management):
        self._conn = conn_factory
        self.management = management
        self.work_service = None

    def ensure_schema(self):
        self.management.ensure_schema()
        with closing(self._conn()) as conn, conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(manager_goals)")}
            for name, declaration in (
                ("next_wake_at", "TEXT"), ("timer_generation", "INTEGER NOT NULL DEFAULT 1"), ("wait_run_id", "TEXT"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE manager_goals ADD COLUMN {name} {declaration}")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS manager_inbox (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, dedup_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('timer','task_terminal')),
                    goal_id TEXT, goal_revision INTEGER,
                    payload TEXT NOT NULL CHECK(json_valid(payload) AND length(CAST(payload AS BLOB))<=4096),
                    created_at TEXT NOT NULL, consumed_execution_id TEXT,
                    UNIQUE(session_id,dedup_key)
                );
                CREATE INDEX IF NOT EXISTS manager_inbox_session_event ON manager_inbox(session_id,event_id);
                CREATE TABLE IF NOT EXISTS manager_executions (
                    execution_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('bound','completed','failed','superseded')),
                    controller_revision INTEGER NOT NULL,
                    event_ids TEXT NOT NULL CHECK(json_valid(event_ids)),
                    goal_revisions TEXT NOT NULL CHECK(json_valid(goal_revisions)),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS manager_executions_session ON manager_executions(session_id);
                CREATE TABLE IF NOT EXISTS manager_deliveries (
                    delivery_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS manager_deliveries_session ON manager_deliveries(session_id);
            """)

    @staticmethod
    def _has_table(conn, name):
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _matter_link(self, conn, goal_id):
        if not self._has_table(conn, "manager_goal_matter_links"):
            return None
        return conn.execute("SELECT matter_id FROM manager_goal_matter_links WHERE goal_id=?", (goal_id,)).fetchone()

    def _bound_matter(self, conn, goal_id):
        link = self._matter_link(conn, goal_id)
        if link is None:
            return None
        if self.work_service is None:
            raise ManagementConflictError("Bound Matter service is unavailable")
        # Use the domain reader in the same snapshot, never bypass source checks
        # by hydrating a Matter directly from its database row.
        return self.work_service.matters.get_matter(matter_id=link["matter_id"], conn=conn)

    def _project(self, conn, row):
        result = dict(row)
        matter = self._bound_matter(conn, result["goal_id"])
        if matter is not None:
            result.update(matter_id=matter.matter_id, matter_revision=matter.revision, matter_status=matter.status)
            result["objective"] = matter.title + ("\n" + matter.summary if matter.summary and matter.summary != matter.title else "")
        return result

    def enabled_in_transaction(self, conn, goal_id):
        """A scheduling goal cannot revive finished or paused bound work."""
        goal = conn.execute("SELECT status FROM manager_goals WHERE goal_id=?", (goal_id,)).fetchone()
        if goal is None or goal["status"] not in {"active", "waiting"}:
            return False
        try:
            matter = self._bound_matter(conn, goal_id)
        except (KeyError, PermissionError, ManagementConflictError):
            return False
        if matter is None:
            return True
        if matter.status in {"done", "cancelled"}:
            return False
        if self._has_table(conn, "matter_management"):
            sidecar = conn.execute("SELECT management_state FROM matter_management WHERE matter_id=?", (matter.matter_id,)).fetchone()
            if sidecar and sidecar["management_state"] == "paused":
                return False
        return True

    @staticmethod
    def _manager(conn, session_id):
        if conn.execute("SELECT 1 FROM manager_controllers c JOIN agent_sessions s ON s.session_id=c.session_id "
                        "WHERE c.session_id=? AND s.kind='manager' AND s.status!='deleted'", (session_id,)).fetchone() is None:
            raise KeyError(session_id)

    @staticmethod
    def _objective(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 16000:
            raise ValueError("objective must contain 1..16000 nonblank characters")
        return value

    @staticmethod
    def _status(value):
        if value not in _STATUSES:
            raise ValueError("Invalid goal status")
        return value

    @staticmethod
    def _wait_run(conn, value):
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("wait_run_id must be a bounded run ID or null")
        row = conn.execute("SELECT 1 FROM agent_runs r JOIN agent_sessions s ON s.session_id=r.session_id "
                           "WHERE r.run_id=? AND s.kind='ordinary' AND s.status!='deleted' "
                           "AND json_extract(r.record_payload,'$.parent_run_id') IS NULL", (value,)).fetchone()
        if row is None:
            raise ValueError("wait_run_id must identify an existing ordinary root task")
        return value

    @staticmethod
    def _page(limit, offset):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and cursor/offset non-negative")

    def create_goal(self, session_id, *, objective, status="active", next_wake_at=None, wait_run_id=None):
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.create_goal_in_transaction(conn, session_id, objective=objective, status=status,
                                                   next_wake_at=next_wake_at, wait_run_id=wait_run_id)

    def create_goal_in_transaction(self, conn, session_id, *, objective, status="active",
                                   next_wake_at=None, wait_run_id=None, goal_id=None):
        if not conn.in_transaction:
            raise ValueError("Goal changes require a caller-owned transaction")
        objective, status = self._objective(objective), self._status(status)
        wake = _instant(next_wake_at).isoformat() if next_wake_at is not None else None
        now = _instant().isoformat()
        goal_id = goal_id if goal_id is not None else "goal_" + uuid4().hex
        if not isinstance(goal_id, str) or not goal_id.strip() or len(goal_id) > 256:
            raise ValueError("goal_id must contain 1..256 characters")
        self._manager(conn, session_id)
        self._wait_run(conn, wait_run_id)
        if conn.execute("SELECT 1 FROM manager_goals WHERE goal_id=?", (goal_id,)).fetchone():
            raise ManagementConflictError("Goal ID already exists")
        conn.execute("INSERT INTO manager_goals(goal_id,session_id,status,revision,objective,created_at,updated_at,"
                     "next_wake_at,timer_generation,wait_run_id) VALUES(?,?,?,1,?,?,?,?,1,?)",
                     (goal_id, session_id, status, objective, now, now, wake, wait_run_id))
        return dict(conn.execute("SELECT * FROM manager_goals WHERE goal_id=?", (goal_id,)).fetchone())

    def get_goal(self, session_id, goal_id):
        with closing(self._conn()) as conn:
            conn.execute("BEGIN")
            self._manager(conn, session_id)
            row = conn.execute("SELECT * FROM manager_goals WHERE session_id=? AND goal_id=?", (session_id, goal_id)).fetchone()
            if row is None:
                raise KeyError(goal_id)
            return self._project(conn, row)

    def list_goals(self, session_id, *, limit=50, offset=0):
        self._page(limit, offset)
        with closing(self._conn()) as conn:
            conn.execute("BEGIN")
            self._manager(conn, session_id)
            rows = conn.execute("SELECT * FROM manager_goals WHERE session_id=? ORDER BY created_at,goal_id "
                                "LIMIT ? OFFSET ?", (session_id, limit + 1, offset)).fetchall()
            return {"goals": [self._project(conn, row) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}

    def update_goal(self, session_id, goal_id, *, expected_revision, objective=_UNSET, status=_UNSET,
                    next_wake_at=_UNSET, wait_run_id=_UNSET):
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.update_goal_in_transaction(conn, session_id, goal_id, expected_revision=expected_revision,
                                                   objective=objective, status=status,
                                                   next_wake_at=next_wake_at, wait_run_id=wait_run_id)

    def update_goal_in_transaction(self, conn, session_id, goal_id, *, expected_revision,
                                   objective=_UNSET, status=_UNSET, next_wake_at=_UNSET, wait_run_id=_UNSET):
        if not conn.in_transaction:
            raise ValueError("Goal changes require a caller-owned transaction")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        changes = {}
        if objective is not _UNSET:
            changes["objective"] = self._objective(objective)
        if status is not _UNSET:
            changes["status"] = self._status(status)
        if next_wake_at is not _UNSET:
            changes["next_wake_at"] = _instant(next_wake_at).isoformat() if next_wake_at is not None else None
        self._manager(conn, session_id)
        row = conn.execute("SELECT revision FROM manager_goals WHERE session_id=? AND goal_id=?", (session_id, goal_id)).fetchone()
        if row is None:
            raise KeyError(goal_id)
        if row["revision"] != expected_revision:
            raise ManagementConflictError("Goal revision changed")
        if self._matter_link(conn, goal_id) is not None:
            if objective is not _UNSET or status is not _UNSET:
                raise ManagementConflictError("Bound goal objective and status belong to Matter; update the Matter instead")
            self._bound_matter(conn, goal_id)
        if wait_run_id is not _UNSET:
            changes["wait_run_id"] = self._wait_run(conn, wait_run_id)
        assignments = [f"{name}=?" for name in changes]
        assignments.extend(["revision=revision+1", "timer_generation=timer_generation+1", "updated_at=?"])
        conn.execute("UPDATE manager_goals SET " + ",".join(assignments) + " WHERE session_id=? AND goal_id=?",
                     [*changes.values(), _instant().isoformat(), session_id, goal_id])
        return self._project(conn, conn.execute("SELECT * FROM manager_goals WHERE goal_id=?", (goal_id,)).fetchone())

    @staticmethod
    def _event(conn, session_id, dedup_key, kind, payload, stamp, goal_id=None, goal_revision=None):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(encoded.encode("utf-8")) > 4096:
            raise ValueError("Event payload exceeds 4096 bytes")
        return conn.execute("INSERT OR IGNORE INTO manager_inbox(session_id,dedup_key,kind,goal_id,goal_revision,"
                            "payload,created_at) VALUES(?,?,?,?,?,?,?)",
                            (session_id, dedup_key, kind, goal_id, goal_revision, encoded, stamp)).rowcount

    @staticmethod
    def _waiting_ref(value):
        return value if isinstance(value, str) and 0 < len(value) <= 256 else None

    def _waiting_payload(self, conn, record):
        status = record.get("status")
        if status not in {"waiting_user", "waiting_confirmation"}:
            return None
        payload = {"task_waiting": True, "run_id": record["run_id"], "status": status}
        metadata = record.get("metadata", {})
        if metadata.get("cancel_requested") or metadata.get("pending_user_answer_command_id"):
            return None
        if status == "waiting_user":
            question = metadata.get("pending_user_question")
            reference = self._waiting_ref(question.get("question_id")) if isinstance(question, dict) else None
            if isinstance(question, dict) and question.get("question_id") is not None and reference is None:
                return None
            if reference is not None:
                payload["question_id"] = reference
        elif self._has_table(conn, "agent_safety_reviews"):
            reference = self._waiting_ref(metadata.get("confirmation_id"))
            review = None
            if reference is not None:
                review = conn.execute("SELECT review_id FROM agent_safety_reviews WHERE review_id=? AND run_id=? AND status='pending'",
                                      (reference, record["run_id"])).fetchone()
            if review is None:
                review = conn.execute("SELECT review_id FROM agent_safety_reviews WHERE run_id=? AND status='pending' "
                                      "ORDER BY created_at DESC,review_id DESC LIMIT 1", (record["run_id"],)).fetchone()
            if review is None and conn.execute("SELECT 1 FROM agent_safety_reviews WHERE run_id=? LIMIT 1", (record["run_id"],)).fetchone():
                # Approval may commit before the run leaves its waiting state.
                # Do not turn a resolved review into a new generic request.
                return None
            if review is not None and self._waiting_ref(review["review_id"]) is not None:
                payload["review_id"] = review["review_id"]
        return payload

    def waiting_event_current(self, conn, event):
        """A reminder is valid only for the same linked root and waiting request."""
        payload = event["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(payload, dict) or payload.get("task_waiting") is not True:
            return False
        if not self._has_table(conn, "managed_session_links"):
            return False
        try:
            self._manager(conn, event["session_id"])
        except KeyError:
            return False
        row = conn.execute("SELECT r.record_payload,r.status FROM agent_runs r JOIN agent_sessions s ON s.session_id=r.session_id "
                           "WHERE r.run_id=? AND s.kind='ordinary' AND s.status!='deleted' AND EXISTS ("
                           "SELECT 1 FROM managed_session_links l WHERE l.run_id=r.run_id AND l.manager_session_id=?)",
                           (payload.get("run_id"), event["session_id"])).fetchone()
        if row is None or row["status"] != payload.get("status"):
            return False
        record = json.loads(row["record_payload"])
        if record.get("parent_run_id") is not None:
            return False
        current = self._waiting_payload(conn, record)
        return current is not None and all(current.get(key) == payload.get(key)
                                           for key in ("run_id", "status", "question_id", "review_id"))

    def capture_events(self, now=None):
        instant = _instant(now)
        stamp = instant.isoformat()
        count = 0
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            managers = [r[0] for r in conn.execute("SELECT c.session_id FROM manager_controllers c JOIN agent_sessions s "
                                                 "ON s.session_id=c.session_id WHERE s.kind='manager' AND s.status!='deleted'")]
            for sid in managers:
                due = conn.execute("SELECT * FROM manager_goals WHERE session_id=? AND status IN ('active','waiting') "
                                   "AND next_wake_at IS NOT NULL AND next_wake_at<=?", (sid, stamp)).fetchall()
                for goal in due:
                    if not self.enabled_in_transaction(conn, goal["goal_id"]):
                        continue
                    count += self._event(conn, sid, f"timer:{goal['goal_id']}:{goal['timer_generation']}", "timer",
                                         {"timer_generation": goal["timer_generation"]}, stamp,
                                         goal["goal_id"], goal["revision"])
                    conn.execute("UPDATE manager_goals SET next_wake_at=NULL WHERE goal_id=?", (goal["goal_id"],))
                waiting = conn.execute("SELECT goal_id,revision,wait_run_id FROM manager_goals WHERE session_id=? "
                                       "AND status IN ('active','waiting') AND wait_run_id IS NOT NULL", (sid,)).fetchall()
                waiting = [goal for goal in waiting if self.enabled_in_transaction(conn, goal["goal_id"])]
                linked = set()
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_session_links'").fetchone():
                    linked.update(row[0] for row in conn.execute("SELECT run_id FROM managed_session_links WHERE manager_session_id=?", (sid,)))
                tracked = linked | {row["wait_run_id"] for row in waiting}
                for run_id in sorted(tracked):
                    row = conn.execute("SELECT r.record_payload FROM agent_runs r JOIN agent_sessions s ON s.session_id=r.session_id "
                                       "WHERE r.run_id=? AND s.kind='ordinary' AND s.status!='deleted'", (run_id,)).fetchone()
                    if row is None:
                        continue
                    record = json.loads(row[0])
                    if record.get("parent_run_id") is not None:
                        continue
                    if run_id in linked:
                        pending = self._waiting_payload(conn, record)
                        if pending is not None:
                            reference = {key: pending[key] for key in ("question_id", "review_id") if key in pending}
                            reference_key = sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest()[:32]
                            count += self._event(conn, sid, f"waiting:{run_id}:{record['status']}:{reference_key}",
                                                 "timer", pending, stamp)
                    if record.get("status") not in _TERMINAL:
                        continue
                    goals = [goal for goal in waiting if goal["wait_run_id"] == run_id]
                    error_type = record.get("error_type")
                    payload = {"run_id": run_id, "status": record["status"],
                               "error_type": str(error_type)[:80] if error_type is not None else None}
                    if run_id in linked:
                        count += self._event(conn, sid, f"task:{run_id}:{record['status']}", "task_terminal", payload, stamp)
                    for goal in goals:
                        key = f"task:{run_id}:{record['status']}:{goal['goal_id']}:{goal['revision']}"
                        count += self._event(conn, sid, key, "task_terminal", payload, stamp,
                                             goal["goal_id"], goal["revision"])
        return count

    def list_events(self, session_id, *, after=0, limit=50):
        self._page(limit, after)
        with closing(self._conn()) as conn:
            self._manager(conn, session_id)
            rows = conn.execute("SELECT * FROM manager_inbox WHERE session_id=? AND event_id>? ORDER BY event_id LIMIT ?",
                                (session_id, after, limit + 1)).fetchall()
        events = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows[:limit]]
        return {"events": events, "next_after": events[-1]["event_id"] if len(rows) > limit else None}

    def _records(self, table, session_id, after, limit):
        self._page(limit, after)
        with closing(self._conn()) as conn:
            self._manager(conn, session_id)
            rows = conn.execute(f"SELECT rowid AS cursor,* FROM {table} WHERE session_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                                (session_id, after, limit + 1)).fetchall()
        records = [dict(row) for row in rows[:limit]]
        if table == "manager_executions":
            for row in records:
                row["event_ids"] = json.loads(row["event_ids"])
                row["goal_revisions"] = json.loads(row["goal_revisions"])
        return records, records[-1]["cursor"] if len(rows) > limit else None

    def list_executions(self, session_id, *, after=0, limit=50):
        rows, cursor = self._records("manager_executions", session_id, after, limit)
        return {"executions": rows, "next_after": cursor}

    def list_deliveries(self, session_id, *, after=0, limit=50):
        rows, cursor = self._records("manager_deliveries", session_id, after, limit)
        return {"deliveries": rows, "next_after": cursor}
