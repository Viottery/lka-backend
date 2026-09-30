from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.agent_runs import AgentRunEvent, AgentRunRecord, AgentRunStatus
from app.core.agent_turn import (
    AgentTurnLLMEvent,
    AgentTurnProgressEvent,
    AgentTurnResult,
    AgentTurnToolEvent,
    AgentTurnVerificationWarning,
)
from app.core.context import SessionContext, TaskContext
from app.core.events import EventRecord
from app.core.llm import LLMResponse, LLMResponseMode
from app.core.retrieval import RetrievalResult
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewRecord
from app.core.sessions import (
    AgentSession,
    AgentSessionDetail,
    AgentSessionList,
    AgentSessionMessage,
    SessionRole,
    SessionWorkspace,
)
from app.core.tools import ToolInvocation, ToolResult
from app.core.tracing import TraceRecord
from app.domains.knowledge import (
    KnowledgeChunkLoadResult,
    KnowledgeDocumentInput,
    KnowledgeDocumentRecord,
    KnowledgeImportResult,
    KnowledgeSearchResult,
    KnowledgeSemanticSyncResult,
)
from app.domains.mail import (
    MailAccountInput,
    MailAttachmentInput,
    MailImportResult,
    MailMatter,
    MailMatterList,
    MailMessageInput,
    MailSearchResult,
)
from app.domains.mail_knowledge import MailKnowledgeMirrorResult
from app.domains.matters import (
    MatterCreateInput,
    MatterList,
    MatterRecord,
    MatterSearchResult,
    MatterSourceLinkInput,
    MatterUpdateInput,
)
from app.integrations.outlook import (
    OutlookAuthCompleteResult,
    OutlookAuthStartResult,
    OutlookSyncResult,
)


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    service: str = "local-knowledge-agent-os"


class WorkspaceIndexRequest(BaseModel):
    workspace: str
    source_frontend: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class WorkspaceIndexResponse(BaseModel):
    workspace_id: str
    status: str
    indexed_files: int
    indexed_chunks: int
    knowledge_errors: list[str] = Field(default_factory=list)


class CapabilityItem(BaseModel):
    name: str
    type: str
    risk: str
    requires_confirmation: bool
    read_only: bool | None = None


class CapabilityListResponse(BaseModel):
    capabilities: list[CapabilityItem]


class RuntimeDebugRequest(BaseModel):
    session_id: str
    workspace: str | None = None
    user_input: str


class RuntimeDebugResponse(BaseModel):
    trace_id: str
    session_context: SessionContext
    task_context: TaskContext
    events: list[EventRecord]
    retrieval_result: RetrievalResult
    tool_invocation: ToolInvocation
    tool_result: ToolResult
    llm_response: LLMResponse
    trace: TraceRecord


class AgentTurnLLMOptions(BaseModel):
    client_name: str | None = None
    model: str | None = None
    response_mode: LLMResponseMode = LLMResponseMode.TEXT


class AgentTurnRequest(BaseModel):
    session_id: str | None = None
    user_input: str
    llm: AgentTurnLLMOptions | None = None
    safety_review_mode: SafetyReviewMode | None = None


class AgentTurnLLMEventSummary(BaseModel):
    """Public, prompt-free audit summary for one LLM invocation."""

    llm_call_id: str | None = None
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
    attempt: int = 1
    http_status: int | None = None
    provider_request_id: str | None = None
    provider_error_type: str | None = None
    provider_error_code: str | None = None
    error_category: str | None = None
    is_retriable: bool | None = None
    finish_reason: str | None = None
    input_token_count: int | None = None
    output_token_count: int | None = None
    total_token_count: int | None = None
    content_length: int | None = None
    partial: bool = False

    @classmethod
    def from_event(cls, event: AgentTurnLLMEvent) -> AgentTurnLLMEventSummary:
        return cls(
            **event.model_dump(
                include=set(cls.model_fields),
                mode="python",
            )
        )


class AgentTurnToolEventSummary(BaseModel):
    tool_name: str
    selected_at: str
    completed_at: str
    status: str | None = None

    @classmethod
    def from_event(cls, event: AgentTurnToolEvent) -> AgentTurnToolEventSummary:
        status = event.result.get("status") if isinstance(event.result, dict) else None
        return cls(
            tool_name=event.tool_name,
            selected_at=event.selected_at,
            completed_at=event.completed_at,
            status=status if isinstance(status, str) else None,
        )


class AgentTurnProgressEventSummary(BaseModel):
    event_index: int
    created_at: str
    type: str
    message: str
    stage: str | None = None
    tool_name: str | None = None
    package_name: str | None = None
    status: str | None = None

    @classmethod
    def from_event(
        cls, event: AgentTurnProgressEvent
    ) -> AgentTurnProgressEventSummary:
        return cls(**event.model_dump(include=set(cls.model_fields), mode="python"))


class AgentTurnVerificationWarningSummary(BaseModel):
    code: str
    message: str
    severity: str

    @classmethod
    def from_warning(
        cls, warning: AgentTurnVerificationWarning
    ) -> AgentTurnVerificationWarningSummary:
        return cls(**warning.model_dump(include=set(cls.model_fields), mode="python"))


class AgentTurnResponse(BaseModel):
    """Public Agent turn result.

    Full prompts, session context and raw tool output are local audit data.  They
    remain in ``AgentTurnResult`` and the local run log, but must never cross
    the normal HTTP response boundary.
    """

    run_id: str
    session_id: str
    trace_id: str
    answer: str
    selected_package: str | None = None
    initial_package: str | None = None
    expanded_packages: list[str] = Field(default_factory=list)
    used_packages: list[str] = Field(default_factory=list)
    active_package: str | None = None
    tool_events: list[AgentTurnToolEventSummary] = Field(default_factory=list)
    progress_events: list[AgentTurnProgressEventSummary] = Field(default_factory=list)
    verification_warnings: list[AgentTurnVerificationWarningSummary] = Field(default_factory=list)
    llm_events: list[AgentTurnLLMEventSummary] = Field(default_factory=list)

    @classmethod
    def from_result(cls, result: AgentTurnResult) -> AgentTurnResponse:
        return cls(
            run_id=result.run_id,
            session_id=result.session_id,
            trace_id=result.trace_id,
            answer=result.answer,
            selected_package=result.selected_package,
            initial_package=result.initial_package,
            expanded_packages=result.expanded_packages,
            used_packages=result.used_packages,
            active_package=result.active_package,
            tool_events=[AgentTurnToolEventSummary.from_event(event) for event in result.tool_events],
            progress_events=[
                AgentTurnProgressEventSummary.from_event(event)
                for event in result.progress_events
            ],
            verification_warnings=[
                AgentTurnVerificationWarningSummary.from_warning(warning)
                for warning in result.verification_warnings
            ],
            llm_events=[AgentTurnLLMEventSummary.from_event(event) for event in result.llm_events],
        )


class PendingAgentQuestionResponse(BaseModel):
    question_id: str
    patch_id: str | None = None
    question: str = Field(max_length=4000)
    asked_at: str

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> PendingAgentQuestionResponse | None:
        value = metadata.get("pending_user_question")
        if not isinstance(value, dict):
            return None
        question_id = value.get("question_id")
        question = value.get("question")
        asked_at = value.get("asked_at")
        if not all(isinstance(item, str) and item for item in (question_id, question, asked_at)):
            return None
        patch_id = value.get("patch_id")
        return cls(
            question_id=question_id,
            patch_id=patch_id if isinstance(patch_id, str) else None,
            question=question,
            asked_at=asked_at,
        )


class AgentRunResponse(BaseModel):
    run_id: str
    session_id: str
    trace_id: str
    parent_run_id: str | None = None
    status: AgentRunStatus
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    cancelled_at: str | None = None
    waiting_since: str | None = None
    error_type: str | None = None
    error: str | None = None
    has_result: bool = False
    pending_user_question: PendingAgentQuestionResponse | None = None

    @classmethod
    def from_record(cls, record: AgentRunRecord) -> AgentRunResponse:
        payload = record.model_dump(include=set(cls.model_fields) - {"has_result"}, mode="python")
        return cls(
            **payload,
            has_result=record.result_snapshot is not None,
            pending_user_question=(
                PendingAgentQuestionResponse.from_metadata(record.metadata)
                if record.status == AgentRunStatus.WAITING_USER
                and not record.metadata.get("waiting_child_user_run_ids")
                and not record.metadata.get("pending_user_answer_command_id")
                else None
            ),
        )


class AgentRunEventsResponse(BaseModel):
    run_id: str
    events: list[AgentRunEvent] = Field(default_factory=list)


class AgentTaskResultSnapshot(BaseModel):
    """Safe current result summary for one child attempt."""

    result_id: str
    child_run_id: str
    plan_id: str
    step_id: str
    snapshot_id: str
    status: str
    summary: str
    artifact_refs: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    verification: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    missing_requirements: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    completed_at: str


class AgentChildRunSnapshot(BaseModel):
    run_id: str
    parent_run_id: str
    plan_id: str | None = None
    step_id: str | None = None
    attempt: int | None = None
    status: AgentRunStatus
    depth: int
    pending_user_question: PendingAgentQuestionResponse | None = None
    step_status: str | None = None
    step: dict[str, Any] | None = None
    has_result: bool = False
    result: AgentTaskResultSnapshot | None = None
    children: list[AgentChildRunSnapshot] = Field(default_factory=list)


class AgentRunSnapshotResponse(BaseModel):
    run: AgentRunResponse
    plan: dict[str, Any] | None = None
    children: list[AgentChildRunSnapshot] = Field(default_factory=list)


class SafetyReviewResponse(BaseModel):
    """Public safety-review summary.

    Raw tool input and reviewer LLM output are local audit material.  They stay
    in the durable review record and run log, but are not exposed through the
    unauthenticated Agent HTTP/SSE transport.
    """

    review_id: str
    run_id: str
    invocation_id: str
    tool_name: str
    tool_risk: str
    side_effects: list[str] = Field(default_factory=list)
    read_only: bool | None = None
    mode: str
    reason: str
    created_at: str
    status: str
    decided_by: str | None = None
    decision_reason: str | None = None
    decided_at: str | None = None
    parent_run_id: str | None = None
    child_run_id: str | None = None
    input_fields: list[str] = Field(default_factory=list)
    invocation_fingerprint: str = ""

    @classmethod
    def from_record(cls, record: SafetyReviewRecord) -> SafetyReviewResponse:
        encoded_input = json.dumps(
            record.tool_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return cls(
            **record.model_dump(
                include=set(cls.model_fields) - {
                    "parent_run_id", "child_run_id", "input_fields",
                    "invocation_fingerprint",
                },
                mode="python",
            ),
            input_fields=sorted(record.tool_input)[:32],
            invocation_fingerprint=hashlib.sha256(encoded_input).hexdigest()[:20],
        )


class SafetyReviewListResponse(BaseModel):
    reviews: list[SafetyReviewResponse]


class SafetyReviewQueueResponse(BaseModel):
    reviews: list[SafetyReviewResponse]


class SafetyReviewDecisionRequest(BaseModel):
    decision: SafetyReviewDecision
    reason: str | None = None
    decided_by: str = "user"


class ContinueAgentRunRequest(BaseModel):
    command_id: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=20_000)

    @field_validator("command_id", "answer")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be empty or whitespace.")
        return value


class ContinueAgentRunResponse(BaseModel):
    run_id: str
    command_id: str
    question_id: str
    status: AgentRunStatus
    replayed: bool
    resume_scheduled: bool


class SessionCreateRequest(BaseModel):
    title: str | None = None
    initial_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionCreateResponse(AgentSessionDetail):
    pass


class SessionWorkspaceUpdateRequest(BaseModel):
    path: str = Field(min_length=1)
    platform: str


class SessionWorkspaceResponse(BaseModel):
    session_id: str
    workspace: SessionWorkspace


class SessionListResponse(AgentSessionList):
    sessions: list[AgentSession]


class SessionDetailResponse(AgentSessionDetail):
    pass


class SessionAppendMessageRequest(BaseModel):
    role: SessionRole = "user"
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionAppendMessageResponse(AgentSessionMessage):
    pass


class MailImportRequest(BaseModel):
    account: MailAccountInput
    messages: list[MailMessageInput]


class MailImportResponse(MailImportResult):
    pass


class MailSearchResponse(MailSearchResult):
    pass


class KnowledgeImportRequest(KnowledgeDocumentInput):
    pass


class KnowledgeImportResponse(KnowledgeImportResult):
    pass


class KnowledgeSearchResponse(KnowledgeSearchResult):
    pass


class KnowledgeSemanticSyncResponse(KnowledgeSemanticSyncResult):
    pass


class KnowledgeSemanticSyncRequest(BaseModel):
    allow_model_download: bool = False


class MailKnowledgeMirrorSyncRequest(BaseModel):
    account_id: str | None = None


class MailKnowledgeMirrorSyncResponse(MailKnowledgeMirrorResult):
    pass


class KnowledgeChunkLoadRequest(BaseModel):
    chunk_ids: list[str]
    max_chars_per_chunk: int = Field(default=420, ge=1, le=1200)


class KnowledgeChunkLoadResponse(KnowledgeChunkLoadResult):
    pass


class KnowledgeDocumentResponse(KnowledgeDocumentRecord):
    pass


class MailMatterListResponse(MailMatterList):
    matters: list[MailMatter]


class OutlookAuthStartResponse(OutlookAuthStartResult):
    pass


class OutlookAuthCompleteRequest(BaseModel):
    device_code: str


class OutlookAuthCompleteResponse(OutlookAuthCompleteResult):
    pass


class OutlookSyncRequest(BaseModel):
    folder: str | None = None
    limit: int = Field(default=25, ge=1, le=100)
    max_pages: int = Field(default=1, ge=1, le=10)


class OutlookSyncResponse(OutlookSyncResult):
    pass


class MatterCreateRequest(MatterCreateInput):
    pass


class MatterRecordResponse(MatterRecord):
    pass


class MatterListResponse(MatterList):
    matters: list[MatterRecord]


class MatterSearchResponse(MatterSearchResult):
    pass


class MatterUpdateRequest(MatterUpdateInput):
    pass


class MatterLinkSourceRequest(MatterSourceLinkInput):
    pass


__all__ = [
    "KnowledgeDocumentInput",
    "MailAccountInput",
    "MailAttachmentInput",
    "MailMessageInput",
    "MatterCreateInput",
    "MatterSourceLinkInput",
    "MatterUpdateInput",
]
