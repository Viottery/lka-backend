from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.api.schemas import (
    KnowledgeChunkLoadRequest,
    KnowledgeChunkLoadResponse,
    KnowledgeDocumentResponse,
    KnowledgeImportRequest,
    KnowledgeImportResponse,
    KnowledgeSearchResponse,
    KnowledgeSemanticSyncRequest,
    KnowledgeSemanticSyncResponse,
    MailKnowledgeMirrorSyncRequest,
    MailKnowledgeMirrorSyncResponse,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post("/import", response_model=KnowledgeImportResponse)
def import_knowledge_document(
    payload: KnowledgeImportRequest,
    request: Request,
) -> KnowledgeImportResponse:
    result = request.app.state.runtime.import_knowledge_document(payload=payload)
    return KnowledgeImportResponse(**result.model_dump(mode="json"))


@router.get("/search", response_model=KnowledgeSearchResponse)
def search_knowledge(
    request: Request,
    response: Response = None,
    q: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
    source_type: Annotated[list[str] | None, Query()] = None,
    source_id: Annotated[list[str] | None, Query()] = None,
    mode: str | None = Query(default=None, pattern="^(keyword|semantic|hybrid)$"),
) -> KnowledgeSearchResponse:
    runtime = request.app.state.runtime
    if not isinstance(source_id, list):
        source_id = None
    if not isinstance(source_type, list):
        source_type = None
    if not isinstance(mode, str):
        mode = None
    known_message_sources = {row["source_id"] for row in runtime.message_history.source_inventory()}
    explicit_message = ((source_type is not None and "chat_message" in source_type)
                        or (source_id is not None and any(value in known_message_sources for value in source_id)))
    authenticated = False
    headers = getattr(request, "headers", {})
    token_supplied = bool(headers.get("x-lka-messages-token") or
                          headers.get("authorization", "").lower().startswith("bearer "))
    if explicit_message or token_supplied:
        from app.api.routes.message_reading import _reader
        _reader(request)
        authenticated = True
    if authenticated and response is not None:
        response.headers["Cache-Control"] = "no-store"
    result = runtime.knowledge_service.search(
        query=q, limit=limit, source_types=source_type, mode=mode, source_ids=source_id,
        provider_account_ids=None if authenticated else [],
    )
    return KnowledgeSearchResponse(**result.model_dump(mode="json"))


@router.post("/semantic-index/sync", response_model=KnowledgeSemanticSyncResponse)
def sync_knowledge_semantic_index(
    payload: KnowledgeSemanticSyncRequest,
    request: Request,
) -> KnowledgeSemanticSyncResponse:
    result = request.app.state.runtime.sync_knowledge_semantic_index(
        allow_model_download=payload.allow_model_download,
    )
    return KnowledgeSemanticSyncResponse(**result.model_dump(mode="json"))


@router.post("/mail-mirror/sync", response_model=MailKnowledgeMirrorSyncResponse)
def sync_mail_knowledge_mirror(
    payload: MailKnowledgeMirrorSyncRequest,
    request: Request,
) -> MailKnowledgeMirrorSyncResponse:
    result = request.app.state.runtime.sync_mail_knowledge_mirror(
        account_id=payload.account_id,
    )
    return MailKnowledgeMirrorSyncResponse(**result.model_dump(mode="json"))


@router.post("/chunks/load", response_model=KnowledgeChunkLoadResponse)
def load_knowledge_chunks(
    payload: KnowledgeChunkLoadRequest,
    request: Request,
    response: Response = None,
) -> KnowledgeChunkLoadResponse:
    runtime = request.app.state.runtime
    protected_ids = any(any(provider.handles_id(value) for provider in runtime.knowledge_service._source_providers)
                        for value in payload.chunk_ids)
    if protected_ids:
        from app.api.routes.message_reading import _reader
        _reader(request)
        if response is not None:
            response.headers["Cache-Control"] = "no-store"
    result = runtime.knowledge_service.load_chunks(
        chunk_ids=payload.chunk_ids,
        max_chars_per_chunk=payload.max_chars_per_chunk,
        offset=payload.offset,
        provider_account_ids=None if protected_ids else [],
    )
    return KnowledgeChunkLoadResponse(**result.model_dump(mode="json"))


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentResponse)
def load_knowledge_document(
    document_id: str,
    request: Request,
    response: Response = None,
    include_text: bool = Query(default=False),
    max_chars: int = Query(default=12000, ge=1, le=12000),
) -> KnowledgeDocumentResponse:
    runtime = request.app.state.runtime
    protected_id = any(provider.handles_id(document_id) for provider in runtime.knowledge_service._source_providers)
    if protected_id:
        from app.api.routes.message_reading import _reader
        _reader(request)
        if response is not None:
            response.headers["Cache-Control"] = "no-store"
    try:
        result = runtime.knowledge_service.load_document(
            document_id=document_id,
            include_text=include_text,
            max_chars=max_chars,
            provider_account_ids=None if protected_id else [],
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return KnowledgeDocumentResponse(**result.model_dump(mode="json"))
