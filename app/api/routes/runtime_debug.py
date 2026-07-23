from fastapi import APIRouter, Request

from app.api.schemas import RuntimeDebugRequest, RuntimeDebugResponse

router = APIRouter(prefix="/runtime", tags=["runtime"])


@router.post("/debug", response_model=RuntimeDebugResponse)
def run_debug(payload: RuntimeDebugRequest, request: Request) -> RuntimeDebugResponse:
    result = request.app.state.runtime.run_debug(
        session_id=payload.session_id,
        workspace=payload.workspace,
        user_input=payload.user_input,
    )
    return RuntimeDebugResponse(**result.model_dump())
