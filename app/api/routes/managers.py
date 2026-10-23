"""Management session lifecycle; task execution uses the generic Agent API."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.routes.memories import require_local_memory_control
from app.core.sessions import AgentSession, SessionWorkspace
from app.domains.management import ManagementConflictError

router = APIRouter(prefix="/managers", tags=["managers"],
                   dependencies=[Depends(require_local_memory_control)])


class ExecutionWorkspaceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    platform: Literal["linux", "windows", "macos"]


class DefaultManagerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_execution_workspace: ExecutionWorkspaceInput | None = None


class ManagerController(BaseModel):
    session_id: str
    status: Literal["active", "paused"]
    revision: int
    default_execution_workspace: SessionWorkspace | None = None
    llm_client_name: str | None
    llm_model: str | None
    llm_answer_client_name: str | None = None
    llm_answer_model: str | None = None
    reasoning_level: Literal["off", "low", "high", "max"] | None = None
    created_at: str
    updated_at: str


class ManagerResponse(BaseModel):
    session: AgentSession
    controller: ManagerController


class CreateManagerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=40)
    make_default: bool = False
    default_execution_workspace: ExecutionWorkspaceInput | None = None
    llm_client_name: str | None = Field(default=None, min_length=1, max_length=200)
    llm_model: str | None = Field(default=None, min_length=1, max_length=200)
    llm_answer_client_name: str | None = Field(default=None, min_length=1, max_length=200)
    llm_answer_model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_level: Literal["off", "low", "high", "max"] | None = None

    @field_validator("title", "llm_client_name", "llm_model", "llm_answer_client_name", "llm_answer_model")
    @classmethod
    def trim_nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value.strip() if value is not None else None


class UpdateManagerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    default_execution_workspace: ExecutionWorkspaceInput | None = None
    status: Literal["active", "paused"] | None = None
    llm_client_name: str | None = Field(default=None, min_length=1, max_length=200)
    llm_model: str | None = Field(default=None, min_length=1, max_length=200)
    llm_answer_client_name: str | None = Field(default=None, min_length=1, max_length=200)
    llm_answer_model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_level: Literal["off", "low", "high", "max"] | None = None

    _trim_nonblank = field_validator("llm_client_name", "llm_model", "llm_answer_client_name", "llm_answer_model")(CreateManagerRequest.trim_nonblank.__func__)


def _service(request: Request):
    runtime = request.app.state.runtime
    if not runtime.local_app_config.management.enabled:
        raise HTTPException(409, detail="management_disabled")
    return runtime.management_service


def _validate_client(request: Request, client_name: str | None):
    if client_name is not None and client_name not in {
        client.name for client in request.app.state.runtime.local_app_config.llm.client_configs()
    }:
        raise HTTPException(422, detail="unknown_llm_client")


@router.post("/default", response_model=ManagerResponse)
def default_manager(request: Request, payload: DefaultManagerRequest | None = None):
    try:
        return _service(request).get_default(**payload.model_dump() if payload else {})
    except ManagementConflictError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.post("", response_model=ManagerResponse, status_code=201)
def create_manager(payload: CreateManagerRequest, request: Request):
    service = _service(request)
    _validate_client(request, payload.llm_client_name)
    _validate_client(request, payload.llm_answer_client_name)
    try:
        return service.create(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("")
def list_managers(request: Request, limit: int = Query(default=50, ge=1, le=200),
                  offset: int = Query(default=0, ge=0)) -> list[ManagerResponse]:
    return _service(request).list(limit=limit, offset=offset)


@router.get("/{session_id}", response_model=ManagerResponse)
def get_manager(session_id: str, request: Request):
    try:
        return _service(request).get(session_id)
    except KeyError as exc:
        raise HTTPException(404, detail="manager_not_found") from exc


@router.patch("/{session_id}", response_model=ManagerResponse)
def update_manager(session_id: str, payload: UpdateManagerRequest, request: Request):
    service = _service(request)
    _validate_client(request, payload.llm_client_name)
    _validate_client(request, payload.llm_answer_client_name)
    try:
        return service.update(session_id, **payload.model_dump(exclude_unset=True))
    except KeyError as exc:
        raise HTTPException(404, detail="manager_not_found") from exc
    except ManagementConflictError as exc:
        raise HTTPException(409, detail="management_revision_conflict") from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


class CreateGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=16000)
    status: Literal["active", "waiting", "paused", "completed", "cancelled"] = "active"
    next_wake_at: str | None = None
    wait_run_id: str | None = Field(default=None, min_length=1, max_length=256)


class UpdateGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    objective: str | None = Field(default=None, min_length=1, max_length=16000)
    status: Literal["active", "waiting", "paused", "completed", "cancelled"] | None = None
    next_wake_at: str | None = None
    wait_run_id: str | None = Field(default=None, min_length=1, max_length=256)


def _goal_operation(request, method, *args, **kwargs):
    _service(request)
    try:
        return getattr(request.app.state.runtime.management_goals, method)(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, detail="manager_or_goal_not_found") from exc
    except ManagementConflictError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.post("/{session_id}/goals", status_code=201)
def create_manager_goal(session_id: str, payload: CreateGoalRequest, request: Request):
    return _goal_operation(request, "create_goal", session_id, **payload.model_dump())


@router.get("/{session_id}/goals")
def list_manager_goals(session_id: str, request: Request,
                       limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    return _goal_operation(request, "list_goals", session_id, limit=limit, offset=offset)


@router.get("/{session_id}/goals/{goal_id}")
def get_manager_goal(session_id: str, goal_id: str, request: Request):
    return _goal_operation(request, "get_goal", session_id, goal_id)


@router.patch("/{session_id}/goals/{goal_id}")
def update_manager_goal(session_id: str, goal_id: str, payload: UpdateGoalRequest, request: Request):
    return _goal_operation(request, "update_goal", session_id, goal_id, **payload.model_dump(exclude_unset=True))


@router.get("/{session_id}/events")
def list_manager_events(session_id: str, request: Request,
                        after: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
    return _goal_operation(request, "list_events", session_id, after=after, limit=limit)


@router.get("/{session_id}/executions")
def list_manager_executions(session_id: str, request: Request,
                            after: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
    return _goal_operation(request, "list_executions", session_id, after=after, limit=limit)


@router.get("/{session_id}/deliveries")
def list_manager_deliveries(session_id: str, request: Request,
                            after: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
    return _goal_operation(request, "list_deliveries", session_id, after=after, limit=limit)
