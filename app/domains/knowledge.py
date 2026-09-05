"""Local source-agnostic knowledge storage and retrieval."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domains.knowledge_retrieval import (
    CallableCandidateRetriever,
    ConfigurableKnowledgeRetriever,
    EmbeddingProvider,
    RetrievalCandidate,
    SemanticCandidateRetriever,
    SemanticIndex,
)


KnowledgeSensitivity = Literal["public", "personal", "sensitive", "secret"]
KnowledgeRemotePolicy = Literal["allow", "redact", "confirm", "deny"]

DEFAULT_CHUNK_CHARS = 1800
MAX_CHUNK_CHARS = 6000
DEFAULT_SNIPPET_CHARS = 420
MAX_SNIPPET_CHARS = 1200
MAX_LOAD_CHUNKS = 20
MAX_LOAD_DOCUMENT_CHARS = 12000
SECRET_PLACEHOLDER = "[REDACTED SECRET-LIKE CONTENT]"

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]{8,}", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


class KnowledgeSourceInput(BaseModel):
    source_type: str = "local_document"
    display_name: str
    uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    sensitivity: KnowledgeSensitivity = "personal"
    remote_policy: KnowledgeRemotePolicy = "redact"
    access_scope: dict[str, Any] = Field(default_factory=dict)


class KnowledgeDocumentInput(BaseModel):
    source: KnowledgeSourceInput
    title: str
    text: str
    uri: str | None = None
    mime_type: str = "text/plain"
    metadata: dict[str, Any] = Field(default_factory=dict)
    sensitivity: KnowledgeSensitivity | None = None
    remote_policy: KnowledgeRemotePolicy | None = None
    source_ref: str | None = None
    chunk_chars: int = Field(default=DEFAULT_CHUNK_CHARS, ge=200, le=MAX_CHUNK_CHARS)


class KnowledgeImportResult(BaseModel):
    source_id: str
    document_id: str
    imported_chunks: int
    checksum: str
    sensitivity: KnowledgeSensitivity
    remote_policy: KnowledgeRemotePolicy
    secret_chunks_redacted: int


class KnowledgeSearchItem(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    source_type: str
    uri: str | None = None
    chunk_index: int
    snippet: str
    source_ref: str
    sensitivity: KnowledgeSensitivity
    remote_policy: KnowledgeRemotePolicy
    policy_decision: str
    retrieval_channels: list[str] = Field(default_factory=list)
    retrieval_score: float | None = None
    untrusted_data: bool = True


class KnowledgeSearchResult(BaseModel):
    query: str
    query_id: str
    results: list[KnowledgeSearchItem]
    filtered_count: int = 0
    requested_mode: str = "keyword"
    applied_mode: str = "keyword"
    retrieval_warning: str | None = None


class KnowledgeChunkRecord(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    source_type: str
    uri: str | None = None
    chunk_index: int
    text: str
    char_count: int
    token_estimate: int
    source_ref: str
    sensitivity: KnowledgeSensitivity
    remote_policy: KnowledgeRemotePolicy
    policy_decision: str
    untrusted_data: bool = True


class KnowledgeChunkLoadResult(BaseModel):
    query_id: str
    chunks: list[KnowledgeChunkRecord]
    filtered_count: int = 0


class KnowledgeDocumentRecord(BaseModel):
    document_id: str
    source_id: str
    source_type: str
    title: str
    uri: str | None = None
    checksum: str
    mime_type: str
    status: str
    sensitivity: KnowledgeSensitivity
    remote_policy: KnowledgeRemotePolicy
    source_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_ids: list[str] = Field(default_factory=list)
    text: str | None = None
    truncated: bool = False
    policy_decision: str
    untrusted_data: bool = True


class KnowledgeSemanticSyncResult(BaseModel):
    enabled: bool
    index_key: str | None = None
    scanned_chunks: int = 0
    embedded_chunks: int = 0
    updated_chunks: int = 0
    removed_chunks: int = 0
    indexed_chunks: int = 0
    reason: str | None = None


class KnowledgeService:
    """Local-first document/chunk storage with privacy-aware retrieval output."""

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        *,
        embedding_provider: EmbeddingProvider | None = None,
        semantic_index: SemanticIndex | None = None,
        default_retrieval_mode: str = "hybrid",
        auto_index_on_import: bool = False,
    ) -> None:
        self._conn_factory = conn_factory
        self.privacy_gateway = PrivacyGateway()
        self._embedding_provider = embedding_provider
        self._semantic_index = semantic_index
        self._default_retrieval_mode = default_retrieval_mode
        self._auto_index_on_import = auto_index_on_import

    def import_text_document(self, payload: KnowledgeDocumentInput) -> KnowledgeImportResult:
        source = payload.source
        source_id = _stable_id(
            "knowledge_source",
            source.source_type,
            source.uri or source.display_name,
        )
        text = payload.text.strip()
        checksum = sha256(text.encode("utf-8")).hexdigest()
        document_uri = payload.uri or source.uri
        document_id = _stable_id(
            "knowledge_doc",
            source_id,
            document_uri or payload.title,
            checksum,
        )
        sensitivity = payload.sensitivity or source.sensitivity
        remote_policy = payload.remote_policy or source.remote_policy
        source_ref = payload.source_ref or self._source_ref(
            source_type=source.source_type,
            uri=document_uri,
            title=payload.title,
        )
        now = _now_iso()
        chunks = self._chunk_text(text, payload.chunk_chars)
        secret_chunks_redacted = 0

        removed_chunk_ids: list[str] = []
        committed = False
        conn = self._conn_factory()
        try:
            removed_chunk_ids = self._delete_replaced_documents(
                conn,
                source_id=source_id,
                document_uri=document_uri,
                keep_document_id=document_id,
            )
            conn.execute(
                """
                INSERT INTO knowledge_sources(
                    source_id, source_type, display_name, uri, metadata, status,
                    sensitivity, remote_policy, access_scope, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    uri=excluded.uri,
                    metadata=excluded.metadata,
                    status=excluded.status,
                    sensitivity=excluded.sensitivity,
                    remote_policy=excluded.remote_policy,
                    access_scope=excluded.access_scope,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    source.source_type,
                    source.display_name,
                    source.uri,
                    self._json(source.metadata),
                    source.status,
                    source.sensitivity,
                    source.remote_policy,
                    self._json(source.access_scope),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO knowledge_documents(
                    document_id, source_id, source_type, title, uri, checksum, mime_type,
                    status, sensitivity, remote_policy, source_ref, created_at, updated_at,
                    indexed_at, metadata
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    title=excluded.title,
                    uri=excluded.uri,
                    checksum=excluded.checksum,
                    mime_type=excluded.mime_type,
                    status=excluded.status,
                    sensitivity=excluded.sensitivity,
                    remote_policy=excluded.remote_policy,
                    source_ref=excluded.source_ref,
                    updated_at=excluded.updated_at,
                    indexed_at=excluded.indexed_at,
                    metadata=excluded.metadata
                """,
                (
                    document_id,
                    source_id,
                    source.source_type,
                    payload.title,
                    document_uri,
                    checksum,
                    payload.mime_type,
                    "active",
                    sensitivity,
                    remote_policy,
                    source_ref,
                    now,
                    now,
                    now,
                    self._json(payload.metadata),
                ),
            )
            removed_chunk_ids.extend(self._delete_document_chunks(conn, document_id))
            for index, chunk_text in enumerate(chunks):
                chunk_sensitivity = sensitivity
                chunk_remote_policy = remote_policy
                chunk_metadata: dict[str, Any] = {}
                stored_text = chunk_text
                if self.privacy_gateway.contains_secret(chunk_text):
                    chunk_sensitivity = "secret"
                    chunk_remote_policy = "deny"
                    chunk_metadata["secret_detected"] = True
                    stored_text = SECRET_PLACEHOLDER
                    secret_chunks_redacted += 1
                chunk_id = _stable_id("knowledge_chunk", document_id, str(index))
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks(
                        chunk_id, document_id, chunk_index, text, char_count,
                        token_estimate, source_ref, sensitivity, remote_policy,
                        metadata, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        document_id,
                        index,
                        stored_text,
                        len(stored_text),
                        self._estimate_tokens(stored_text),
                        f"{source_ref}#chunk={index}",
                        chunk_sensitivity,
                        chunk_remote_policy,
                        self._json(chunk_metadata),
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks_fts(
                        chunk_id, document_id, title, text, source_type
                    )
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (chunk_id, document_id, payload.title, stored_text, source.source_type),
                )
            conn.commit()
            committed = True
        finally:
            conn.close()

        if committed and removed_chunk_ids and self._semantic_index is not None:
            self._semantic_index.remove(removed_chunk_ids)
        if self._auto_index_on_import:
            self.sync_semantic_index(chunk_ids=[
                _stable_id("knowledge_chunk", document_id, str(index))
                for index in range(len(chunks))
            ])

        return KnowledgeImportResult(
            source_id=source_id,
            document_id=document_id,
            imported_chunks=len(chunks),
            checksum=checksum,
            sensitivity=sensitivity,
            remote_policy=remote_policy,
            secret_chunks_redacted=secret_chunks_redacted,
        )

    def import_text_file(
        self,
        *,
        path: Path,
        source_type: str = "local_document",
        sensitivity: KnowledgeSensitivity = "personal",
        remote_policy: KnowledgeRemotePolicy = "redact",
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgeImportResult:
        text = path.read_text(encoding="utf-8")
        return self.import_text_document(
            KnowledgeDocumentInput(
                source=KnowledgeSourceInput(
                    source_type=source_type,
                    display_name=path.name,
                    uri=path.as_posix(),
                    metadata=metadata or {},
                    sensitivity=sensitivity,
                    remote_policy=remote_policy,
                ),
                title=path.name,
                uri=path.as_posix(),
                text=text,
                mime_type=self._mime_type(path),
                metadata=metadata or {},
                sensitivity=sensitivity,
                remote_policy=remote_policy,
            )
        )

    def search(
        self,
        *,
        query: str,
        limit: int = 10,
        source_types: list[str] | None = None,
        max_snippet_chars: int = DEFAULT_SNIPPET_CHARS,
        tool_name: str | None = None,
        mode: str | None = None,
        sort_by: Literal["relevance", "source_time_desc"] = "relevance",
        distinct_documents: bool = False,
    ) -> KnowledgeSearchResult:
        query_id = _stable_id("knowledge_query", query, _now_iso())
        requested_mode = mode or self._default_retrieval_mode
        keyword_retriever = CallableCandidateRetriever(
            channel="keyword",
            callback=lambda candidate_query, candidate_limit: self._keyword_candidates(
                query=candidate_query,
                limit=candidate_limit,
                source_types=source_types,
                sort_by=sort_by,
            ),
        )
        retrievers = [keyword_retriever]
        if self._embedding_provider is not None and self._semantic_index is not None:
            retrievers.append(
                SemanticCandidateRetriever(
                    provider=self._embedding_provider,
                    index=self._semantic_index,
                )
            )
        selection = ConfigurableKnowledgeRetriever(retrievers=retrievers).retrieve(
            query=query,
            limit=max(limit * 4, limit),
            mode=requested_mode,
        )
        rows = self._rows_for_chunk_ids(
            chunk_ids=[candidate.chunk_id for candidate in selection.candidates],
            source_types=source_types,
        )
        candidates_by_id = {candidate.chunk_id: candidate for candidate in selection.candidates}
        results: list[KnowledgeSearchItem] = []
        filtered_count = 0
        seen_document_ids: set[str] = set()
        for row in rows:
            document_id = str(row["document_id"])
            if distinct_documents and document_id in seen_document_ids:
                continue
            candidate = candidates_by_id[str(row["chunk_id"])]
            decision = self.privacy_gateway.decide(
                sensitivity=row["sensitivity"],
                remote_policy=row["remote_policy"],
                text=row["snippet"],
            )
            if not decision.allowed:
                filtered_count += 1
                continue
            results.append(
                KnowledgeSearchItem(
                    chunk_id=row["chunk_id"],
                    document_id=row["document_id"],
                    title=row["title"],
                    source_type=row["source_type"],
                    uri=row["uri"],
                    chunk_index=row["chunk_index"],
                    snippet=self._trim(decision.text, max_snippet_chars),
                    source_ref=row["source_ref"],
                    sensitivity=row["sensitivity"],
                    remote_policy=row["remote_policy"],
                    policy_decision=decision.policy_decision,
                    retrieval_channels=candidate.channel.split("+"),
                    retrieval_score=candidate.score,
                )
            )
            seen_document_ids.add(document_id)
            if len(results) >= limit:
                break
        self._audit(
            query_id=query_id,
            tool_name=tool_name,
            action="search",
            result_count=len(results),
            policy_decision="filtered" if filtered_count else "allowed",
            metadata={
                "query": query,
                "filtered_count": filtered_count,
                "requested_mode": selection.requested_mode,
                "applied_mode": selection.applied_mode,
                "retrieval_warning": selection.warning,
                "sort_by": sort_by,
                "distinct_documents": distinct_documents,
            },
        )
        return KnowledgeSearchResult(
            query=query,
            query_id=query_id,
            results=results,
            filtered_count=filtered_count,
            requested_mode=selection.requested_mode,
            applied_mode=selection.applied_mode,
            retrieval_warning=selection.warning,
        )

    def sync_semantic_index(
        self,
        *,
        chunk_ids: list[str] | None = None,
        allow_model_download: bool = False,
    ) -> KnowledgeSemanticSyncResult:
        if self._embedding_provider is None or self._semantic_index is None:
            return KnowledgeSemanticSyncResult(
                enabled=False,
                reason="semantic retrieval is not configured",
            )
        self._embedding_provider.prepare(allow_model_download=allow_model_download)
        conn = self._conn_factory()
        try:
            where_clause = ""
            params: list[Any] = []
            if chunk_ids:
                placeholders = ", ".join("?" for _ in chunk_ids)
                where_clause = f"AND c.chunk_id IN ({placeholders})"
                params.extend(chunk_ids)
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, c.text
                FROM knowledge_chunks c
                JOIN knowledge_documents d ON d.document_id = c.document_id
                WHERE d.status = 'active'
                  AND c.sensitivity != 'secret'
                  AND c.remote_policy != 'deny'
                  {where_clause}
                ORDER BY c.document_id, c.chunk_index
                """,
                params,
            ).fetchall()
            active_rows = conn.execute(
                """
                SELECT c.chunk_id
                FROM knowledge_chunks c
                JOIN knowledge_documents d ON d.document_id = c.document_id
                WHERE d.status = 'active'
                  AND c.sensitivity != 'secret'
                  AND c.remote_policy != 'deny'
                """
            ).fetchall()
        finally:
            conn.close()
        vectors = self._embedding_provider.embed_passages([str(row["text"]) for row in rows])
        from app.domains.knowledge_retrieval import SemanticVectorRecord

        records = [
            SemanticVectorRecord(
                chunk_id=str(row["chunk_id"]),
                checksum=sha256(str(row["text"]).encode("utf-8")).hexdigest(),
                vector=vector,
            )
            for row, vector in zip(rows, vectors, strict=True)
        ]
        inserted, updated = self._semantic_index.upsert(records)
        removed = self._semantic_index.reconcile(
            str(row["chunk_id"]) for row in active_rows
        )
        status = self._semantic_index.status()
        return KnowledgeSemanticSyncResult(
            enabled=True,
            index_key=status.index_key,
            scanned_chunks=len(rows),
            embedded_chunks=inserted,
            updated_chunks=updated,
            removed_chunks=removed,
            indexed_chunks=status.indexed_chunks,
            reason=status.reason,
        )

    def load_chunks(
        self,
        *,
        chunk_ids: list[str],
        max_chars_per_chunk: int = DEFAULT_SNIPPET_CHARS,
        tool_name: str | None = None,
    ) -> KnowledgeChunkLoadResult:
        query_id = _stable_id("knowledge_load", ",".join(chunk_ids), _now_iso())
        if not chunk_ids:
            return KnowledgeChunkLoadResult(query_id=query_id, chunks=[])
        limited_ids = chunk_ids[:MAX_LOAD_CHUNKS]
        placeholders = ", ".join("?" for _ in limited_ids)
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"""
                SELECT
                    c.chunk_id, c.document_id, d.title, d.source_type, d.uri,
                    c.chunk_index, c.text, c.char_count, c.token_estimate,
                    c.source_ref, c.sensitivity, c.remote_policy
                FROM knowledge_chunks c
                JOIN knowledge_documents d ON d.document_id = c.document_id
                WHERE c.chunk_id IN ({placeholders})
                """,
                limited_ids,
            ).fetchall()
        finally:
            conn.close()
        rows_by_id = {row["chunk_id"]: row for row in rows}
        chunks: list[KnowledgeChunkRecord] = []
        filtered_count = 0
        for chunk_id in limited_ids:
            row = rows_by_id.get(chunk_id)
            if row is None:
                continue
            decision = self.privacy_gateway.decide(
                sensitivity=row["sensitivity"],
                remote_policy=row["remote_policy"],
                text=row["text"],
            )
            if not decision.allowed:
                filtered_count += 1
                continue
            chunks.append(
                KnowledgeChunkRecord(
                    chunk_id=row["chunk_id"],
                    document_id=row["document_id"],
                    title=row["title"],
                    source_type=row["source_type"],
                    uri=row["uri"],
                    chunk_index=row["chunk_index"],
                    text=self._trim(decision.text, max_chars_per_chunk),
                    char_count=min(row["char_count"], max_chars_per_chunk),
                    token_estimate=self._estimate_tokens(decision.text[:max_chars_per_chunk]),
                    source_ref=row["source_ref"],
                    sensitivity=row["sensitivity"],
                    remote_policy=row["remote_policy"],
                    policy_decision=decision.policy_decision,
                )
            )
        self._audit(
            query_id=query_id,
            tool_name=tool_name,
            action="load_chunks",
            result_count=len(chunks),
            policy_decision="filtered" if filtered_count else "allowed",
            metadata={"chunk_ids": limited_ids, "filtered_count": filtered_count},
        )
        return KnowledgeChunkLoadResult(
            query_id=query_id,
            chunks=chunks,
            filtered_count=filtered_count,
        )

    def load_document(
        self,
        *,
        document_id: str,
        include_text: bool = False,
        max_chars: int = MAX_LOAD_DOCUMENT_CHARS,
        tool_name: str | None = None,
    ) -> KnowledgeDocumentRecord:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                SELECT
                    document_id, source_id, source_type, title, uri, checksum,
                    mime_type, status, sensitivity, remote_policy, source_ref, metadata
                FROM knowledge_documents
                WHERE document_id = ?
                """,
                (document_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"knowledge document not found: {document_id}")
            chunk_rows = conn.execute(
                """
                SELECT chunk_id, text, sensitivity, remote_policy
                FROM knowledge_chunks
                WHERE document_id = ?
                ORDER BY chunk_index ASC
                """,
                (document_id,),
            ).fetchall()
        finally:
            conn.close()
        decision = self.privacy_gateway.decide(
            sensitivity=row["sensitivity"],
            remote_policy=row["remote_policy"],
            text="",
        )
        text: str | None = None
        truncated = False
        if include_text and decision.allowed:
            selected: list[str] = []
            used = 0
            for chunk in chunk_rows:
                chunk_decision = self.privacy_gateway.decide(
                    sensitivity=chunk["sensitivity"],
                    remote_policy=chunk["remote_policy"],
                    text=chunk["text"],
                )
                if not chunk_decision.allowed:
                    continue
                if used + len(chunk_decision.text) > max_chars:
                    selected.append(chunk_decision.text[: max(0, max_chars - used)])
                    truncated = True
                    break
                selected.append(chunk_decision.text)
                used += len(chunk_decision.text)
            text = "\n".join(part for part in selected if part)
            truncated = truncated or len(chunk_rows) > len(selected)
        record = KnowledgeDocumentRecord(
            document_id=row["document_id"],
            source_id=row["source_id"],
            source_type=row["source_type"],
            title=row["title"],
            uri=row["uri"],
            checksum=row["checksum"],
            mime_type=row["mime_type"],
            status=row["status"],
            sensitivity=row["sensitivity"],
            remote_policy=row["remote_policy"],
            source_ref=row["source_ref"],
            metadata=self._json_dict(row["metadata"]),
            chunk_ids=[chunk["chunk_id"] for chunk in chunk_rows],
            text=text,
            truncated=truncated,
            policy_decision=decision.policy_decision,
        )
        self._audit(
            query_id=_stable_id("knowledge_document", document_id, _now_iso()),
            tool_name=tool_name,
            document_id=document_id,
            action="load_document",
            result_count=1,
            policy_decision=record.policy_decision,
            metadata={"include_text": include_text},
        )
        return record

    def _search_rows(
        self,
        *,
        query: str,
        limit: int,
        source_types: list[str] | None,
        max_candidates: int,
        sort_by: Literal["relevance", "source_time_desc"],
    ) -> list[sqlite3.Row]:
        normalized_query = query.strip()
        source_params: list[Any] = []
        source_clause = ""
        if source_types:
            placeholders = ", ".join("?" for _ in source_types)
            source_clause = f"AND d.source_type IN ({placeholders})"
            source_params.extend(source_types)
        conn = self._conn_factory()
        try:
            if normalized_query and sort_by == "relevance":
                rows = conn.execute(
                    f"""
                    SELECT
                        c.chunk_id, c.document_id, d.title, d.source_type, d.uri,
                        c.chunk_index,
                        snippet(knowledge_chunks_fts, 3, '[', ']', '...', 18) AS snippet,
                        c.source_ref, c.sensitivity, c.remote_policy
                    FROM knowledge_chunks_fts
                    JOIN knowledge_chunks c ON c.chunk_id = knowledge_chunks_fts.chunk_id
                    JOIN knowledge_documents d ON d.document_id = c.document_id
                    WHERE knowledge_chunks_fts MATCH ? {source_clause}
                    ORDER BY rank
                    LIMIT ?
                    """,
                    [self._fts_query(normalized_query), *source_params, max_candidates],
                ).fetchall()
                if len(rows) < max_candidates:
                    rows = self._append_substring_matches(
                        conn=conn,
                        rows=rows,
                        query=normalized_query,
                    source_clause=source_clause,
                    source_params=source_params,
                    limit=max_candidates,
                    sort_by=sort_by,
                )
            elif normalized_query:
                rows = conn.execute(
                    f"""
                    SELECT
                        c.chunk_id, c.document_id, d.title, d.source_type, d.uri,
                        c.chunk_index, c.text AS snippet, c.source_ref,
                        c.sensitivity, c.remote_policy
                    FROM knowledge_chunks_fts
                    JOIN knowledge_chunks c ON c.chunk_id = knowledge_chunks_fts.chunk_id
                    JOIN knowledge_documents d ON d.document_id = c.document_id
                    WHERE knowledge_chunks_fts MATCH ? {source_clause}
                    ORDER BY COALESCE(
                        json_extract(d.metadata, '$.source_time'),
                        json_extract(d.metadata, '$.received_at'),
                        d.updated_at
                    ) DESC,
                        c.chunk_index ASC
                    LIMIT ?
                    """,
                    [self._fts_query(normalized_query), *source_params, max_candidates],
                ).fetchall()
                if len(rows) < max_candidates:
                    rows = self._append_substring_matches(
                        conn=conn,
                        rows=rows,
                        query=normalized_query,
                        source_clause=source_clause,
                        source_params=source_params,
                        limit=max_candidates,
                        sort_by=sort_by,
                    )
            else:
                rows = conn.execute(
                    f"""
                    SELECT
                        c.chunk_id, c.document_id, d.title, d.source_type, d.uri,
                        c.chunk_index, c.text AS snippet, c.source_ref,
                        c.sensitivity, c.remote_policy
                    FROM knowledge_chunks c
                    JOIN knowledge_documents d ON d.document_id = c.document_id
                    WHERE d.status = 'active' {source_clause}
                    ORDER BY COALESCE(
                        json_extract(d.metadata, '$.source_time'),
                        json_extract(d.metadata, '$.received_at'),
                        d.updated_at
                    ) DESC,
                        c.chunk_index ASC
                    LIMIT ?
                    """,
                    [*source_params, max_candidates],
                ).fetchall()
        finally:
            conn.close()
        return rows[: max(limit, 1) * 4]

    def _keyword_candidates(
        self,
        *,
        query: str,
        limit: int,
        source_types: list[str] | None,
        sort_by: Literal["relevance", "source_time_desc"],
    ) -> list[RetrievalCandidate]:
        rows = self._search_rows(
            query=query,
            limit=limit,
            source_types=source_types,
            max_candidates=limit,
            sort_by=sort_by,
        )
        return [
            RetrievalCandidate(
                chunk_id=str(row["chunk_id"]),
                rank=index + 1,
                score=None,
                channel="keyword",
            )
            for index, row in enumerate(rows)
        ]

    def _rows_for_chunk_ids(
        self,
        *,
        chunk_ids: list[str],
        source_types: list[str] | None,
    ) -> list[sqlite3.Row]:
        if not chunk_ids:
            return []
        source_clause = ""
        source_params: list[Any] = []
        if source_types:
            source_clause = f"AND d.source_type IN ({', '.join('?' for _ in source_types)})"
            source_params.extend(source_types)
        placeholders = ", ".join("?" for _ in chunk_ids)
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"""
                SELECT
                    c.chunk_id, c.document_id, d.title, d.source_type, d.uri,
                    c.chunk_index, c.text AS snippet, c.source_ref,
                    c.sensitivity, c.remote_policy
                FROM knowledge_chunks c
                JOIN knowledge_documents d ON d.document_id = c.document_id
                WHERE c.chunk_id IN ({placeholders})
                  AND d.status = 'active' {source_clause}
                """,
                [*chunk_ids, *source_params],
            ).fetchall()
        finally:
            conn.close()
        by_id = {str(row["chunk_id"]): row for row in rows}
        return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]

    def _append_substring_matches(
        self,
        *,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        query: str,
        source_clause: str,
        source_params: list[Any],
        limit: int,
        sort_by: Literal["relevance", "source_time_desc"],
    ) -> list[sqlite3.Row]:
        """Supplement FTS for languages whose token boundaries are not whitespace-based."""

        terms = [term.strip() for term in query.split() if term.strip()]
        if not terms:
            return rows
        match_clause = " OR ".join(
            "(instr(c.text, ?) > 0 OR instr(d.title, ?) > 0)" for _ in terms
        )
        match_params = [value for term in terms for value in (term, term)]
        fallback_rows = conn.execute(
            f"""
            SELECT
                c.chunk_id, c.document_id, d.title, d.source_type, d.uri,
                c.chunk_index, c.text AS snippet, c.source_ref,
                c.sensitivity, c.remote_policy
            FROM knowledge_chunks c
            JOIN knowledge_documents d ON d.document_id = c.document_id
            WHERE d.status = 'active' {source_clause} AND ({match_clause})
            ORDER BY COALESCE(
                json_extract(d.metadata, '$.source_time'),
                json_extract(d.metadata, '$.received_at'),
                d.updated_at
            ) DESC,
                c.chunk_index ASC
            LIMIT ?
            """,
            [*source_params, *match_params, limit],
        ).fetchall()
        seen_ids = {row["chunk_id"] for row in rows}
        rows.extend(row for row in fallback_rows if row["chunk_id"] not in seen_ids)
        return rows

    def _delete_document_chunks(self, conn: sqlite3.Connection, document_id: str) -> list[str]:
        rows = conn.execute(
            "SELECT chunk_id FROM knowledge_chunks WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        for row in rows:
            conn.execute("DELETE FROM knowledge_chunks_fts WHERE chunk_id = ?", (row["chunk_id"],))
        conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        return [str(row["chunk_id"]) for row in rows]

    def _delete_replaced_documents(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: str,
        document_uri: str | None,
        keep_document_id: str,
    ) -> list[str]:
        """Remove superseded versions of the same source URI before inserting a new one."""

        if document_uri is None:
            return []
        rows = conn.execute(
            """
            SELECT document_id FROM knowledge_documents
            WHERE source_id = ? AND uri = ? AND document_id != ?
            """,
            (source_id, document_uri, keep_document_id),
        ).fetchall()
        removed_chunk_ids: list[str] = []
        for row in rows:
            document_id = str(row["document_id"])
            removed_chunk_ids.extend(self._delete_document_chunks(conn, document_id))
            conn.execute("DELETE FROM knowledge_documents WHERE document_id = ?", (document_id,))
        return removed_chunk_ids

    def _chunk_text(self, text: str, chunk_chars: int) -> list[str]:
        clean_text = text.strip()
        if not clean_text:
            return [""]
        size = min(max(200, chunk_chars), MAX_CHUNK_CHARS)
        paragraphs = re.split(r"(\n\s*\n)", clean_text)
        chunks: list[str] = []
        current = ""
        for part in paragraphs:
            if len(current) + len(part) <= size:
                current += part
                continue
            if current.strip():
                chunks.append(current.strip())
            if len(part) > size:
                chunks.extend(part[index : index + size] for index in range(0, len(part), size))
                current = ""
            else:
                current = part
        if current.strip() or not chunks:
            chunks.append(current.strip())
        return chunks

    def _audit(
        self,
        *,
        query_id: str,
        action: str,
        result_count: int,
        policy_decision: str,
        tool_name: str | None = None,
        document_id: str | None = None,
        chunk_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn = self._conn_factory()
        try:
            conn.execute(
                """
                INSERT INTO knowledge_access_audit(
                    audit_id, query_id, tool_name, document_id, chunk_id, action,
                    result_count, policy_decision, remote_data_sent, metadata, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _stable_id("knowledge_audit", query_id, action, _now_iso()),
                    query_id,
                    tool_name,
                    document_id,
                    chunk_id,
                    action,
                    result_count,
                    policy_decision,
                    0,
                    self._json(metadata or {}),
                    _now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _source_ref(self, *, source_type: str, uri: str | None, title: str) -> str:
        return f"{source_type}:{uri or title}"

    def _fts_query(self, query: str) -> str:
        terms = [term.strip().replace('"', "") for term in query.split() if term.strip()]
        if not terms:
            return '""'
        return " OR ".join(f'"{term}"' for term in terms)

    def _trim(self, text: str, max_chars: int) -> str:
        limit = min(max(1, max_chars), MAX_SNIPPET_CHARS)
        if len(text) <= limit:
            return text
        if limit <= 3:
            return text[:limit]
        return f"{text[: limit - 3]}..."

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4) if text else 0

    def _json(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _json_dict(self, value: str) -> dict[str, Any]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _mime_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".md":
            return "text/markdown"
        if suffix in {".txt", ".text"}:
            return "text/plain"
        return "text/plain"


class PrivacyDecision(BaseModel):
    allowed: bool
    text: str
    policy_decision: str


class PrivacyGateway:
    """Minimum deterministic privacy gate before knowledge reaches prompts."""

    def contains_secret(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in SECRET_PATTERNS)

    def decide(
        self,
        *,
        sensitivity: str,
        remote_policy: str,
        text: str,
    ) -> PrivacyDecision:
        if sensitivity == "secret" or remote_policy == "deny":
            return PrivacyDecision(allowed=False, text="", policy_decision="deny")
        if self.contains_secret(text):
            return PrivacyDecision(allowed=False, text="", policy_decision="secret_deny")
        if remote_policy == "confirm":
            return PrivacyDecision(allowed=False, text="", policy_decision="confirm_required")
        if sensitivity == "sensitive" or remote_policy == "redact":
            return PrivacyDecision(
                allowed=True,
                text=self.redact(text),
                policy_decision="redacted",
            )
        return PrivacyDecision(allowed=True, text=text, policy_decision="allowed")

    def redact(self, text: str) -> str:
        redacted = text
        for pattern in SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        redacted = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", redacted)
        return redacted
