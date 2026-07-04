from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import TaskPlanRequest, TaskPlanResponse, TaskRecordResponse, TaskRunRequest, TaskRunResponse

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("/plan", response_model=TaskPlanResponse)
def plan_task(payload: TaskPlanRequest, request: Request) -> TaskPlanResponse:
    return request.app.state.runtime.plan_task(
        payload.task,
        workspace=payload.workspace,
        frontend=payload.frontend,
    )


@router.post("/run", response_model=TaskRunResponse)
def run_task(payload: TaskRunRequest, request: Request) -> TaskRunResponse:
    return request.app.state.runtime.run_task(
        payload.task,
        workspace=payload.workspace,
        frontend=payload.frontend,
        mode=payload.mode,
    )


@router.get("/{task_id}", response_model=TaskRecordResponse)
def get_task(task_id: str, request: Request) -> TaskRecordResponse:
    task = request.app.state.runtime.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task

