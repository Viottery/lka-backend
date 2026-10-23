"""Hub admission and direct node client login; no remote execution proxy."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes.accounts import AccountRoute

router = APIRouter(prefix="/connections", tags=["device connections"], route_class=AccountRoute)
hub_router = APIRouter(prefix="/devices", tags=["device connections"])
node_router = APIRouter(prefix="/node", tags=["device connections"])


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_ids: list[str] | None = Field(default=None, max_length=100)


class RedemptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant: str = Field(min_length=32, max_length=256, repr=False)


def _service(request):
    service = getattr(request.app.state, "device_connection_service", None)
    if service is None or not service.config.enabled:
        raise HTTPException(404, "device connections disabled")
    return service


def _call(method, *args):
    try:
        return method(*args)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@hub_router.post("/{device_id}/connections", status_code=201)
def issue_grant(device_id: str, payload: GrantRequest, request: Request, response: Response):
    account = getattr(request.state, "account", None)
    if not isinstance(account, dict) or not account.get("user_id"):
        raise HTTPException(401, "account authentication required")
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).issue_grant, account["user_id"], device_id,
                 payload.workspace_ids)


@node_router.get("/connection-grants")
def pending_grants(request: Request, response: Response):
    principal = getattr(request.state, "device", None)
    if not isinstance(principal, dict):
        raise HTTPException(401, "device authentication required")
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).pending_grants, principal)


@hub_router.post("/{device_id}/connections/revoke")
def revoke_authorization(device_id: str, request: Request, response: Response):
    account = getattr(request.state, "account", None)
    if not isinstance(account, dict) or not account.get("user_id"):
        raise HTTPException(401, "account authentication required")
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).revoke_authorization, account["user_id"], device_id)


@router.post("/redeem")
def redeem(payload: RedemptionRequest, request: Request, response: Response):
    service = _service(request)
    if not getattr(request.state, "device_connection_login", False):
        raise HTTPException(503, "device connection middleware required")
    result = _call(service.redeem, payload.grant)
    token = result.pop("token")
    if request.state.device_connection_native:
        result["token"] = token
    else:
        response.set_cookie(service.config.cookie_name, token, httponly=True, secure=True,
                            samesite="strict", path="/", max_age=service.config.session_ttl_seconds)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/me")
def me(request: Request):
    principal = getattr(request.state, "device_connection", None)
    if not principal:
        raise HTTPException(401, "device connection authentication required")
    return {**principal, "csrf_token": getattr(request.state, "device_connection_csrf", None)}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    principal = me(request)
    service = _service(request)
    service.revoke(principal["session_id"])
    response.delete_cookie(service.config.cookie_name, path="/", secure=True,
                           httponly=True, samesite="strict")


@router.get("/sessions")
def sessions(request: Request):
    principal = me(request)
    return {"items": _call(_service(request).list_sessions, principal["user_id"], principal["session_id"])}


@router.delete("/sessions/{session_id}", status_code=204)
def revoke_session(session_id: str, request: Request):
    principal = me(request)
    _call(_service(request).revoke_session, principal["user_id"], session_id)
