"""Deterministic management commands and task receipts; never calls an LLM."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from app.core.agent_runs import AgentRunRecord, AgentRunStatus
from app.core.turn_input import TurnInputEnvelope
from app.domains.management import ManagementConflictError, ManagementPausedError
from app.storage.commit_signals import notify_after_commit

TERMINAL = {"completed", "failed", "cancelled", "timed_out"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ManagedTaskRunMismatchError(ManagementConflictError):
    """A stale or invalid reference, without claiming a concurrent update."""

    code = "expected_run_mismatch"

    def __init__(self, *, expected_run_id, current_run):
        super().__init__("Target run changed or expected_run_id is incorrect; use the current run view to reassess")
        self.expected_run_id = expected_run_id
        self.current_run = current_run
        self.observed_at = datetime.now(UTC).isoformat()


class ManagedTaskService:
    def __init__(self, conn_factory, *, sessions, management, run_store, run_manager,
                 policy, workspace, enqueue, replace, interrupt, enabled, guard, durable_execution,
                 review_mode=None, review_policy=None):
        self._conn = conn_factory
        self.sessions, self.management = sessions, management
        self.run_store, self.run_manager = run_store, run_manager
        self.policy, self.workspace, self.enqueue = policy, workspace, enqueue
        self.replace, self.interrupt, self.enabled = replace, interrupt, enabled
        self.guard = guard
        self.durable_execution = durable_execution
        self.review_mode = review_mode or (lambda: None)
        self.review_policy = review_policy or (lambda: "standard")
        # Optional {path, platform} configured by the operator; set by the runtime.
        self.default_task_parent = None

    def dispatch_review_mode(self, requested=None, inherited=None):
        """Resolve the server's dispatch default before caller and inherited modes."""
        configured = self.review_mode()
        return configured if configured is not None else (requested or inherited)

    def require_durable_execution(self):
        if not self.durable_execution():
            raise ManagementConflictError("Management dispatch requires agent.checkpoint_backend=sqlite")

    @staticmethod
    def begin_task_transaction(conn):
        """Bounded admission retry before any task rows or effects exist."""
        for attempt in range(2):
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                if attempt or conn.in_transaction or code is None or code & 0xff != sqlite3.SQLITE_BUSY:
                    raise
                time.sleep(0.05)

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS management_commands (
                    manager_session_id TEXT NOT NULL, command_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, action TEXT NOT NULL,
                    receipt TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(manager_session_id, command_id)
                );
                CREATE TABLE IF NOT EXISTS managed_session_links (
                    manager_session_id TEXT NOT NULL, task_session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, command_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(manager_session_id, command_id)
                );
                CREATE TABLE IF NOT EXISTS manager_goal_proposals (
                    execution_id TEXT NOT NULL, command_id TEXT NOT NULL,
                    goal_id TEXT NOT NULL, args TEXT NOT NULL,
                    PRIMARY KEY(execution_id,command_id), UNIQUE(execution_id,goal_id)
                );
                CREATE TABLE IF NOT EXISTS manager_timer_causes (
                    goal_id TEXT NOT NULL, timer_generation INTEGER NOT NULL,
                    cause_paths TEXT NOT NULL,
                    PRIMARY KEY(goal_id,timer_generation)
                );
            """)

    def caller(self, context, *, mutate=False, conn=None, supervisor_only=False):
        if not self.enabled():
            raise ManagementConflictError("Management pilot is disabled")
        if conn is not None:
            stored = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (context.run_id,)).fetchone()
            run = AgentRunRecord.model_validate_json(stored[0]) if stored else None
        else:
            run = self.run_manager.get_run(context.run_id) if context.run_id else None
        if (run is None or run.session_id != context.session_id or run.parent_run_id is not None
                or run.status != AgentRunStatus.RUNNING or run.metadata.get("cancel_requested")):
            raise ManagementConflictError("Management tools require an executing management root")
        self.guard(run.run_id, conn=conn)
        def check(db):
            delegates = getattr(self, "delegates", None)
            if delegates is not None:
                if supervisor_only and delegates.get_for_session(db, context.session_id) is not None:
                    raise ManagementConflictError("This tool requires a top-level management supervisor")
                delegates.check_session(db, context.session_id)
            row = db.execute("SELECT c.status FROM manager_controllers c JOIN agent_sessions s "
                             "ON s.session_id=c.session_id WHERE c.session_id=? "
                             "AND s.kind='manager' AND s.status!='deleted'", (context.session_id,)).fetchone()
            if row is None:
                raise ManagementConflictError("Activate the management controller before issuing commands")
            if mutate and row["status"] != "active":
                raise ManagementPausedError("Management is paused. No task was accepted. Activate the management controller before issuing commands.")
        if conn is not None:
            check(conn)
        else:
            with closing(self._conn()) as db:
                check(db)
        return run

    @staticmethod
    def _page(limit, offset):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and offset non-negative")

    def _target(self, conn, session_id, *, manager_session_id=None):
        row = conn.execute("SELECT * FROM agent_sessions WHERE session_id=? AND status!='deleted'",
                           (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        if row["kind"] != "ordinary":
            raise ManagementConflictError("Only ordinary task sessions can be controlled")
        delegates = getattr(self, "delegates", None)
        if manager_session_id and delegates is not None and delegates.get_for_session(conn, manager_session_id):
            delegates.check_session(conn, manager_session_id)
            if not conn.execute("SELECT 1 FROM managed_session_links WHERE manager_session_id=? AND task_session_id=?",
                                (manager_session_id, session_id)).fetchone():
                raise ManagementConflictError("Subagents may only manage their own execution sessions")
        return row

    def list_sessions(self, context, *, limit=30, offset=0, q=""):
        self.caller(context)
        self._page(limit, offset)
        if not isinstance(q, str) or len(q) > 500:
            raise ValueError("q must be at most 500 characters")
        with closing(self._conn()) as conn:
            delegates = getattr(self, "delegates", None)
            scoped = delegates is not None and delegates.get_for_session(conn, context.session_id) is not None
            rows = conn.execute(
                "SELECT s.session_id,s.title,s.status,s.creation_source,s.creator_session_id,"
                "s.updated_at,r.record_payload AS latest_run_payload "
                "FROM agent_sessions s LEFT JOIN agent_runs r ON r.run_id=("
                "SELECT a.run_id FROM agent_runs a WHERE a.session_id=s.session_id "
                "AND json_extract(a.record_payload,'$.parent_run_id') IS NULL "
                "ORDER BY a.created_at DESC,a.run_id DESC LIMIT 1) "
                "WHERE s.kind='ordinary' AND s.status!='deleted' AND instr(lower(s.title),lower(?))>0 "
                "AND (?=0 OR EXISTS(SELECT 1 FROM managed_session_links l "
                "WHERE l.manager_session_id=? AND l.task_session_id=s.session_id)) "
                "ORDER BY s.updated_at DESC,s.session_id LIMIT ? OFFSET ?", (q, int(scoped), context.session_id, limit + 1, offset),
            ).fetchall()
        sessions = []
        for row in rows[:limit]:
            item = dict(row)
            payload = item.pop("latest_run_payload")
            item["latest_run"] = self._run_view(json.loads(payload)) if payload else None
            sessions.append(item)
        return {"sessions": sessions, "observed_at": datetime.now(UTC).isoformat(),
                "next_offset": offset + limit if len(rows) > limit else None}

    def _latest_run(self, conn, session_id):
        row = conn.execute("SELECT record_payload FROM agent_runs WHERE session_id=? "
                           "AND json_extract(record_payload,'$.parent_run_id') IS NULL "
                           "ORDER BY created_at DESC,run_id DESC LIMIT 1", (session_id,)).fetchone()
        return self._run_view(json.loads(row[0])) if row else None

    def read_session(self, context, *, session_id, limit=30, offset=0, message_id=None, start_char=0):
        self.caller(context)
        self._page(limit, offset)
        if type(start_char) is not int or start_char < 0:
            raise ValueError("start_char must be non-negative")
        with closing(self._conn()) as conn:
            # State and message pages belong to the same read snapshot. A task
            # may finish concurrently, but cannot split these two observations.
            conn.execute("BEGIN")
            self._target(conn, session_id, manager_session_id=context.session_id)
            latest_run = self._latest_run(conn, session_id)
            if message_id is not None:
                rows = conn.execute("SELECT message_id,role,content,created_at FROM agent_session_messages "
                                    "WHERE session_id=? AND message_id=?", (session_id, message_id)).fetchall()
                if not rows:
                    raise KeyError(message_id)
            else:
                rows = conn.execute("SELECT message_id,role,content,created_at FROM agent_session_messages "
                                    "WHERE session_id=? ORDER BY rowid LIMIT ? OFFSET ?",
                                    (session_id, limit + 1, offset)).fetchall()
        messages = []
        for row in rows[:limit]:
            item = dict(row)
            content = item["content"]
            start = start_char if message_id else 0
            item["content"] = content[start:start + 4000]
            item["next_char"] = start + 4000 if start + 4000 < len(content) else None
            messages.append(item)
        return {"session_id": session_id, "messages": messages, "latest_run": latest_run,
                "observed_at": datetime.now(UTC).isoformat(),
                "next_offset": offset + limit if message_id is None and len(rows) > limit else None}

    def list_runs(self, session_id, *, limit=30, offset=0, context=None, client_request_id=None):
        self._page(limit, offset)
        with closing(self._conn()) as conn:
            if context is not None:
                self.caller(context, conn=conn)
                self._target(conn, session_id, manager_session_id=context.session_id)
            row = conn.execute("SELECT 1 FROM agent_sessions WHERE session_id=? AND status!='deleted'", (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            correlation_sql = ("AND json_extract(record_payload,'$.metadata.client_request_id')=? "
                               if client_request_id is not None else "")
            correlation_args = (client_request_id,) if client_request_id is not None else ()
            rows = conn.execute("SELECT record_payload FROM agent_runs WHERE session_id=? "
                                "AND json_extract(record_payload,'$.parent_run_id') IS NULL "
                                + correlation_sql + "ORDER BY created_at DESC,run_id DESC LIMIT ? OFFSET ?",
                                (session_id, *correlation_args, limit + 1, offset)).fetchall()
        return {"session_id": session_id, "runs": [self._run_view(json.loads(r[0])) for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}

    @staticmethod
    def _run_view(record):
        result = {key: record.get(key) for key in (
            "run_id", "session_id", "trace_id", "parent_run_id", "status", "created_at",
            "started_at", "completed_at", "paused_at", "waiting_since", "error_type",
        )}
        metadata = record.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        client_request_id = metadata.get("client_request_id")
        if isinstance(client_request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", client_request_id):
            result["client_request_id"] = client_request_id
        unfinished = record.get("status") not in TERMINAL
        result.update(pause_requested=unfinished and metadata.get("pause_requested") is True,
                      cancel_requested=unfinished and metadata.get("cancel_requested") is True,
                      paused_from_status=metadata.get("paused_from_status"),
                      superseded_by_run_id=metadata.get("superseded_by_run_id"),
                      has_result=record.get("result_snapshot") is not None)
        if record.get("status") == "waiting_user":
            question = metadata.get("pending_user_question")
            if isinstance(question, dict):
                question_id, text = question.get("question_id"), question.get("question")
                if (isinstance(question_id, str) and question_id.strip() and len(question_id) <= 256
                        and isinstance(text, str) and text.strip()):
                    pending = {"question_id": question_id, "question": text[:4000]}
                    patch_id, asked_at = question.get("patch_id"), question.get("asked_at")
                    if isinstance(patch_id, str) and patch_id.strip() and len(patch_id) <= 256:
                        pending["patch_id"] = patch_id
                    if isinstance(asked_at, str):
                        pending["asked_at"] = asked_at[:80]
                    result["pending_user_question"] = pending
                    result["user_control"] = {"session_id": record.get("session_id"), "run_id": record.get("run_id"),
                                              "continue_path": "/agent/runs/" + record["run_id"] + "/continue"}
        elif record.get("status") == "waiting_confirmation":
            result["user_control"] = {"session_id": record.get("session_id"), "run_id": record.get("run_id"),
                                      "requires_original_approval": True}
        return result

    def inspect_run(self, context, *, run_id):
        self.caller(context)
        run = self.run_store.load_run(run_id)
        if run is None:
            raise KeyError(run_id)
        with closing(self._conn()) as conn:
            self._target(conn, run["session_id"], manager_session_id=context.session_id)
        # Session message pagination is the complete answer/evidence reader.
        return {**self._run_view(run), "read_result": {"session_id": run["session_id"]},
                "observed_at": datetime.now(UTC).isoformat()}

    def _command(self, conn, context, command_id, action, args):
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 128:
            raise ValueError("command_id must contain 1..128 characters")
        fingerprint = sha256(_json({"action": action, **args}).encode()).hexdigest()
        row = conn.execute("SELECT fingerprint,receipt FROM management_commands "
                           "WHERE manager_session_id=? AND command_id=?", (context.session_id, command_id)).fetchone()
        if row and row["fingerprint"] != fingerprint:
            raise ManagementConflictError("command_id already used with different arguments")
        return fingerprint, json.loads(row["receipt"]) if row else None

    def _receipt(self, conn, context, command_id, action, fingerprint, run):
        row = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (run.run_id,)).fetchone()
        if row is None:
            raise ManagementConflictError("Command target run is unavailable")
        persisted = json.loads(row[0])
        now = datetime.now(UTC).isoformat()
        result = {"command_id": command_id, "session_id": run.session_id, "run_id": run.run_id,
                  "status": "accepted", "action": action, "snapshot_at": now,
                  "snapshot_kind": "command_commit", "run_snapshot": self._run_view(persisted)}
        metadata = persisted.get("metadata", {})
        if (metadata.get("execution_workspace") is None and "remote_execution" not in metadata
                and action == "start_task"):
            # Accepted, but file and command tools are withheld; say so at dispatch
            # instead of letting the task discover it after an empty session list.
            result["workspace_notice"] = ("no_execution_workspace: file and shell tools are unavailable; "
                                          "pass workspace_path, set default_execution_workspace or "
                                          "management.default_task_parent for work that needs files or commands")
        replaced_id = persisted.get("metadata", {}).get("replaces_run_id")
        if replaced_id:
            previous = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (replaced_id,)).fetchone()
            if previous:
                result["replaced_run_snapshot"] = self._run_view(json.loads(previous[0]))
        conn.execute("INSERT INTO management_commands VALUES(?,?,?,?,?,?)",
                     (context.session_id, command_id, fingerprint, action, _json(result), now))
        conn.execute("INSERT INTO managed_session_links VALUES(?,?,?,?,?)",
                     (context.session_id, run.session_id, run.run_id, command_id, now))
        return result

    def _envelope(self, context, run, command_id, purpose):
        plural = getattr(self.sessions, "get_turn_user_messages", None)
        if plural is not None:
            originals = plural(session_id=context.session_id, trace_id=run.trace_id)
        else:
            original = self.sessions.get_turn_user_message(session_id=context.session_id, trace_id=run.trace_id)
            originals = [original] if original else []
        quoted = [message for message in originals[:8] if message.content]
        return TurnInputEnvelope(
            origin="manager", input_id=sha256(_json([context.session_id, command_id]).encode()).hexdigest(),
            sender_label="管理会话 " + context.session_id, purpose=purpose,
            original_user_message_refs=[message.message_id for message in originals[:16]],
            original_user_quotes=[message.content[:8000] for message in quoted],
            related_session_id=context.session_id, related_run_id=run.run_id,
            debug_info={"command_id": command_id,
                        "original_quote_truncated": len(originals) > len(quoted) or any(len(message.content) > 8000 for message in quoted),
                        "original_refs_truncated": len(originals) > 16},
        )

    def _new_run(self, session_id, envelope, source=None, *, workspace=None, safety_review_mode=None,
                 reasoning_level=None, reasoning_source=None):
        policy = self.policy(session_id).model_dump(mode="json")
        policy["safety_review_policy"] = self.review_policy()
        if source is not None:
            inherited = source.metadata.get("session_policy", {})
            for name in ("llm_answer_client_name", "llm_answer_model"):
                if name in inherited:
                    policy[name] = inherited[name]
        configured_review_mode = self.review_mode()
        if configured_review_mode is not None:
            policy["safety_review_mode"] = configured_review_mode
        elif policy.get("safety_review_mode") is None:
            policy["safety_review_mode"] = safety_review_mode or (
                source.metadata.get("session_policy", {}).get("safety_review_mode") if source else None
            )
        inherited_reasoning = (reasoning_level if reasoning_level in {"off", "low", "high", "max"}
                               else source.metadata.get("llm_reasoning_level") if source else None)
        if inherited_reasoning in {"off", "low", "high", "max"}:
            policy["llm_reasoning_level"] = inherited_reasoning
            policy["llm_reasoning_source"] = reasoning_source or "inherited"
        metadata = {"entrypoint": "agent.turn", "orchestrator": "langgraph",
                    "input_envelope": envelope.model_dump(mode="json"),
                    "session_policy": {**policy,
                                       "allow_default_workspace": False,
                                       "allowed_paths": None if workspace else []},
                    "execution_workspace": workspace}
        if policy.get("llm_reasoning_level") in {"off", "low", "high", "max"}:
            metadata["llm_reasoning_level"] = policy["llm_reasoning_level"]
            metadata["llm_reasoning_source"] = policy.get("llm_reasoning_source") or ("inherited" if source else "session")
        metadata.update(self.budget_metadata(source.metadata) if source else {})
        return AgentRunRecord(
            run_id="agent_run_" + uuid4().hex, session_id=session_id, trace_id="agent_turn_" + uuid4().hex,
            status=AgentRunStatus.QUEUED, user_input=envelope.render(), created_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )

    def _freeze_delegate_policy(self, conn, context, run):
        delegates = getattr(self, "delegates", None)
        delegate = delegates.get_for_session(conn, context.session_id) if delegates is not None else None
        if delegate is None:
            return
        controller = conn.execute("SELECT llm_client_name,llm_model,reasoning_level,llm_answer_client_name,llm_answer_model FROM manager_controllers WHERE session_id=?",
                                  (context.session_id,)).fetchone()
        run.metadata["delegate_session_id"] = context.session_id
        run.metadata["session_policy"] = {
            "allow_default_workspace": False, "allow_fork": False,
            "allowed_tools": delegate["allowed_tools"],
            "allowed_paths": None if run.metadata.get("execution_workspace") else [],
            "llm_client_name": controller[0], "llm_model": controller[1],
            "llm_answer_client_name": run.metadata.get("session_policy", {}).get("llm_answer_client_name", controller[3]),
            "llm_answer_model": run.metadata.get("session_policy", {}).get("llm_answer_model", controller[4]),
            "llm_reasoning_level": run.metadata.get("llm_reasoning_level") or controller[2] or run.metadata.get("session_policy", {}).get("llm_reasoning_level"),
            "llm_reasoning_source": run.metadata.get("llm_reasoning_source") or ("inherited" if controller[2] else run.metadata.get("session_policy", {}).get("llm_reasoning_source")),
            "safety_review_mode": run.metadata["session_policy"].get("safety_review_mode"),
            "safety_review_policy": run.metadata["session_policy"].get("safety_review_policy", "standard"),
        }
        frozen_level = run.metadata["session_policy"].get("llm_reasoning_level")
        if frozen_level in {"off", "low", "high", "max"}:
            run.metadata["llm_reasoning_level"] = frozen_level
            run.metadata["llm_reasoning_source"] = run.metadata["session_policy"].get("llm_reasoning_source") or "inherited"

    @staticmethod
    def budget_metadata(*metadata):
        """Merge server-owned cumulative allowances without dropping older caps."""
        scopes, paths = {}, {}
        for item in metadata:
            for scope in item.get("work_budget_scopes", []):
                old = scopes.get(scope["scope_id"])
                scopes[scope["scope_id"]] = {key: min(v for v in (old.get(key), value) if v is not None)
                                            if old and key != "scope_id" and (old.get(key) is not None or value is not None) else value
                                            for key, value in scope.items()}
            for key, depth in item.get("manager_cause_paths", {}).items():
                paths[key] = max(depth, paths.get(key, 0))
        if len(scopes) > 64 or len(paths) > 64:
            raise ManagementConflictError("Task budget or causal ancestry exceeds the bounded management scope")
        return {"work_budget_scopes": list(scopes.values()), "manager_cause_paths": paths} if scopes or paths else {}

    def list_goals(self, context, *, limit=30, offset=0):
        self.caller(context)
        result = self.goals.list_goals(context.session_id, limit=limit, offset=offset)
        for goal in result["goals"]:
            objective = goal["objective"]
            goal["objective"] = objective[:500]
            goal["next_char"] = 500 if len(objective) > 500 else None
        return result

    def read_goal(self, context, *, goal_id, start_char=0):
        self.caller(context)
        if type(start_char) is not int or start_char < 0:
            raise ValueError("start_char must be non-negative")
        goal = self.goals.get_goal(context.session_id, goal_id)
        objective = goal["objective"]
        goal["objective"] = objective[start_char:start_char + 4000]
        goal["next_char"] = start_char + 4000 if start_char + 4000 < len(objective) else None
        return goal

    def read_events(self, context, *, after=0, limit=30):
        self.caller(context)
        return self.goals.list_events(context.session_id, after=after, limit=limit)

    @notify_after_commit
    def set_goal(self, context, *, command_id, objective=None, goal_id=None, expected_revision=None,
                 **changes):
        """Foreground changes commit now; autonomous proposals publish with the answer."""
        args = {**changes, **({"objective": objective} if objective is not None else {})}
        if goal_id is not None:
            args["expected_revision"] = expected_revision
        elif expected_revision is not None:
            raise ValueError("expected_revision requires goal_id")
        fingerprint_args = {"goal_id": goal_id, **args}
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self._command(conn, context, command_id, "set_goal", fingerprint_args)
            if prior:
                prior_execution = prior.get("execution_id")
                if prior_execution and prior_execution != run.metadata.get("manager_execution_id"):
                    state = conn.execute("SELECT e.status,r.status FROM manager_executions e JOIN agent_runs r ON r.run_id=e.run_id "
                                         "WHERE e.execution_id=?", (prior_execution,)).fetchone()
                    if state is None or state[0] in {"failed", "superseded"} or state[1] in {"failed", "cancelled", "timed_out"}:
                        raise ManagementConflictError("The prior goal proposal did not publish; read the current goal and use a new command_id")
                return prior
            creating = goal_id is None
            goal_id = goal_id or "goal_" + sha256(_json([context.session_id, command_id]).encode()).hexdigest()[:32]
            execution_id = run.metadata.get("manager_execution_id")
            if execution_id and conn.execute("SELECT 1 FROM manager_goal_proposals WHERE execution_id=? AND goal_id=?",
                                              (execution_id, goal_id)).fetchone():
                raise ManagementConflictError("Only one proposal per goal is allowed in a management execution")
            if execution_id:
                conn.execute("SAVEPOINT goal_proposal_validation")
            if creating:
                if objective is None:
                    raise ValueError("New goals require objective")
                result = self.goals.create_goal_in_transaction(conn, context.session_id, goal_id=goal_id, **args)
            else:
                result = self.goals.update_goal_in_transaction(conn, context.session_id, goal_id, **args)
            if execution_id:
                conn.execute("ROLLBACK TO goal_proposal_validation")
                conn.execute("RELEASE goal_proposal_validation")
                conn.execute("INSERT INTO manager_goal_proposals VALUES(?,?,?,?)",
                             (execution_id, command_id, goal_id, _json({"create": creating, "changes": args})))
            receipt = {"command_id": command_id, "action": "set_goal", "goal": result,
                       "status": "proposed" if execution_id else "applied",
                       "execution_id": execution_id}
            conn.execute("INSERT INTO management_commands(manager_session_id,command_id,fingerprint,action,receipt,created_at) "
                         "VALUES(?,?,?,'set_goal',?,?)", (context.session_id, command_id, fingerprint,
                                                       _json(receipt), datetime.now(UTC).isoformat()))
        return receipt

    def apply_goal_proposals(self, conn, execution_id, session_id):
        source = conn.execute("SELECT r.record_payload FROM agent_runs r JOIN manager_executions e ON r.run_id=e.run_id "
                              "WHERE e.execution_id=?", (execution_id,)).fetchone()
        paths = json.loads(source[0]).get("metadata", {}).get("manager_cause_paths", {})
        for row in conn.execute("SELECT goal_id,args FROM manager_goal_proposals WHERE execution_id=? ORDER BY rowid",
                                (execution_id,)).fetchall():
            args = json.loads(row["args"])
            if args["create"]:
                goal = self.goals.create_goal_in_transaction(conn, session_id, goal_id=row["goal_id"], **args["changes"])
            else:
                goal = self.goals.update_goal_in_transaction(conn, session_id, row["goal_id"], **args["changes"])
            if goal["next_wake_at"] is not None:
                conn.execute("INSERT INTO manager_timer_causes VALUES(?,?,?)",
                             (goal["goal_id"], goal["timer_generation"], _json(paths)))

    def prepare_task(self, context, *, command_id, purpose, title="管理任务", workspace_path=None,
                     create_workspace=True):
        """Freeze task scope before the transaction; never inherit the backend checkout."""
        caller = self.caller(context, mutate=True)
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 128:
            raise ValueError("command_id must contain 1..128 characters")
        workspace = None
        delegates = getattr(self, "delegates", None)
        with closing(self._conn()) as conn:
            delegate = delegates.get_for_session(conn, context.session_id) if delegates else None
            prior = conn.execute("SELECT receipt FROM management_commands WHERE manager_session_id=? AND command_id=?",
                                 (context.session_id, command_id)).fetchone()
            parent = conn.execute("SELECT default_execution_workspace FROM manager_controllers WHERE session_id=?",
                                  (context.session_id,)).fetchone()
            if prior and workspace_path is None:
                receipt = json.loads(prior["receipt"])
                original = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?",
                                        (receipt.get("run_id"),)).fetchone()
                metadata = json.loads(original["record_payload"]).get("metadata", {}) if original else {}
                if "execution_workspace" in metadata:
                    workspace = metadata["execution_workspace"]
                else:
                    target = conn.execute("SELECT metadata FROM agent_sessions WHERE session_id=?",
                                          (receipt.get("session_id"),)).fetchone()
                    workspace = json.loads(target["metadata"]).get("workspace") if target else None
        if delegate is not None:
            # A line's default directory is stable; each task may select a
            # different existing directory without changing its tool grant.
            workspace = self.workspace(workspace_path) if workspace_path is not None else delegate["workspace"]
        elif workspace_path is not None:
            workspace = self.workspace(workspace_path)
        elif not prior and create_workspace and (parent and parent["default_execution_workspace"] or self.default_task_parent):
            configured = json.loads(parent["default_execution_workspace"]) if parent and parent["default_execution_workspace"] else self.default_task_parent
            base = self.management.resolve_execution_workspace(configured)
            # Stable across concurrent retries, distinct for every accepted command/session.
            name = "task_" + sha256(_json([context.session_id, command_id]).encode()).hexdigest()[:24]
            directory = Path(base["backend_path"]) / name
            directory.mkdir(exist_ok=True)
            if directory.resolve().parent != Path(base["backend_path"]).resolve():
                raise ValueError("Task workspace must be a child of its configured parent")
            source_path = PureWindowsPath(base["path"]) if base["platform"] == "windows" else Path(base["path"])
            workspace = self.management.resolve_execution_workspace({"path": str(source_path / name),
                                                                     "platform": base["platform"]})
        return {"title": self.management._title(title),
                "envelope": self._envelope(context, caller, command_id, purpose), "workspace": workspace}

    @notify_after_commit
    def start_task(self, context, *, command_id, purpose, title="管理任务", workspace_path=None,
                   execution_target=None):
        if execution_target is not None:
            if workspace_path is not None:
                raise ValueError("Remote workspaces require an approved workspace_id, not workspace_path")
            remote = getattr(self, "remote_execution", None)
            if remote is None:
                raise ManagementConflictError("Remote execution is disabled; target was not changed")
            return remote.start_managed_task(context, command_id=command_id, purpose=purpose,
                                             title=title, execution_target=execution_target)
        prepared = self.prepare_task(context, command_id=command_id, purpose=purpose, title=title, workspace_path=workspace_path)
        with closing(self._conn()) as conn, conn:
            self.begin_task_transaction(conn)
            result, run = self.start_task_in_transaction(
                conn, context, command_id=command_id, purpose=purpose, prepared=prepared,
            )
            self.caller(context, mutate=True, conn=conn)
        if run is not None:
            self.run_manager.get_run(run.run_id)
        return result

    def start_task_in_transaction(self, conn, context, *, command_id, purpose, title="管理任务",
                                  workspace_path=None, command_action="start_task", command_context=None,
                                  before_create=None, prepared=None, defer_dispatch=False):
        """Accept a task inside a domain transaction; restore run only after commit.

        Server-owned context and callback let a work service fence dependencies
        and bind its business record without exposing a partially accepted task.
        """
        if not conn.in_transaction:
            raise ValueError("Task creation requires a caller-owned transaction")
        caller = self.caller(context, mutate=True, conn=conn)
        if prepared is None:
            raise ValueError("Call prepare_task before opening the write transaction")
        title, workspace = prepared["title"], prepared["workspace"]
        args = {"purpose": purpose, "title": title, "workspace": workspace}
        if command_context is not None:
            args["work"] = command_context
        fingerprint, prior = self._command(conn, context, command_id, command_action, args)
        if prior:
            return prior, None
        self.require_durable_execution()
        if before_create is not None:
            before_create(conn)
        envelope = prepared["envelope"]
        sid = "session_" + uuid4().hex
        now = datetime.now(UTC).isoformat()
        conn.execute("INSERT INTO agent_sessions(session_id,title,status,metadata,kind,created_at,updated_at,"
                     "creation_source,creator_session_id) VALUES(?,?,'active',?,'ordinary',?,?,'assistant',?)",
                     (sid, title, _json({"workspace": workspace} if workspace else {}), now, now, context.session_id))
        run = self._new_run(sid, envelope, caller, workspace=workspace,
                            safety_review_mode=context.safety_review_mode,
                            reasoning_level=getattr(context, "llm_reasoning_level", None),
                            reasoning_source=getattr(context, "llm_reasoning_source", None))
        self._freeze_delegate_policy(conn, context, run)
        self.run_store.save_run_in_transaction(conn, run.model_dump(mode="json"), updated_at=now)
        if not defer_dispatch:
            self.enqueue(run, conn=conn)
        result = self._receipt(conn, context, command_id, command_action, fingerprint, run)
        return result, run

    def list_execution_targets(self, context):
        self.caller(context)
        remote = getattr(self, "remote_execution", None)
        local = {"name": "Cloud hub" if remote is not None else "Current backend",
                 "execution_target": None, "default": True,
                 "available": bool(self.durable_execution()), "workspace_required": False,
                 "execution_role": "backend_executor",
                 "responsibilities": ["backend_services", "tasks_without_terminal_files", "backend_workspace_tasks"]}
        roles = {"supervisor": "Plans, dispatches, verifies and reports; can create execution sessions on this same backend.",
                 "delegate": "Manages an independent work line; its session role does not select a machine.",
                 "execution": "Runs tools on the selected backend or approved terminal workspace; ordinary local services remain independent."}
        if remote is None:
            return {"items": [], "enabled": False, "local_execution": local, "session_roles": roles}
        return {"enabled": True, **remote.execution_targets(), "local_execution": local, "session_roles": roles}

    @notify_after_commit
    def send_instruction(self, context, *, command_id, session_id, expected_run_id, purpose):
        caller = self.caller(context, mutate=True)
        target_session = self.sessions.get_session_or_none(session_id=session_id)
        if target_session is not None and target_session.metadata.get("remote_execution"):
            remote = getattr(self, "remote_execution", None)
            if remote is None:
                raise ManagementConflictError("Remote task authority is unavailable")
            return remote.send_instruction(context, command_id=command_id, session_id=session_id,
                                           expected_run_id=expected_run_id, purpose=purpose)
        envelope = self._envelope(context, caller, command_id, purpose)
        args = {"session_id": session_id, "expected_run_id": expected_run_id, "purpose": purpose}
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self._command(conn, context, command_id, "send_instruction", args)
            if prior:
                return prior
            self.require_durable_execution()
            target = self._target(conn, session_id, manager_session_id=context.session_id)
            latest = conn.execute("SELECT record_payload FROM agent_runs WHERE session_id=? "
                                  "AND json_extract(record_payload,'$.parent_run_id') IS NULL "
                                  "ORDER BY created_at DESC,run_id DESC LIMIT 1", (session_id,)).fetchone()
            old = json.loads(latest[0]) if latest else None
            if (old["run_id"] if old else None) != expected_run_id:
                raise ManagedTaskRunMismatchError(expected_run_id=expected_run_id,
                                                  current_run=self._run_view(old) if old else None)
            if old and old["status"] not in TERMINAL | {"paused"}:
                raise ManagementConflictError("Pause the target and wait for run_paused before replacing it")
            if old and old["status"] == "paused" and old.get("metadata", {}).get("paused_from_status") in {"waiting_confirmation", "waiting_user"}:
                raise ManagementConflictError("Resolve the original confirmation or user question before sending instructions")
            if not old or old["status"] in TERMINAL:
                workspace = (old or {}).get("metadata", {}).get("execution_workspace",
                    json.loads(target["metadata"]).get("workspace"))
                run = self._new_run(session_id, envelope, caller, workspace=workspace,
                                    safety_review_mode=context.safety_review_mode,
                                    reasoning_level=getattr(context, "llm_reasoning_level", None),
                                    reasoning_source=getattr(context, "llm_reasoning_source", None))
                self._freeze_delegate_policy(conn, context, run)
                # Later user/model configuration cannot widen accepted tools.
                if old and old.get("metadata", {}).get("delegate_session_id"):
                    preference = run.metadata["session_policy"].get("safety_review_mode")
                    review_policy = run.metadata["session_policy"]["safety_review_policy"]
                    run.metadata["session_policy"] = {**old["metadata"]["session_policy"],
                                                      "safety_review_mode": preference,
                                                      "safety_review_policy": review_policy}
                if old:
                    run.metadata.update(self.budget_metadata(old.get("metadata", {}), caller.metadata))
                self.run_store.save_run_in_transaction(conn, run.model_dump(mode="json"), updated_at=run.created_at)
                self.enqueue(run, conn=conn)
                result = self._receipt(conn, context, command_id, "send_instruction", fingerprint, run)
            else:
                run = None
        if run is None:
            def created(db, replacement):
                self.caller(context, mutate=True, conn=db)
                self._target(db, session_id, manager_session_id=context.session_id)
                _, duplicate = self._command(db, context, command_id, "send_instruction", args)
                if duplicate:
                    raise ManagementConflictError("Command concurrently completed")
                self.enqueue(replacement, conn=db)
                self._receipt(db, context, command_id, "send_instruction", fingerprint, replacement)
            run = self.replace(expected_run_id, user_input=envelope.render(),
                               command_id=context.session_id + ":" + command_id,
                               request_fingerprint=fingerprint,
                               metadata_updates={"input_envelope": envelope.model_dump(mode="json"),
                                                 **self.budget_metadata(old.get("metadata", {}), caller.metadata)},
                               on_created=created)
            # Read the exact committed receipt, including idempotent low-level
            # replacement replay. Never reconstruct it from pre-replace status.
            with closing(self._conn()) as conn:
                _, result = self._command(conn, context, command_id, "send_instruction", args)
            if result is None:
                raise ManagementConflictError("Replacement receipt is unavailable")
        else:
            self.run_manager.get_run(run.run_id)
        return result

    @notify_after_commit
    def control_run(self, context, *, command_id, run_id, action):
        self.caller(context, mutate=True)
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("action must be pause, resume or cancel")
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self._command(conn, context, command_id, action, {"run_id": run_id})
            if prior:
                return prior
            record = self.run_store.load_run(run_id)
            if record is None:
                raise KeyError(run_id)
            self._target(conn, record["session_id"], manager_session_id=context.session_id)
            if record.get("parent_run_id"):
                raise ManagementConflictError("Control the task root, not its child")
            remote_marker = record.get("metadata", {}).get("remote_execution")
            if remote_marker and action != "cancel":
                remote = getattr(self, "remote_execution", None)
                if remote is None:
                    raise ManagementConflictError("Remote task authority is unavailable")
                if action == "resume" and context.run_id:
                    caller_row = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (context.run_id,)).fetchone()
                    if caller_row and self.budget_metadata(json.loads(caller_row[0]).get("metadata", {})):
                        raise ManagementConflictError("Remote resume cannot transfer cumulative background budgets")
                # Exit the local write transaction before the remote command service
                # performs its own durable admission. Never dispatch a hub shadow.
                conn.commit()
                remote.control(remote_marker["task_id"], command_id=context.session_id + ":" + command_id, action=action)
                conn.execute("BEGIN IMMEDIATE")
                self.caller(context, mutate=True, conn=conn)
                _, prior = self._command(conn, context, command_id, action, {"run_id": run_id})
                if prior:
                    return prior
                run = AgentRunRecord.model_validate(record)
                return self._receipt(conn, context, command_id, action, fingerprint, run)
            if action == "resume":
                self.require_durable_execution()
                if record["status"] != "paused":
                    raise ManagementConflictError("Only paused runs can resume; use original confirmation/answer controls for waiting runs")
                if context.run_id:
                    caller_row = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (context.run_id,)).fetchone()
                    caller_metadata = json.loads(caller_row[0])["metadata"]
                    required = self.budget_metadata(caller_metadata)
                    existing = record.get("metadata", {})
                    if required and self.budget_metadata(existing, caller_metadata) != self.budget_metadata(existing):
                        raise ManagementConflictError("Autonomous resume requires matching existing budgets and causal scope; use a paused replacement to attach the current allowance")
                run = AgentRunRecord.model_validate(record)
                self.enqueue(run, conn=conn, mode="resume", command_id=context.session_id + ":" + command_id)
                return self._receipt(conn, context, command_id, action, fingerprint, run)
        # These controls are idempotent against the explicit run ID. They never
        # retarget a later user run, and do not execute a model in this service.
        self.caller(context, mutate=True)
        remote_marker = record.get("metadata", {}).get("remote_execution")
        if remote_marker:
            remote = getattr(self, "remote_execution", None)
            if remote is None:
                raise ManagementConflictError("Remote task authority is unavailable")
            remote.cancel(remote_marker["task_id"])
            run = self.run_manager.get_run(run_id)
        elif action == "pause":
            run = self.interrupt(run_id)
        else:
            run = self.run_manager.cancel_run(run_id, reason="Management command")
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self._command(conn, context, command_id, action, {"run_id": run_id})
            return prior or self._receipt(conn, context, command_id, action, fingerprint, run)
