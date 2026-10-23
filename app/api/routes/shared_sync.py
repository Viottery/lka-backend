"""Account-only shared projections and explicitly scoped device transport."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from app.domains.shared_sync import NodeProposal, SyncConflictError

router = APIRouter(prefix="/sync", tags=["shared sync"])
node_router = APIRouter(prefix="/node/sync", tags=["shared sync"])


def _admin(request):
    account = getattr(request.state, "account", None)
    if not isinstance(account, dict) or not account.get("user_id"):
        raise HTTPException(401, "account authentication required")


def _node(request):
    principal = getattr(request.state, "device", None)
    if not isinstance(principal, dict):
        raise HTTPException(401, "device authentication required")
    return principal


def _service(request):
    service = getattr(request.app.state, "sync_service", None)
    if service is None or not service.config.enabled or service.config.role != "hub":
        raise HTTPException(404, "shared sync hub disabled")
    return service


def _call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except SyncConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "proposal not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/status")
def status(request: Request):
    _admin(request)
    return _call(_service(request).status)


@router.get("/changes")
def changes(
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    _admin(request)
    return _call(_service(request).pull, after=after, limit=limit)


@node_router.get("/changes")
def node_changes(
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    return _call(_service(request).pull, after=after, limit=limit, principal=_node(request))


@node_router.get("/status")
def node_status(request: Request):
    return _call(_service(request).status, principal=_node(request))


@node_router.post("/proposals", status_code=201)
def propose(payload: NodeProposal, request: Request):
    return _call(_service(request).propose, _node(request), payload)


@router.get("/proposals")
def proposals(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    _admin(request)
    return _call(_service(request).proposals, limit)


class ProposalResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["rejected", "reviewed"]


@router.post("/proposals/{node_id}/{proposal_id}/resolve")
def resolve_proposal(node_id: str, proposal_id: str, payload: ProposalResolution, request: Request):
    _admin(request)
    return _call(_service(request).resolve_proposal, node_id, proposal_id, payload.status)


@router.get("/replica/records")
def replica_records(
    request: Request,
    kind: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    _admin(request)
    store = getattr(request.app.state, "shared_replica", None)
    if store is None:
        raise HTTPException(404, "shared replica unavailable")
    return _call(store.list_records, kind, limit, offset)
