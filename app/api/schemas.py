from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

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
    MailProcessResult,
    MailSearchResult,
)
from app.core.retrieval import RetrievalResult
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


class MailImportRequest(BaseModel):
    account: MailAccountInput
    messages: list[MailMessageInput]


class MailImportResponse(MailImportResult):
    pass


class MailSearchResponse(MailSearchResult):
    pass


class MailProcessRequest(BaseModel):
    query: str | None = None
    limit: int = 10


class MailProcessResponse(MailProcessResult):
    pass


class MailMatterListResponse(MailMatterList):
    matters: list[MailMatter]


__all__ = [
    "MailAccountInput",
    "MailAttachmentInput",
    "MailMessageInput",
]
