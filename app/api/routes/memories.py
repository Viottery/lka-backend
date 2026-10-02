"""Local user controls for durable memory and background status."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.memory_files import MemoryFileConflictError, MemoryFileError
from app.domains.memory import MemoryConflictError, MemoryInput, MemorySourceInput


def require_local_memory_control(request: Request) -> None:
    """Do not expose private memory or job metadata on an unauthenticated LAN bind."""

    expected = os.getenv("LKA_MEMORY_API_TOKEN")
    if expected:
        authorization = request.headers.get("authorization", "")
        supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="memory API authorization required")
        return
    host = request.client.host if request.client else ""
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise HTTPException(status_code=403, detail="memory API is local-only without LKA_MEMORY_API_TOKEN")


router = APIRouter(tags=["memory"], dependencies=[Depends(require_local_memory_control)])


class CreateMemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    memory_type: Literal["preference", "project_decision", "user_fact"] = "preference"
    scope: Literal["global", "project"] = "global"
    workspace_path: str | None = None
    sensitivity: Literal["normal", "personal"] = "normal"


class CorrectMemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=1)


class LearningPolicyRequest(BaseModel):
    scope: Literal["global", "project"] = "global"
    workspace_path: str | None = None
    enabled: bool


class RelocateProjectRequest(BaseModel):
    old_workspace_path: str = Field(min_length=1)
    new_workspace_path: str = Field(min_length=1)


@router.post("/memories/projects/{project_id}/relocate")
def relocate_project(project_id: str, payload: RelocateProjectRequest, request: Request):
    runtime = request.app.state.runtime
    paths = []
    for raw in (payload.old_workspace_path, payload.new_workspace_path):
        resolved = runtime.path_resolver.resolve_workspace(raw)
        if not runtime.path_resolver.is_allowed_workspace(resolved):
            raise HTTPException(status_code=403, detail="workspace is outside configured roots")
        paths.append(resolved.normalized_path)
    try:
        runtime.memory_service.bind_project_path(project_id, paths[1], old_path=paths[0])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"project_id": project_id, "workspace_path": paths[1]}


def _project_id(request: Request, scope: str, workspace_path: str | None, *, create: bool) -> str | None:
    if scope != "project":
        return None
    if not workspace_path:
        raise HTTPException(status_code=422, detail="project scope requires workspace_path")
    resolved = request.app.state.runtime.path_resolver.resolve_workspace(workspace_path)
    if not request.app.state.runtime.path_resolver.is_allowed_workspace(resolved):
        raise HTTPException(status_code=403, detail="workspace is outside configured roots")
    return request.app.state.runtime.memory_service.resolve_project(
        resolved.normalized_path, create=create,
    )


def _authorized_record(request: Request, memory_id: str, workspace_path: str | None):
    service = request.app.state.runtime.memory_service
    try:
        record = service.get(memory_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="memory not found") from exc
    if record.scope == "project":
        project_id = _project_id(request, "project", workspace_path, create=False)
        if project_id is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="memory not found")
    return record


def _refresh_file(request: Request, scope: str, project_id: str | None) -> str:
    try:
        request.app.state.runtime.memory_files.generate(scope=scope, project_id=project_id)
        return "synced"
    except (MemoryFileError, OSError):
        return "conflict_or_unavailable"


@router.get("/memories")
def list_memories(
    request: Request,
    scope: Literal["global", "project"] = "global",
    workspace_path: str | None = None,
    include_candidates: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = 0,
):
    project_id = _project_id(request, scope, workspace_path, create=False)
    if not 0 <= offset <= 1000000:
        raise HTTPException(status_code=422, detail="offset must be between 0 and 1000000")
    if scope == "project" and project_id is None:
        return {"memories": [], "next_offset": None}
    statuses = ("active", "candidate") if include_candidates else ("active",)
    rows = request.app.state.runtime.memory_service.list(
        scope=scope, project_id=project_id, statuses=statuses, limit=limit + 1, offset=offset,
    )
    return {"memories": [item.model_dump(mode="json") for item in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None}


@router.get("/memories/export")
def export_memories(request: Request, scope: Literal["global", "project"] = "global",
                    workspace_path: str | None = None, include_candidates: bool = False,
                    limit: int = Query(default=500, ge=1, le=500), offset: int = 0):
    return {"version": 1, "scope": scope,
            **list_memories(request, scope, workspace_path, include_candidates, limit, offset)}


@router.get("/memories/{memory_id}/sources")
def memory_sources(memory_id: str, request: Request, workspace_path: str | None = None):
    _authorized_record(request, memory_id, workspace_path)
    return {"sources": request.app.state.runtime.memory_service.sources_for(memory_id)}


@router.post("/memories", status_code=201)
def create_memory(payload: CreateMemoryRequest, request: Request):
    service = request.app.state.runtime.memory_service
    project_id = _project_id(request, payload.scope, payload.workspace_path, create=True)
    source_id = service.register_source(MemorySourceInput(
        source_type="user_api", source_ref="manual:" + hashlib.sha256(
            f"{payload.scope}:{project_id}:{payload.content}".encode()
        ).hexdigest(),
        trusted_source=True,
    ))
    item = service.create(MemoryInput(
        content=payload.content, memory_type=payload.memory_type,
        scope=payload.scope, project_id=project_id, source_id=source_id,
        sensitivity=payload.sensitivity, user_confirmed=True,
    ))
    return {**item.model_dump(mode="json"),
            "memory_file_status": _refresh_file(request, item.scope, item.project_id)}


@router.get("/memories/learning")
def get_learning_policy(
    request: Request, scope: Literal["global", "project"] = "global",
    workspace_path: str | None = None,
):
    project_id = _project_id(request, scope, workspace_path, create=False)
    return {"scope": scope, "project_id": project_id, "enabled":
        request.app.state.runtime.memory_service.learning_enabled(
            scope=scope, project_id=project_id,
        )}


@router.put("/memories/learning")
def set_learning_policy(payload: LearningPolicyRequest, request: Request):
    project_id = _project_id(request, payload.scope, payload.workspace_path, create=True)
    request.app.state.runtime.memory_service.set_learning_enabled(
        scope=payload.scope, project_id=project_id, enabled=payload.enabled,
    )
    return {"scope": payload.scope, "project_id": project_id, "enabled": payload.enabled}


@router.get("/memories/file")
def get_memory_file(
    request: Request, scope: Literal["global", "project"] = "global",
    workspace_path: str | None = None,
):
    project_id = _project_id(request, scope, workspace_path, create=False)
    if scope == "project" and project_id is None:
        raise HTTPException(status_code=404, detail="project memory view not found")
    path = request.app.state.runtime.memory_files.path_for(scope=scope, project_id=project_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="memory view not generated")
    try:
        content = request.app.state.runtime.memory_files._read_view(path).decode("utf-8")
    except UnicodeError as exc:
        raise HTTPException(status_code=422, detail="memory view is not valid UTF-8") from exc
    except MemoryFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"path": str(path), "content": content}


@router.post("/memories/file/generate")
def generate_memory_file(
    request: Request, scope: Literal["global", "project"] = "global",
    workspace_path: str | None = None,
):
    project_id = _project_id(request, scope, workspace_path, create=True)
    try:
        path = request.app.state.runtime.memory_files.generate(scope=scope, project_id=project_id)
    except MemoryFileConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MemoryFileError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"path": str(path), "status": "synced"}


@router.get("/memories/file/preview")
def preview_memory_file(
    request: Request, scope: Literal["global", "project"] = "global",
    workspace_path: str | None = None,
):
    project_id = _project_id(request, scope, workspace_path, create=False)
    try:
        preview = request.app.state.runtime.memory_files.preview_import(
            scope=scope, project_id=project_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="memory view not generated") from exc
    except MemoryFileConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemoryFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"path": str(preview.path), "generated_hash": preview.generated_hash,
            "current_hash": preview.current_hash,
            "edits": [edit.__dict__ for edit in preview.edits]}


@router.post("/memories/file/import")
def import_memory_file(
    request: Request, scope: Literal["global", "project"] = "global",
    workspace_path: str | None = None,
):
    project_id = _project_id(request, scope, workspace_path, create=False)
    files = request.app.state.runtime.memory_files
    try:
        preview = files.preview_import(scope=scope, project_id=project_id)
        changed = files.import_edits(scope=scope, project_id=project_id, preview=preview)
        status = _refresh_file(request, scope, project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="memory view not generated") from exc
    except MemoryFileConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemoryFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"changed": [item.model_dump(mode="json") for item in changed],
            "memory_file_status": status}


@router.get("/memories/{memory_id}")
def get_memory(memory_id: str, request: Request, workspace_path: str | None = None):
    return _authorized_record(request, memory_id, workspace_path).model_dump(mode="json")


@router.patch("/memories/{memory_id}")
def correct_memory(
    memory_id: str, payload: CorrectMemoryRequest, request: Request,
    workspace_path: str | None = None,
):
    current = _authorized_record(request, memory_id, workspace_path)
    service = request.app.state.runtime.memory_service
    source_id = service.register_source(MemorySourceInput(
        source_type="user_api", source_ref="correction:" + hashlib.sha256(
            f"{memory_id}:{payload.expected_version}:{payload.content}".encode()
        ).hexdigest(),
        trusted_source=True,
    ))
    try:
        updated = service.correct(
            current.memory_id, content=payload.content,
            expected_version=payload.expected_version, source_id=source_id,
        )
    except MemoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**updated.model_dump(mode="json"),
            "memory_file_status": _refresh_file(request, updated.scope, updated.project_id)}


@router.delete("/memories/{memory_id}")
def forget_memory(
    memory_id: str, request: Request, expected_version: int = Query(ge=1),
    workspace_path: str | None = None,
):
    current = _authorized_record(request, memory_id, workspace_path)
    try:
        updated = request.app.state.runtime.memory_service.retract(
            current.memory_id, expected_version=expected_version,
        )
    except MemoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**updated.model_dump(mode="json"),
            "memory_file_status": _refresh_file(request, updated.scope, updated.project_id)}


@router.get("/background/jobs")
def list_background_jobs(
    request: Request, status: str | None = None, scope_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    try:
        jobs = request.app.state.runtime.background_job_store.list(
            status=status, scope_id=scope_id, limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"jobs": jobs, "last_background_error": request.app.state.runtime.last_background_error,
            "last_memory_file_error": request.app.state.runtime.memory_background.last_file_error}


@router.get("/background/health")
def background_health(request: Request):
    """Expose queue health without job payloads or source identifiers."""

    runtime = request.app.state.runtime
    return {**runtime.background_job_store.health_metrics(),
            "llm_workloads": runtime.llm_workloads.health()}
