"""Explicitly trusted backend replication, separate from human memory controls."""
from fastapi import APIRouter, HTTPException, Query, Request

from app.api.routes.devices import _owner
from app.domains.shared_sync import SyncConflictError

node_router = APIRouter(prefix="/node/sync", tags=["memory replication"])
hub_router = APIRouter(prefix="/sync/memory-records", tags=["memory replication"])


@hub_router.get("/conflicts")
def list_conflicts(request: Request, limit: int = Query(default=100, ge=1, le=100),
                   offset: int = Query(default=0, ge=0, le=1_000_000)):
    _owner(request)
    service = getattr(request.app.state, "memory_replication_service", None)
    if service is None:
        raise HTTPException(404, "memory replication unavailable")
    try:
        return service.list_conflicts(limit=limit, offset=offset)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@node_router.post("/memory-records")
def replicate_memory(payload: dict, request: Request):
    principal = getattr(request.state, "device", None)
    if not isinstance(principal, dict):
        raise HTTPException(401, "device authentication required")
    service = getattr(request.app.state, "memory_replication_service", None)
    if service is None:
        raise HTTPException(404, "memory replication unavailable")
    try:
        return service.submit(principal, payload)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except SyncConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
