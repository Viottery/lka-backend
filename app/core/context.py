"""Context objects for the runtime debug infrastructure."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RelatedFile(BaseModel):
    path: str
    role: str
    reason: str
    confidence: float = 0.5


class RelatedSnippet(BaseModel):
    source: str
    text: str
    reason: str


class SuggestedTool(BaseModel):
    name: str
    type: str
    reason: str


class VerificationStep(BaseModel):
    method: str
    target: str
    expected: str


class BaseContext(BaseModel):
    context_id: str
    context_type: str
    lifecycle_status: str
    session_id: str
    workspace_id: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    project_constraints: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    suggested_tools: list[SuggestedTool] = Field(default_factory=list)
    verification_plan: list[VerificationStep] = Field(default_factory=list)
    visibility: dict[str, bool] = Field(default_factory=dict)
    token_budget: int | None = None


class SessionContext(BaseContext):
    context_type: str = "session"
    workspace: str | None = None
    goal_summary: str
    user_messages: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)


class TaskContext(BaseContext):
    context_type: str = "task"
    goal_summary: str
    related_files: list[RelatedFile] = Field(default_factory=list)
    related_snippets: list[RelatedSnippet] = Field(default_factory=list)
    reasoning_summary: str


class ContextAssembler:
    """Build debug contexts without depending on a full planning stack."""

    def build_session_context(
        self,
        *,
        context_id: str,
        session_id: str,
        workspace: str | None,
        workspace_id: str | None,
        user_input: str,
    ) -> SessionContext:
        return SessionContext(
            context_id=context_id,
            lifecycle_status="gathering",
            session_id=session_id,
            workspace=workspace,
            workspace_id=workspace_id,
            goal_summary=user_input.strip(),
            user_messages=[user_input],
            source_refs=["user_input", "runtime_debug_request"],
            project_constraints=[
                "Use local-only debug infrastructure for this stage.",
                "Do not call a real LLM provider during Runtime Debug Infrastructure.",
                "Do not modify user files from the debug runtime path.",
            ],
            risk_notes=[
                "Debug loop is read-only and uses mock/local providers.",
            ],
            suggested_tools=[
                SuggestedTool(
                    name="search_local_knowledge",
                    type="retrieval_provider",
                    reason="Collect local workspace context before planning or execution.",
                )
            ],
            verification_plan=[
                VerificationStep(
                    method="api_smoke_test",
                    target="POST /runtime/debug",
                    expected=(
                        "Return session context, task context, events, provider outputs, and trace."
                    ),
                )
            ],
            visibility={"llm": True, "tools": True, "trace": True},
        )

    def derive_task_context(self, *, context_id: str, session: SessionContext) -> TaskContext:
        return TaskContext(
            context_id=context_id,
            lifecycle_status="ready_for_planning",
            session_id=session.session_id,
            workspace_id=session.workspace_id,
            goal_summary=session.goal_summary,
            source_refs=[*session.source_refs, session.context_id],
            project_constraints=session.project_constraints,
            risk_notes=session.risk_notes,
            suggested_tools=session.suggested_tools,
            verification_plan=session.verification_plan,
            visibility=session.visibility,
            reasoning_summary=(
                "The task context is derived from the session goal and debug constraints; "
                "retrieval results can enrich related files and snippets before planning."
            ),
        )

    def enrich_task_context(
        self,
        *,
        task_context: TaskContext,
        related_files: list[RelatedFile],
        related_snippets: list[RelatedSnippet],
        source_ref: str,
    ) -> TaskContext:
        return task_context.model_copy(
            update={
                "related_files": related_files,
                "related_snippets": related_snippets,
                "source_refs": [*task_context.source_refs, source_ref],
                "reasoning_summary": (
                    "The task context combines the session goal, project constraints, "
                    "mock/local retrieval results, suggested tools, and verification hints."
                ),
            }
        )
