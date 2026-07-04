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


class CapabilityItem(BaseModel):
    name: str
    type: str
    risk: str
    requires_confirmation: bool


class CapabilityListResponse(BaseModel):
    capabilities: list[CapabilityItem]
