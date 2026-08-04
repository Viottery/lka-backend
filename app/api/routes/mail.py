from fastapi import APIRouter, HTTPException, Query, Request

from app.api.schemas import (
    MailImportRequest,
    MailImportResponse,
    MailMatterListResponse,
    MailSearchResponse,
    OutlookAuthCompleteRequest,
    OutlookAuthCompleteResponse,
    OutlookAuthStartResponse,
    OutlookSyncRequest,
    OutlookSyncResponse,
)
from app.core.outlook import OutlookConfigError, OutlookRemoteError, OutlookServiceError

router = APIRouter(prefix="/mail", tags=["mail"])


@router.post("/import", response_model=MailImportResponse)
def import_mail(payload: MailImportRequest, request: Request) -> MailImportResponse:
    result = request.app.state.runtime.import_mail(
        account=payload.account,
        messages=payload.messages,
    )
    return MailImportResponse(**result.model_dump())


@router.get("/search", response_model=MailSearchResponse)
def search_mail(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
) -> MailSearchResponse:
    result = request.app.state.runtime.search_mail(query=q, limit=limit)
    return MailSearchResponse(**result.model_dump())


@router.get("/matters", response_model=MailMatterListResponse)
def list_mail_matters(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> MailMatterListResponse:
    result = request.app.state.runtime.list_mail_matters(limit=limit)
    return MailMatterListResponse(**result.model_dump())


@router.post("/outlook/auth/start", response_model=OutlookAuthStartResponse)
def start_outlook_auth(request: Request) -> OutlookAuthStartResponse:
    try:
        result = request.app.state.runtime.start_outlook_auth()
    except OutlookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OutlookServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return OutlookAuthStartResponse(**result.model_dump())


@router.post("/outlook/auth/complete", response_model=OutlookAuthCompleteResponse)
def complete_outlook_auth(
    payload: OutlookAuthCompleteRequest,
    request: Request,
) -> OutlookAuthCompleteResponse:
    try:
        result = request.app.state.runtime.complete_outlook_auth(device_code=payload.device_code)
    except OutlookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OutlookServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return OutlookAuthCompleteResponse(**result.model_dump())


@router.post("/outlook/sync", response_model=OutlookSyncResponse)
def sync_outlook_mail(payload: OutlookSyncRequest, request: Request) -> OutlookSyncResponse:
    try:
        result = request.app.state.runtime.sync_outlook_mail(
            folder=payload.folder,
            limit=payload.limit,
            max_pages=payload.max_pages,
        )
    except OutlookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OutlookRemoteError, OutlookServiceError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return OutlookSyncResponse(**result.model_dump())
