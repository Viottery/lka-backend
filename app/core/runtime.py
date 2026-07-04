"""Runtime orchestration for the current backend scaffold."""

from __future__ import annotations

import json
import os
import sqlite3
from hashlib import sha1
from pathlib import Path

from app.api.schemas import (
    CapabilityItem,
    TaskPlanResponse,
    TaskRecordResponse,
    TaskRunResponse,
    TraceRecordResponse,
    WorkspaceIndexResponse,
)
from app.core.config import Settings
from app.storage.db import connect, get_db_path, init_db


def _stable_id(prefix: str, text: str) -> str:
    digest = sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


class LocalKnowledgeAgentRuntime:
    """Rule-based runtime used by the current API layer."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = get_db_path(settings.data_dir)
        init_db(self.db_path)

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def health(self) -> dict[str, str]:
        """Return a small health payload used by the `/health` route."""

        return {
            "status": "ok",
            "version": self.settings.version,
            "service": self.settings.app_name,
        }

    def _count_workspace_items(self, workspace_path: Path) -> tuple[int, int]:
        indexed_files = 0
        indexed_chunks = 0
        for _root, _, files in os.walk(workspace_path):
            indexed_files += len(files)
            indexed_chunks += len(files) * 4
        return indexed_files, indexed_chunks

    def _upsert_workspace(
        self,
        workspace_id: str,
        workspace: str,
        source_frontend: str | None,
        indexed_files: int,
        indexed_chunks: int,
    ) -> None:
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO workspaces(workspace_id, workspace_path, source_frontend, status, indexed_files, indexed_chunks)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    workspace_path=excluded.workspace_path,
                    source_frontend=excluded.source_frontend,
                    status=excluded.status,
                    indexed_files=excluded.indexed_files,
                    indexed_chunks=excluded.indexed_chunks
                """,
                (workspace_id, workspace, source_frontend, "completed", indexed_files, indexed_chunks),
            )
            conn.commit()
        finally:
            conn.close()

    def index_workspace(self, workspace: str, source_frontend: str | None = None, options: dict | None = None) -> WorkspaceIndexResponse:
        """Record a lightweight workspace index summary.

        The current implementation counts files only. The `options` payload is
        accepted for compatibility with the API contract, but is not yet used.
        """

        workspace_path = Path(workspace)
        workspace_id = _stable_id("ws", workspace)
        indexed_files = 0
        indexed_chunks = 0
        if workspace_path.exists():
            indexed_files, indexed_chunks = self._count_workspace_items(workspace_path)
        self._upsert_workspace(workspace_id, workspace, source_frontend, indexed_files, indexed_chunks)
        return WorkspaceIndexResponse(
            workspace_id=workspace_id,
            status="completed",
            indexed_files=indexed_files,
            indexed_chunks=indexed_chunks,
        )

    def _classify_task(self, task: str) -> tuple[str, list[str], list[str], str]:
        text = task.lower()
        intent = "general_assistant"
        plan = ["analyze request", "retrieve context", "prepare response"]
        capabilities = ["search_local_knowledge"]
        risk = "low"

        if any(keyword in text for keyword in ["整理", "organize", "sort"]):
            intent = "file_organization"
            plan = ["scan workspace", "retrieve relevant context", "summarize folder", "generate organization suggestions"]
            capabilities = ["summarize_folder", "extract_tasks", "organize_files"]
        elif any(keyword in text for keyword in ["todo", "待办", "任务"]):
            intent = "task_extraction"
            plan = ["scan workspace", "extract TODO items", "group findings", "summarize action items"]
            capabilities = ["search_local_knowledge", "extract_tasks"]

        if any(keyword in text for keyword in ["删除", "delete", "remove", "modify"]):
            risk = "high"

        return intent, plan, capabilities, risk

    def plan_task(self, task: str, workspace: str | None = None, frontend: str | None = None) -> TaskPlanResponse:
        _ = workspace, frontend
        intent, plan, capabilities, risk = self._classify_task(task)

        return TaskPlanResponse(
            intent=intent,
            plan=plan,
            suggested_capabilities=capabilities,
            risk=risk,
        )

    def run_task(self, task: str, workspace: str | None = None, frontend: str | None = None, mode: str = "interactive") -> TaskRunResponse:
        """Create a task record and a trace record for the submitted request."""

        plan = self.plan_task(task, workspace=workspace, frontend=frontend)
        task_id = _stable_id("task", f"{task}|{workspace or ''}|{frontend or ''}|{mode}")
        trace_id = _stable_id("trace", f"{task_id}|{plan.intent}")
        summary = "完成任务规划与基础执行骨架。"
        requires_user_action = plan.risk == "high" and mode == "interactive"

        self._upsert_task(task_id, task, workspace, frontend, summary, trace_id, requires_user_action)
        self._upsert_trace(trace_id, task, plan.intent, plan.plan, plan.suggested_capabilities)

        return TaskRunResponse(
            task_id=task_id,
            status="waiting_for_confirmation" if requires_user_action else "completed",
            summary=summary,
            trace_id=trace_id,
            requires_user_action=requires_user_action,
            artifacts=[],
        )

    def _upsert_task(
        self,
        task_id: str,
        task: str,
        workspace: str | None,
        frontend: str | None,
        summary: str,
        trace_id: str,
        requires_user_action: bool,
    ) -> None:
        status = "waiting_for_confirmation" if requires_user_action else "completed"
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO tasks(task_id, task_text, workspace_path, frontend, status, summary, trace_id)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    task_text=excluded.task_text,
                    workspace_path=excluded.workspace_path,
                    frontend=excluded.frontend,
                    status=excluded.status,
                    summary=excluded.summary,
                    trace_id=excluded.trace_id
                """,
                (task_id, task, workspace, frontend, status, summary, trace_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _upsert_trace(
        self,
        trace_id: str,
        user_goal: str,
        intent: str,
        plan: list[str],
        capabilities: list[str],
    ) -> None:
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO traces(trace_id, user_goal, intent, plan_json, context_summary, capabilities_json, verification_json, success)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    user_goal=excluded.user_goal,
                    intent=excluded.intent,
                    plan_json=excluded.plan_json,
                    context_summary=excluded.context_summary,
                    capabilities_json=excluded.capabilities_json,
                    verification_json=excluded.verification_json,
                    success=excluded.success
                """,
                (
                    trace_id,
                    user_goal,
                    intent,
                    json.dumps(plan, ensure_ascii=False),
                    "基础上下文已构造。",
                    json.dumps(capabilities, ensure_ascii=False),
                    json.dumps({"status": "not_run"}, ensure_ascii=False),
                    1,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_task(self, task_id: str) -> TaskRecordResponse | None:
        """Fetch a previously persisted task record."""

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT task_id, status, summary, trace_id FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return TaskRecordResponse(**dict(row))

    def list_capabilities(self) -> list[CapabilityItem]:
        """Return the currently advertised capability catalog."""

        return [
            CapabilityItem(name="summarize_folder", type="native_skill", risk="low", requires_confirmation=False),
            CapabilityItem(name="extract_tasks", type="native_skill", risk="low", requires_confirmation=False),
            CapabilityItem(name="organize_files", type="native_skill", risk="medium", requires_confirmation=True),
            CapabilityItem(name="claude_code", type="expert_tool", risk="medium", requires_confirmation=True),
            CapabilityItem(name="codex", type="expert_tool", risk="medium", requires_confirmation=True),
        ]

    def list_traces(self) -> list[dict[str, str]]:
        """Return a compact trace summary list for the `/traces` endpoint."""

        conn = self._conn()
        try:
            rows = conn.execute("SELECT trace_id, user_goal, intent FROM traces ORDER BY trace_id DESC").fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def get_trace(self, trace_id: str) -> TraceRecordResponse | None:
        """Fetch a full trace record."""

        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT trace_id, user_goal, intent, plan_json, context_summary, capabilities_json, verification_json, success
                FROM traces
                WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return TraceRecordResponse(
            trace_id=row["trace_id"],
            user_goal=row["user_goal"],
            intent=row["intent"],
            plan=json.loads(row["plan_json"]),
            context_summary=row["context_summary"],
            capabilities_used=json.loads(row["capabilities_json"]),
            verification_result=json.loads(row["verification_json"]),
            success=bool(row["success"]),
        )

    def confirm(self, confirmation_id: str, decision: str) -> dict[str, str]:
        """Persist a confirmation decision and mark it resolved."""

        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO confirmations(confirmation_id, decision, status)
                VALUES(?, ?, ?)
                ON CONFLICT(confirmation_id) DO UPDATE SET
                    decision=excluded.decision,
                    status=excluded.status
                """,
                (confirmation_id, decision, "resolved"),
            )
            conn.commit()
        finally:
            conn.close()
        return {"confirmation_id": confirmation_id, "decision": decision, "status": "resolved"}
