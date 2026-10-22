"""Pydantic models for one Agent turn: events, the final result and the resumable working set.

Split out of agent_turn.py so callers can import the data types without the loop.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.llm.models import IMAGE_INPUT_TOKEN_RESERVATION, LLMAudioBlock, LLMImageBlock
from app.core.task_completion import TaskCompletionState
from app.core.task_progress import TaskProgressState


class AgentInputContent(BaseModel):
    """Validated source previews and visual evidence supplied by the runtime."""

    images: list[LLMImageBlock] = Field(default_factory=list)
    audio: list[LLMAudioBlock] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def token_reservation(self) -> int:
        return IMAGE_INPUT_TOKEN_RESERVATION * len(self.images) + sum(block.token_reservation for block in self.audio)


class AgentTurnToolEvent(BaseModel):
    tool_name: str
    selected_at: str
    completed_at: str
    input: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    feedback: dict[str, Any] = Field(default_factory=dict)
    cache_metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTurnLLMEvent(BaseModel):
    llm_call_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    session_id: str | None = None
    stage: str
    client_name: str | None = None
    provider: str
    model: str | None = None
    response_mode: str | None = None
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    duration_ms: int | None = None
    system_prompt: str
    user_prompt: str
    output: str
    attempt: int = 1
    http_status: int | None = None
    provider_request_id: str | None = None
    provider_error_type: str | None = None
    provider_error_code: str | None = None
    provider_error_param: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    is_retriable: bool | None = None
    finish_reason: str | None = None
    input_token_count: int | None = None
    output_token_count: int | None = None
    total_token_count: int | None = None
    content_length: int | None = None
    prompt_summary: str | None = None
    partial: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    audit_record: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = None
    status_code: int | None = None
    retry_after: str | None = None
    error: str | None = None


class AgentTurnDecisionEvent(BaseModel):
    step_index: int
    decided_at: str
    source: str
    action: str
    selected_package: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    answer: str | None = None
    reason: str | None = None
    assistant_message: str | None = None
    operation: dict[str, Any] = Field(default_factory=dict)
    raw_output: str | None = None


class AgentTurnProgressEvent(BaseModel):
    event_index: int
    created_at: str
    type: str
    message: str
    stage: str | None = None
    tool_name: str | None = None
    package_name: str | None = None
    status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTurnVerificationWarning(BaseModel):
    code: str
    message: str
    severity: str = "warning"
    evidence: dict[str, Any] = Field(default_factory=dict)


class AgentTurnResult(BaseModel):
    run_id: str
    session_id: str
    trace_id: str
    answer: str
    user_visible: bool = True
    notification: dict[str, Any] | None = None
    task_outcome: Literal["unknown", "incomplete"] = "unknown"
    stop_reason: str | None = None
    selected_package: str | None = None
    initial_package: str | None = None
    expanded_packages: list[str] = Field(default_factory=list)
    used_packages: list[str] = Field(default_factory=list)
    active_package: str | None = None
    package_catalog: list[dict[str, Any]] = Field(default_factory=list)
    session_context_window: dict[str, Any] = Field(default_factory=dict)
    expanded_tools: list[dict[str, Any]] = Field(default_factory=list)
    decision_events: list[AgentTurnDecisionEvent] = Field(default_factory=list)
    tool_events: list[AgentTurnToolEvent] = Field(default_factory=list)
    progress_events: list[AgentTurnProgressEvent] = Field(default_factory=list)
    verification_warnings: list[AgentTurnVerificationWarning] = Field(default_factory=list)
    llm_events: list[AgentTurnLLMEvent] = Field(default_factory=list)
    log_path: str | None = None


class AgentTurnWorkingSet(BaseModel):
    """Durable-reference-friendly state shared by external ReAct graph nodes.

    Large prompt inputs, LLM audit records and raw tool output remain in artifacts
    and the local run log.  This model intentionally carries only the data needed
    to resume the next control-flow decision.
    """

    run_id: str
    session_id: str
    trace_id: str
    user_input: str
    context_artifact_ref: dict[str, Any] = Field(default_factory=dict)
    route: dict[str, Any] = Field(default_factory=dict)
    initial_package: str | None = None
    active_package: str | None = None
    expanded_packages: list[str] = Field(default_factory=list)
    used_packages: list[str] = Field(default_factory=list)
    step_index: int = 0
    pending_decision: dict[str, Any] = Field(default_factory=dict)
    pending_invocation_id: str | None = None
    pending_tool_name: str | None = None
    pending_tool_input: dict[str, Any] = Field(default_factory=dict)
    pending_review_id: str | None = None
    observation_artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    terminal_answer: str | None = None
    terminal_reason: str | None = None
    completion_state: TaskCompletionState = Field(default_factory=TaskCompletionState)
    task_working_state: TaskProgressState = Field(default_factory=TaskProgressState)
