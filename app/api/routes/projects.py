"""Local project folders and their independent display names."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.routes.memories import require_local_memory_control
from app.domains.projects import ProjectConflictError

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(require_local_memory_control)])


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4000)
    platform: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if value is not None and not value.strip():
            raise ValueError("project name must not be blank")
        return value.strip() if value is not None else value


class RenameProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    expected_revision: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("project name must not be blank")
        return value.strip()


@router.post("", status_code=201)
def create_project(payload: CreateProjectRequest, request: Request):
    runtime = request.app.state.runtime
    try:
        workspace = runtime._resolve_session_workspace(path=payload.path, platform=payload.platform or runtime.platform.name)
        return runtime.project_service.register(workspace.backend_path, payload.name or Path(workspace.backend_path).name[:120] or "Project")
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("")
def list_projects(request: Request, limit: int = Query(default=50, ge=1, le=200),
                  offset: int = Query(default=0, ge=0, le=1_000_000),
                  q: str | None = Query(default=None, max_length=500)):
    return request.app.state.runtime.project_service.list(limit=limit, offset=offset, q=q)


@router.get("/{project_id}")
def get_project(project_id: str, request: Request):
    try:
        return request.app.state.runtime.project_service.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, detail="project not found") from exc


@router.patch("/{project_id}")
def rename_project(project_id: str, payload: RenameProjectRequest, request: Request):
    try:
        return request.app.state.runtime.project_service.rename(project_id, payload.name, payload.expected_revision)
    except KeyError as exc:
        raise HTTPException(404, detail="project not found") from exc
    except ProjectConflictError as exc:
        raise HTTPException(409, detail="project_revision_conflict") from exc


@router.get("/{project_id}/sessions")
def project_sessions(project_id: str, request: Request,
                     limit: int = Query(default=50, ge=1, le=200),
                     offset: int = Query(default=0, ge=0), q: str | None = Query(default=None, max_length=500)):
    get_project(project_id, request)
    return request.app.state.runtime.list_sessions(project_id=project_id, limit=limit, offset=offset, q=q).model_dump(mode="json")
