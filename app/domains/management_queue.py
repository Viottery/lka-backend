"""Durable, explicitly accepted management work awaiting dependency and resource readiness.

Queue admission creates its independent root and preserves the original allowance.
Only release is scheduled later; neither a manager turn nor a fresh budget is made.
Write reservations intentionally survive waits/pauses until that root terminates.
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domains.management import ManagementConflictError

TERMINAL = {"completed", "failed", "cancelled", "timed_out"}
PRIORITY = {"low": 0, "normal": 1, "high": 2, "urgent": 3}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _aware(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("deadline must include a timezone")
    return parsed.astimezone(UTC).isoformat()


class QueueWorkInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: str = Field(min_length=1, max_length=128)
    matter_id: str | None = Field(default=None, min_length=1, max_length=256)
    expected_revision: int | None = Field(default=None, ge=1)
    purpose: str = Field(min_length=1, max_length=16000)
    title: str = Field(default="管理任务", min_length=1, max_length=40)
    workspace_path: str | None = Field(default=None, min_length=1, max_length=4096)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    deadline: str | None = Field(default=None, max_length=80)
    read_only_work: bool = True
    write_resources: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("deadline")
    @classmethod
    def aware_deadline(cls, value):
        return _aware(value)

    @field_validator("write_resources")
    @classmethod
    def resource_keys(cls, value):
        if any(not key or len(key) > 256 or key != key.strip() for key in value):
            raise ValueError("write_resources require exact nonempty keys of at most 256 characters")
        return sorted(set(value))

    @model_validator(mode="after")
    def paired_matter(self):
        if (self.matter_id is None) != (self.expected_revision is None):
            raise ValueError("matter_id and expected_revision must be supplied together")
        if self.read_only_work and self.write_resources:
            raise ValueError("write_resources require read_only_work=false")
        if not self.read_only_work and self.workspace_path is None:
            raise ValueError("Writing queued work requires an explicit workspace_path")
        return self


class ManagementQueueService:
    def __init__(self, conn_factory, *, tasks, assignments, dispatcher, config, read_only_tools=None):
        self._conn = conn_factory
        self.tasks, self.assignments = tasks, assignments
        self.dispatcher, self.config = dispatcher, config
        self.read_only_tools = read_only_tools or (lambda: ())

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS manager_work_queue (
                    queue_id TEXT PRIMARY KEY, manager_session_id TEXT NOT NULL,
                    command_id TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE,
                    matter_id TEXT, matter_revision INTEGER,
                    priority TEXT NOT NULL, deadline TEXT, claim_keys TEXT NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    issue TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(manager_session_id,command_id)
                );
                CREATE INDEX IF NOT EXISTS idx_manager_work_queue_pending ON manager_work_queue(state,created_at);
                CREATE TABLE IF NOT EXISTS manager_work_claims (
                    resource_key TEXT PRIMARY KEY, queue_id TEXT NOT NULL, run_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manager_queue_fairness (
                    manager_session_id TEXT PRIMARY KEY, last_served INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manager_queue_scheduler (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), sequence INTEGER NOT NULL,
                    scan_cursor INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO manager_queue_scheduler VALUES(1,0,0);
            """)

    @staticmethod
    def _view(row):
        return {key: row[key] for key in ("queue_id", "manager_session_id", "command_id", "run_id", "matter_id",
                "matter_revision", "priority", "deadline", "state", "revision", "issue", "created_at", "updated_at")}

    def queue_work(self, context, **kwargs):
        args = QueueWorkInput.model_validate(kwargs)
        prepared = self.tasks.prepare_task(context, command_id=args.command_id, purpose=args.purpose,
                                           title=args.title, workspace_path=args.workspace_path)
        readonly = set(self.read_only_tools()) if args.read_only_work else set()
        keys = ["resource:" + key for key in args.write_resources]
        if not args.read_only_work:
            workspace = prepared["workspace"]
            if not workspace or not workspace.get("backend_path"):
                raise ManagementConflictError("Writing queued work requires a validated workspace")
            keys.append(self._workspace_key(workspace["backend_path"]))
        keys = sorted(set(keys))
        queue_id = "manager_queue_" + uuid4().hex
        details = {"matter_id": args.matter_id, "expected_revision": args.expected_revision,
                   "priority": args.priority, "deadline": args.deadline, "claim_keys": keys,
                   "read_only_work": args.read_only_work}

        def validate(conn):
            if args.matter_id:
                matter = self.assignments._matter(conn, context.session_id, args.matter_id,
                                                   args.expected_revision, mutate=True)
                if matter.status in {"done", "cancelled"}:
                    raise ManagementConflictError("Terminal work cannot be queued")

        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            receipt, run = self.tasks.start_task_in_transaction(
                conn, context, command_id=args.command_id, purpose=args.purpose, title=args.title,
                workspace_path=args.workspace_path, command_action="queue_work", command_context=details,
                before_create=validate, prepared=prepared, defer_dispatch=True,
            )
            if run is not None:
                if args.read_only_work:
                    policy = dict(run.metadata["session_policy"])
                    allowed = policy.get("allowed_tools")
                    policy["allowed_tools"] = sorted(readonly if allowed is None else readonly.intersection(allowed))
                    policy["allow_fork"] = False
                    run.metadata["session_policy"] = policy
                    self.tasks.run_store.save_run_in_transaction(conn, run.model_dump(mode="json"), updated_at=run.created_at)
                now = datetime.now(UTC).isoformat()
                conn.execute("INSERT INTO manager_work_queue VALUES(?,?,?,?,?,?,?,?,?,'pending',1,NULL,?,?)",
                             (queue_id, context.session_id, args.command_id, run.run_id, args.matter_id,
                              args.expected_revision, args.priority, args.deadline, _json(keys), now, now))
                if args.matter_id:
                    conn.execute("INSERT INTO manager_work_assignments VALUES(?,?,?,?,?)",
                                 (context.session_id, args.matter_id, run.run_id, args.expected_revision, now))
                receipt.update(queue_id=queue_id, queue_state="pending", queue_revision=1)
                conn.execute("UPDATE management_commands SET receipt=? WHERE manager_session_id=? AND command_id=?",
                             (_json(receipt), context.session_id, args.command_id))
            self.tasks.caller(context, mutate=True, conn=conn)
        if run is not None:
            self.tasks.run_manager.restore_run(run)
        return receipt

    @staticmethod
    def _workspace_key(path):
        path = path.replace("\\", "/")
        if len(path) > 1 and path[1] == ":":
            path = path.casefold()
        return "workspace:" + (path.rstrip("/") or "/")

    def _source(self, conn, row):
        session = conn.execute("SELECT s.status FROM agent_sessions s JOIN agent_runs r ON r.session_id=s.session_id "
                               "WHERE r.run_id=?", (row["run_id"],)).fetchone()
        if session is None or session["status"] == "deleted":
            raise ManagementConflictError("Queued task session is unavailable")
        if row["matter_id"]:
            self.assignments._matter(conn, row["manager_session_id"], row["matter_id"], row["matter_revision"])
        workspace_keys = [key for key in json.loads(row["claim_keys"]) if key.startswith("workspace:")]
        if workspace_keys:
            session = conn.execute("SELECT s.metadata FROM agent_sessions s JOIN agent_runs r ON r.session_id=s.session_id "
                                   "WHERE r.run_id=?", (row["run_id"],)).fetchone()
            workspace = json.loads(session[0]).get("workspace") if session else None
            if (not isinstance(workspace, dict) or not isinstance(workspace.get("backend_path"), str)
                    or self._workspace_key(workspace["backend_path"]) not in workspace_keys):
                raise ManagementConflictError("Queued work workspace changed")

    @staticmethod
    def _transition(conn, row, state, now, issue=None):
        conn.execute("UPDATE manager_work_queue SET state=?,revision=revision+1,issue=?,updated_at=? "
                     "WHERE queue_id=? AND revision=?", (state, issue, now, row["queue_id"], row["revision"]))

    @staticmethod
    def _resources_busy(conn, keys):
        for key in keys:
            if conn.execute("SELECT 1 FROM manager_work_claims WHERE resource_key=?", (key,)).fetchone():
                return True
            if key.startswith("workspace:"):
                path = key.removeprefix("workspace:")
                for row in conn.execute("SELECT resource_key FROM manager_work_claims WHERE resource_key LIKE 'workspace:%'"):
                    other = row[0].removeprefix("workspace:")
                    if path.startswith(other.rstrip("/") + "/") or other.startswith(path.rstrip("/") + "/"):
                        return True
        return False

    def tick(self, now=None):
        now = now or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("tick time must include a timezone")
        now = now.astimezone(UTC)
        stamp = now.isoformat()
        released = 0
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            # Reconcile only persisted terminal outcomes; waits/pauses retain write reservations.
            for row in conn.execute("SELECT q.*,r.status AS run_status FROM manager_work_queue q "
                                    "JOIN agent_runs r ON r.run_id=q.run_id WHERE q.state!='finished' "
                                    "AND r.status IN ('completed','failed','cancelled','timed_out') LIMIT 256").fetchall():
                self._transition(conn, row, "finished", stamp, "run_" + row["run_status"])
                conn.execute("DELETE FROM manager_work_claims WHERE queue_id=?", (row["queue_id"],))
            if not self.config.enabled or not self.tasks.durable_execution():
                return 0
            cursor = conn.execute("SELECT scan_cursor FROM manager_queue_scheduler WHERE singleton=1").fetchone()[0]
            rows = conn.execute("SELECT rowid AS scan_id,* FROM manager_work_queue WHERE state='pending' "
                                "AND rowid>? ORDER BY rowid LIMIT 256", (cursor,)).fetchall()
            if not rows:
                rows = conn.execute("SELECT rowid AS scan_id,* FROM manager_work_queue WHERE state='pending' "
                                    "ORDER BY rowid LIMIT 256").fetchall()
            if rows:
                conn.execute("UPDATE manager_queue_scheduler SET scan_cursor=? WHERE singleton=1", (rows[-1]["scan_id"],))
            ready = {}
            for row in rows:
                run = conn.execute("SELECT status FROM agent_runs WHERE run_id=?", (row["run_id"],)).fetchone()
                if run is None:
                    self._transition(conn, row, "blocked", stamp, "run_unavailable")
                    continue
                if run[0] in TERMINAL:
                    self._transition(conn, row, "finished", stamp, "run_" + run[0])
                    conn.execute("DELETE FROM manager_work_claims WHERE queue_id=?", (row["queue_id"],))
                    continue
                if run[0] != "queued":
                    continue
                controller = conn.execute("SELECT c.status,s.status FROM manager_controllers c JOIN agent_sessions s "
                                          "ON s.session_id=c.session_id WHERE c.session_id=?",
                                          (row["manager_session_id"],)).fetchone()
                if not controller or controller[1] == "deleted":
                    self._transition(conn, row, "blocked", stamp, "manager_unavailable")
                    continue
                if controller[0] != "active":
                    continue
                try:
                    self._source(conn, row)
                    if row["matter_id"] and not self.assignments._readiness(
                            conn, row["manager_session_id"], row["matter_id"])["ready"]:
                        continue
                except (KeyError, ManagementConflictError):
                    self._transition(conn, row, "blocked", stamp, "work_source_changed")
                    continue
                if self._resources_busy(conn, json.loads(row["claim_keys"])):
                    continue
                ready.setdefault(row["manager_session_id"], []).append(row)
            served = {r[0]: r[1] for r in conn.execute("SELECT manager_session_id,last_served FROM manager_queue_fairness")}

            def score(row):
                age = max(0, (now - datetime.fromisoformat(row["created_at"])).total_seconds() / 60)
                urgency = 0 if not row["deadline"] else min(60, max(0,
                    60 - (datetime.fromisoformat(row["deadline"]) - now).total_seconds() / 60))
                return (-(PRIORITY[row["priority"]] * 60 + age + urgency), row["deadline"] or "9999",
                        row["created_at"], row["queue_id"])

            while ready:
                sid = min(ready, key=lambda key: (served.get(key, 0), key))
                candidates = ready[sid]
                row = min(candidates, key=score)
                candidates.remove(row)
                if not candidates:
                    del ready[sid]
                keys = json.loads(row["claim_keys"])
                if self._resources_busy(conn, keys):
                    continue
                for key in keys:
                    conn.execute("INSERT INTO manager_work_claims VALUES(?,?,?)", (key, row["queue_id"], row["run_id"]))
                self.dispatcher.enqueue(row["run_id"], conn=conn)
                self._transition(conn, row, "released", stamp)
                conn.execute("UPDATE manager_queue_scheduler SET sequence=sequence+1 WHERE singleton=1")
                sequence = conn.execute("SELECT sequence FROM manager_queue_scheduler WHERE singleton=1").fetchone()[0]
                conn.execute("INSERT INTO manager_queue_fairness VALUES(?,?) ON CONFLICT(manager_session_id) "
                             "DO UPDATE SET last_served=excluded.last_served", (sid, sequence))
                served[sid] = sequence
                released += 1
        return released

    def guard_replacement(self, run_id):
        """Queue instructions retain their admitted policy and reservation identity."""
        with closing(self._conn()) as conn:
            if conn.execute("SELECT 1 FROM manager_work_queue WHERE run_id=?", (run_id,)).fetchone():
                raise ValueError("Queued work must be cancelled and requeued to replace its instruction")

    def check_run(self, run_id, conn=None):
        """Validate a queue root without writing, caller admission, or manager locks."""
        def check(db):
            row = db.execute("SELECT * FROM manager_work_queue WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return
            if row["state"] != "released":
                raise ManagementConflictError("Queued work has not been released")
            self._source(db, row)
            for key in json.loads(row["claim_keys"]):
                owner = db.execute("SELECT queue_id,run_id FROM manager_work_claims WHERE resource_key=?", (key,)).fetchone()
                if owner is None or owner[0] != row["queue_id"] or owner[1] != run_id:
                    raise ManagementConflictError("Queued work reservation is no longer owned")
        if conn is not None:
            check(conn)
        else:
            with closing(self._conn()) as db:
                db.execute("BEGIN")
                check(db)

    def list_queue(self, context, *, limit=30, offset=0):
        self.tasks._page(limit, offset)
        with closing(self._conn()) as conn:
            conn.execute("BEGIN")
            self.tasks.caller(context, conn=conn)
            rows = conn.execute("SELECT * FROM manager_work_queue WHERE manager_session_id=? "
                                "ORDER BY created_at,queue_id LIMIT ? OFFSET ?",
                                (context.session_id, limit + 1, offset)).fetchall()
            return {"items": [self._view(r) for r in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}

    def control_queue(self, context, *, command_id, queue_id, expected_revision, action):
        if (not isinstance(command_id, str) or not 1 <= len(command_id) <= 128
                or not isinstance(queue_id, str) or not 1 <= len(queue_id) <= 256
                or type(expected_revision) is not int or expected_revision < 1
                or action not in {"pause", "resume"}):
            raise ValueError("Invalid queue control arguments")
        args = {"queue_id": queue_id, "expected_revision": expected_revision, "action": action}
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self.tasks.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self.tasks._command(conn, context, command_id, "control_queue", args)
            if prior:
                return prior
            row = conn.execute("SELECT * FROM manager_work_queue WHERE queue_id=? AND manager_session_id=?",
                               (queue_id, context.session_id)).fetchone()
            if row is None:
                raise KeyError(queue_id)
            if row["revision"] != expected_revision:
                raise ManagementConflictError("Queue revision changed")
            expected_state = "pending" if action == "pause" else "paused"
            if row["state"] != expected_state:
                raise ManagementConflictError("Only unreleased pending/paused queue work can be controlled")
            now = datetime.now(UTC).isoformat()
            self._transition(conn, row, "paused" if action == "pause" else "pending", now)
            result = {"command_id": command_id, "action": "control_queue", "queue_id": queue_id,
                      "status": "applied", "queue_state": "paused" if action == "pause" else "pending",
                      "queue_revision": expected_revision + 1}
            self.tasks.caller(context, mutate=True, conn=conn)
            conn.execute("INSERT INTO management_commands VALUES(?,?,?,?,?,?)", (context.session_id, command_id,
                         fingerprint, "control_queue", _json(result), now))
            return result
