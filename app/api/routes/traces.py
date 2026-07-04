from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import TraceRecordResponse

router = APIRouter(prefix="/traces", tags=["traces"])


@router.get("")
def list_traces(request: Request) -> list[dict[str, str]]:
    return request.app.state.runtime.list_traces()


@router.get("/{trace_id}", response_model=TraceRecordResponse)
def get_trace(trace_id: str, request: Request) -> TraceRecordResponse:
    trace = request.app.state.runtime.get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace

