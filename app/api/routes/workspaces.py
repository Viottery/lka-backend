from fastapi import APIRouter, Request

from app.api.schemas import WorkspaceIndexRequest, WorkspaceIndexResponse

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("/index", response_model=WorkspaceIndexResponse)
def index_workspace(payload: WorkspaceIndexRequest, request: Request) -> WorkspaceIndexResponse:
    return request.app.state.runtime.index_workspace(
        payload.workspace,
        source_frontend=payload.source_frontend,
        options=payload.options,
    )

