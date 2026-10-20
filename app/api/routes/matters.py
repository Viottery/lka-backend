from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.schemas import (
    MatterCreateRequest,
    MatterLinkSourceRequest,
    MatterListResponse,
    MatterRecordResponse,
    MatterSearchResponse,
    MatterUpdateRequest,
)
from app.domains.matters import MatterConflictError, MatterSyncPage

router = APIRouter(prefix="/matters", tags=["matters"])


@router.post("", response_model=MatterRecordResponse)
def create_matter(payload: MatterCreateRequest, request: Request) -> MatterRecordResponse:
    try:
        result = request.app.state.runtime.create_matter(payload=payload)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError:
        raise HTTPException(status_code=403, detail="This mutation requires its dedicated human review workflow.") from None
    return MatterRecordResponse(**result.model_dump())


@router.get("", response_model=MatterListResponse)
def list_matters(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    q: Annotated[str | None, Query(max_length=500)] = None,
    kind: Literal["task", "plan"] | None = None,
    visibility: Literal["user", "internal", "all"] = "user",
    parent_matter_id: str | None = None,
    project_id: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    unscheduled: bool = False,
    scheduled_on: str | None = None,
    scheduled_before: str | None = None,
    scheduled_after: str | None = None,
    inbox: bool | None = None,
    section: Annotated[str | None, Query(max_length=120)] = None,
) -> MatterListResponse:
    try:
        result = request.app.state.runtime.list_matters(
            visibility=visibility, limit=limit, status=status, offset=offset, q=q, kind=kind,
            parent_matter_id=parent_matter_id, project_id=project_id,
            due_before=due_before, due_after=due_after, unscheduled=unscheduled,
            scheduled_on=scheduled_on, scheduled_before=scheduled_before,
            scheduled_after=scheduled_after, inbox=inbox, section=section,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return MatterListResponse(**result.model_dump())


@router.get("/search", response_model=MatterSearchResponse)
def search_matters(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
    kind: Literal["task", "plan"] | None = None,
    visibility: Literal["user", "internal", "all"] = "user",
    parent_matter_id: str | None = None,
    project_id: str | None = None,
) -> MatterSearchResponse:
    result = request.app.state.runtime.search_matters(visibility=visibility, query=q, limit=limit, kind=kind, parent_matter_id=parent_matter_id, project_id=project_id)
    return MatterSearchResponse(**result.model_dump())


@router.get("/sync", response_model=MatterSyncPage)
def sync_matters(
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> MatterSyncPage:
    return request.app.state.runtime.matter_service.sync_matters(after=after, limit=limit)


@router.get("/{matter_id}", response_model=MatterRecordResponse)
def get_matter(matter_id: str, request: Request) -> MatterRecordResponse:
    try:
        result = request.app.state.runtime.matter_service.get_matter(matter_id=matter_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return MatterRecordResponse(**result.model_dump())


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
    except MatterConflictError as exc:
        raise HTTPException(status_code=409, detail="matter_revision_conflict") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError:
        raise HTTPException(status_code=403, detail="This mutation requires its dedicated human review workflow.") from None
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
    except PermissionError:
        raise HTTPException(status_code=403, detail="This mutation requires its dedicated human review workflow.") from None
    return MatterRecordResponse(**result.model_dump())
