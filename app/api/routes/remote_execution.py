"""Account control and credential-scoped node polling for delegated Agent turns."""
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes.devices import _owner
from app.domains.devices import DeviceUnavailableError
from app.domains.remote_execution import ExecutionFence, ExecutionReport, RemoteExecutionConflict

router = APIRouter(prefix="/remote-execution", tags=["remote-execution"])
node_router = APIRouter(prefix="/node/execution", tags=["remote-execution"])


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment_id: str = Field(min_length=1, max_length=200)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str | None = Field(default=None, max_length=4000)


class ControlCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=120, pattern=r"^[\w.-]+$")


class UserAnswer(ControlCommand):
    question_id: str = Field(min_length=1, max_length=256)
    answer: str = Field(min_length=1, max_length=16000)


class FollowupInstruction(ControlCommand):
    expected_run_id: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=16000)


def _service(request):
    service = getattr(request.app.state.runtime, "remote_execution_service", None)
    if service is None:
        raise HTTPException(404, "remote execution disabled")
    return service


def _principal(request):
    principal = getattr(request.state, "device", None)
    if not isinstance(principal, dict):
        raise HTTPException(401, "device authentication required")
    return principal


def _call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, "execution task or review not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (RemoteExecutionConflict, DeviceUnavailableError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/targets")
def execution_targets(request: Request):
    _owner(request)
    return _call(_service(request).execution_targets)


@router.get("/tasks")
def list_tasks(request: Request, owner_session_id: str | None = Query(default=None, max_length=200),
               limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    _owner(request)
    return _call(_service(request).list, owner_session_id=owner_session_id, limit=limit, offset=offset)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request, limit: int = Query(default=20, ge=1, le=100),
             offset: int = Query(default=0, ge=0)):
    _owner(request)
    return _call(_service(request).get, task_id, limit=limit, offset=offset)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    _owner(request)
    return _call(_service(request).cancel, task_id)


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: str, payload: ControlCommand, request: Request):
    owner = _owner(request)
    return _call(_service(request).control, task_id, command_id=owner + ":" + payload.command_id, action="pause")


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str, payload: ControlCommand, request: Request):
    owner = _owner(request)
    return _call(_service(request).control, task_id, command_id=owner + ":" + payload.command_id, action="resume")


@router.post("/tasks/{task_id}/answer")
def answer_question(task_id: str, payload: UserAnswer, request: Request):
    owner = _owner(request)
    return _call(_service(request).control, task_id, command_id=owner + ":" + payload.command_id,
                 action="answer", question_id=payload.question_id, answer=payload.answer)


@router.post("/tasks/{task_id}/instruction")
def send_instruction(task_id: str, payload: FollowupInstruction, request: Request):
    owner = _owner(request)
    return _call(_service(request).account_instruction, task_id, **payload.model_dump(), actor=owner)


@router.post("/tasks/{task_id}/reviews/{review_id}")
def decide_review(task_id: str, review_id: str, payload: ReviewDecision, request: Request):
    owner = _owner(request)
    return _call(_service(request).decide_review, task_id, review_id, **payload.model_dump(), decided_by=owner)


@node_router.post("/claim")
def claim(payload: ClaimRequest, request: Request):
    return _call(_service(request).claim, _principal(request), environment_id=payload.environment_id)


@node_router.post("/start")
def start(payload: ExecutionFence, request: Request):
    return _call(_service(request).check, _principal(request), payload, start=True)


@node_router.post("/check")
def check(payload: ExecutionFence, request: Request):
    return _call(_service(request).check, _principal(request), payload)


@node_router.post("/report")
def report(payload: ExecutionReport, request: Request):
    return _call(_service(request).report, _principal(request), payload)
