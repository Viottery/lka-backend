"""Durable hub admission and fencing for approved nodes; never remote shell RPC."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.core.agent_runs import AgentRunRecord, AgentRunStatus
from app.domains.devices import DeviceUnavailableError

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[\w.-]+$")]
EXECUTION_SCOPES = {"execution.poll", "execution.run", "execution.result"}
TERMINAL = {"completed", "failed", "cancelled", "unknown"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now():
    return datetime.now(UTC).isoformat()


class RemoteExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    environment_id: Identifier | None = None
    workspace_mappings: dict[Identifier, str] = Field(default_factory=dict, max_length=100)
    shell_trust: Literal["disabled", "host_user"] = "disabled"
    review_policy: Literal["manual", "inherit"] = "manual"
    allowed_tools: list[str] = Field(default_factory=list, max_length=256)
    poll_seconds: float = Field(default=3, ge=0.25, le=3600)
    lease_seconds: int = Field(default=90, ge=10, le=3600)
    max_task_bytes: int = Field(default=1048576, ge=16384, le=1048576)


class ExecutionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: Identifier | None = None
    environment_id: Identifier | None = None
    workspace_id: Identifier | None = None
    capabilities: list[Identifier] = Field(default_factory=list, max_length=100)


class ExecutionFence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: Identifier
    lease_epoch: int = Field(ge=1)
    lease_token: str = Field(min_length=20, max_length=200)
    environment_id: Identifier


class ExecutionApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested_mode: Literal["skip", "llm", "manual"]
    effective_mode: Literal["skip", "llm", "manual"]
    source: Literal["request", "node_policy", "host_execution", "legacy"]


class ExecutionReport(ExecutionFence):
    report_id: Identifier
    status: Literal["running", "waiting_confirmation", "waiting_user", "paused", "completed", "failed", "cancelled"]
    messages: list[dict] = Field(default_factory=list, max_length=100)
    reviews: list[dict] = Field(default_factory=list, max_length=20)
    result: dict | None = None
    error_type: str | None = Field(default=None, max_length=200)
    effect_constraints: dict[str, dict] = Field(default_factory=dict, max_length=64)
    pending_user_question: dict | None = None
    paused_from_status: str | None = None
    applied_commands: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(default_factory=list, max_length=100)
    approval: ExecutionApproval | None = None


class RemoteExecutionConflict(ValueError):
    pass


class RemoteExecutionService:
    def __init__(self, conn_factory, devices, *, hub_id, managed_tasks=None,
                 sessions=None, run_store=None, run_manager=None, tool_executor=None,
                 lease_seconds=90):
        self._conn = conn_factory
        self.devices, self.hub_id = devices, hub_id
        self.tasks, self.sessions = managed_tasks, sessions
        self.run_store, self.run_manager, self.executor = run_store, run_manager, tool_executor
        if not hub_id or not 10 <= lease_seconds <= 3600:
            raise ValueError("hub identity and bounded lease required")
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread = None

    @contextmanager
    def _connection(self, *, write=False):
        conn = self._conn()
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_schema(self):
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS remote_execution_tasks (
                    task_id TEXT PRIMARY KEY, owner_session_id TEXT NOT NULL,
                    command_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    node_id TEXT NOT NULL, environment_id TEXT NOT NULL, workspace_id TEXT,
                    target TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                    lease_epoch INTEGER NOT NULL DEFAULT 0, lease_hash TEXT,
                    credential_version INTEGER, lease_expires_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_session_id,command_id));
                CREATE TABLE IF NOT EXISTS remote_execution_workers (
                    node_id TEXT NOT NULL, environment_id TEXT NOT NULL,
                    credential_version INTEGER NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(node_id,environment_id));
                CREATE TABLE IF NOT EXISTS remote_execution_reports (
                    task_id TEXT NOT NULL, report_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(task_id,report_id));
                CREATE TABLE IF NOT EXISTS remote_execution_decisions (
                    task_id TEXT NOT NULL, review_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(task_id,review_id));
                CREATE TABLE IF NOT EXISTS remote_execution_controls (
                    task_id TEXT NOT NULL, command_id TEXT NOT NULL, payload TEXT NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    PRIMARY KEY(task_id,command_id));
                CREATE INDEX IF NOT EXISTS remote_execution_pending ON remote_execution_tasks(node_id,state,created_at);
            """)

    @staticmethod
    def _profile_allows(profile, target, environment):
        system = next((s for s in profile["systems"] if s["environment_id"] == environment), None)
        return bool(system and EXECUTION_SCOPES.issubset(profile["scopes"])
                    and set(target["capabilities"]).issubset(profile["capabilities"])
                    and (not target["workspace_id"] or target["workspace_id"] in system["workspace_ids"]
                         and target["workspace_id"] in profile["workspace_ids"]))

    def _worker_online(self, conn, node_id, environment_id, credential_version):
        worker = conn.execute("SELECT * FROM remote_execution_workers WHERE node_id=? AND environment_id=?",
                              (node_id, environment_id)).fetchone()
        return bool(worker and worker["credential_version"] == credential_version
                    and (datetime.now(UTC) - datetime.fromisoformat(worker["last_seen"])).total_seconds()
                        <= self.devices.config.online_threshold_seconds)

    def execution_targets(self):
        items = self.devices.list()["items"]
        with self._connection() as conn:
            for item in items:
                device = conn.execute("SELECT credential_version FROM cloud_devices WHERE device_id=?", (item["device_id"],)).fetchone()
                for system in item["systems"]:
                    system["execution_online"] = bool(item["online"] and EXECUTION_SCOPES.issubset(item["scopes"])
                        and self._worker_online(conn, item["device_id"], system["environment_id"], device[0]))
                item["execution_online"] = any(system["execution_online"] for system in item["systems"])
        return {"items": items}

    def _select(self, conn, target):
        for row in conn.execute("SELECT * FROM cloud_devices WHERE revoked=0 ORDER BY device_id"):
            profile = json.loads(row["profile"])
            if target["device_id"] and target["device_id"] != row["device_id"]:
                continue
            if (not row["last_seen"] or (datetime.now(UTC) - datetime.fromisoformat(row["last_seen"])).total_seconds()
                    > self.devices.config.online_threshold_seconds):
                continue
            for system in sorted(profile["systems"], key=lambda s: s["environment_id"]):
                environment = system["environment_id"]
                if target["environment_id"] and target["environment_id"] != environment:
                    continue
                if (self._profile_allows(profile, target, environment)
                        and self._worker_online(conn, row["device_id"], environment, row["credential_version"])):
                    return row["device_id"], environment
        raise DeviceUnavailableError("no approved online node satisfies execution target; target was not changed")

    def start_managed_task(self, context, *, command_id, purpose, title="管理任务", execution_target):
        if self.tasks is None or self.executor is None:
            raise RemoteExecutionConflict("remote execution admission unavailable")
        from app.core.context_driver import ToolView

        target = ExecutionTarget.model_validate(execution_target).model_dump(mode="json")
        caller = self.tasks.caller(context, mutate=True)
        # These allowances reference a cloud-local usage ledger. Moving them to a
        # different database would reset consumption, so unsupported transfer fails closed.
        if caller.metadata.get("work_budget_scopes"):
            raise RemoteExecutionConflict("remote execution cannot transfer cumulative background budgets")
        args = {"purpose": purpose, "title": title, "target": target}
        fingerprint = hashlib.sha256(_json(args).encode()).hexdigest()
        prepared = self.tasks.prepare_task(context, command_id=command_id, purpose=purpose,
                                           title=title, create_workspace=False)
        constraints = self.executor.constraint_store.get(self.executor._constraint_subjects(context))
        policies = {}
        for key in constraints:
            policy = self.executor.registry.effect_constraints.get(key)
            if policy is None:
                raise PermissionError("unknown source authority cannot cross execution boundary")
            policies[key] = policy
        with self._connection(write=True) as conn:
            self.tasks.caller(context, mutate=True, conn=conn)
            previous = conn.execute("SELECT * FROM remote_execution_tasks WHERE owner_session_id=? AND command_id=?",
                                    (context.session_id, command_id)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise RemoteExecutionConflict("command_id already accepted with different arguments")
                receipt = json.loads(conn.execute("SELECT receipt FROM management_commands WHERE manager_session_id=? AND command_id=?",
                                                  (context.session_id, command_id)).fetchone()[0])
                return receipt
            node_id, environment = self._select(conn, target)
            receipt, run = self.tasks.start_task_in_transaction(
                conn, context, command_id=command_id, purpose=purpose, prepared=prepared,
                command_context={"execution_target": target}, defer_dispatch=True,
            )
            if run is None:
                raise RemoteExecutionConflict("command_id was already accepted for another execution route")
            task_id = "remote_task_" + uuid4().hex
            marker = {"hub_id": self.hub_id, "task_id": task_id,
                      "owner_session_id": context.session_id, "node_id": node_id,
                      "environment_id": environment, "workspace_id": target["workspace_id"]}
            run.metadata["remote_execution"] = marker
            policy = run.metadata["session_policy"]
            policy.update(allow_default_workspace=False, allow_fork=False)
            policy["safety_review_mode"] = policy.get("safety_review_mode") or "manual"
            run.metadata["remote_approval_requested"] = policy["safety_review_mode"]
            # Source grants are copied as bounded structured scope, never inferred
            # from delegated prose. Absent source grants expose no node-local data.
            source_view = (caller.metadata.get("context_views") or {}).get("tool")
            scope = ToolView.model_validate(source_view).model_dump(mode="json") if source_view else None
            if scope and scope.get("max_tool_calls") is not None:
                raise RemoteExecutionConflict("remote execution cannot transfer cumulative source tool budgets")
            payload = {"hub_id": self.hub_id, "task_id": task_id, "target": marker,
                       "title": prepared["title"],
                       "run": run.model_dump(mode="json"), "source_view": scope,
                       "effect_constraints": policies}
            if len(_json(payload).encode()) > 1048576:
                raise ValueError("remote task snapshot exceeds 1 MiB")
            self.run_store.save_run_in_transaction(conn, run.model_dump(mode="json"), updated_at=_now())
            conn.execute("UPDATE agent_sessions SET metadata=? WHERE session_id=?",
                         (_json({"remote_execution": marker}), run.session_id))
            now = _now()
            conn.execute("INSERT INTO remote_execution_tasks(task_id,owner_session_id,command_id,fingerprint,node_id,environment_id,workspace_id,target,payload,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)",
                         (task_id, context.session_id, command_id, fingerprint, node_id, environment,
                          target["workspace_id"], _json(target), _json(payload), now, now))
            receipt["remote_execution"] = marker
            conn.execute("UPDATE management_commands SET receipt=? WHERE manager_session_id=? AND command_id=?",
                         (_json(receipt), context.session_id, command_id))
        self.run_manager.refresh_after_transaction(run)
        return receipt

    def _row(self, conn, task_id):
        row = conn.execute("SELECT * FROM remote_execution_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row

    def _fence(self, conn, principal, fence, scope):
        profile = self.devices.require_node(conn, principal, scope)
        row = self._row(conn, fence.task_id)
        if (row["node_id"] != principal["node_id"] or row["environment_id"] != fence.environment_id
                or not self._profile_allows(profile, json.loads(row["target"]), fence.environment_id)):
            raise PermissionError("execution target no longer approved")
        if (row["lease_epoch"] != fence.lease_epoch or row["credential_version"] != principal["credential_version"]
                or not row["lease_hash"] or row["lease_hash"] != hashlib.sha256(fence.lease_token.encode()).hexdigest()):
            raise PermissionError("execution lease identity changed")
        if row["state"] in TERMINAL or not row["lease_expires_at"] or row["lease_expires_at"] <= _now():
            raise RemoteExecutionConflict("execution cancelled, expired or finished; do not replay")
        self._continuation_authority(conn, row)
        payload = json.loads(row["payload"])
        if self.run_store:
            hub_run = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (payload["run"]["run_id"],)).fetchone()
            metadata = json.loads(hub_run[0]).get("metadata", {}) if hub_run else {}
            if hub_run is None or metadata.get("cancel_requested"):
                raise RemoteExecutionConflict("hub run cancelled")
            if self.tasks:
                controller = conn.execute("SELECT status FROM manager_controllers WHERE session_id=?", (row["owner_session_id"],)).fetchone()
                if controller is None or controller["status"] != "active":
                    raise RemoteExecutionConflict("owner controller no longer active")
        return row

    def claim(self, principal, *, environment_id):
        self.reconcile()
        token = uuid4().hex + uuid4().hex
        with self._connection(write=True) as conn:
            profile = self.devices.require_node(conn, principal, "execution.poll")
            if not self._profile_allows(profile, {"workspace_id": None, "capabilities": []}, environment_id):
                raise PermissionError("execution worker environment or scopes are not approved")
            conn.execute("INSERT INTO remote_execution_workers VALUES(?,?,?,?) ON CONFLICT(node_id,environment_id) DO UPDATE SET credential_version=excluded.credential_version,last_seen=excluded.last_seen",
                         (principal["node_id"], environment_id, principal["credential_version"], _now()))
            # A lost lease has unknown effects. Never put it back into the queue.
            # Reconciliation handles expiry and updates hub shadow runs together.
            rows = conn.execute("SELECT * FROM remote_execution_tasks WHERE node_id=? AND environment_id=? AND state='queued' ORDER BY created_at,task_id",
                                (principal["node_id"], environment_id)).fetchall()
            for row in rows:
                if (not self._profile_allows(profile, json.loads(row["target"]), environment_id)
                        or row["credential_version"] is not None and row["credential_version"] != principal["credential_version"]):
                    continue
                expires = (datetime.now(UTC) + timedelta(seconds=self.lease_seconds)).isoformat()
                conn.execute("UPDATE remote_execution_tasks SET state='leased',lease_epoch=lease_epoch+1,lease_hash=?,credential_version=?,lease_expires_at=?,updated_at=? WHERE task_id=?",
                             (hashlib.sha256(token.encode()).hexdigest(), principal["credential_version"], expires, _now(), row["task_id"]))
                return {"task": json.loads(row["payload"]), "fence": {"task_id": row["task_id"],
                        "lease_epoch": row["lease_epoch"] + 1, "lease_token": token, "environment_id": environment_id},
                        "lease_expires_at": expires}
        return {"task": None}

    def check(self, principal, fence, *, start=False):
        fence = ExecutionFence.model_validate(fence)
        with self._connection(write=True) as conn:
            row = self._fence(conn, principal, fence, "execution.run")
            state = "running" if start and row["state"] == "leased" else row["state"]
            conn.execute("UPDATE remote_execution_workers SET last_seen=? WHERE node_id=? AND environment_id=? AND credential_version=?",
                         (_now(), principal["node_id"], fence.environment_id, principal["credential_version"]))
            expires = (datetime.now(UTC) + timedelta(seconds=self.lease_seconds)).isoformat()
            conn.execute("UPDATE remote_execution_tasks SET state=?,lease_expires_at=?,updated_at=? WHERE task_id=?",
                         (state, expires, _now(), fence.task_id))
            decisions = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM remote_execution_decisions WHERE task_id=?", (fence.task_id,))]
            commands = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM remote_execution_controls WHERE task_id=? AND applied=0 ORDER BY created_at,command_id LIMIT 100", (fence.task_id,))]
            return {"task_id": fence.task_id, "state": state, "lease_expires_at": expires,
                    "decisions": decisions, "commands": commands}

    def report(self, principal, report):
        report = ExecutionReport.model_validate(report)
        report_data = report.model_dump(mode="json", exclude={"lease_token", "approval"})
        # Keep pre-upgrade durable report fingerprints stable on retry.
        if report.approval is not None:
            report_data["approval"] = report.approval.model_dump(mode="json")
        encoded = _json(report_data)
        if len(encoded.encode()) > 1048576:
            raise ValueError("execution report exceeds 1 MiB")
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        updated = None
        with self._connection(write=True) as conn:
            # Exact result retries still authenticate and revalidate current scope;
            # they do not re-publish messages or mutate a completed run.
            profile = self.devices.require_node(conn, principal, "execution.result")
            row = self._row(conn, report.task_id)
            if (row["node_id"] != principal["node_id"] or row["credential_version"] != principal["credential_version"]
                    or report.environment_id != row["environment_id"]
                    or row["lease_epoch"] != report.lease_epoch
                    or row["lease_hash"] != hashlib.sha256(report.lease_token.encode()).hexdigest()
                    or not self._profile_allows(profile, json.loads(row["target"]), row["environment_id"])):
                raise PermissionError("result authority changed")
            previous = conn.execute("SELECT fingerprint FROM remote_execution_reports WHERE task_id=? AND report_id=?", (report.task_id, report.report_id)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise RemoteExecutionConflict("report_id already has different content")
                return {"task_id": report.task_id, "report_id": report.report_id, "accepted": True}
            self._fence(conn, principal, report, "execution.result")
            if row["state"] == "leased":
                raise RemoteExecutionConflict("task must start before reporting")
            payload = json.loads(row["payload"])
            if self.executor:
                for constraint_id, expected in report.effect_constraints.items():
                    current = self.executor.registry.effect_constraints.get(constraint_id)
                    if current is None or _json(current) != _json(expected):
                        raise PermissionError("reported source constraint is unknown or changed on hub")
                    # Only authenticated node-worker records can add restrictions;
                    # result text can never clear constraints or grant authority.
                    for kind, subject in (("run", payload["run"]["run_id"]),
                                          ("session", payload["run"]["session_id"]),
                                          ("session", row["owner_session_id"])):
                        conn.execute("INSERT OR IGNORE INTO tool_origin_constraints VALUES(?,?,?)", (kind, subject, constraint_id))
            if self.run_store:
                stored = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (payload["run"]["run_id"],)).fetchone()
                run = AgentRunRecord.model_validate_json(stored[0])
                if report.pending_user_question is not None:
                    question = report.pending_user_question
                    if (report.status != "waiting_user" or set(question) - {"question_id", "question", "patch_id", "asked_at"}
                            or not isinstance(question.get("question_id"), str)
                            or not isinstance(question.get("question"), str)
                            or len(_json(question)) > 8192):
                        raise ValueError("invalid relayed user question")
                    run.metadata["pending_user_question"] = question
                elif report.status != "paused":
                    run.metadata.pop("pending_user_question", None)
                if report.status == "paused":
                    if report.paused_from_status not in {"queued", "running", "waiting_confirmation", "waiting_user"}:
                        raise ValueError("invalid remote pause checkpoint state")
                    run.metadata["paused_from_status"] = report.paused_from_status
                for message in report.messages:
                    if (set(message) - {"message_id", "role", "content", "payload"}
                            or message.get("role") not in {"user", "agent", "tool", "system"}
                            or not isinstance(message.get("message_id"), str)
                            or not isinstance(message.get("content"), str)):
                        raise ValueError("invalid relayed session message")
                    self.sessions.append_message_in_transaction(conn, session_id=run.session_id,
                        message_id=message["message_id"], role=message["role"], content=message["content"],
                        payload={**message.get("payload", {}), "remote_task_id": report.task_id, "trace_id": run.trace_id})
                if report.status == "completed":
                    # The hub publishes only after revalidating the lease and node scope
                    # in this exact transaction, preserving normal supervision events.
                    run.status = AgentRunStatus.RUNNING
                    self.run_store.save_run_in_transaction(conn, run.model_dump(mode="json"), updated_at=_now())
                    updated = self.run_manager.complete_run_in_transaction(conn, run.run_id, result_snapshot=report.result or {})
                else:
                    run.status = AgentRunStatus(report.status)
                    if report.status in {"failed", "cancelled"}:
                        if report.result is not None:
                            run.result_snapshot = report.result
                        run.error_type = report.error_type or "remote_execution_" + report.status
                        run.error = run.error_type
                    self.run_store.save_run_in_transaction(conn, run.model_dump(mode="json"), updated_at=_now())
                    updated = run
            task_state = "resuming" if row["state"] == "resuming" and report.status == "paused" else report.status
            for command_id in report.applied_commands:
                command = conn.execute("SELECT payload,applied FROM remote_execution_controls WHERE task_id=? AND command_id=?", (report.task_id, command_id)).fetchone()
                # Enqueue acknowledgement is durable, but a still-paused local
                # checkpoint has not resumed yet. Do not admit a second resume
                # that could unexpectedly consume a later pause.
                if (command and not command["applied"] and json.loads(command["payload"])["action"] == "resume"
                        and report.status == "paused"):
                    task_state = "resuming"
                conn.execute("UPDATE remote_execution_controls SET applied=1 WHERE task_id=? AND command_id=?",
                             (report.task_id, command_id))
            conn.execute("INSERT INTO remote_execution_reports VALUES(?,?,?,?,?)", (report.task_id, report.report_id, fingerprint, encoded, _now()))
            conn.execute("UPDATE remote_execution_tasks SET state=?,updated_at=? WHERE task_id=?", (task_state, _now(), report.task_id))
        if updated is not None:
            self.run_manager.refresh_after_transaction(updated)
        return {"task_id": report.task_id, "report_id": report.report_id, "accepted": True}

    @staticmethod
    def _summary(row, conn):
        payload = json.loads(row["payload"])
        session = conn.execute("SELECT title FROM agent_sessions WHERE session_id=?",
                               (payload["run"]["session_id"],)).fetchone()
        latest = conn.execute("SELECT json_extract(payload,'$.approval') FROM remote_execution_reports WHERE task_id=? "
                              "ORDER BY created_at DESC,report_id DESC LIMIT 1", (row["task_id"],)).fetchone()
        approval = json.loads(latest[0]) if latest and latest[0] else None
        if not approval:
            metadata = payload["run"].get("metadata", {})
            approval = {"requested_mode": metadata.get("remote_approval_requested") or
                        metadata.get("session_policy", {}).get("safety_review_mode") or "manual",
                        "effective_mode": None, "source": "node_policy" if "remote_approval_requested" in metadata else "legacy"}
        return {key: row[key] for key in ("task_id", "owner_session_id", "command_id", "node_id", "environment_id", "workspace_id", "state", "lease_epoch", "lease_expires_at", "created_at", "updated_at")} | {
            "session_id": payload["run"]["session_id"], "run_id": payload["run"]["run_id"],
            "title": session[0] if session else payload.get("title", "管理任务"),
            "approval": approval, "target": json.loads(row["target"])}

    def list(self, *, owner_session_id=None, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("invalid execution task page")
        self.reconcile()
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM remote_execution_tasks WHERE (? IS NULL OR owner_session_id=?) ORDER BY created_at DESC,task_id DESC LIMIT ? OFFSET ?",
                                (owner_session_id, owner_session_id, limit + 1, offset)).fetchall()
            return {"items": [self._summary(row, conn) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}

    def get(self, task_id, *, owner_session_id=None, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("invalid execution report page")
        self.reconcile()
        with self._connection() as conn:
            row = self._row(conn, task_id)
            if owner_session_id is not None and owner_session_id != row["owner_session_id"]:
                raise PermissionError("task belongs to another assistant session")
            rows = conn.execute("SELECT payload FROM remote_execution_reports WHERE task_id=? ORDER BY created_at,report_id LIMIT ? OFFSET ?", (task_id, limit + 1, offset)).fetchall()
            reports, size = [], 0
            for report in rows[:limit]:
                if reports and size + len(report[0].encode()) > 1048576:
                    break
                reports.append(json.loads(report[0]))
                size += len(report[0].encode())
            return self._summary(row, conn) | {"reports": reports,
                "next_offset": offset + len(reports) if len(rows) > len(reports) else None}

    def cancel(self, task_id):
        updated = None
        with self._connection(write=True) as conn:
            row = self._row(conn, task_id)
            if row["state"] not in TERMINAL:
                conn.execute("UPDATE remote_execution_tasks SET state='cancelled',lease_epoch=lease_epoch+1,updated_at=? WHERE task_id=?", (_now(), task_id))
                payload = json.loads(row["payload"])
                if self.run_store:
                    stored = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (payload["run"]["run_id"],)).fetchone()
                    updated = AgentRunRecord.model_validate_json(stored[0])
                    updated.status = AgentRunStatus.CANCELLED
                    updated.cancelled_at = _now()
                    updated.metadata["cancel_requested"] = True
                    self.run_store.save_run_in_transaction(conn, updated.model_dump(mode="json"), updated_at=_now())
        if updated:
            self.run_manager.refresh_after_transaction(updated)
        return self.get(task_id)

    def _continuation_authority(self, conn, row):
        payload = json.loads(row["payload"])
        source = payload.get("source_view") or {}
        if source.get("expires_at") and datetime.fromisoformat(source["expires_at"]) <= datetime.now(UTC):
            raise PermissionError("delegated source authority expired")
        if self.executor:
            for key, expected in payload.get("effect_constraints", {}).items():
                current = self.executor.registry.effect_constraints.get(key)
                if current is None or _json(current) != _json(expected):
                    raise PermissionError("delegated source constraints changed")
        device = conn.execute("SELECT * FROM cloud_devices WHERE device_id=?", (row["node_id"],)).fetchone()
        if (device is None or device["revoked"]
                or row["credential_version"] is not None and row["credential_version"] != device["credential_version"]
                or not self._profile_allows(json.loads(device["profile"]), json.loads(row["target"]), row["environment_id"])):
            raise PermissionError("remote continuation authority changed")
        if self.tasks:
            owner = conn.execute("SELECT c.status,s.status AS session_status FROM manager_controllers c JOIN agent_sessions s ON s.session_id=c.session_id WHERE c.session_id=?", (row["owner_session_id"],)).fetchone()
            session = conn.execute("SELECT creator_session_id,status FROM agent_sessions WHERE session_id=?", (json.loads(row["payload"])["run"]["session_id"],)).fetchone()
            if (owner is None or owner["status"] != "active" or owner["session_status"] == "deleted"
                    or session is None or session["status"] == "deleted" or session["creator_session_id"] != row["owner_session_id"]):
                raise PermissionError("remote execution owner changed or inactive")

    def control(self, task_id, *, command_id, action, question_id=None, answer=None):
        if not command_id or len(command_id) > 400 or action not in {"pause", "resume", "answer"}:
            raise ValueError("invalid remote control command")
        if action == "answer" and (not question_id or not isinstance(answer, str) or not answer.strip() or len(answer) > 16000):
            raise ValueError("a bounded answer and question_id are required")
        payload = {"command_id": command_id, "action": action}
        if action == "answer":
            payload.update(question_id=question_id, answer=answer.strip())
        self.reconcile()
        with self._connection(write=True) as conn:
            row = self._row(conn, task_id)
            self._continuation_authority(conn, row)
            previous = conn.execute("SELECT payload FROM remote_execution_controls WHERE task_id=? AND command_id=?", (task_id, command_id)).fetchone()
            if previous:
                if json.loads(previous[0]) != payload:
                    raise RemoteExecutionConflict("command_id already accepted with different arguments")
                return {"task_id": task_id, **payload}
            if row["state"] in TERMINAL or not row["lease_expires_at"] or row["lease_expires_at"] <= _now():
                raise RemoteExecutionConflict("execution is no longer live; do not replay")
            required = {"pause": {"running", "waiting_user", "waiting_confirmation"}, "resume": {"paused"}, "answer": {"waiting_user"}}
            if row["state"] not in required[action]:
                raise RemoteExecutionConflict("remote control is not valid for current checkpoint state")
            pending = conn.execute("SELECT 1 FROM remote_execution_controls WHERE task_id=? AND applied=0", (task_id,)).fetchone()
            if pending:
                raise RemoteExecutionConflict("wait for the previous remote control acknowledgement")
            if action == "answer":
                run = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (json.loads(row["payload"])["run"]["run_id"],)).fetchone()
                question = json.loads(run[0]).get("metadata", {}).get("pending_user_question", {}) if run else {}
                if question.get("question_id") != question_id:
                    raise RemoteExecutionConflict("pending question changed")
            conn.execute("INSERT INTO remote_execution_controls VALUES(?,?,?,0,?)", (task_id, command_id, _json(payload), _now()))
        return {"task_id": task_id, **payload}

    def account_instruction(self, task_id, *, command_id, expected_run_id, purpose, actor):
        from types import SimpleNamespace
        with self._connection() as conn:
            row = self._row(conn, task_id)
            session_id = json.loads(row["payload"])["run"]["session_id"]
            owner = row["owner_session_id"]
        context = SimpleNamespace(session_id=owner)
        key = "account_" + hashlib.sha256(_json([actor, command_id]).encode()).hexdigest()
        return self.send_instruction(context, command_id=key, session_id=session_id,
            expected_run_id=expected_run_id, purpose=purpose, account_actor=actor)

    def send_instruction(self, context, *, command_id, session_id, expected_run_id, purpose, account_actor=None):
        caller = self.tasks.caller(context, mutate=True) if account_actor is None else None
        if caller and caller.metadata.get("work_budget_scopes"):
            raise RemoteExecutionConflict("remote execution cannot transfer cumulative background budgets")
        if account_actor is None:
            envelope = self.tasks._envelope(context, caller, command_id, purpose)
        else:
            from app.core.turn_input import TurnInputEnvelope
            envelope = TurnInputEnvelope(origin="user", input_id=hashlib.sha256(_json([account_actor, command_id]).encode()).hexdigest(),
                sender_label="Account " + account_actor, purpose=purpose, original_user_quotes=[purpose[:8000]],
                related_session_id=session_id, related_run_id=expected_run_id)
        args = {"session_id": session_id, "expected_run_id": expected_run_id, "purpose": purpose}
        with self._connection(write=True) as conn:
            if account_actor is None:
                self.tasks.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self.tasks._command(conn, context, command_id, "send_instruction", args)
            if prior:
                return prior
            self.tasks._target(conn, session_id, manager_session_id=context.session_id)
            rows = conn.execute("SELECT * FROM remote_execution_tasks WHERE owner_session_id=? ORDER BY created_at DESC,task_id DESC", (context.session_id,)).fetchall()
            old = next((r for r in rows if json.loads(r["payload"])["run"]["session_id"] == session_id), None)
            latest = conn.execute("SELECT run_id FROM agent_runs WHERE session_id=? ORDER BY created_at DESC,run_id DESC LIMIT 1", (session_id,)).fetchone()
            if old is None or json.loads(old["payload"])["run"]["run_id"] != expected_run_id or latest[0] != expected_run_id:
                raise RemoteExecutionConflict("expected_run_id does not identify the latest remote execution")
            self._continuation_authority(conn, old)
            if old["state"] not in {"completed", "failed", "cancelled"}:
                raise RemoteExecutionConflict("followup requires a known terminal result; unknown effects cannot replay")
            confirmed = conn.execute("SELECT 1 FROM remote_execution_reports WHERE task_id=? AND json_extract(payload,'$.status') IN ('completed','failed','cancelled')", (old["task_id"],)).fetchone()
            if confirmed is None:
                raise RemoteExecutionConflict("predecessor has no confirmed terminal result; effects may be unknown")
            previous = json.loads(old["payload"])
            caller_scope = (caller.metadata.get("context_views") or {}).get("tool") if caller else None
            if caller_scope and _json(caller_scope) != _json(previous.get("source_view")):
                raise RemoteExecutionConflict("followup cannot change frozen source scope")
            old_run = self.run_store.load_run(expected_run_id)
            if old_run.get("metadata", {}).get("work_budget_scopes"):
                raise RemoteExecutionConflict("remote execution cannot reset cumulative background budgets")
            # The new instruction does not grant new tools, sources, paths or budgets.
            run = self.tasks._new_run(session_id, envelope, caller)
            previous_policy = previous["run"]["metadata"]["session_policy"]
            requested_mode = (getattr(context, "safety_review_mode", None) if caller else None) or previous_policy.get("safety_review_mode") or "manual"
            run.metadata["session_policy"] = {**previous_policy, "safety_review_mode": requested_mode}
            run.metadata["remote_approval_requested"] = requested_mode
            task_id = "remote_task_" + uuid4().hex
            marker = {**previous["target"], "task_id": task_id}
            run.metadata["remote_execution"] = marker
            policies = dict(previous.get("effect_constraints", {}))
            subjects = self.executor._constraint_subjects(context) if caller else [("session", context.session_id), ("session", session_id)]
            for key in self.executor.constraint_store.get(subjects):
                policy = self.executor.registry.effect_constraints.get(key)
                if policy is None:
                    raise PermissionError("unknown source authority cannot cross execution boundary")
                policies[key] = policy
            payload = {**previous, "task_id": task_id, "target": marker,
                       "run": run.model_dump(mode="json"), "effect_constraints": policies,
                       "predecessor_task_id": old["task_id"]}
            if len(_json(payload).encode()) > 1048576:
                raise ValueError("remote task snapshot exceeds 1 MiB")
            self.run_store.save_run_in_transaction(conn, run.model_dump(mode="json"), updated_at=_now())
            conn.execute("UPDATE agent_sessions SET metadata=? WHERE session_id=?", (_json({"remote_execution": marker}), session_id))
            conn.execute("INSERT INTO remote_execution_tasks(task_id,owner_session_id,command_id,fingerprint,node_id,environment_id,workspace_id,target,payload,state,credential_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?)", (task_id, context.session_id, command_id, fingerprint, old["node_id"], old["environment_id"], old["workspace_id"], old["target"], _json(payload), old["credential_version"], _now(), _now()))
            receipt = self.tasks._receipt(conn, context, command_id, "send_instruction", fingerprint, run)
            receipt["remote_execution"] = marker
            conn.execute("UPDATE management_commands SET receipt=? WHERE manager_session_id=? AND command_id=?", (_json(receipt), context.session_id, command_id))
        self.run_manager.refresh_after_transaction(run)
        return receipt

    def decide_review(self, task_id, review_id, *, decision, decided_by, reason=None):
        if decision not in {"approve", "reject"}:
            raise ValueError("invalid review decision")
        with self._connection(write=True) as conn:
            row = self._row(conn, task_id)
            self._continuation_authority(conn, row)
            if row["state"] != "waiting_confirmation" or row["lease_expires_at"] <= _now():
                raise RemoteExecutionConflict("review is no longer live")
            reviews = []
            for report in conn.execute("SELECT payload FROM remote_execution_reports WHERE task_id=? ORDER BY created_at", (task_id,)):
                reviews.extend(json.loads(report[0]).get("reviews", []))
            review = next((r for r in reversed(reviews) if r.get("review_id") == review_id), None)
            if review is None or review.get("run_id") != json.loads(row["payload"])["run"]["run_id"]:
                raise KeyError(review_id)
            payload = {"review_id": review_id, "decision": decision, "decided_by": decided_by,
                       "reason": reason, "review_fingerprint": hashlib.sha256(_json(review).encode()).hexdigest()}
            previous = conn.execute("SELECT payload FROM remote_execution_decisions WHERE task_id=? AND review_id=?", (task_id, review_id)).fetchone()
            if previous and json.loads(previous[0]) != payload:
                raise RemoteExecutionConflict("review already decided differently")
            conn.execute("INSERT OR IGNORE INTO remote_execution_decisions VALUES(?,?,?)", (task_id, review_id, _json(payload)))
        return {"task_id": task_id, **payload}

    def reconcile(self):
        """Fence unknown effects and cancelled targets; never resubmit a run."""
        changed = []
        with self._connection(write=True) as conn:
            rows = conn.execute("SELECT * FROM remote_execution_tasks WHERE state NOT IN ('completed','failed','cancelled','unknown')").fetchall()
            for row in rows:
                state = None
                device = conn.execute("SELECT * FROM cloud_devices WHERE device_id=?", (row["node_id"],)).fetchone()
                target = json.loads(row["target"])
                if (device is None or device["revoked"] or not self._profile_allows(json.loads(device["profile"]), target, row["environment_id"])
                        or row["credential_version"] is not None and row["credential_version"] != device["credential_version"]):
                    state = "unknown" if row["state"] != "queued" else "cancelled"
                elif row["lease_expires_at"] and row["lease_expires_at"] <= _now():
                    state = "unknown"
                payload = json.loads(row["payload"])
                if self.run_store:
                    stored = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (payload["run"]["run_id"],)).fetchone()
                    run = AgentRunRecord.model_validate_json(stored[0]) if stored else None
                    if run is None or run.metadata.get("cancel_requested"):
                        state = "cancelled"
                    if state and run and run.status.value not in {"completed", "failed", "cancelled", "timed_out"}:
                        run.status = AgentRunStatus.CANCELLED if state == "cancelled" else AgentRunStatus.FAILED
                        run.error_type = "remote_execution_" + state
                        run.error = "Remote execution authority ended; effects may be unknown. Do not automatically replay."
                        self.run_store.save_run_in_transaction(conn, run.model_dump(mode="json"), updated_at=_now())
                        changed.append(run)
                if state:
                    conn.execute("UPDATE remote_execution_tasks SET state=?,lease_epoch=lease_epoch+1,updated_at=? WHERE task_id=?", (state, _now(), row["task_id"]))
        for run in changed:
            self.run_manager.refresh_after_transaction(run)
        return len(changed)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="lka-remote-hub-leases", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001, S110 - retry bounded local reconciliation.
                pass
            self._stop.wait(min(5, self.lease_seconds / 3))

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
