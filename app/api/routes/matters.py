from fastapi import APIRouter, HTTPException, Query, Request

from app.api.schemas import (
    MatterCreateRequest,
    MatterLinkSourceRequest,
    MatterListResponse,
    MatterRecordResponse,
    MatterSearchResponse,
    MatterUpdateRequest,
)

router = APIRouter(prefix="/matters", tags=["matters"])


@router.post("", response_model=MatterRecordResponse)
def create_matter(payload: MatterCreateRequest, request: Request) -> MatterRecordResponse:
    result = request.app.state.runtime.create_matter(payload=payload)
    return MatterRecordResponse(**result.model_dump())


@router.get("", response_model=MatterListResponse)
def list_matters(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
) -> MatterListResponse:
    result = request.app.state.runtime.list_matters(limit=limit, status=status)
    return MatterListResponse(**result.model_dump())


@router.get("/search", response_model=MatterSearchResponse)
def search_matters(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
) -> MatterSearchResponse:
    result = request.app.state.runtime.search_matters(query=q, limit=limit)
    return MatterSearchResponse(**result.model_dump())


@router.patch("/{matter_id}", response_model=MatterRecordResponse)
def update_matter(
    matter_id: str,
    payload: MatterUpdateRequest,
    request: Request,
) -> MatterRecordResponse:
    try:
        result = request.app.state.runtime.update_matter(
            matter_id=matter_id,
            payload=payload,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return MatterRecordResponse(**result.model_dump())


@router.post("/{matter_id}/source-links", response_model=MatterRecordResponse)
def link_matter_source(
    matter_id: str,
    payload: MatterLinkSourceRequest,
    request: Request,
) -> MatterRecordResponse:
    try:
        result = request.app.state.runtime.link_matter_source(
            matter_id=matter_id,
            source_link=payload,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return MatterRecordResponse(**result.model_dump())
