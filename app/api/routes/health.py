from fastapi import APIRouter, Request

from app.api.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
def health(request: Request) -> HealthResponse:
    return HealthResponse(**request.app.state.runtime.health())
