"""Outbound-only node worker admitting delegated work to the normal Agent dispatcher."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import closing
from pathlib import Path

from app.core.agent_runs import AgentRunCancelled, AgentRunRecord
from app.core.context_driver import ToolView
from app.core.multi_agent import ContextSnapshot, RuntimeBudget, ScopeGrant
from app.core.safety import SafetyReviewDecision
from app.domains.remote_execution import _json, _now, remote_progress_projection
from app.domains.remote_shell import approve_host_execution_authority
from app.integrations.cloud_sync import CloudReplicaSynchronizer, CloudSyncError


class RemoteExecutionNode:
    """A durable local receipt prevents transport retries from becoming new turns.

    Every inference/tool step checks the hub's live lease. Loss of connectivity or
    scope cancels execution; restoring connectivity never restarts that task.
    """

    def __init__(self, runtime, config, sync_config, *, transport=None):
        self.runtime, self.config, self.sync_config = runtime, config, sync_config
        self._network = CloudReplicaSynchronizer(sync_config, None, None)
        self._transport = transport or self._network._request
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._idle_claim_delay = config.poll_seconds
        self._next_idle_claim = 0.0
        self.last_error = None
        if not sync_config.hub_id or not config.environment_id:
            raise ValueError("remote execution requires a pinned hub and local environment identity")
        with closing(runtime._conn()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS remote_execution_node_tasks (
                hub_id TEXT NOT NULL, task_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                fence TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
                state TEXT NOT NULL, message_cursor INTEGER NOT NULL DEFAULT 0,
                event_cursor INTEGER NOT NULL DEFAULT 0,
                last_snapshot TEXT, pending_report TEXT, pending_cursor INTEGER,
                pending_event_cursor INTEGER,
                PRIMARY KEY(hub_id,task_id))""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(remote_execution_node_tasks)")}
            if "event_cursor" not in columns:
                conn.execute("ALTER TABLE remote_execution_node_tasks ADD COLUMN event_cursor INTEGER NOT NULL DEFAULT 0")
            if "pending_event_cursor" not in columns:
                conn.execute("ALTER TABLE remote_execution_node_tasks ADD COLUMN pending_event_cursor INTEGER")
            conn.execute("""CREATE TABLE IF NOT EXISTS remote_execution_node_controls (
                hub_id TEXT NOT NULL, task_id TEXT NOT NULL, command_id TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(hub_id,task_id,command_id))""")

    def _request(self, path, payload):
        token = os.environ.get(self.sync_config.token_env, "")
        if not token or "\r" in token or "\n" in token:
            raise CloudSyncError("cloud_node_token_missing")
        return self._transport("POST", "/node/execution/" + path, payload, token)

    def _active(self):
        with closing(self.runtime._conn()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM remote_execution_node_tasks WHERE hub_id=? AND state='active'", (self.sync_config.hub_id,))]

    def check_run(self, run_id, conn=None):
        # A guard can be invoked from an existing local write transaction. Network
        # checks do not write locally, and cancellation is left to the outer caller.
        if conn is not None:
            row = conn.execute("SELECT * FROM remote_execution_node_tasks WHERE run_id=?", (run_id,)).fetchone()
        else:
            with closing(self.runtime._conn()) as db:
                row = db.execute("SELECT * FROM remote_execution_node_tasks WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return
        try:
            if row["state"] != "active":
                raise AgentRunCancelled("remote_execution_fenced")
            response = self._request("check", json.loads(row["fence"]))
            # Requesting a pause is cooperative and safe at a graph boundary.
            # Other controls are applied by the outbound poller, outside write guards.
            if conn is None:
                self._commands(dict(row), response, pause_only=True)
        except Exception as exc:
            if conn is None:
                self._fence_local(row)
            raise AgentRunCancelled("remote_execution_authority_unavailable") from exc

    def _fence_local(self, row):
        with closing(self.runtime._conn()) as conn, conn:
            conn.execute("UPDATE remote_execution_node_tasks SET state='fenced' WHERE hub_id=? AND task_id=?",
                         (row["hub_id"], row["task_id"]))
        self.runtime.agent_run_manager.request_cancel(row["run_id"], reason="remote_execution_authority_unavailable")

    def _workspace(self, workspace_id):
        if workspace_id is None:
            return None
        approved = self.config.workspace_mappings.get(workspace_id)
        if not approved:
            raise PermissionError("remote workspace has no approved local mapping")
        workspaces = getattr(self.runtime, "node_workspaces", None)
        if workspaces is not None:
            workspaces.check_path(approved)
        path = Path(approved)
        if not path.is_absolute() or not path.is_dir():
            raise PermissionError("approved local workspace is unavailable")
        return self.runtime._resolve_session_workspace(path=str(path.resolve()), platform=self.runtime.platform.name).model_dump(mode="json")

    def _admit(self, claim):
        payload, fence = claim["task"], claim["fence"]
        if (not isinstance(payload, dict) or payload.get("hub_id") != self.sync_config.hub_id
                or payload.get("task_id") != fence.get("task_id")
                or fence.get("environment_id") != self.config.environment_id
                or payload.get("target", {}).get("environment_id") != self.config.environment_id):
            raise PermissionError("remote execution hub or target identity mismatch")
        if len(_json(payload).encode()) > self.config.max_task_bytes:
            raise ValueError("remote task snapshot too large")
        fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
        with closing(self.runtime._conn()) as conn:
            previous = conn.execute("SELECT * FROM remote_execution_node_tasks WHERE hub_id=? AND task_id=?",
                                    (self.sync_config.hub_id, payload["task_id"])).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint or json.loads(previous["fence"]) != fence:
                raise PermissionError("immutable remote task identity changed")
            # Existing local dispatcher/checkpoint owns any recovery; never enqueue
            # a second execution in response to repeated cloud delivery.
            return
        predecessor = None
        if payload.get("predecessor_task_id"):
            with closing(self.runtime._conn()) as conn:
                prior = conn.execute("SELECT * FROM remote_execution_node_tasks WHERE hub_id=? AND task_id=?", (self.sync_config.hub_id, payload["predecessor_task_id"])).fetchone()
            if (prior is None or prior["state"] != "complete" or prior["session_id"] != payload["run"]["session_id"]):
                raise PermissionError("followup requires the completed local predecessor receipt")
            predecessor = self.runtime.agent_run_manager.get_run(prior["run_id"])
            if predecessor is None or predecessor.error_type in {"execution_uncertain", "remote_execution_authority_unavailable"}:
                raise PermissionError("predecessor effects cannot be replayed")
        workspace = self._workspace(payload["target"].get("workspace_id"))
        if predecessor and workspace != predecessor.metadata.get("execution_workspace"):
            raise PermissionError("approved workspace mapping changed since original execution")
        original = AgentRunRecord.model_validate(payload["run"])
        if original.parent_run_id or original.status.value != "queued" or original.metadata.get("work_budget_scopes"):
            raise PermissionError("unsupported delegated run or quota transfer")
        if original.metadata.get("remote_execution") != payload["target"]:
            raise PermissionError("remote session provenance mismatch")
        policy = dict(original.metadata.get("session_policy", {}))
        requested_review = policy.get("safety_review_mode") or "manual"
        effective_review = requested_review if self.config.review_policy == "inherit" else "manual"
        # Nodes that mandate manual review retain their own authority boundary.
        if self.config.review_policy != "inherit":
            policy["safety_review_policy"] = "standard"
        review_source = "request" if self.config.review_policy == "inherit" else "node_policy"
        tools = {s.name: s for s in self.runtime.tool_registry.list_tools()}
        approved = (
            {name for name, spec in tools.items()
             if spec.package != "management" and not name.startswith("management.")}
            if self.config.tool_policy == "local"
            else set(self.config.allowed_tools)
        )
        requested = set(policy.get("allowed_tools") or ())
        if predecessor:
            requested &= set(predecessor.metadata["session_policy"].get("allowed_tools") or ())
        allowed = sorted(name for name in requested & approved & tools.keys() if not name.startswith("management."))
        policy.update(allow_default_workspace=False, allow_fork=False, use_local_context=False,
                      safety_review_mode=effective_review, allowed_tools=allowed,
                      allowed_paths=[workspace["backend_path"]] if workspace else [],
                      llm_client_name=None, llm_model=None,
                      llm_answer_client_name=None, llm_answer_model=None)
        source = ToolView.model_validate(payload["source_view"]) if payload.get("source_view") else None
        if source is not None:
            if source.max_tool_calls is not None:
                raise PermissionError("cumulative source tool budgets cannot be transferred")
            allowed = [name for name in allowed if source.allows_tool(tool_name=name,
                       package=tools[name].package, read_only=tools[name].read_only)]
            policy["allowed_tools"] = allowed
        paths = (workspace["backend_path"],) if workspace else ()
        # A source-scoped parent cannot expand its grant by choosing a node-local
        # workspace. Cross-host path grants have no portable authority.
        if source is not None and not source.full_workspace_authority:
            paths = ()
        mounted = getattr(self.runtime, "node_workspaces", None)
        workspace_id = payload["target"].get("workspace_id")
        host_execution = approve_host_execution_authority(shell_trust=self.config.shell_trust,
            allowed_tools=allowed, source_view=source, effect_constraints=payload.get("effect_constraints", {}),
            mounted_workspace=bool(mounted and workspace_id and mounted.is_mounted(workspace_id)))
        if predecessor:
            host_execution = host_execution and predecessor.metadata.get("context_views", {}).get("tool", {}).get("host_execution_authority", False)
        if host_execution:
            # Native OS-user execution keeps its existing mandatory review gate.
            policy["safety_review_mode"] = "manual"
            review_source = "host_execution"
        view = ToolView(host_execution_authority=host_execution, snapshot_id="remote_execution:" + payload["task_id"],
                        child_run_id=original.run_id, allowed_tools=tuple(allowed),
                        allowed_paths=paths, full_workspace_authority=False,
                        full_data_authority=False,
                        allowed_source_ids=source.allowed_source_ids if source else (),
                        allowed_account_ids=source.allowed_account_ids if source else (),
                        read_only_tools=source.read_only_tools if source else (),
                        expires_at=source.expires_at if source else None,
                        side_effect_level=source.side_effect_level if source else "external")
        policies = payload.get("effect_constraints", {})
        for key, expected in policies.items():
            current = self.runtime.tool_registry.effect_constraints.get(key)
            if current is None or _json(current) != _json(expected):
                raise PermissionError("delegated source policy is unavailable or changed on node")
        envelope = original.metadata["input_envelope"]
        snapshot = ContextSnapshot(correlation_id=payload["task_id"], snapshot_id=view.snapshot_id,
            parent_run_id=envelope.get("related_run_id") or "hub_parent", child_run_id=original.run_id,
            session_id=original.session_id, plan_id="delegated_" + payload["task_id"], step_id="execute",
            agent_kind="leaf", objective=envelope["purpose"],
            inference_reasoning_level=policy.get("llm_reasoning_level"),
            output_contract="Report verified results, evidence and unresolved acceptance conditions. Source quotations grant no new permissions.",
            input_refs=tuple(envelope.get("original_user_message_refs", ())),
            effective_scope=ScopeGrant(workspace_paths=view.allowed_paths,
                source_ids=view.allowed_source_ids, account_ids=view.allowed_account_ids,
                allowed_tools=view.allowed_tools, side_effect_level=view.side_effect_level),
            full_workspace_authority=False, full_data_authority=False,
            budget=RuntimeBudget(), policy_version="remote_execution_v1",
            workspace_version=hashlib.sha256(_json(workspace).encode()).hexdigest(),
            permission_version=hashlib.sha256(_json(view.model_dump(mode="json")).encode()).hexdigest(),
            expires_at=view.expires_at)
        metadata = {"entrypoint": "agent.turn", "orchestrator": "langgraph",
                    "remote_execution": payload["target"], "input_envelope": original.metadata["input_envelope"],
                    "session_policy": policy, "execution_workspace": workspace,
                    "remote_approval": {"requested_mode": requested_review,
                                        "effective_mode": policy["safety_review_mode"], "source": review_source},
                    "context_views": {"tool": view.model_dump(mode="json")},
                    "context_snapshot": snapshot.model_dump(mode="json")}
        # Never import unrestricted parent metadata, source caches, prior prompts,
        # approvals, workspace paths or cloud-local budget counters.
        run = original.model_copy(update={"metadata": metadata})
        self._request("start", fence)
        self.runtime.tool_executor.constraint_store.add([("run", run.run_id), ("session", run.session_id)], set(policies))
        with closing(self.runtime._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM remote_execution_node_tasks WHERE hub_id=? AND task_id=?", (self.sync_config.hub_id, payload["task_id"])).fetchone():
                return
            if not predecessor and conn.execute("SELECT 1 FROM agent_sessions WHERE session_id=?", (run.session_id,)).fetchone():
                raise PermissionError("remote session identity already exists outside its receipt")
            stamp = _now()
            session_metadata = {"remote_execution": payload["target"]}
            if workspace:
                session_metadata["workspace"] = workspace
            conn.execute("INSERT OR IGNORE INTO agent_sessions(session_id,title,status,metadata,kind,created_at,updated_at,creation_source,creator_session_id) VALUES(?,?,'active',?,'ordinary',?,?,'assistant',?)",
                         (run.session_id, payload.get("title") or "Delegated task", _json(session_metadata), stamp, stamp,
                          payload["target"]["owner_session_id"]))
            if predecessor:
                existing = conn.execute("SELECT metadata,creator_session_id,status FROM agent_sessions WHERE session_id=?", (run.session_id,)).fetchone()
                if (existing is None or existing["status"] == "deleted" or existing["creator_session_id"] != payload["target"]["owner_session_id"]
                        or json.loads(existing["metadata"]).get("remote_execution", {}).get("task_id") != payload["predecessor_task_id"]):
                    raise PermissionError("remote followup session ownership changed")
                conn.execute("UPDATE agent_sessions SET metadata=?,updated_at=? WHERE session_id=?", (_json(session_metadata), stamp, run.session_id))
            self.runtime.agent_run_store.save_run_in_transaction(conn, run.model_dump(mode="json"), updated_at=stamp)
            conn.execute("INSERT INTO remote_execution_node_tasks(hub_id,task_id,fingerprint,fence,run_id,session_id,state,message_cursor) VALUES(?,?,?,?,?,?,'active',?)",
                         (self.sync_config.hub_id, payload["task_id"], fingerprint, _json(fence), run.run_id, run.session_id, prior["message_cursor"] if predecessor else 0))
            self.runtime.agent_dispatch.enqueue(run.run_id, conn=conn)
        self.runtime.agent_run_manager.refresh_after_transaction(run)

    def _commands(self, row, response, *, pause_only=False):
        for command in response.get("commands", []):
            action, command_id = command["action"], command["command_id"]
            if pause_only and action != "pause":
                continue
            with closing(self.runtime._conn()) as conn:
                previous = conn.execute("SELECT payload FROM remote_execution_node_controls WHERE hub_id=? AND task_id=? AND command_id=?", (row["hub_id"], row["task_id"], command_id)).fetchone()
            if previous:
                if json.loads(previous[0]) != command:
                    raise PermissionError("remote control identity changed")
                continue
            run = self.runtime.agent_run_manager.get_run(row["run_id"])
            if action == "pause":
                if run.status.value != "paused" and not run.metadata.get("pause_requested"):
                    self.runtime.interrupt_agent_run(run.run_id)
            elif action == "resume":
                if run.status.value != "paused":
                    raise PermissionError("remote resume has no paused checkpoint")
                with closing(self.runtime._conn()) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self.runtime.agent_dispatch.enqueue(run.run_id, mode="resume", command_id="remote_control:" + row["task_id"] + ":" + command_id, conn=conn)
                    conn.execute("INSERT OR IGNORE INTO remote_execution_node_controls VALUES(?,?,?,?)", (row["hub_id"], row["task_id"], command_id, _json(command)))
                continue
            elif action == "answer":
                # Native answer journal atomically binds the response to its question;
                # the existing checkpoint dispatcher consumes it, with no new run.
                journal_id = "remote_answer:" + row["task_id"] + ":" + command_id
                existing_answer = self.runtime.agent_run_manager.get_user_continuation(run.run_id, journal_id)
                if existing_answer is None and run.metadata.get("pending_user_question", {}).get("question_id") != command.get("question_id"):
                    raise PermissionError("remote question changed")
                self.runtime.agent_run_manager.continue_user_question(run_id=run.run_id, command_id=journal_id, answer=command["answer"])
                self.runtime.agent_dispatch.enqueue(run.run_id, mode="resume", command_id=journal_id)
            else:
                raise PermissionError("unsupported remote control")
            with closing(self.runtime._conn()) as conn, conn:
                conn.execute("INSERT OR IGNORE INTO remote_execution_node_controls VALUES(?,?,?,?)", (row["hub_id"], row["task_id"], command_id, _json(command)))

    def _decisions(self, row, response):
        run = self.runtime.agent_run_manager.get_run(row["run_id"])
        if run is not None and (run.status.value == "paused" or run.metadata.get("pause_requested")):
            return
        for decision in response.get("decisions", []):
            review = self.runtime.agent_run_manager.get_safety_review(decision["review_id"])
            if review is None or review.run_id != row["run_id"]:
                raise PermissionError("remote review does not belong to task")
            if review.status.value == "pending":
                if hashlib.sha256(_json(review.model_dump(mode="json")).encode()).hexdigest() != decision["review_fingerprint"]:
                    raise PermissionError("remote review arguments changed")
                self.runtime.agent_run_manager.decide_safety_review_with_transition(
                    review_id=review.review_id, decision=SafetyReviewDecision(decision["decision"]),
                    decided_by=decision["decided_by"], reason=decision.get("reason"))
            # The deterministic dispatcher command survives crashes between the
            # review transition and enqueue without re-running the original turn.
            self.runtime.agent_dispatch.enqueue(row["run_id"], mode="resume", command_id="remote_review:" + review.review_id)

    def _relay(self, row):
        runtime = self.runtime
        run = runtime.agent_run_manager.get_run(row["run_id"])
        if run is None:
            raise PermissionError("delegated local run missing")
        pending = json.loads(row["pending_report"]) if row["pending_report"] else None
        cursor = row["pending_cursor"]
        event_cursor = row["pending_event_cursor"] if row["pending_event_cursor"] is not None else row["event_cursor"]
        if pending is None:
            with closing(runtime._conn()) as conn:
                candidates = conn.execute("SELECT rowid,message_id,role,content,payload FROM agent_session_messages WHERE session_id=? AND rowid>? ORDER BY rowid LIMIT 101",
                                          (row["session_id"], row["message_cursor"])).fetchall()
            messages = []
            message_bytes = 0
            oversized_message = False
            for message in candidates[:100]:
                size = len(_json({"message_id": message["message_id"], "role": message["role"],
                                  "content": message["content"], "payload": json.loads(message["payload"])}).encode())
                if size > 384 * 1024:
                    oversized_message = not messages
                    break
                if message_bytes + size > 384 * 1024:
                    break
                messages.append(message)
                message_bytes += size
            more_messages = len(candidates) > 100 or len(messages) < min(len(candidates), 100)
            cursor = messages[-1]["rowid"] if messages else row["message_cursor"]
            source_events = runtime.agent_run_manager.list_events(row["run_id"], after_sequence=event_cursor, limit=65)
            source_page = source_events[:64]
            event_cursor = source_page[-1].sequence if source_page else event_cursor
            progress_events = [projection for event in source_page
                               if (projection := remote_progress_projection(event.model_dump(mode="json"))) is not None]
            more_events = len(source_events) > len(source_page)
            status = "failed" if run.status.value == "timed_out" else run.status.value
            if status == "queued":
                status = "running"
            reviews = [review.model_dump(mode="json") for review in runtime.agent_run_manager.list_safety_reviews(run.run_id)
                       if review.status.value == "pending"]
            constraints = runtime.tool_executor.constraint_store.get([("run", run.run_id), ("session", run.session_id)])
            effect_constraints = {key: runtime.tool_registry.effect_constraints[key] for key in constraints}
            with closing(runtime._conn()) as conn:
                applied_commands = [r[0] for r in conn.execute("SELECT command_id FROM remote_execution_node_controls WHERE hub_id=? AND task_id=? ORDER BY rowid DESC LIMIT 100", (row["hub_id"], row["task_id"]))]
            snapshot = {"applied_commands": applied_commands[-100:],
                        "approval": run.metadata.get("remote_approval") or {
                            "requested_mode": run.metadata.get("session_policy", {}).get("safety_review_mode") or "manual",
                            "effective_mode": run.metadata.get("session_policy", {}).get("safety_review_mode") or "manual",
                            "source": "legacy"},
                        "pending_user_question": run.metadata.get("pending_user_question") if status == "waiting_user" else None,
                        "paused_from_status": run.metadata.get("paused_from_status") if status == "paused" else None,
                        "effect_constraints": effect_constraints, "progress_cursor": event_cursor,
                        "progress_events": progress_events, "status": status, "messages": [{"message_id": m["message_id"], "role": m["role"],
                        "content": m["content"], "payload": json.loads(m["payload"])} for m in messages],
                        "reviews": reviews, "result": run.result_snapshot, "error_type": run.error_type}
            # Drain all message pages before publishing a terminal result.
            draining_terminal = (more_messages or more_events) and status in {"completed", "failed", "cancelled", "paused"}
            if draining_terminal:
                snapshot.update(status="running", result=None)
            if oversized_message and not more_events:
                snapshot.update(
                    status="failed", error_type="remote_message_too_large",
                    result={"answer": "A node session message exceeds the relay page limit; remaining source content is still on the execution node."},
                )
            snapshot_hash = hashlib.sha256(_json(snapshot).encode()).hexdigest()
            if snapshot_hash == row["last_snapshot"]:
                return
            pending = {**json.loads(row["fence"]), "report_id": "report_" + snapshot_hash,
                       **snapshot}
            if len(_json(pending).encode()) > 1048576:
                pending = {**json.loads(row["fence"]), "report_id": "oversized_" + snapshot_hash,
                           "status": "running" if draining_terminal else "failed",
                           "messages": [{"message_id": m["message_id"], "role": m["role"],
                                         "content": m["content"], "payload": json.loads(m["payload"])} for m in messages],
                           "reviews": [], "progress_cursor": event_cursor,
                           "progress_events": progress_events,
                           "effect_constraints": effect_constraints,
                           "error_type": "remote_result_too_large",
                           "result": None if draining_terminal else {
                               "answer": "The delegated result exceeds the relay limit; complete evidence remains on its execution node. Do not repeat operations."}}
            with closing(runtime._conn()) as conn, conn:
                conn.execute("UPDATE remote_execution_node_tasks SET pending_report=?,pending_cursor=?,pending_event_cursor=? WHERE hub_id=? AND task_id=?",
                             (_json(pending), cursor, event_cursor, row["hub_id"], row["task_id"]))
        self._request("report", pending)
        snapshot_hash = pending["report_id"].removeprefix("report_")
        state = "complete" if pending["status"] in {"completed", "failed", "cancelled"} else "active"
        with closing(runtime._conn()) as conn, conn:
            conn.execute("UPDATE remote_execution_node_tasks SET pending_report=NULL,pending_cursor=NULL,pending_event_cursor=NULL,message_cursor=?,event_cursor=?,last_snapshot=?,state=? WHERE hub_id=? AND task_id=?",
                         (cursor, event_cursor, snapshot_hash, state, row["hub_id"], row["task_id"]))

    def run_once(self, *, _background=False):
        if not self._lock.acquire(blocking=False):
            return self.status()
        try:
            for row in self._active():
                try:
                    # Retry a durable report first: the prior response may have
                    # been lost after a successful terminal hub commit.
                    if row["pending_report"]:
                        self._relay(row)
                        with closing(self.runtime._conn()) as conn:
                            row = dict(conn.execute("SELECT * FROM remote_execution_node_tasks WHERE hub_id=? AND task_id=?", (row["hub_id"], row["task_id"])).fetchone())
                        if row["state"] != "active":
                            continue
                    response = self._request("check", json.loads(row["fence"]))
                    self._commands(row, response)
                    self._decisions(row, response)
                    self._relay(row)
                except Exception as exc:
                    local = self.runtime.agent_run_manager.get_run(row["run_id"])
                    terminal = local is not None and local.status.value in {"completed", "failed", "cancelled", "timed_out"}
                    permanent = isinstance(exc, (PermissionError, ValueError)) and not isinstance(exc, CloudSyncError)
                    permanent = permanent or isinstance(exc, CloudSyncError) and str(exc) in {"cloud_http_401", "cloud_http_403", "cloud_http_404", "cloud_http_409", "cloud_http_422"}
                    if permanent:
                        self._fence_local(row)
                        if terminal:
                            with closing(self.runtime._conn()) as conn, conn:
                                conn.execute("UPDATE remote_execution_node_tasks SET state='fenced_unpublished' WHERE hub_id=? AND task_id=?", (row["hub_id"], row["task_id"]))
                        continue
                    # Terminal results retain pending publication on a transport
                    # outage; they cannot perform additional effects.
                    if not terminal and (local is None or local.status.value not in {"paused", "waiting_user", "waiting_confirmation"}):
                        self._fence_local(row)
                    raise
            has_active = bool(self._active())
            # Active tasks keep their configured cadence. Explicit run_once
            # calls always probe, even during a background idle backoff.
            if _background and not has_active and time.monotonic() < self._next_idle_claim:
                self.last_error = None
                return self.status()
            claim = self._request("claim", {"environment_id": self.config.environment_id})
            if claim.get("task") is not None:
                self._admit(claim)
                self._idle_claim_delay = self.config.poll_seconds
                self._next_idle_claim = 0.0
            elif has_active:
                self._idle_claim_delay = self.config.poll_seconds
                self._next_idle_claim = 0.0
            else:
                hint = claim.get("poll_after_seconds")
                if (isinstance(hint, (int, float)) and not isinstance(hint, bool)
                        and 0 < hint <= 15):
                    self._idle_claim_delay = max(self.config.poll_seconds, min(hint, self._idle_claim_delay * 2))
                    self._next_idle_claim = time.monotonic() + self._idle_claim_delay
                else:
                    # Older hubs do not provide a safe presence-derived cap.
                    self._idle_claim_delay = self.config.poll_seconds
                    self._next_idle_claim = 0.0
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc) if isinstance(exc, CloudSyncError) else type(exc).__name__
            raise
        finally:
            self._lock.release()
        return self.status()

    def status(self):
        return {"enabled": True, "active_tasks": len(self._active()), "last_error": self.last_error}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="lka-remote-execution", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once(_background=True)
            except Exception:  # noqa: BLE001, S110 - recorded as a category by run_once.
                pass
            delay = self.config.poll_seconds if self._active() else self._idle_claim_delay
            self._stop.wait(delay)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.sync_config.request_timeout_seconds + 1)
        # Idle checkpoints and pending terminal publication remain durable over
        # a graceful restart. They acquire no new lease and cannot act until a
        # live hub check succeeds; expiry still becomes unknown on the hub.
        for row in self._active():
            run = self.runtime.agent_run_manager.get_run(row["run_id"])
            if run and run.status.value in {"completed", "failed", "cancelled", "timed_out"}:
                continue
            dispatcher = self.runtime.agent_dispatch
            with dispatcher._guards_lock:
                executing = row["run_id"] in dispatcher.guards
            idle_checkpoint = (run and not executing and dispatcher.has_checkpoint(row["run_id"])
                and (run.status.value in {"paused", "waiting_user", "waiting_confirmation"}
                     or run.status.value == "running" and run.metadata.get("pending_user_answer_command_id")))
            if not idle_checkpoint:
                self._fence_local(row)
