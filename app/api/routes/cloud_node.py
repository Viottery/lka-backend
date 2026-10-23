"""Local controls for a cloud replica's durable, untrusted proposal outbox."""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.routes.memories import require_local_memory_control
from app.domains.shared_sync import NodeProposal, SyncConflictError

router = APIRouter(prefix="/sync/replica", tags=["sync"],
                   dependencies=[Depends(require_local_memory_control)])


def _worker(request):
    worker = getattr(request.app.state.runtime, "cloud_replica_sync", None)
    if worker is None:
        raise HTTPException(404, "cloud replica disabled")
    return worker


@router.get("/status")
def replica_status(request: Request):
    return _worker(request).status()


@router.post("/proposals", status_code=202)
def queue_proposal(payload: NodeProposal, request: Request):
    try:
        return _worker(request).outbox.enqueue(payload.model_dump())
    except SyncConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
