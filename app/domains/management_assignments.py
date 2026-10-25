"""Bind existing managed runs to business work; completion needs separate review.

start_work accepts an independent task only after readiness checks in the same
transaction as its binding. Accepted reviews never update a Matter. Autonomous
assign_existing bindings/reviews are proposals until the coordinator publishes them in its fenced
transaction, after goal/work proposals. Evidence support is deliberately limited
to persisted agent messages with matching run provenance; existence checks do not
independently prove semantic correctness. Readiness requires the reviewed Matter
revision (or its proposed status-done revision); later edits invalidate that review.
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domains.management import ManagementConflictError
from app.storage.commit_signals import notify_after_commit


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


RunStatus = Literal["queued", "running", "paused", "waiting_user", "waiting_confirmation",
                    "completed", "failed", "cancelled", "timed_out"]


class StartWorkInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: str = Field(min_length=1, max_length=128)
    matter_id: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=1)
    purpose: str = Field(min_length=1, max_length=16000)
    title: str = Field(default="管理任务", min_length=1, max_length=40)
    workspace_path: str | None = Field(default=None, min_length=1, max_length=4096)


class AssignmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: str = Field(min_length=1, max_length=128)
    matter_id: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=256)
    expected_run_status: RunStatus | None = None


class EvidenceReference(BaseModel):
    """References are review evidence pointers, never content or new authority."""
    model_config = ConfigDict(extra="forbid", strict=True)
    source_type: Literal["session_message"]
    source_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="", max_length=500)


class ReviewInput(AssignmentInput):
    verdict: Literal["accepted", "needs_review"]
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=10)
    summary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def acceptance_has_evidence(self):
        if self.verdict == "accepted" and not self.evidence:
            raise ValueError("Accepted business results require evidence references")
        return self


class ManagementAssignmentService:
    def __init__(self, conn_factory, *, tasks, matters, work, goals):
        self._conn = conn_factory
        self.tasks, self.matters, self.work, self.goals = tasks, matters, work, goals

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS manager_work_assignments (
                    manager_session_id TEXT NOT NULL, matter_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, matter_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(manager_session_id,matter_id,run_id)
                );
                CREATE TABLE IF NOT EXISTS manager_work_reviews (
                    manager_session_id TEXT NOT NULL, command_id TEXT NOT NULL,
                    matter_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    matter_revision INTEGER NOT NULL, completion_revision INTEGER NOT NULL,
                    verdict TEXT NOT NULL, evidence TEXT NOT NULL, summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(manager_session_id,command_id)
                );
                CREATE TABLE IF NOT EXISTS manager_assignment_proposals (
                    execution_id TEXT NOT NULL, command_id TEXT NOT NULL,
                    manager_session_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(execution_id,command_id)
                );
            """)

    def _matter(self, conn, session_id, matter_id, expected_revision=None, *, mutate=False):
        matter = self.matters.get_matter(matter_id=matter_id, conn=conn)
        sidecar = self.work._sidecar(conn, matter_id)
        foreign = conn.execute("SELECT 1 FROM manager_work_assignments WHERE matter_id=? "
                               "AND manager_session_id!=? LIMIT 1", (matter_id, session_id)).fetchone()
        if foreign or (sidecar and sidecar["manager_session_id"] != session_id):
            raise ManagementConflictError("Matter belongs to another management session")
        if mutate and sidecar and sidecar["management_state"] == "paused":
            raise ManagementConflictError("Matter management is paused")
        if expected_revision is not None and matter.revision != expected_revision:
            raise ManagementConflictError("Matter revision changed")
        return matter

    def _run(self, conn, session_id, run_id, expected_status=None):
        row = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        run = json.loads(row[0])
        self.tasks._target(conn, run["session_id"])
        if run.get("parent_run_id") is not None or not conn.execute(
            "SELECT 1 FROM managed_session_links WHERE manager_session_id=? AND task_session_id=? "
            "AND run_id=? LIMIT 1", (session_id, run["session_id"], run_id),
        ).fetchone():
            raise ManagementConflictError("Run must be an existing root managed by this session")
        if expected_status is not None and run["status"] != expected_status:
            raise ManagementConflictError("Run status changed")
        return run

    def _latest_review(self, conn, session_id, matter_id):
        row = conn.execute("SELECT * FROM manager_work_reviews WHERE manager_session_id=? AND matter_id=? "
                           "ORDER BY rowid DESC LIMIT 1", (session_id, matter_id)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["evidence"] = json.loads(result["evidence"])
        return result

    def _readiness(self, conn, session_id, matter_id):
        matter = self._matter(conn, session_id, matter_id)
        sidecar = self.work._sidecar(conn, matter_id)
        paused = bool(sidecar and sidecar["management_state"] == "paused")
        rows = conn.execute("SELECT target_matter_id FROM manager_work_relations "
                            "WHERE manager_session_id=? AND source_matter_id=? AND relation='depends_on' "
                            "ORDER BY target_matter_id LIMIT 101", (session_id, matter_id)).fetchall()
        dependencies = []
        for row in rows[:100]:
            target = self.matters.get_matter(matter_id=row[0], conn=conn)
            review = self._latest_review(conn, session_id, target.matter_id)
            verified = bool(review and review["verdict"] == "accepted"
                            and review["completion_revision"] == target.revision)
            dependencies.append({"matter_id": target.matter_id, "revision": target.revision,
                                 "status": target.status, "verified": verified,
                                 "ready": target.status == "done" and verified})
        return {"matter_id": matter_id, "revision": matter.revision,
                "management_paused": paused,
                "ready": not paused and matter.status not in {"done", "cancelled"} and len(rows) <= 100
                and all(item["ready"] for item in dependencies),
                "dependencies": dependencies, "dependencies_truncated": len(rows) > 100}

    def readiness(self, context, matter_id):
        with closing(self._conn()) as conn:
            conn.execute("BEGIN")
            self.tasks.caller(context, conn=conn)
            return self._readiness(conn, context.session_id, matter_id)

    def result_view(self, context, matter_id):
        with closing(self._conn()) as conn:
            conn.execute("BEGIN")
            self.tasks.caller(context, conn=conn)
            matter = self._matter(conn, context.session_id, matter_id)
            assignments = []
            for row in conn.execute("SELECT run_id,matter_revision FROM manager_work_assignments "
                                    "WHERE manager_session_id=? AND matter_id=? ORDER BY rowid DESC LIMIT 21",
                                    (context.session_id, matter_id)):
                run = self._run(conn, context.session_id, row["run_id"])
                assignments.append({"run": self.tasks._run_view(run), "matter_revision": row["matter_revision"],
                                    "read_result": {"session_id": run["session_id"]}})
            review = self._latest_review(conn, context.session_id, matter_id)
            verified = bool(review and review["verdict"] == "accepted" and review["completion_revision"] == matter.revision)
            return {"matter_id": matter_id, "revision": matter.revision, "business_status": matter.status,
                    "business_complete": matter.status == "done" and verified,
                    "assignments": assignments[:20], "assignments_truncated": len(assignments) > 20,
                    "latest_review": review, "readiness": self._readiness(conn, context.session_id, matter_id)}

    def _apply(self, conn, session_id, payload):
        args = payload["args"]
        matter = self._matter(conn, session_id, args["matter_id"], args["expected_revision"], mutate=True)
        run = self._run(conn, session_id, args["run_id"], args.get("expected_run_status"))
        if payload["action"] == "assign_work":
            ready = self._readiness(conn, session_id, matter.matter_id)
            if not ready["ready"]:
                raise ManagementConflictError("Work dependencies are not verified complete or work is terminal")
            prior = conn.execute("SELECT matter_revision FROM manager_work_assignments WHERE manager_session_id=? "
                                 "AND matter_id=? AND run_id=?", (session_id, matter.matter_id, run["run_id"])).fetchone()
            if prior and prior[0] != matter.revision:
                raise ManagementConflictError("Run is already assigned to another Matter revision")
            conn.execute("INSERT OR IGNORE INTO manager_work_assignments VALUES(?,?,?,?,?)",
                         (session_id, matter.matter_id, run["run_id"], matter.revision, datetime.now(UTC).isoformat()))
            return {"matter_id": matter.matter_id, "revision": matter.revision,
                    "run": self.tasks._run_view(run), "read_result": {"session_id": run["session_id"]},
                    "readiness": ready}
        binding = conn.execute("SELECT matter_revision FROM manager_work_assignments WHERE manager_session_id=? "
                               "AND matter_id=? AND run_id=?", (session_id, matter.matter_id, run["run_id"])).fetchone()
        if not binding or binding[0] != matter.revision:
            raise ManagementConflictError("Review requires a run assigned to the current Matter revision")
        if run["status"] != "completed":
            raise ManagementConflictError("Only completed execution results can be reviewed")
        for evidence in args["evidence"]:
            message = conn.execute("SELECT payload FROM agent_session_messages WHERE message_id=? AND session_id=? "
                                   "AND role='agent'", (evidence["source_id"], run["session_id"])).fetchone()
            if message is None:
                raise ManagementConflictError("Review evidence must reference an existing result message in the assigned run session")
            try:
                source_run = json.loads(message[0]).get("run_id")
            except (TypeError, ValueError, AttributeError) as exc:
                raise ManagementConflictError("Review evidence message provenance is unavailable") from exc
            if source_run != run["run_id"]:
                raise ManagementConflictError("Review evidence lacks matching run provenance")
        completion_revision = matter.revision if matter.status == "done" else matter.revision + 1
        conn.execute("INSERT INTO manager_work_reviews VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (session_id, args["command_id"], matter.matter_id, run["run_id"], matter.revision,
                      completion_revision, args["verdict"], _json(args["evidence"]), args["summary"], datetime.now(UTC).isoformat()))
        update = {"expected_revision": matter.revision, "changes": {"status": "done"}} if args["verdict"] == "accepted" and matter.status != "done" else None
        return {"matter_id": matter.matter_id, "revision": matter.revision, "run_id": run["run_id"],
                "verdict": args["verdict"], "evidence": args["evidence"], "summary": args["summary"],
                "business_status": matter.status, "completion_update": update,
                "completion_requires_organize_work": update is not None}

    def _replay(self, conn, execution_id, session_id):
        for row in conn.execute("SELECT command_id,payload FROM manager_assignment_proposals "
                                "WHERE execution_id=? AND manager_session_id=? ORDER BY rowid",
                                (execution_id, session_id)).fetchall():
            receipt = json.loads(conn.execute("SELECT receipt FROM management_commands "
                "WHERE manager_session_id=? AND command_id=?", (session_id, row["command_id"])).fetchone()[0])
            if receipt["status"] == "applied":
                continue
            receipt["result"] = self._apply(conn, session_id, json.loads(row["payload"]))
            receipt["status"] = "applied"
            conn.execute("UPDATE management_commands SET receipt=? WHERE manager_session_id=? AND command_id=?",
                         (_json(receipt), session_id, row["command_id"]))

    def _mutate(self, context, args, action):
        payload = {"action": action, "args": args.model_dump(mode="json")}
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self.tasks.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self.tasks._command(conn, context, args.command_id, action, payload)
            execution_id = run.metadata.get("manager_execution_id")
            if prior:
                if prior["status"] == "proposed":
                    if prior["execution_id"] != execution_id:
                        raise ManagementConflictError("Assignment proposal belongs to another execution")
                    self.work._execution(conn, execution_id, context.session_id, run.run_id)
                return prior
            if execution_id:
                self.work._execution(conn, execution_id, context.session_id, run.run_id)
                conn.execute("SAVEPOINT assignment_validation")
                try:
                    self.tasks.apply_goal_proposals(conn, execution_id, context.session_id)
                    self.work.apply_proposals(conn, execution_id, context.session_id, authority_checked=True)
                    self._replay(conn, execution_id, context.session_id)
                    result = self._apply(conn, context.session_id, payload)
                finally:
                    conn.execute("ROLLBACK TO assignment_validation")
                    conn.execute("RELEASE assignment_validation")
                conn.execute("INSERT INTO manager_assignment_proposals VALUES(?,?,?,?)",
                             (execution_id, args.command_id, context.session_id, _json(payload)))
            else:
                result = self._apply(conn, context.session_id, payload)
            receipt = {"command_id": args.command_id, "action": action, "execution_id": execution_id,
                       "status": "proposed" if execution_id else "applied", "result": result}
            self.tasks.caller(context, mutate=True, conn=conn)
            conn.execute("INSERT INTO management_commands VALUES(?,?,?,?,?,?)",
                         (context.session_id, args.command_id, fingerprint, action, _json(receipt), datetime.now(UTC).isoformat()))
            return receipt

    @notify_after_commit
    def start_work(self, context, **kwargs):
        """Atomically accept one ready business task; no worker sees a partial bind.

        Like start_task, acceptance of this independent task is durable even if
        the initiating manager's later result fails to publish. Proposed Matter
        records must be published before they can receive an execution.
        """
        args = StartWorkInput.model_validate(kwargs)
        prepared = self.tasks.prepare_task(context, command_id=args.command_id, purpose=args.purpose,
                                           title=args.title, workspace_path=args.workspace_path)

        def ready_to_create(conn):
            self._matter(conn, context.session_id, args.matter_id, args.expected_revision, mutate=True)
            if not self._readiness(conn, context.session_id, args.matter_id)["ready"]:
                raise ManagementConflictError("Work dependencies are not verified complete or work is terminal")

        with closing(self._conn()) as conn, conn:
            self.tasks.begin_task_transaction(conn)
            receipt, run = self.tasks.start_task_in_transaction(
                conn, context, command_id=args.command_id, purpose=args.purpose, title=args.title,
                workspace_path=args.workspace_path, command_action="start_work",
                command_context={"matter_id": args.matter_id, "expected_revision": args.expected_revision},
                before_create=ready_to_create, prepared=prepared,
            )
            if run is not None:
                assignment = self._apply(conn, context.session_id, {"action": "assign_work", "args": {
                    "matter_id": args.matter_id, "expected_revision": args.expected_revision,
                    "run_id": run.run_id, "expected_run_status": "queued",
                }})
                receipt.update(matter_id=args.matter_id, matter_revision=args.expected_revision,
                               assignment=assignment)
                conn.execute("UPDATE management_commands SET receipt=? WHERE manager_session_id=? AND command_id=?",
                             (_json(receipt), context.session_id, args.command_id))
            self.tasks.caller(context, mutate=True, conn=conn)
        if run is not None:
            self.tasks.run_manager.get_run(run.run_id)
        return receipt

    @notify_after_commit
    def assign_existing(self, context, **kwargs):
        return self._mutate(context, AssignmentInput.model_validate(kwargs), "assign_work")

    @notify_after_commit
    def review(self, context, **kwargs):
        return self._mutate(context, ReviewInput.model_validate(kwargs), "review_work")

    def apply_proposals(self, conn, execution_id, session_id, *, authority_checked=False):
        self.work._execution(conn, execution_id, session_id, authority_checked=authority_checked)
        self._replay(conn, execution_id, session_id)
