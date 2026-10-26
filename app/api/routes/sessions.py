from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app.api.routes.memories import require_local_memory_control
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
from app.core.sessions import (
    AgentSessionMessagePage,
    SessionCreationSource,
    SessionHistoryCursorError,
    SessionMessageView,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=40)

    @field_validator("title")
    @classmethod
    def require_non_whitespace_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title must not be empty or whitespace")
        return title


@router.post("", response_model=SessionCreateResponse)
def create_session(payload: SessionCreateRequest, request: Request) -> SessionCreateResponse:
    if payload.project_id is not None or payload.metadata.get("project_id") is not None:
        require_local_memory_control(request)
    try:
        result = request.app.state.runtime.create_session(
            title=payload.title, metadata=payload.metadata,
            initial_message=payload.initial_message, project_id=payload.project_id,
        )
    except KeyError as exc:
        raise HTTPException(404, detail="project not found") from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return SessionCreateResponse(**result.model_dump())


@router.get("", response_model=SessionListResponse)
def list_sessions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = 0,
    q: str | None = None,
    project_id: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    creation_source: SessionCreationSource | None = None,
    creator_session_id: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
) -> SessionListResponse:
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be non-negative")
    if q is not None and len(q) > 500:
        raise HTTPException(status_code=422, detail="q must be at most 500 characters")
    if project_id is not None:
        require_local_memory_control(request)
    result = request.app.state.runtime.list_sessions(
        limit=limit, offset=offset, q=q, project_id=project_id,
        creation_source=creation_source, creator_session_id=creator_session_id,
        **({"ordinary_only": True} if getattr(getattr(request, "state", None), "ordinary_sessions_only", False) else {}),
    )
    return SessionListResponse(**result.model_dump())


@router.get("/deleted", response_model=SessionListResponse)
def list_deleted_sessions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = 0,
    q: str | None = None,
    creation_source: SessionCreationSource | None = None,
    creator_session_id: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
) -> SessionListResponse:
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be non-negative")
    if q is not None and len(q) > 500:
        raise HTTPException(status_code=422, detail="q must be at most 500 characters")
    result = request.app.state.runtime.list_deleted_sessions(
        limit=limit, offset=offset, q=q,
        creation_source=creation_source, creator_session_id=creator_session_id,
        **({"ordinary_only": True} if getattr(getattr(request, "state", None), "ordinary_sessions_only", False) else {}),
    )
    return SessionListResponse(**result.model_dump())


@router.post("/{session_id}/restore", response_model=SessionDetailResponse)
def restore_session(session_id: str, request: Request) -> SessionDetailResponse:
    restored = request.app.state.runtime.restore_session(session_id=session_id)
    if not restored:
        raise HTTPException(status_code=404, detail=f"Deleted session not found: {session_id}")
    result = request.app.state.runtime.get_session(session_id=session_id)
    return SessionDetailResponse(**result.model_dump())


@router.get("/{session_id}", response_model=AgentSessionMessagePage | SessionDetailResponse)
def get_session(
    session_id: str, request: Request,
    message_limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    before_message_id: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    after_message_id: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    around_message_id: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    message_view: SessionMessageView = "all",
) -> SessionDetailResponse | AgentSessionMessagePage:
    try:
        if message_view != "all" and message_limit is None:
            raise HTTPException(422, detail="message_view requires message_limit")
        cursors = (before_message_id, after_message_id, around_message_id)
        if any(cursors) and (
            message_limit is None or sum(bool(cursor) for cursor in cursors) > 1
        ):
            raise HTTPException(422, detail="Use message_limit and at most one message cursor")
        if message_limit is not None:
            return request.app.state.runtime.session_service.get_message_page(
                session_id=session_id, limit=message_limit,
                before_message_id=before_message_id, after_message_id=after_message_id,
                **({"around_message_id": around_message_id} if around_message_id else {}),
                message_view=message_view,
            )
        result = request.app.state.runtime.get_session(session_id=session_id)
    except SessionHistoryCursorError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SessionDetailResponse(**result.model_dump())


@router.get("/{session_id}/runs")
def list_session_runs(
    session_id: str, request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    client_request_id: Annotated[
        str | None, Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    ] = None,
) -> dict:
    """Server-side run discovery, independent of a device's SSE cursor."""
    try:
        return request.app.state.runtime.managed_tasks.list_runs(
            session_id, limit=limit, offset=offset,
            **({"client_request_id": client_request_id} if client_request_id is not None else {}),
        )
    except KeyError as exc:
        raise HTTPException(404, detail="Session not found") from exc


@router.patch("/{session_id}", response_model=SessionDetailResponse)
def rename_session(
    session_id: str,
    payload: SessionRenameRequest,
    request: Request,
) -> SessionDetailResponse:
    try:
        result = request.app.state.runtime.rename_session(
            session_id=session_id,
            title=payload.title,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SessionDetailResponse(**result.model_dump())


@router.delete("/{session_id}", status_code=204)
def delete_session(
    session_id: str, request: Request, only_if_empty: bool = False,
    expected_updated_at: str | None = None,
) -> None:
    try:
        deleted = request.app.state.runtime.delete_session(
            session_id=session_id, only_if_empty=only_if_empty,
            expected_updated_at=expected_updated_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")


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
            reserve=payload.reserve,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session = request.app.state.runtime.session_service.get_session_or_none(session_id=session_id)
    return SessionWorkspaceResponse(session_id=session_id, workspace=workspace,
                                    project_id=session.project_id if session else None)


@router.post("/{session_id}/workspace/materialize", response_model=SessionWorkspaceResponse)
def materialize_session_workspace(session_id: str, request: Request) -> SessionWorkspaceResponse:
    """Create a reserved session workspace on explicit user request (upload, file panel)."""
    runtime = request.app.state.runtime
    try:
        workspace = runtime.materialize_session_workspace(session_id=session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot create session workspace: {exc}") from exc
    session = runtime.session_service.get_session_or_none(session_id=session_id)
    return SessionWorkspaceResponse(session_id=session_id, workspace=workspace,
                                    project_id=session.project_id if session else None)


@router.post("/{session_id}/messages", response_model=SessionAppendMessageResponse)
def append_session_message(
    session_id: str,
    payload: SessionAppendMessageRequest,
    request: Request,
) -> SessionAppendMessageResponse:
    try:
        result = request.app.state.runtime.append_session_message(
            session_id=session_id,
            role=payload.role,
            content=payload.content,
            payload=payload.payload,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SessionAppendMessageResponse(**result.model_dump())
