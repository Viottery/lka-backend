from fastapi import APIRouter, HTTPException, Query, Request

from app.api.schemas import (
    SessionAppendMessageRequest,
    SessionAppendMessageResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionDetailResponse,
    SessionListResponse,
    SessionWorkspaceResponse,
    SessionWorkspaceUpdateRequest,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionCreateResponse)
def create_session(payload: SessionCreateRequest, request: Request) -> SessionCreateResponse:
    result = request.app.state.runtime.create_session(
        title=payload.title,
        metadata=payload.metadata,
        initial_message=payload.initial_message,
    )
    return SessionCreateResponse(**result.model_dump())


@router.get("", response_model=SessionListResponse)
def list_sessions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> SessionListResponse:
    result = request.app.state.runtime.list_sessions(limit=limit)
    return SessionListResponse(**result.model_dump())


@router.get("/{session_id}", response_model=SessionDetailResponse)
def get_session(session_id: str, request: Request) -> SessionDetailResponse:
    try:
        result = request.app.state.runtime.get_session(session_id=session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SessionDetailResponse(**result.model_dump())


@router.put("/{session_id}/workspace", response_model=SessionWorkspaceResponse)
def set_session_workspace(
    session_id: str,
    payload: SessionWorkspaceUpdateRequest,
    request: Request,
) -> SessionWorkspaceResponse:
    try:
        workspace = request.app.state.runtime.set_session_workspace(
            session_id=session_id,
            path=payload.path,
            platform=payload.platform,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SessionWorkspaceResponse(session_id=session_id, workspace=workspace)


@router.post("/{session_id}/messages", response_model=SessionAppendMessageResponse)
def append_session_message(
    session_id: str,
    payload: SessionAppendMessageRequest,
    request: Request,
) -> SessionAppendMessageResponse:
    result = request.app.state.runtime.append_session_message(
        session_id=session_id,
        role=payload.role,
        content=payload.content,
        payload=payload.payload,
    )
    return SessionAppendMessageResponse(**result.model_dump())
