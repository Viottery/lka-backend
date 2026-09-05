from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.agent_turn import AgentTurnResult
from app.core.context import SessionContext, TaskContext
from app.core.events import EventRecord
from app.core.llm import LLMResponse, LLMResponseMode
from app.domains.mail import (
    MailAccountInput,
    MailAttachmentInput,
    MailImportResult,
    MailMatter,
    MailMatterList,
    MailMessageInput,
    MailSearchResult,
)
from app.domains.knowledge import (
    KnowledgeChunkLoadResult,
    KnowledgeDocumentInput,
    KnowledgeDocumentRecord,
    KnowledgeImportResult,
    KnowledgeSemanticSyncResult,
    KnowledgeSearchResult,
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
from app.core.retrieval import RetrievalResult
from app.core.sessions import (
    AgentSession,
    AgentSessionDetail,
    AgentSessionList,
    AgentSessionMessage,
    SessionRole,
    SessionWorkspace,
)
from app.core.safety import SafetyReviewDecision, SafetyReviewRecord
from app.core.tools import ToolInvocation, ToolResult
from app.core.tracing import TraceRecord


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


class AgentTurnResponse(AgentTurnResult):
    pass


class SafetyReviewResponse(SafetyReviewRecord):
    pass


class SafetyReviewListResponse(BaseModel):
    reviews: list[SafetyReviewRecord]


class SafetyReviewDecisionRequest(BaseModel):
    decision: SafetyReviewDecision
    reason: str | None = None
    decided_by: str = "user"


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
    "MailAccountInput",
    "MailAttachmentInput",
    "MailMessageInput",
    "KnowledgeDocumentInput",
    "MatterCreateInput",
    "MatterSourceLinkInput",
    "MatterUpdateInput",
]
