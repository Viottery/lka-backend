from fastapi import APIRouter, Query, Request

from app.api.schemas import (
    MailImportRequest,
    MailImportResponse,
    MailMatterListResponse,
    MailProcessRequest,
    MailProcessResponse,
    MailSearchResponse,
)

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


@router.post("/process", response_model=MailProcessResponse)
def process_mail(payload: MailProcessRequest, request: Request) -> MailProcessResponse:
    result = request.app.state.runtime.process_mail(query=payload.query, limit=payload.limit)
    return MailProcessResponse(**result.model_dump())


@router.get("/matters", response_model=MailMatterListResponse)
def list_mail_matters(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> MailMatterListResponse:
    result = request.app.state.runtime.list_mail_matters(limit=limit)
    return MailMatterListResponse(**result.model_dump())
