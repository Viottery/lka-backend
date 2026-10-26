"""Workspace IDs resolve locally; these routes never proxy through the hub."""
import logging
from functools import partial
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.domains.device_connections import ActiveSessionRunError
from app.domains.node_workspaces import MAX_TRANSFER_BYTES

router = APIRouter(tags=["direct node workspaces"])
logger = logging.getLogger(__name__)


class PathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class WorkspaceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=200)


class DirectorySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


async def _service(request, workspace_id=None):
    service = getattr(request.app.state.runtime, "node_workspaces", None)
    if service is None:
        raise HTTPException(404, "direct workspaces disabled")
    connection = getattr(request.state, "device_connection", None)
    if connection:
        connections = getattr(request.app.state, "device_connection_service", None)
        mappings = await run_in_threadpool(connections.workspace_roots, connection) if connections else {}
        if workspace_id is not None and workspace_id not in mappings:
            raise HTTPException(403, "workspace not granted")
        if any(key not in service.mappings for key in mappings):
            from app.domains.node_workspaces import NodeWorkspaceService
            # Retain all operator mount guards internally, while every endpoint
            # still checks the caller's effective mapping before resolving an ID.
            internal_mappings = {**service.mappings, **mappings}
            service = NodeWorkspaceService(internal_mappings, service.mount_guard.mounts)
    if not connection:
        account = getattr(request.state, "account", None)
        if not isinstance(account, dict) or not account.get("is_admin"):
            from app.api.routes.session_files import _require_loopback

            _require_loopback(request)
    return service


async def _call(method, *args, **kwargs):
    try:
        return await run_in_threadpool(partial(method, *args, **kwargs))
    except FileExistsError as exc:
        raise HTTPException(409, "path already exists") from exc
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(404, "workspace or path unavailable") from exc
    except (PermissionError, OSError) as exc:
        raise HTTPException(403, "workspace path unavailable") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/local/workspaces")
async def list_workspaces(request: Request):
    service = await _service(request)
    connection = getattr(request.state, "device_connection", None)
    connections = getattr(request.app.state, "device_connection_service", None)
    mappings = (await run_in_threadpool(connections.workspace_roots, connection)
                if connection and connections else service.mappings)
    return {"items": [{"workspace_id": key} for key in sorted(mappings)]}


@router.get("/local/filesystem/entries")
async def browse_filesystem(request: Request, path: str = "",
                            query: str = Query(default="", max_length=120),
                            offset: int = Query(default=0, ge=0),
                            limit: int = Query(default=200, ge=1, le=200)):
    connection = getattr(request.state, "device_connection", None)
    connections = getattr(request.app.state, "device_connection_service", None)
    if not connection or connections is None:
        raise HTTPException(401, "authenticated device connection required")
    return await _call(connections.browse_directories, path, query=query, offset=offset, limit=limit)


@router.post("/local/workspaces/selections", status_code=201)
async def select_filesystem_directory(payload: DirectorySelection, request: Request):
    connection = getattr(request.state, "device_connection", None)
    connections = getattr(request.app.state, "device_connection_service", None)
    if not connection or connections is None:
        raise HTTPException(401, "authenticated device connection required")
    return await _call(connections.select_workspace, connection, payload.path)


@router.get("/local/workspaces/{workspace_id}/entries")
async def entries(workspace_id: str, request: Request, path: str = ""):
    service = await _service(request, workspace_id)
    return await _call(service.entries, workspace_id, path)


@router.get("/local/workspaces/{workspace_id}/status")
async def workspace_status(workspace_id: str, request: Request):
    service = await _service(request, workspace_id)
    return await _call(service.status, workspace_id)


@router.get("/local/cache")
async def cache_status(request: Request):
    await _service(request)
    cache = request.app.state.runtime.shared_memory_cache
    result = cache.cache_status() if cache is not None else {"available": False, "reads_allow_stale": True}
    journal = getattr(request.app.state.runtime, "memory_replication_journal", None)
    return {**result, "memory_aggregation": {
                "enabled": request.app.state.runtime.local_app_config.sync.memory_aggregation_enabled,
                "ownership_tracking": True, **journal.status()} if journal is not None
            else {"enabled": False}}


@router.get("/local/workspaces/{workspace_id}/file")
async def download(workspace_id: str, request: Request, path: str):
    service = await _service(request, workspace_id)
    raw = await _call(service.read, workspace_id, path)
    return Response(raw, media_type="application/octet-stream", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(path.rsplit("/", 1)[-1]),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
    })


@router.put("/local/workspaces/{workspace_id}/file", status_code=201)
async def upload(workspace_id: str, request: Request, path: str):
    service = await _service(request, workspace_id)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_TRANSFER_BYTES:
            raise HTTPException(413, "file exceeds direct transfer limit")
        raw.extend(chunk)
    return await _call(service.create_file, workspace_id, path, bytes(raw))


@router.post("/local/workspaces/{workspace_id}/directories", status_code=201)
async def directory(workspace_id: str, payload: PathRequest, request: Request):
    service = await _service(request, workspace_id)
    return await _call(service.create_directory, workspace_id, payload.path)


@router.post("/sessions/{session_id}/workspace-id")
async def bind(session_id: str, payload: WorkspaceBinding, request: Request):
    runtime = request.app.state.runtime
    service = await _service(request, payload.workspace_id)
    connection = getattr(request.state, "device_connection", None)
    connections = getattr(request.app.state, "device_connection_service", None)
    root = await _call(service.root, payload.workspace_id)
    if connection:
        from app.core.sessions import SessionWorkspace

        workspace = SessionWorkspace(path=str(root), platform=runtime.platform.name,
                                     backend_path=str(root), state="created")
        if await _call(connections.session_has_active_run, session_id):
            raise HTTPException(409, "finish or cancel the active Agent run before changing its workspace")
        project = await _call(runtime.project_service.register, root,
                              Path(root).name[:120] or "Project")
        try:
            data = await _call(connections.bind_session_workspace, session_id, connection,
                               payload.workspace_id, workspace, project["project_id"])
        except ActiveSessionRunError as exc:
            raise HTTPException(409, str(exc)) from exc
        changed = getattr(runtime.session_service, "on_change", None)
        if changed:
            try:
                changed()
            except Exception:  # Persistence is committed; keep the response stable.
                logger.warning("Session workspace changed, but its update notification failed", exc_info=True)
        return data
    else:
        workspace = await _call(runtime.set_session_workspace, session_id=session_id,
                                path=str(root), platform=runtime.platform.name)
    return workspace.model_dump(mode="json")
