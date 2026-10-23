"""Account-controlled device enrollment and credential-fenced node liveness."""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field

from app.domains.devices import (
    DeviceConflictError,
    DeviceEnrollment,
    DeviceSelection,
    DeviceUnavailableError,
    DeviceUpdate,
    StrictModel,
)

router = APIRouter(prefix="/devices", tags=["devices"])
node_router = APIRouter(prefix="/node", tags=["devices"])


class RevisionRequest(StrictModel):
    expected_revision: int = Field(ge=1)


class HeartbeatRequest(StrictModel):
    status: str = Field(default="online", max_length=30)
    environment_id: str | None = Field(default=None, max_length=120)


def _service(request):
    service = getattr(request.app.state, "device_service", None)
    if service is None or not service.config.enabled:
        raise HTTPException(404, "devices disabled")
    return service


def _owner(request):
    account = getattr(request.state, "account", None)
    owner = (
        account.get("user_id") if isinstance(account, dict) else getattr(account, "user_id", None)
    )
    if not owner:
        raise HTTPException(401, "account authentication required")
    return owner


def _call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, "device or selection not found") from exc
    except (DeviceConflictError, DeviceUnavailableError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("", status_code=201)
def enroll(payload: DeviceEnrollment, request: Request):
    _owner(request)
    return _call(_service(request).enroll, payload)


@router.get("")
def list_devices(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1000000),
):
    _owner(request)
    return _call(_service(request).list, limit, offset)


# Static selection routes precede device identity routes.
@router.get("/selection")
def get_selection(request: Request, selection_key: str = Query(default="default", max_length=200)):
    return _call(_service(request).get_selection, _owner(request), selection_key)


@router.put("/selection")
def save_selection(
    payload: DeviceSelection,
    request: Request,
    selection_key: str = Query(default="default", max_length=200),
):
    return _call(_service(request).save_selection, _owner(request), payload, selection_key)


@router.post("/selection/resolve")
def resolve_selection(
    request: Request, selection_key: str = Query(default="default", max_length=200)
):
    return _call(_service(request).resolve, _owner(request), selection_key)


@router.get("/{device_id}")
def get_device(device_id: str, request: Request):
    _owner(request)
    return _call(_service(request).get, device_id)


@router.patch("/{device_id}")
def update_device(device_id: str, payload: DeviceUpdate, request: Request):
    _owner(request)
    return _call(_service(request).update, device_id, payload)


@router.post("/{device_id}/revoke")
def revoke_device(device_id: str, payload: RevisionRequest, request: Request):
    _owner(request)
    return _call(_service(request).revoke, device_id, payload.expected_revision)


@router.post("/{device_id}/rotate")
def rotate_device(device_id: str, payload: RevisionRequest, request: Request):
    _owner(request)
    return _call(_service(request).rotate, device_id, payload.expected_revision)


@node_router.post("/heartbeat")
def heartbeat(payload: HeartbeatRequest, request: Request):
    principal = getattr(request.state, "device", None)
    if not isinstance(principal, dict):
        raise HTTPException(401, "device authentication required")
    return _call(_service(request).heartbeat, principal, **payload.model_dump())
