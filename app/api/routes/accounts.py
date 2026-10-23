"""Instance login and trusted coadministrator management (one shared dataset)."""
from __future__ import annotations

from functools import partial

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.domains.accounts import AccountError


class AccountRoute(APIRoute):
    """Prevent validation errors from reflecting submitted passwords."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def sanitized(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                errors = [{key: error[key] for key in ("loc", "msg", "type") if key in error}
                          for error in exc.errors()]
                return JSONResponse({"detail": errors}, status_code=422,
                                    headers={"Cache-Control": "no-store"})
        return sanitized


router = APIRouter(prefix="/auth", tags=["accounts"], route_class=AccountRoute)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256, repr=False)


class CreateAccountRequest(LoginRequest):
    pass


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256, repr=False)
    new_password: str = Field(min_length=12, max_length=256, repr=False)


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_password: str = Field(min_length=12, max_length=256, repr=False)


class DisableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disabled: bool


def _service(request):
    service = getattr(request.app.state, "account_service", None)
    if service is None or not service.config.enabled:
        raise HTTPException(404, "Account authentication disabled")
    return service


def _account(request):
    _service(request)
    account = getattr(request.state, "account", None)
    if not account:
        raise HTTPException(401, "Account authentication required")
    return account


async def _call(func, *args, **kwargs):
    try:
        return await run_in_threadpool(partial(func, *args, **kwargs))
    except AccountError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response):
    service = _service(request)
    # This route requires the global middleware; fail closed if mounted alone.
    if not hasattr(request.state, "native_login"):
        raise HTTPException(503, "Authentication middleware required")
    source = request.client.host if request.client else "unknown"
    result = await _call(service.login, payload.username, payload.password, source)
    token = result.pop("token")
    if request.state.native_login:
        result["token"] = token
    else:
        response.set_cookie(service.config.cookie_name, token, httponly=True,
                            secure=service.config.secure_cookie, samesite="strict", path="/",
                            max_age=service.config.session_ttl_seconds)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/me")
async def me(request: Request):
    return _account(request)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    account = _account(request)
    await _call(_service(request).revoke_session, account["user_id"], account["session_id"])
    response.delete_cookie(_service(request).config.cookie_name, path="/",
                           secure=_service(request).config.secure_cookie, httponly=True, samesite="strict")


@router.post("/password", status_code=204)
async def change_password(payload: PasswordRequest, request: Request, response: Response):
    account = _account(request)
    service = _service(request)
    await _call(service.change_password, account["user_id"], payload.current_password, payload.new_password)
    response.delete_cookie(service.config.cookie_name, path="/", secure=service.config.secure_cookie,
                           httponly=True, samesite="strict")


@router.get("/sessions")
async def sessions(request: Request):
    return {"items": await _call(_service(request).list_sessions, _account(request)["user_id"])}


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(session_id: str, request: Request):
    await _call(_service(request).revoke_session, _account(request)["user_id"], session_id)


@router.get("/accounts")
async def accounts(request: Request):
    return {"items": await _call(_service(request).list_accounts, _account(request)["user_id"]),
            "data_scope": "shared_personal_instance"}


@router.post("/accounts", status_code=201)
async def create_account(payload: CreateAccountRequest, request: Request):
    return await _call(_service(request).create_admin, payload.username, payload.password,
                       actor_user_id=_account(request)["user_id"])


@router.post("/accounts/{user_id}/password", status_code=204)
async def reset_password(user_id: str, payload: ResetPasswordRequest, request: Request):
    await _call(_service(request).reset_password, _account(request)["user_id"], user_id, payload.new_password)


@router.patch("/accounts/{user_id}", status_code=204)
async def disable_account(user_id: str, payload: DisableRequest, request: Request):
    await _call(_service(request).set_disabled, _account(request)["user_id"], user_id, payload.disabled)
