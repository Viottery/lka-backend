from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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


class TaskPlanRequest(BaseModel):
    task: str
    workspace: str | None = None
    frontend: str | None = None


class TaskPlanResponse(BaseModel):
    intent: str
    plan: list[str]
    suggested_capabilities: list[str]
    risk: str


class TaskRunRequest(BaseModel):
    task: str
    workspace: str | None = None
    frontend: str | None = None
    mode: str = "interactive"


class TaskRunResponse(BaseModel):
    task_id: str
    status: str
    summary: str
    trace_id: str
    requires_user_action: bool = False
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class TaskRecordResponse(BaseModel):
    task_id: str
    status: str
    summary: str
    trace_id: str


class CapabilityItem(BaseModel):
    name: str
    type: str
    risk: str
    requires_confirmation: bool


class CapabilityListResponse(BaseModel):
    capabilities: list[CapabilityItem]


class TraceRecordResponse(BaseModel):
    trace_id: str
    user_goal: str
    intent: str
    plan: list[str]
    context_summary: str
    capabilities_used: list[str]
    verification_result: dict[str, Any]
    success: bool


class ConfirmationDecisionRequest(BaseModel):
    decision: str
    frontend: str | None = None


class ConfirmationResponse(BaseModel):
    confirmation_id: str
    decision: str
    status: str

