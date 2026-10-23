"""Workspace IDs resolve locally; these routes never proxy through the hub."""
from functools import partial
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.domains.node_workspaces import MAX_TRANSFER_BYTES

router = APIRouter(tags=["direct node workspaces"])


class PathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class WorkspaceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=200)


def _service(request, workspace_id=None):
    service = getattr(request.app.state.runtime, "node_workspaces", None)
    if service is None:
        raise HTTPException(404, "direct workspaces disabled")
    connection = getattr(request.state, "device_connection", None)
    if (connection and workspace_id is not None
            and workspace_id not in connection.get("workspace_ids", [])):
        raise HTTPException(403, "workspace not granted")
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
def list_workspaces(request: Request):
    service = _service(request)
    connection = getattr(request.state, "device_connection", None)
    allowed = set(connection["workspace_ids"]) if connection else set(service.mappings)
    return {"items": [{"workspace_id": key} for key in sorted(service.mappings) if key in allowed]}


@router.get("/local/workspaces/{workspace_id}/entries")
async def entries(workspace_id: str, request: Request, path: str = ""):
    return await _call(_service(request, workspace_id).entries, workspace_id, path)


@router.get("/local/workspaces/{workspace_id}/status")
async def workspace_status(workspace_id: str, request: Request):
    return await _call(_service(request, workspace_id).status, workspace_id)


@router.get("/local/cache")
def cache_status(request: Request):
    _service(request)
    cache = request.app.state.runtime.shared_memory_cache
    result = cache.cache_status() if cache is not None else {"available": False, "reads_allow_stale": True}
    journal = getattr(request.app.state.runtime, "memory_replication_journal", None)
    return {**result, "memory_aggregation": {
                "enabled": request.app.state.runtime.local_app_config.sync.memory_aggregation_enabled,
                "ownership_tracking": True, **journal.status()} if journal is not None
            else {"enabled": False}}


@router.get("/local/workspaces/{workspace_id}/file")
async def download(workspace_id: str, request: Request, path: str):
    raw = await _call(_service(request, workspace_id).read, workspace_id, path)
    return Response(raw, media_type="application/octet-stream", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(path.rsplit("/", 1)[-1]),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
    })


@router.put("/local/workspaces/{workspace_id}/file", status_code=201)
async def upload(workspace_id: str, request: Request, path: str):
    service = _service(request, workspace_id)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_TRANSFER_BYTES:
            raise HTTPException(413, "file exceeds direct transfer limit")
        raw.extend(chunk)
    return await _call(service.create_file, workspace_id, path, bytes(raw))


@router.post("/local/workspaces/{workspace_id}/directories", status_code=201)
async def directory(workspace_id: str, payload: PathRequest, request: Request):
    return await _call(_service(request, workspace_id).create_directory, workspace_id, payload.path)


@router.post("/sessions/{session_id}/workspace-id")
async def bind(session_id: str, payload: WorkspaceBinding, request: Request):
    runtime = request.app.state.runtime
    service = _service(request, payload.workspace_id)
    root = await _call(service.root, payload.workspace_id)
    workspace = await _call(runtime.set_session_workspace, session_id=session_id,
                            path=str(root), platform=runtime.platform.name)
    return workspace.model_dump(mode="json")
