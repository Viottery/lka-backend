from fastapi import APIRouter, Request

from app.api.schemas import CapabilityListResponse

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities", response_model=CapabilityListResponse)
def list_capabilities(request: Request) -> CapabilityListResponse:
    return CapabilityListResponse(capabilities=request.app.state.runtime.list_capabilities())

