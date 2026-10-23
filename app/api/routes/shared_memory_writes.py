"""Authenticated human memory control; no model-visible write tool is added."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.domains.shared_memory_writes import LocalMemoryMutation, SharedMemoryMutation
from app.domains.shared_sync import SyncConflictError

router = APIRouter(prefix="/local/shared-memory", tags=["shared memory control"])
node_router = APIRouter(prefix="/node/sync", tags=["shared memory control"])


class MemoryMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mutation_id: str = Field(min_length=1, max_length=120, pattern=r"^[\w.-]+$")
    operation: Literal["create", "update", "delete"]
    memory_id: str | None = Field(default=None, max_length=512)
    expected_version: int = Field(ge=0)
    scope: Literal["global", "project"] = "global"
    workspace_id: str | None = Field(default=None, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=500)
    memory_type: Literal["preference", "project_decision", "user_fact"] = "preference"
    sensitivity: Literal["normal", "personal"] = "normal"
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_at: str | None = None
    confirmed: Literal[True]


def _human(request):
    principal = getattr(request.state, "device_connection", None)
    if (isinstance(principal, dict) and principal.get("user_id") and principal.get("session_id")
            and principal.get("node_id") and principal.get("authorization_epoch")):
        return principal
    raise HTTPException(401, "hub-authorized human node connection required for shared memory control")


def _outbox(request):
    outbox = getattr(request.app.state, "shared_memory_write_outbox", None)
    config = request.app.state.runtime.local_app_config.sync
    if outbox is None or not config.enabled or config.role != "replica":
        raise HTTPException(404, "shared memory node unavailable")
    return outbox


def _call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except SyncConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "shared memory target unavailable") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _project(request, scope, workspace_id, principal):
    if scope == "global":
        if workspace_id is not None:
            raise HTTPException(422, "global scope cannot specify a workspace")
        return None
    service = getattr(request.app.state, "device_connection_service", None)
    roots = service.config.workspace_roots if service else {}
    if not workspace_id or workspace_id not in roots:
        raise HTTPException(403, "workspace is not approved")
    connection = getattr(request.state, "device_connection", None)
    if connection and workspace_id not in principal.get("workspace_ids", []):
        raise HTTPException(403, "workspace is not approved for this connection")
    return request.app.state.runtime.memory_service.resolve_project(roots[workspace_id], create=False)


def _entry_allowed(request, principal, entry):
    proposal = entry["proposal"]
    if proposal["scope"] == "global":
        return True
    outbox = _outbox(request)
    for workspace_id in principal.get("workspace_ids", []):
        project_id = _project(request, "project", workspace_id, principal)
        if project_id and outbox.cache.project_bindings.get(project_id) == proposal["project_id"]:
            return True
    return False


@router.get("/status")
def status(request: Request):
    principal = _human(request)
    outbox = _outbox(request)
    return {**outbox.status(principal["user_id"]),
            "write_enabled": request.app.state.runtime.local_app_config.sync.memory_write_enabled,
            "cache": outbox.cache.cache_status(), "pending_edits_affect_recall": False}


@router.get("/records")
def records(request: Request, scope: Literal["global", "project"] = "global",
            workspace_id: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    principal = _human(request)
    outbox = _outbox(request)
    project_id = _project(request, scope, workspace_id, principal)
    if scope == "project" and project_id is None:
        return {"memories": [], "project_id": None, "bound": False}
    rows = _call(outbox.cache._records, scope=scope, project_id=project_id, limit=limit)
    return {"memories": [row.model_dump(mode="json") for row in rows
                         if row.metadata.get("origin") == "cloud_replica"],
            "project_id": project_id,
            "shared_project_id": outbox.cache.project_bindings.get(project_id),
            "bound": scope == "global" or project_id in outbox.cache.project_bindings}


@router.get("/mutations")
def mutations(request: Request, limit: int = Query(default=100, ge=1, le=100)):
    principal = _human(request)
    items = _call(_outbox(request).list, principal["user_id"], limit)["items"]
    return {"items": [item for item in items if _entry_allowed(request, principal, item)]}


@router.get("/mutations/{mutation_id}")
def mutation(mutation_id: str, request: Request):
    principal = _human(request)
    item = _call(_outbox(request).get, mutation_id, principal["user_id"])
    if not _entry_allowed(request, principal, item):
        raise HTTPException(404, "shared memory mutation unavailable in this connection")
    return item


@router.post("/mutations", status_code=201)
def enqueue(payload: MemoryMutationRequest, request: Request):
    principal = _human(request)
    outbox = _outbox(request)
    if not request.app.state.runtime.local_app_config.sync.memory_write_enabled:
        raise HTTPException(403, "shared memory writes disabled")
    project_id = _project(request, payload.scope, payload.workspace_id, principal)
    values = payload.model_dump(exclude={"workspace_id"})
    values["project_id"] = project_id
    return _call(outbox.enqueue, _call(LocalMemoryMutation.model_validate, values), principal)


@node_router.post("/memory-mutations")
def submit(payload: SharedMemoryMutation, request: Request):
    principal = getattr(request.state, "device", None)
    if not isinstance(principal, dict):
        raise HTTPException(401, "device authentication required")
    service = getattr(request.app.state, "shared_memory_writes", None)
    if service is None:
        raise HTTPException(404, "shared memory writes unavailable")
    return _call(service.submit, principal, payload)
