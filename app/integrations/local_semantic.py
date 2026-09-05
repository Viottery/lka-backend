"""Local FastEmbed and sqlite-vec implementations for knowledge retrieval."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from hashlib import sha256

from app.domains.knowledge_retrieval import (
    EmbeddingModelInfo,
    EmbeddingProvider,
    SemanticHit,
    SemanticIndex,
    SemanticIndexStatus,
    SemanticVectorRecord,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FastEmbedEmbeddingProvider(EmbeddingProvider):
    """Lazy local ONNX embedding provider with explicit query/passage transforms."""

    def __init__(
        self,
        *,
        model_name: str,
        dimensions: int,
        cache_dir: str,
        batch_size: int,
        query_prefix: str = "",
        normalized: bool = True,
        local_files_only: bool = True,
    ) -> None:
        self._model_info = EmbeddingModelInfo(
            provider_name="fastembed",
            model_name=model_name,
            dimensions=dimensions,
            normalized=normalized,
        )
        self._cache_dir = cache_dir
        self._batch_size = batch_size
        self._query_prefix = query_prefix
        self._local_files_only = local_files_only
        self._model = None

    @property
    def model_info(self) -> EmbeddingModelInfo:
        return self._model_info

    def embed_passages(self, passages: Sequence[str]) -> list[list[float]]:
        return self._embed(passages)

    def embed_query(self, query: str) -> list[float]:
        values = self._embed([f"{self._query_prefix}{query}"])
        return values[0] if values else []

    def prepare(self, *, allow_model_download: bool) -> None:
        if self._model is None:
            self._local_files_only = not allow_model_download

    def _embed(self, values: Sequence[str]) -> list[list[float]]:
        if not values:
            return []
        model = self._get_model()
        vectors = list(model.embed(values, batch_size=self._batch_size))
        normalized = [self._normalize(vector.tolist()) for vector in vectors]
        for vector in normalized:
            if len(vector) != self._model_info.dimensions:
                raise ValueError(
                    f"embedding dimension mismatch: expected {self._model_info.dimensions}, got {len(vector)}"
                )
        return normalized

    def _get_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(
                model_name=self._model_info.model_name,
                cache_dir=self._cache_dir,
                lazy_load=True,
                local_files_only=self._local_files_only,
            )
        return self._model

    def _normalize(self, vector: list[float]) -> list[float]:
        if not self._model_info.normalized:
            return [float(value) for value in vector]
        length_squared = sum(float(value) * float(value) for value in vector)
        if length_squared <= 0:
            return [float(value) for value in vector]
        scale = length_squared**-0.5
        return [float(value) * scale for value in vector]


class SQLiteVecSemanticIndex(SemanticIndex):
    """Local sqlite-vec index with SQLite-owned chunk-to-vector lifecycle metadata."""

    def __init__(
        self,
        *,
        conn_factory: Callable[[], sqlite3.Connection],
        model_info: EmbeddingModelInfo,
    ) -> None:
        self._conn_factory = conn_factory
        self._model_info = model_info
        self._table_name = self._table_name_for(model_info.index_key)

    def upsert(self, records: Sequence[SemanticVectorRecord]) -> tuple[int, int]:
        if not records:
            return 0, 0
        conn = self._connect()
        inserted = 0
        updated = 0
        try:
            for record in records:
                existing = conn.execute(
                    """
                    SELECT vector_id, chunk_checksum FROM knowledge_embedding_records
                    WHERE index_key = ? AND chunk_id = ?
                    """,
                    (self._model_info.index_key, record.chunk_id),
                ).fetchone()
                if existing is not None and existing["chunk_checksum"] == record.checksum:
                    continue
                if existing is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO knowledge_embedding_records(
                            index_key, chunk_id, chunk_checksum, provider_name, model_name,
                            dimensions, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self._model_info.index_key,
                            record.chunk_id,
                            record.checksum,
                            self._model_info.provider_name,
                            self._model_info.model_name,
                            self._model_info.dimensions,
                            _now_iso(),
                            _now_iso(),
                        ),
                    )
                    vector_id = int(cursor.lastrowid)
                    inserted += 1
                else:
                    vector_id = int(existing["vector_id"])
                    conn.execute(f"DELETE FROM {self._table_name} WHERE vector_id = ?", (vector_id,))
                    conn.execute(
                        """
                        UPDATE knowledge_embedding_records
                        SET chunk_checksum = ?, updated_at = ?
                        WHERE vector_id = ?
                        """,
                        (record.checksum, _now_iso(), vector_id),
                    )
                    updated += 1
                conn.execute(
                    f"INSERT INTO {self._table_name}(vector_id, embedding) VALUES(?, ?)",
                    (vector_id, self._serialize(record.vector)),
                )
            conn.commit()
        finally:
            conn.close()
        return inserted, updated

    def remove(self, chunk_ids: Sequence[str]) -> int:
        if not chunk_ids:
            return 0
        conn = self._connect()
        try:
            placeholders = ", ".join("?" for _ in chunk_ids)
            rows = conn.execute(
                f"""
                SELECT vector_id FROM knowledge_embedding_records
                WHERE index_key = ? AND chunk_id IN ({placeholders})
                """,
                [self._model_info.index_key, *chunk_ids],
            ).fetchall()
            for row in rows:
                conn.execute(f"DELETE FROM {self._table_name} WHERE vector_id = ?", (row["vector_id"],))
            conn.execute(
                f"""
                DELETE FROM knowledge_embedding_records
                WHERE index_key = ? AND chunk_id IN ({placeholders})
                """,
                [self._model_info.index_key, *chunk_ids],
            )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def reconcile(self, active_chunk_ids: Iterable[str]) -> int:
        active = set(active_chunk_ids)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT vector_id, chunk_id FROM knowledge_embedding_records WHERE index_key = ?",
                (self._model_info.index_key,),
            ).fetchall()
            stale_ids = [int(row["vector_id"]) for row in rows if row["chunk_id"] not in active]
        finally:
            conn.close()
        if not stale_ids:
            return 0
        return self._remove_vector_ids(stale_ids)

    def search(self, vector: Sequence[float], limit: int) -> list[SemanticHit]:
        if not vector or limit < 1:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT vector_id, distance FROM {self._table_name}
                WHERE embedding MATCH ? AND k = ?
                """,
                (self._serialize(vector), limit),
            ).fetchall()
            vector_ids = [int(row["vector_id"]) for row in rows]
            if not vector_ids:
                return []
            placeholders = ", ".join("?" for _ in vector_ids)
            mapping_rows = conn.execute(
                f"""
                SELECT vector_id, chunk_id FROM knowledge_embedding_records
                WHERE index_key = ? AND vector_id IN ({placeholders})
                """,
                [self._model_info.index_key, *vector_ids],
            ).fetchall()
            chunk_ids = {int(row["vector_id"]): str(row["chunk_id"]) for row in mapping_rows}
            return [
                SemanticHit(
                    chunk_id=chunk_ids[int(row["vector_id"])],
                    rank=index + 1,
                    score=1 - float(row["distance"]),
                )
                for index, row in enumerate(rows)
                if int(row["vector_id"]) in chunk_ids
            ]
        finally:
            conn.close()

    def status(self) -> SemanticIndexStatus:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM knowledge_embedding_records WHERE index_key = ?",
                (self._model_info.index_key,),
            ).fetchone()
            return SemanticIndexStatus(
                available=bool(row["count"]),
                index_key=self._model_info.index_key,
                indexed_chunks=int(row["count"]),
            )
        finally:
            conn.close()

    def _remove_vector_ids(self, vector_ids: Sequence[int]) -> int:
        conn = self._connect()
        try:
            for vector_id in vector_ids:
                conn.execute(f"DELETE FROM {self._table_name} WHERE vector_id = ?", (vector_id,))
            placeholders = ", ".join("?" for _ in vector_ids)
            conn.execute(
                f"DELETE FROM knowledge_embedding_records WHERE vector_id IN ({placeholders})",
                vector_ids,
            )
            conn.commit()
            return len(vector_ids)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        import sqlite_vec

        conn = self._conn_factory()
        sqlite_vec.load(conn)
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {self._table_name} USING vec0(
                vector_id INTEGER PRIMARY KEY,
                embedding float[{self._model_info.dimensions}] distance_metric=cosine
            )
            """
        )
        return conn

    def _serialize(self, vector: Sequence[float]) -> bytes:
        import sqlite_vec

        if len(vector) != self._model_info.dimensions:
            raise ValueError(
                f"vector dimension mismatch: expected {self._model_info.dimensions}, got {len(vector)}"
            )
        return sqlite_vec.serialize_float32(list(vector))

    def _table_name_for(self, index_key: str) -> str:
        digest = sha256(index_key.encode("utf-8")).hexdigest()[:16]
        return f"knowledge_vec_{digest}"


def build_local_semantic_components(
    *,
    conn_factory: Callable[[], sqlite3.Connection],
    enabled: bool,
    provider_name: str,
    index_provider_name: str,
    model_name: str,
    dimensions: int,
    cache_dir: str,
    batch_size: int,
    query_prefix: str,
    normalize_embeddings: bool,
    local_files_only: bool,
) -> tuple[EmbeddingProvider | None, SemanticIndex | None]:
    """Build configured local components while keeping runtime independent of implementations."""

    if not enabled:
        return None, None
    if provider_name != "fastembed" or index_provider_name != "sqlite_vec":
        return None, None
    provider = FastEmbedEmbeddingProvider(
        model_name=model_name,
        dimensions=dimensions,
        cache_dir=cache_dir,
        batch_size=batch_size,
        query_prefix=query_prefix,
        normalized=normalize_embeddings,
        local_files_only=local_files_only,
    )
    return provider, SQLiteVecSemanticIndex(
        conn_factory=conn_factory,
        model_info=provider.model_info,
    )
