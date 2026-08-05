from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.agent_turn import AgentTurnResult
from app.core.context import SessionContext, TaskContext
from app.core.events import EventRecord
from app.core.llm import LLMResponse
from app.core.mail import (
    MailAccountInput,
    MailAttachmentInput,
    MailImportResult,
    MailMatter,
    MailMatterList,
    MailMessageInput,
    MailSearchResult,
)
from app.core.matters import (
    MatterCreateInput,
    MatterList,
    MatterRecord,
    MatterSearchResult,
    MatterSourceLinkInput,
    MatterUpdateInput,
)
from app.core.outlook import (
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
)
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


class AgentTurnRequest(BaseModel):
    session_id: str | None = None
    user_input: str


class AgentTurnResponse(AgentTurnResult):
    pass


class SessionCreateRequest(BaseModel):
    title: str | None = None
    initial_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionCreateResponse(AgentSessionDetail):
    pass


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
    "MatterCreateInput",
    "MatterSourceLinkInput",
    "MatterUpdateInput",
]
