"""Local source-agnostic knowledge storage and retrieval."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domains.knowledge_retrieval import (
    CallableCandidateRetriever,
    ConfigurableKnowledgeRetriever,
    EmbeddingProvider,
    Reranker,
    RetrievalCandidate,
    RetrievalSelection,
    SemanticCandidateRetriever,
    SemanticIndex,
)
from app.domains.knowledge_sources import KnowledgeSourceProvider

KnowledgeSensitivity = Literal["public", "personal", "sensitive", "secret"]
KnowledgeRemotePolicy = Literal["allow", "redact", "confirm", "deny"]

DEFAULT_CHUNK_CHARS = 1800
MAX_CHUNK_CHARS = 6000
DEFAULT_SNIPPET_CHARS = 420
_QUERY_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="knowledge-query")
MAX_SNIPPET_CHARS = 1200
MAX_LOAD_CHUNKS = 20
MAX_LOAD_DOCUMENT_CHARS = 12000
SECRET_PLACEHOLDER = "[REDACTED SECRET-LIKE CONTENT]"

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]{8,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b"),
]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    source_id: str | None = None
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
    rerank_score: float | None = None
    untrusted_data: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSearchResult(BaseModel):
    query: str
    query_id: str
    results: list[KnowledgeSearchItem]
    filtered_count: int = 0
    requested_mode: str = "keyword"
    applied_mode: str = "keyword"
    retrieval_warning: str | None = None
    rerank_applied: bool = False


class KnowledgeSourceRecord(BaseModel):
    source_id: str
    source_type: str
    display_name: str
    uri: str | None = None
    document_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeChunkRecord(BaseModel):
    chunk_id: str
    document_id: str
    source_id: str | None = None
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
    total_chars: int | None = None
    truncated: bool = False
    offset: int = 0
    next_offset: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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
        reranker: Reranker | None = None,
        workspace_roots: tuple[Path, ...] = (),
    ) -> None:
        self._conn_factory = conn_factory
        self.privacy_gateway = PrivacyGateway()
        self._embedding_provider = embedding_provider
        self._semantic_index = semantic_index
        self._default_retrieval_mode = default_retrieval_mode
        self._auto_index_on_import = auto_index_on_import
        self._reranker = reranker
        self._workspace_roots = tuple(root.resolve(strict=False) for root in workspace_roots)
        self._candidate_cache: OrderedDict[tuple[Any, ...], tuple[float, RetrievalSelection]] = OrderedDict()
        self._cache_lock = Lock()
        self._cache_ttl_seconds = 30.0
        self._cache_capacity = 128
        self._source_providers: list[KnowledgeSourceProvider] = []

    def register_source_provider(self, provider: KnowledgeSourceProvider) -> None:
        """Register a live source adapter without coupling the service to its domain."""
        if any(item.source_type == provider.source_type for item in self._source_providers):
            raise ValueError(f"knowledge source provider already registered: {provider.source_type}")
        self._source_providers.append(provider)

    def resolve_origin_constraints(self, *, invocation: Any, context: Any) -> list[str]:
        """Ask providers whether the requested evidence IDs carry origin constraints."""
        constraints: set[str] = set()
        payload = getattr(invocation, "input", {})
        for provider in self._source_providers:
            resolver = getattr(provider, "resolve_origin_constraints", None)
            if resolver is not None:
                constraints.update(resolver(input=payload, context=context))
        return sorted(constraints)

    def allow_cached_observation(self, *, result: Any, context: Any = None) -> bool:
        """Do not replay cached evidence whose provider can revoke it dynamically."""
        payload = getattr(result, "output", result)

        def has_live_id(value: Any) -> bool:
            if isinstance(value, str):
                return any(provider.handles_id(value) for provider in self._source_providers)
            if isinstance(value, dict):
                return any(has_live_id(item) for item in value.values())
            if isinstance(value, (list, tuple)):
                return any(has_live_id(item) for item in value)
            return False

        return not has_live_id(payload)

    @staticmethod
    def _effective_provider_accounts(
        account_ids: list[str] | None, provider_account_ids: list[str] | None
    ) -> list[str] | None:
        """Preserve explicit account denials and intersect independent grants."""
        if account_ids == [] or provider_account_ids == []:
            return []
        if account_ids is not None and provider_account_ids is not None:
            return sorted(set(account_ids).intersection(provider_account_ids))
        return account_ids if account_ids is not None else provider_account_ids

    def _retrieval_version(self) -> tuple[Any, ...]:
        conn = self._conn_factory()
        try:
            return tuple(
                tuple(conn.execute(f"SELECT COUNT(*), MAX(updated_at) FROM {table}").fetchone())
                for table in ("knowledge_sources", "knowledge_documents", "knowledge_embedding_records")
            )
        finally:
            conn.close()

    def _clear_candidate_cache(self) -> None:
        with self._cache_lock:
            self._candidate_cache.clear()

    def _cached_selection(self, key: tuple[Any, ...]) -> RetrievalSelection | None:
        with self._cache_lock:
            entry = self._candidate_cache.get(key)
            if entry is None:
                return None
            created_at, selection = entry
            if time.monotonic() - created_at > self._cache_ttl_seconds:
                del self._candidate_cache[key]
                return None
            self._candidate_cache.move_to_end(key)
            return selection

    def _store_selection(self, key: tuple[Any, ...], selection: RetrievalSelection) -> None:
        with self._cache_lock:
            self._candidate_cache[key] = (time.monotonic(), selection)
            self._candidate_cache.move_to_end(key)
            while len(self._candidate_cache) > self._cache_capacity:
                self._candidate_cache.popitem(last=False)

    def list_authorized_source_ids(
        self, *, workspace_path: str | None = None, session_id: str | None = None,
        provider_account_ids: list[str] | None = None,
    ) -> tuple[str, ...]:
        """Return active sources whose persisted access scope allows this context.

        An empty source access_scope is global within this single-user local backend.
        A malformed or explicit scope fails closed.
        """
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                "SELECT source_id, access_scope FROM knowledge_sources WHERE status = 'active' ORDER BY source_id"
            ).fetchall()
        finally:
            conn.close()
        ids = [
            str(row["source_id"]) for row in rows
            if self._source_access_allowed(
                row["access_scope"], workspace_path=workspace_path, session_id=session_id
            )
        ]
        provider_accounts = self._effective_provider_accounts(None, provider_account_ids)
        for provider in self._source_providers:
            ids.extend(source.source_id for source in provider.list_sources(source_ids=None, account_ids=provider_accounts))
        return tuple(sorted(set(ids)))

    @staticmethod
    def _source_access_allowed(
        raw_scope: str, *, workspace_path: str | None, session_id: str | None
    ) -> bool:
        try:
            scope = json.loads(raw_scope)
        except (TypeError, ValueError):
            return False
        if not isinstance(scope, dict):
            return False
        if set(scope) - {"workspace_paths", "session_ids"}:
            return False
        workspace_paths = scope.get("workspace_paths")
        if workspace_paths is not None:
            if not isinstance(workspace_paths, list) or not all(isinstance(x, str) for x in workspace_paths):
                return False
            if not workspace_path or not any(
                KnowledgeService._workspace_paths_overlap(workspace_path, root)
                for root in workspace_paths
            ):
                return False
        session_ids = scope.get("session_ids")
        if session_ids is not None:
            if not isinstance(session_ids, list) or not all(isinstance(x, str) for x in session_ids):
                return False
            if session_id not in session_ids:
                return False
        return True

    @staticmethod
    def _workspace_paths_overlap(left: str, right: str) -> bool:
        def normalize(path: str):
            if re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\"):
                return PureWindowsPath(path.casefold())
            return Path(path).resolve(strict=False)

        a, b = normalize(left), normalize(right)
        if type(a) is not type(b):
            return False
        return a == b or a in b.parents or b in a.parents

    def list_sources(
        self,
        *,
        source_ids: list[str] | None = None,
        source_types: list[str] | None = None,
        limit: int = 100,
        provider_account_ids: list[str] | None = None,
    ) -> list[KnowledgeSourceRecord]:
        clauses = ["s.status = 'active'"]
        params: list[Any] = []
        for column, values in (("s.source_id", source_ids), ("s.source_type", source_types)):
            if values is not None:
                clauses.append(
                    f"{column} IN ({', '.join('?' for _ in values)})" if values else "0"
                )
                params.extend(values)
        source_clause = " AND ".join(clauses)
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"""
                SELECT s.source_id, s.source_type, s.display_name, s.uri,
                       COUNT(d.document_id) AS document_count
                FROM knowledge_sources s
                LEFT JOIN knowledge_documents d ON d.source_id = s.source_id
                    AND d.status = 'active'
                WHERE {source_clause}
                GROUP BY s.source_id
                ORDER BY s.source_type, s.display_name, s.source_id
                LIMIT ?
                """,
                [*params, min(max(1, limit), 200)],
            ).fetchall()
        finally:
            conn.close()
        records = [KnowledgeSourceRecord.model_validate(dict(row)) for row in rows]
        provider_accounts = self._effective_provider_accounts(None, provider_account_ids)
        for provider in self._source_providers:
            if source_types is None or provider.source_type in source_types:
                records.extend(provider.list_sources(source_ids=source_ids, account_ids=provider_accounts))
        return records[:min(max(1, limit), 200)]

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

        if committed:
            self._clear_candidate_cache()
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
        resolved = path.resolve(strict=True)
        roots = [root for root in self._workspace_roots if root == resolved or root in resolved.parents]
        if self._workspace_roots and not roots:
            raise ValueError("file is outside configured workspace roots")
        text = resolved.read_text(encoding="utf-8")
        access_scope = {"workspace_paths": [str(max(roots, key=lambda root: len(root.parts)))]} if roots else {}
        return self.import_text_document(
            KnowledgeDocumentInput(
                source=KnowledgeSourceInput(
                    source_type=source_type,
                    display_name=path.name,
                    uri=path.as_posix(),
                    metadata=metadata or {},
                    access_scope=access_scope,
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

    def prune_workspace_documents(self, *, source_uri: str, keep_uris: set[str]) -> int:
        """Remove stale documents only from one explicit workspace source."""
        source_id = _stable_id("knowledge_source", "workspace_file", source_uri)
        removed_chunk_ids: list[str] = []
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                "SELECT document_id, uri FROM knowledge_documents "
                "WHERE source_id = ? AND source_type = 'workspace_file'",
                (source_id,),
            ).fetchall()
            removed = 0
            for row in rows:
                if row["uri"] in keep_uris:
                    continue
                document_id = str(row["document_id"])
                removed_chunk_ids.extend(self._delete_document_chunks(conn, document_id))
                conn.execute("DELETE FROM knowledge_documents WHERE document_id = ?", (document_id,))
                removed += 1
            conn.commit()
        finally:
            conn.close()
        if removed:
            self._clear_candidate_cache()
        if removed_chunk_ids and self._semantic_index is not None:
            self._semantic_index.remove(removed_chunk_ids)
        return removed

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
        source_ids: list[str] | None = None,
        account_ids: list[str] | None = None,
        provider_account_ids: list[str] | None = None,
        cache_namespace: str | None = None,
        keyword_candidate_k: int | None = None,
        semantic_candidate_k: int | None = None,
        max_chunks_per_document: int | None = None,
        _apply_rerank: bool = True,
    ) -> KnowledgeSearchResult:
        limit = min(max(1, limit), 100)
        candidate_limits = {
            "keyword": min(400, max(limit, keyword_candidate_k or limit * 4)),
            "semantic": min(400, max(limit, semantic_candidate_k or limit * 4)),
        }
        if max_chunks_per_document is not None and max_chunks_per_document < 1:
            raise ValueError("max_chunks_per_document must be positive")
        query_id = _stable_id("knowledge_query", query, _now_iso())
        requested_mode = mode or self._default_retrieval_mode
        if any(values is not None and not values for values in (source_types, source_ids, account_ids)):
            self._audit(
                query_id=query_id, tool_name=tool_name, action="search",
                result_count=0, policy_decision="filtered",
                metadata={"requested_mode": requested_mode, "empty_scope": True},
            )
            return KnowledgeSearchResult(
                query=query, query_id=query_id, results=[], requested_mode=requested_mode,
                applied_mode="none", retrieval_warning="empty source scope",
            )
        keyword_retriever = CallableCandidateRetriever(
            channel="keyword",
            callback=lambda candidate_query, candidate_limit: self._keyword_candidates(
                query=candidate_query,
                limit=candidate_limit,
                source_types=source_types,
                source_ids=source_ids,
                account_ids=account_ids,
                sort_by=sort_by,
            ),
        )
        retrievers = [keyword_retriever]
        if self._embedding_provider is not None and self._semantic_index is not None:
            semantic_retriever = SemanticCandidateRetriever(
                provider=self._embedding_provider,
                index=self._semantic_index,
            )
            if source_types or source_ids or account_ids:
                retrievers.append(
                    CallableCandidateRetriever(
                        channel="semantic",
                        callback=lambda candidate_query, candidate_limit: self._scoped_semantic_candidates(
                            query=candidate_query,
                            limit=candidate_limit,
                            source_types=source_types,
                            source_ids=source_ids,
                            account_ids=account_ids,
                        ),
                    )
                )
            else:
                retrievers.append(semantic_retriever)
        cache_key = (
            cache_namespace, sha256(query.encode("utf-8")).hexdigest(),
            requested_mode, limit, sort_by,
            tuple(sorted(source_types or ())), tuple(sorted(source_ids or ())),
            tuple(sorted(account_ids or ())), tuple(sorted(candidate_limits.items())),
            self._retrieval_version(),
        )
        selection = self._cached_selection(cache_key)
        if selection is None:
            selection = ConfigurableKnowledgeRetriever(retrievers=retrievers).retrieve(
                query=query,
                limit=min(400, max(candidate_limits.values())),
                mode=requested_mode,
                candidate_limits=candidate_limits,
            )
            self._store_selection(cache_key, selection)
        rows = self._rows_for_chunk_ids(
            chunk_ids=[candidate.chunk_id for candidate in selection.candidates],
            source_types=source_types,
            source_ids=source_ids,
            account_ids=account_ids,
        )
        candidates_by_id = {candidate.chunk_id: candidate for candidate in selection.candidates}
        results: list[KnowledgeSearchItem] = []
        filtered_count = 0
        for row in rows:
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
                    source_id=row["source_id"],
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
        provider_results: list[KnowledgeSearchItem] = []
        for provider in self._source_providers:
            if source_types is not None and provider.source_type not in source_types:
                continue
            provider_accounts = self._effective_provider_accounts(account_ids, provider_account_ids)
            sources = provider.list_sources(source_ids=source_ids, account_ids=provider_accounts)
            provider_ids = [source.source_id for source in sources]
            provider_results.extend(provider.search(
                query=query, limit=max(limit * 4, candidate_limits["keyword"]),
                source_ids=provider_ids, account_ids=provider_accounts,
                max_snippet_chars=max_snippet_chars,
            ))
        if provider_results:
            ranked = list(results)
            ranked.extend(provider_results)
            for rank, item in enumerate(results, start=1):
                item.retrieval_score = 1.0 / (60 + rank)
            for rank, item in enumerate(provider_results, start=1):
                item.retrieval_score = 1.0 / (60 + rank)
            results = sorted(ranked, key=lambda item: (-float(item.retrieval_score or 0), item.chunk_id))
        rerank_applied = False
        rerank_warning: str | None = None
        # A time-ordered listing must not be reordered by relevance scores.
        if _apply_rerank and sort_by != "source_time_desc" and self._reranker is not None and results:
            try:
                rerank_count = min(len(results), self._reranker.max_candidates)
                scores = self._reranker.score(
                    query,
                    [f"{item.title}\n{item.snippet}" for item in results[:rerank_count]],
                )
                if len(scores) != rerank_count:
                    raise ValueError("reranker returned the wrong number of scores")
                for item, score in zip(results[:rerank_count], scores, strict=True):
                    item.rerank_score = float(score)
                results[:rerank_count] = sorted(
                    results[:rerank_count],
                    key=lambda item: (-float(item.rerank_score), -float(item.retrieval_score or 0), item.chunk_id),
                )
                rerank_applied = True
            except Exception as exc:  # noqa: BLE001 - optional model failures must fall back
                rerank_warning = f"local reranker unavailable: {type(exc).__name__}"
                for item in results:
                    item.rerank_score = None
        if distinct_documents or max_chunks_per_document is not None:
            deduplicated: list[KnowledgeSearchItem] = []
            document_counts: dict[str, int] = {}
            per_document = 1 if distinct_documents else max_chunks_per_document
            for item in results:
                count = document_counts.get(item.document_id, 0)
                if per_document is not None and count >= per_document:
                    continue
                deduplicated.append(item)
                document_counts[item.document_id] = count + 1
            results = deduplicated
        results = results[:limit]
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
                "rerank_applied": rerank_applied,
                "rerank_warning": rerank_warning,
                "sort_by": sort_by,
                "distinct_documents": distinct_documents,
                "candidate_limits": candidate_limits,
                "max_chunks_per_document": max_chunks_per_document,
            },
        )
        return KnowledgeSearchResult(
            query=query,
            query_id=query_id,
            results=results,
            filtered_count=filtered_count,
            requested_mode=selection.requested_mode,
            applied_mode=selection.applied_mode,
            retrieval_warning="; ".join(part for part in (selection.warning, rerank_warning) if part) or None,
            rerank_applied=rerank_applied,
        )

    def search_queries(
        self,
        *,
        query: str,
        rewritten_queries: Sequence[str],
        limit: int = 10,
        source_types: list[str] | None = None,
        source_ids: list[str] | None = None,
        account_ids: list[str] | None = None,
        cache_namespace: str | None = None,
        mode: str | None = None,
        max_snippet_chars: int = DEFAULT_SNIPPET_CHARS,
        tool_name: str | None = None,
        keyword_candidate_k: int | None = None,
        semantic_candidate_k: int | None = None,
        max_chunks_per_document: int | None = None,
        max_parallel_searches: int = 4,
        max_total_candidates: int = 240,
    ) -> tuple[KnowledgeSearchResult, list[dict[str, Any]]]:
        """Search bounded queries concurrently, then fuse in stable input order."""
        limit = min(max(1, limit), 100)
        queries = [query, *rewritten_queries]
        if len(queries) > 17:
            raise ValueError("too many search queries")
        if not 1 <= max_parallel_searches <= 8:
            raise ValueError("max_parallel_searches must be between 1 and 8")
        if not 30 <= max_total_candidates <= 800:
            raise ValueError("max_total_candidates must be between 30 and 800")
        requested_mode = mode or self._default_retrieval_mode
        channel_count = 2 if requested_mode == "hybrid" else 1
        if len(queries) * channel_count > max_total_candidates:
            raise ValueError("candidate budget is too small for the query/channel count")
        per_query_limit = min(
            100, max(30, limit, keyword_candidate_k or 0, semantic_candidate_k or 0),
            max(1, max_total_candidates // (len(queries) * channel_count)),
        )
        search_kwargs = {
            "limit": per_query_limit,
            "source_types": source_types,
            "source_ids": source_ids,
            "account_ids": account_ids,
            "cache_namespace": cache_namespace,
            "mode": mode,
            "max_snippet_chars": max_snippet_chars,
            "tool_name": tool_name,
            # search() otherwise expands a result limit to limit*4 candidates.
            # Pin each channel to its share of the total cross-query budget.
            "keyword_candidate_k": per_query_limit,
            "semantic_candidate_k": per_query_limit,
            "max_chunks_per_document": max_chunks_per_document,
            "_apply_rerank": False,
        }
        parallel_count = min(max_parallel_searches, len(queries))
        in_flight: dict[Future[KnowledgeSearchResult], int] = {}
        search_results: list[KnowledgeSearchResult | None] = [None] * len(queries)
        next_index = 0
        while next_index < parallel_count:
            future = _QUERY_SEARCH_EXECUTOR.submit(
                self.search, query=queries[next_index], **search_kwargs,
            )
            in_flight[future] = next_index
            next_index += 1
        try:
            while in_flight:
                completed, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in completed:
                    index = in_flight.pop(future)
                    search_results[index] = future.result()
                while next_index < len(queries) and len(in_flight) < parallel_count:
                    future = _QUERY_SEARCH_EXECUTOR.submit(
                        self.search, query=queries[next_index], **search_kwargs,
                    )
                    in_flight[future] = next_index
                    next_index += 1
        except Exception:
            for future in in_flight:
                future.cancel()
            raise
        traces: list[dict[str, Any]] = []
        results_by_chunk: dict[str, KnowledgeSearchItem] = {}
        ranks_by_chunk: dict[str, list[int]] = {}
        filtered_count = 0
        applied_modes: list[str] = []
        warnings: list[str] = []
        for search_query, result in zip(queries, search_results, strict=True):
            if result is None:
                raise RuntimeError("parallel search result is missing")
            traces.append({"query": search_query, "hit_count": len(result.results), "query_id": result.query_id})
            filtered_count += result.filtered_count
            applied_modes.append(result.applied_mode)
            if result.retrieval_warning:
                warnings.append(result.retrieval_warning)
            new_unique_hits = 0
            for rank, item in enumerate(result.results, start=1):
                existing = results_by_chunk.get(item.chunk_id)
                if existing is None:
                    results_by_chunk[item.chunk_id] = item
                    ranks_by_chunk[item.chunk_id] = []
                    new_unique_hits += 1
                else:
                    existing.retrieval_channels = sorted(
                        set(existing.retrieval_channels) | set(item.retrieval_channels)
                    )
                ranks_by_chunk[item.chunk_id].append(rank)
            traces[-1]["new_unique_hits"] = new_unique_hits

        fusion_scores = {
            chunk_id: sum(1.0 / (60 + rank) for rank in ranks)
            for chunk_id, ranks in ranks_by_chunk.items()
        }
        fused = list(results_by_chunk.values())
        for item in fused:
            item.retrieval_score = fusion_scores[item.chunk_id]
        fused.sort(
            key=lambda item: (
                -fusion_scores[item.chunk_id],
                item.chunk_id,
            )
        )
        rerank_applied = False
        rerank_warning = None
        if self._reranker is not None and fused:
            try:
                rerank_count = min(len(fused), self._reranker.max_candidates)
                scores = self._reranker.score(
                    query,
                    [f"{item.title}\n{item.snippet}" for item in fused[:rerank_count]],
                )
                if len(scores) != rerank_count:
                    raise ValueError("reranker returned the wrong number of scores")
                for item, score in zip(fused[:rerank_count], scores, strict=True):
                    item.rerank_score = float(score)
                fused[:rerank_count] = sorted(
                    fused[:rerank_count],
                    key=lambda item: (-float(item.rerank_score), item.chunk_id),
                )
                rerank_applied = True
            except Exception as exc:  # noqa: BLE001 - optional model failures must fall back
                rerank_warning = f"local reranker unavailable: {type(exc).__name__}"
                for item in fused:
                    item.rerank_score = None
        document_counts: dict[str, int] = {}
        final_results: list[KnowledgeSearchItem] = []
        for item in fused:
            count = document_counts.get(item.document_id, 0)
            if max_chunks_per_document is not None and count >= max_chunks_per_document:
                continue
            final_results.append(item)
            document_counts[item.document_id] = count + 1
            if len(final_results) >= limit:
                break
        combined_warning = "; ".join(dict.fromkeys([*warnings, *([rerank_warning] if rerank_warning else [])])) or None
        return KnowledgeSearchResult(
            query=query,
            query_id=traces[0]["query_id"],
            results=final_results,
            filtered_count=filtered_count,
            requested_mode=mode or self._default_retrieval_mode,
            applied_mode=next((applied for applied in applied_modes if applied != "none"), "none"),
            retrieval_warning=combined_warning,
            rerank_applied=rerank_applied,
        ), traces

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
                JOIN knowledge_sources s ON s.source_id = d.source_id
                WHERE d.status = 'active'
                  AND s.status = 'active'
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
                JOIN knowledge_sources s ON s.source_id = d.source_id
                WHERE d.status = 'active'
                  AND s.status = 'active'
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
        self._clear_candidate_cache()
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
        offset: int = 0,
        tool_name: str | None = None,
        source_ids: list[str] | None = None,
        account_ids: list[str] | None = None,
        provider_account_ids: list[str] | None = None,
    ) -> KnowledgeChunkLoadResult:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative character index")
        query_id = _stable_id("knowledge_load", ",".join(chunk_ids), _now_iso())
        if not chunk_ids:
            return KnowledgeChunkLoadResult(query_id=query_id, chunks=[])
        chunk_ids = chunk_ids[:MAX_LOAD_CHUNKS]
        provider_chunks: list[KnowledgeChunkRecord] = []
        provider_ids = [value for value in chunk_ids if any(p.handles_id(value) for p in self._source_providers)]
        for provider in self._source_providers:
            selected = [value for value in provider_ids if provider.handles_id(value)]
            if selected:
                provider_chunks.extend(provider.load_chunks(
                    chunk_ids=selected, max_chars_per_chunk=max_chars_per_chunk, offset=offset,
                    source_ids=source_ids,
                    account_ids=self._effective_provider_accounts(account_ids, provider_account_ids),
                ))
        chunk_ids = [value for value in chunk_ids if value not in provider_ids]
        if not chunk_ids:
            return KnowledgeChunkLoadResult(query_id=query_id, chunks=provider_chunks[:MAX_LOAD_CHUNKS])
        limited_ids = chunk_ids[:MAX_LOAD_CHUNKS]
        placeholders = ", ".join("?" for _ in limited_ids)
        scope_clause, scope_params = self._source_filter_sql(
            source_types=None, source_ids=source_ids, account_ids=account_ids,
        )
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"""
                SELECT
                    c.chunk_id, c.document_id, d.source_id, d.title, d.source_type, d.uri,
                    c.chunk_index, c.text, c.char_count, c.token_estimate,
                    c.source_ref, c.sensitivity, c.remote_policy
                FROM knowledge_chunks c
                JOIN knowledge_documents d ON d.document_id = c.document_id
                JOIN knowledge_sources s ON s.source_id = d.source_id
                WHERE c.chunk_id IN ({placeholders}) AND d.status = 'active'
                  {scope_clause}
                """,
                [*limited_ids, *scope_params],
            ).fetchall()
        finally:
            conn.close()
        rows_by_id = {row["chunk_id"]: row for row in rows}
        chunks: list[KnowledgeChunkRecord] = []
        filtered_count = 0
        # Loading is not a search preview. Keep it bounded by one stored chunk,
        # but honor requests beyond MAX_SNIPPET_CHARS and disclose partial views.
        char_limit = min(max(1, max_chars_per_chunk), MAX_CHUNK_CHARS)
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
            visible_text = decision.text[offset:offset + char_limit]
            end = min(len(decision.text), offset + len(visible_text))
            chunks.append(
                KnowledgeChunkRecord(
                    chunk_id=row["chunk_id"],
                    document_id=row["document_id"],
                    source_id=row["source_id"],
                    title=row["title"],
                    source_type=row["source_type"],
                    uri=row["uri"],
                    chunk_index=row["chunk_index"],
                    text=visible_text,
                    char_count=len(visible_text),
                    token_estimate=self._estimate_tokens(visible_text),
                    source_ref=row["source_ref"],
                    sensitivity=row["sensitivity"],
                    remote_policy=row["remote_policy"],
                    policy_decision=decision.policy_decision,
                    total_chars=len(decision.text),
                    truncated=len(visible_text) < len(decision.text),
                    offset=offset,
                    next_offset=end if end < len(decision.text) else None,
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
            chunks=[*chunks, *provider_chunks][:MAX_LOAD_CHUNKS],
            filtered_count=filtered_count,
        )

    def load_document(
        self,
        *,
        document_id: str,
        include_text: bool = False,
        max_chars: int = MAX_LOAD_DOCUMENT_CHARS,
        tool_name: str | None = None,
        source_ids: list[str] | None = None,
        account_ids: list[str] | None = None,
        provider_account_ids: list[str] | None = None,
    ) -> KnowledgeDocumentRecord:
        for provider in self._source_providers:
            if provider.handles_id(document_id):
                record = provider.load_document(
                    document_id=document_id, include_text=include_text, max_chars=max_chars,
                    source_ids=source_ids,
                    account_ids=self._effective_provider_accounts(account_ids, provider_account_ids),
                )
                if record is None:
                    raise ValueError(f"knowledge document not found: {document_id}")
                return record
        conn = self._conn_factory()
        scope_clause, scope_params = self._source_filter_sql(
            source_types=None, source_ids=source_ids, account_ids=account_ids,
        )
        try:
            row = conn.execute(
                f"""
                SELECT
                    d.document_id, d.source_id, d.source_type, d.title, d.uri, d.checksum,
                    d.mime_type, d.status, d.sensitivity, d.remote_policy, d.source_ref, d.metadata
                FROM knowledge_documents d
                JOIN knowledge_sources s ON s.source_id = d.source_id
                WHERE d.document_id = ? AND d.status = 'active' {scope_clause}
                """,
                [document_id, *scope_params],
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
        source_ids: list[str] | None,
        account_ids: list[str] | None,
        max_candidates: int,
        sort_by: Literal["relevance", "source_time_desc"],
    ) -> list[sqlite3.Row]:
        normalized_query = query.strip()
        source_clause, source_params = self._source_filter_sql(
            source_types=source_types,
            source_ids=source_ids,
            account_ids=account_ids,
        )
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
                    JOIN knowledge_sources s ON s.source_id = d.source_id
                    WHERE knowledge_chunks_fts MATCH ? AND d.status = 'active' {source_clause}
                    ORDER BY rank, c.chunk_id
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
                    JOIN knowledge_sources s ON s.source_id = d.source_id
                    WHERE knowledge_chunks_fts MATCH ? AND d.status = 'active' {source_clause}
                    ORDER BY COALESCE(
                        json_extract(d.metadata, '$.source_time'),
                        json_extract(d.metadata, '$.received_at'),
                        d.updated_at
                    ) DESC,
                        c.chunk_index ASC, c.chunk_id ASC
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
                    JOIN knowledge_sources s ON s.source_id = d.source_id
                    WHERE d.status = 'active' {source_clause}
                    ORDER BY COALESCE(
                        json_extract(d.metadata, '$.source_time'),
                        json_extract(d.metadata, '$.received_at'),
                        d.updated_at
                    ) DESC,
                        c.chunk_index ASC, c.chunk_id ASC
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
        source_ids: list[str] | None,
        account_ids: list[str] | None,
        sort_by: Literal["relevance", "source_time_desc"],
    ) -> list[RetrievalCandidate]:
        rows = self._search_rows(
            query=query,
            limit=limit,
            source_types=source_types,
            source_ids=source_ids,
            account_ids=account_ids,
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
        source_ids: list[str] | None = None,
        account_ids: list[str] | None = None,
    ) -> list[sqlite3.Row]:
        if not chunk_ids:
            return []
        source_clause, source_params = self._source_filter_sql(
            source_types=source_types,
            source_ids=source_ids,
            account_ids=account_ids,
        )
        placeholders = ", ".join("?" for _ in chunk_ids)
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"""
                SELECT
                    c.chunk_id, c.document_id, d.source_id, d.title, d.source_type, d.uri,
                    c.chunk_index, c.text AS snippet, c.source_ref,
                    c.sensitivity, c.remote_policy
                FROM knowledge_chunks c
                JOIN knowledge_documents d ON d.document_id = c.document_id
                JOIN knowledge_sources s ON s.source_id = d.source_id
                WHERE c.chunk_id IN ({placeholders})
                  AND d.status = 'active' {source_clause}
                """,
                [*chunk_ids, *source_params],
            ).fetchall()
        finally:
            conn.close()
        by_id = {str(row["chunk_id"]): row for row in rows}
        return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]

    @staticmethod
    def _source_filter_sql(
        *,
        source_types: list[str] | None,
        source_ids: list[str] | None,
        account_ids: list[str] | None,
    ) -> tuple[str, list[Any]]:
        clauses = ["AND s.status = 'active'"]
        params: list[Any] = []
        for column, values in (("d.source_type", source_types), ("d.source_id", source_ids)):
            if values is not None:
                if not values:
                    return "AND 0", []
                clauses.append(f"AND {column} IN ({', '.join('?' for _ in values)})")
                params.extend(values)
        if account_ids is not None:
            if not account_ids:
                return "AND 0", []
            account_expr = (
                "COALESCE(json_extract(d.metadata, '$.account_id'), "
                "json_extract(d.metadata, '$.mail_account_id'), "
                "json_extract(s.metadata, '$.account_id'))"
            )
            clauses.append(
                f"AND ({account_expr} IS NULL OR {account_expr} IN ("
                + ", ".join("?" for _ in account_ids) + "))"
            )
            params.extend(account_ids)
        return " ".join(clauses), params

    def _scoped_semantic_candidates(
        self,
        *,
        query: str,
        limit: int,
        source_types: list[str] | None,
        source_ids: list[str] | None,
        account_ids: list[str] | None,
    ) -> list[RetrievalCandidate]:
        # sqlite-vec KNN is global. Grow the bounded search window until enough
        # scoped hits survive, without allowing out-of-scope text into results.
        if self._semantic_index is None or self._embedding_provider is None or not query.strip():
            return []
        status = self._semantic_index.status()
        if not status.available:
            return []
        indexed = status.indexed_chunks
        window = min(max(limit, 1), indexed, 2048)
        vector = self._embedding_provider.embed_query(query) if window else []
        scoped_search = getattr(self._semantic_index, "search_scoped", None)
        if callable(scoped_search) and vector:
            hits = scoped_search(
                vector, limit, source_types=source_types,
                source_ids=source_ids, account_ids=account_ids,
            )
            return [
                RetrievalCandidate(chunk_id=hit.chunk_id, rank=i + 1, score=hit.score, channel="semantic")
                for i, hit in enumerate(hits)
            ]
        while window:
            hits = self._semantic_index.search(vector, window)
            allowed = {
                row["chunk_id"] for row in self._rows_for_chunk_ids(
                    chunk_ids=[hit.chunk_id for hit in hits],
                    source_types=source_types,
                    source_ids=source_ids,
                    account_ids=account_ids,
                )
            }
            scoped = [hit for hit in hits if hit.chunk_id in allowed]
            if len(scoped) >= limit or window >= min(indexed, 2048):
                return [
                    RetrievalCandidate(chunk_id=hit.chunk_id, rank=i + 1, score=hit.score, channel="semantic")
                    for i, hit in enumerate(scoped[:limit])
                ]
            window = min(window * 2, indexed, 2048)
        return []

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
            JOIN knowledge_sources s ON s.source_id = d.source_id
            WHERE d.status = 'active' {source_clause} AND ({match_clause})
            ORDER BY COALESCE(
                json_extract(d.metadata, '$.source_time'),
                json_extract(d.metadata, '$.received_at'),
                d.updated_at
            ) DESC,
                c.chunk_index ASC, c.chunk_id ASC
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
