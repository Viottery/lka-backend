from fastapi import APIRouter, HTTPException, Query, Request

from app.api.schemas import (
    KnowledgeChunkLoadRequest,
    KnowledgeChunkLoadResponse,
    KnowledgeDocumentResponse,
    KnowledgeImportRequest,
    KnowledgeImportResponse,
    MailKnowledgeMirrorSyncRequest,
    MailKnowledgeMirrorSyncResponse,
    KnowledgeSearchResponse,
    KnowledgeSemanticSyncRequest,
    KnowledgeSemanticSyncResponse,
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
    q: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
    source_type: list[str] | None = Query(default=None),
    mode: str | None = Query(default=None, pattern="^(keyword|semantic|hybrid)$"),
) -> KnowledgeSearchResponse:
    result = request.app.state.runtime.search_knowledge(
        query=q,
        limit=limit,
        source_types=source_type,
        mode=mode,
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
) -> KnowledgeChunkLoadResponse:
    result = request.app.state.runtime.load_knowledge_chunks(
        chunk_ids=payload.chunk_ids,
        max_chars_per_chunk=payload.max_chars_per_chunk,
    )
    return KnowledgeChunkLoadResponse(**result.model_dump(mode="json"))


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentResponse)
def load_knowledge_document(
    document_id: str,
    request: Request,
    include_text: bool = Query(default=False),
    max_chars: int = Query(default=12000, ge=1, le=12000),
) -> KnowledgeDocumentResponse:
    try:
        result = request.app.state.runtime.load_knowledge_document(
            document_id=document_id,
            include_text=include_text,
            max_chars=max_chars,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return KnowledgeDocumentResponse(**result.model_dump(mode="json"))
