"""Exact, bounded-memory cosine search over MySQL knowledge vector blobs."""
from __future__ import annotations

import heapq
import math
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from app.domains.knowledge_retrieval import (
    EmbeddingModelInfo,
    SemanticHit,
    SemanticIndex,
    SemanticIndexStatus,
    SemanticVectorRecord,
)

_BATCH = 256


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MySQLVectorSemanticIndex(SemanticIndex):
    """MySQL implementation using metadata IDs and float32 BLOB payloads.

    Every search streams all eligible vectors in bounded keyset batches, so
    filtering never truncates the candidate population before top-k ranking.
    """

    def __init__(self, *, conn_factory: Callable[[], Any], model_info: EmbeddingModelInfo):
        self._conn_factory = conn_factory
        self._model_info = model_info

    def _execute(self, conn, sql: str, params=()):
        if getattr(conn, "dialect", None) == "mysql":
            return conn.execute_native(sql, params)
        return conn.execute(sql.replace("%s", "?"), params)

    @staticmethod
    def _begin(conn) -> None:
        if getattr(conn, "dialect", None) == "mysql":
            conn.execute_native("START TRANSACTION")
        else:
            conn.execute("BEGIN IMMEDIATE")

    def _vector(self, vector: Sequence[float]) -> tuple[bytes, Any]:
        import numpy as np

        values = np.asarray(vector, dtype=np.float32)
        if values.ndim != 1 or values.size != self._model_info.dimensions:
            raise ValueError(
                f"vector dimension mismatch: expected {self._model_info.dimensions}, got {values.size}"
            )
        if not np.isfinite(values).all():
            raise ValueError("embedding vector must contain only finite values")
        norm = float(np.linalg.norm(values))
        if not math.isfinite(norm) or norm <= 0.0:
            raise ValueError("embedding vector must have nonzero finite norm")
        return values.astype("<f4", copy=False).tobytes(), values

    def upsert(self, records: Sequence[SemanticVectorRecord]) -> tuple[int, int]:
        if not records:
            return 0, 0
        prepared = [(record, *self._vector(record.vector)) for record in records]
        inserted = updated = 0
        for start in range(0, len(prepared), _BATCH):
            conn = self._conn_factory()
            try:
                self._begin(conn)
                for record, blob, _ in prepared[start:start + _BATCH]:
                    lock_suffix = " FOR UPDATE" if getattr(conn, "dialect", None) == "mysql" else ""
                    existing = self._execute(
                        conn,
                        "SELECT vector_id,chunk_checksum FROM knowledge_embedding_records "
                        "WHERE index_key=%s AND chunk_id=%s" + lock_suffix,
                        (self._model_info.index_key, record.chunk_id),
                    ).fetchone()
                    if existing is not None:
                        vector_id, old_checksum = int(existing[0]), str(existing[1])
                        payload = self._execute(
                            conn, "SELECT 1 FROM knowledge_vector_blobs WHERE vector_id=%s", (vector_id,),
                        ).fetchone()
                        if old_checksum == record.checksum and payload is not None:
                            continue
                        self._execute(
                            conn,
                            "UPDATE knowledge_embedding_records SET chunk_checksum=%s,updated_at=%s "
                            "WHERE vector_id=%s",
                            (record.checksum, _now(), vector_id),
                        )
                        self._execute(
                            conn,
                            "INSERT INTO knowledge_vector_blobs(vector_id,embedding) VALUES(%s,%s) "
                            + ("ON DUPLICATE KEY UPDATE embedding=VALUES(embedding)"
                               if getattr(conn, "dialect", None) == "mysql"
                               else "ON CONFLICT(vector_id) DO UPDATE SET embedding=excluded.embedding"),
                            (vector_id, blob),
                        )
                        updated += 1
                    else:
                        cursor = self._execute(
                            conn,
                            "INSERT INTO knowledge_embedding_records(index_key,chunk_id,chunk_checksum,"
                            "provider_name,model_name,dimensions,created_at,updated_at) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                            (self._model_info.index_key, record.chunk_id, record.checksum,
                             self._model_info.provider_name, self._model_info.model_name,
                             self._model_info.dimensions, _now(), _now()),
                        )
                        vector_id = int(cursor.lastrowid)
                        self._execute(
                            conn, "INSERT INTO knowledge_vector_blobs(vector_id,embedding) VALUES(%s,%s)",
                            (vector_id, blob),
                        )
                        inserted += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        return inserted, updated

    def remove(self, chunk_ids: Sequence[str]) -> int:
        if not chunk_ids:
            return 0
        conn = self._conn_factory()
        count = 0
        try:
            for start in range(0, len(chunk_ids), _BATCH):
                batch = chunk_ids[start:start + _BATCH]
                marks = ",".join("%s" for _ in batch)
                cursor = self._execute(
                    conn, f"DELETE FROM knowledge_embedding_records WHERE index_key=%s "
                    f"AND chunk_id IN ({marks})", (self._model_info.index_key, *batch),
                )
                count += max(0, int(cursor.rowcount))
            conn.commit()
            return count
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reconcile(self, active_chunk_ids: Iterable[str]) -> int:
        active = set(active_chunk_ids)
        conn = self._conn_factory()
        removed = 0
        last_id = 0
        try:
            while True:
                rows = self._execute(
                    conn, "SELECT vector_id,chunk_id FROM knowledge_embedding_records "
                    "WHERE index_key=%s AND vector_id>%s ORDER BY vector_id LIMIT %s",
                    (self._model_info.index_key, last_id, _BATCH),
                ).fetchall()
                if not rows:
                    break
                last_id = int(rows[-1][0])
                stale = [int(row[0]) for row in rows if str(row[1]) not in active]
                for start in range(0, len(stale), _BATCH):
                    group = stale[start:start + _BATCH]
                    marks = ",".join("%s" for _ in group)
                    cursor = self._execute(
                        conn, f"DELETE FROM knowledge_embedding_records WHERE vector_id IN ({marks})", group,
                    )
                    removed += max(0, int(cursor.rowcount))
            conn.commit()
            return removed
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def search(self, vector: Sequence[float], limit: int) -> list[SemanticHit]:
        return self._search(vector, limit, scoped=False)

    def search_scoped(
        self, vector: Sequence[float], limit: int, *, source_types=None, source_ids=None, account_ids=None,
    ) -> list[SemanticHit]:
        return self._search(
            vector, limit, scoped=True, source_types=source_types, source_ids=source_ids,
            account_ids=account_ids,
        )

    def _search(self, vector, limit, *, scoped, source_types=None, source_ids=None, account_ids=None):
        if limit < 1:
            return []
        _, query = self._vector(vector)
        if scoped and any(values is not None and not values for values in (source_types, source_ids, account_ids)):
            return []
        params: list[object] = [self._model_info.index_key]
        where = ["m.index_key=%s"]
        joins = "JOIN knowledge_vector_blobs b ON b.vector_id=m.vector_id"
        if scoped:
            joins += (" JOIN knowledge_chunks c ON c.chunk_id=m.chunk_id"
                      " JOIN knowledge_documents d ON d.document_id=c.document_id"
                      " JOIN knowledge_sources s ON s.source_id=d.source_id")
            where.extend([
                "d.status='active'", "s.status='active'", "c.sensitivity!='secret'",
                "c.remote_policy NOT IN ('deny','confirm')",
            ])
            for col, values in (("d.source_type", source_types), ("d.source_id", source_ids)):
                if values is not None:
                    where.append(f"{col} IN ({','.join('%s' for _ in values)})")
                    params.extend(values)
            if account_ids is not None:
                account = ("COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(d.metadata,'$.account_id')) COLLATE utf8mb4_0900_bin,_utf8mb4'null' COLLATE utf8mb4_0900_bin),"
                           "NULLIF(JSON_UNQUOTE(JSON_EXTRACT(d.metadata,'$.mail_account_id')) COLLATE utf8mb4_0900_bin,_utf8mb4'null' COLLATE utf8mb4_0900_bin),"
                           "NULLIF(JSON_UNQUOTE(JSON_EXTRACT(s.metadata,'$.account_id')) COLLATE utf8mb4_0900_bin,_utf8mb4'null' COLLATE utf8mb4_0900_bin))")
                where.append(f"({account} IS NULL OR {account} IN ({','.join('%s' for _ in account_ids)}))")
                params.extend(account_ids)
        sql = ("SELECT m.vector_id,m.chunk_id,b.embedding FROM knowledge_embedding_records m "
               f"{joins} WHERE {' AND '.join(where)} AND m.vector_id>%s "
               "ORDER BY m.vector_id LIMIT %s")
        conn = self._conn_factory()
        heap: list[tuple[float, int, str]] = []
        last_id = 0
        try:
            while True:
                rows = self._execute(conn, sql, (*params, last_id, _BATCH)).fetchall()
                if not rows:
                    break
                last_id = int(rows[-1][0])
                for row in rows:
                    vector_id, chunk_id, blob = int(row[0]), str(row[1]), row[2]
                    import numpy as np

                    candidate = np.frombuffer(blob, dtype="<f4")
                    if candidate.size != self._model_info.dimensions or not np.isfinite(candidate).all():
                        raise ValueError(f"stored embedding {vector_id} has invalid dimensions or values")
                    norm = float(np.linalg.norm(candidate))
                    if not math.isfinite(norm) or norm <= 0:
                        raise ValueError(f"stored embedding {vector_id} has zero or invalid norm")
                    score = float(np.dot(query, candidate) / (float(np.linalg.norm(query)) * norm))
                    score = max(-1.0, min(1.0, score))
                    item = (score, -vector_id, chunk_id)
                    if len(heap) < limit:
                        heapq.heappush(heap, item)
                    elif item > heap[0]:
                        heapq.heapreplace(heap, item)
            ranked = sorted(heap, key=lambda item: (-item[0], -item[1]))
            return [SemanticHit(chunk_id=item[2], rank=i + 1, score=item[0]) for i, item in enumerate(ranked)]
        finally:
            conn.close()

    def status(self) -> SemanticIndexStatus:
        conn = self._conn_factory()
        try:
            row = self._execute(
                conn, "SELECT COUNT(*) FROM knowledge_embedding_records WHERE index_key=%s",
                (self._model_info.index_key,),
            ).fetchone()
            count = int(row[0])
            return SemanticIndexStatus(bool(count), self._model_info.index_key, count)
        finally:
            conn.close()
