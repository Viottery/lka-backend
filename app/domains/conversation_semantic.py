"""Opt-in local semantic index for source-linked conversation history."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from array import array
from collections import OrderedDict
from collections.abc import Callable
from contextlib import closing
from threading import RLock
from typing import Any

try:
    import numpy as np
except ImportError:  # numpy is an optional acceleration, not a requirement.
    np = None


class ConversationSemanticIndex:
    """Incremental source index. Model construction and policy stay with caller."""

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection],
                 embedding_provider: Any, model_revision: str = "1",
                 batch_size: int = 32, max_candidates: int = 20000,
                 matrix_sessions: int = 2,
                 max_matrix_cache_bytes: int = 64 * 1024 * 1024) -> None:
        self._conn = conn_factory
        self._provider = embedding_provider
        self._revision = str(model_revision)
        self._batch_size = max(1, int(batch_size))
        self._max_candidates = max(1, int(max_candidates))
        self._matrix_sessions = max(1, int(matrix_sessions))
        self._max_matrix_cache_bytes = max(0, int(max_matrix_cache_bytes))
        model_info = getattr(embedding_provider, "model_info", None)
        key = getattr(model_info, "index_key", "")
        if callable(key):
            key = key()
        dimensions = getattr(model_info, "dimensions", None)
        self._expected_dimensions = int(dimensions) if dimensions is not None else None
        self._fingerprint = hashlib.sha256(f"{key}\0{self._revision}".encode()).hexdigest()
        self._lock = RLock()
        self._schema_ready = False
        self._matrix_cache: OrderedDict[str, tuple[int, int, list[int], Any, int]] = OrderedDict()
        self._query_cache: OrderedDict[str, tuple[str, list[float]]] = OrderedDict()
        self._eligible_count_cache: OrderedDict[tuple[str, int], int] = OrderedDict()

    def ensure_schema(self) -> None:
        with self._lock:
            if self._schema_ready:
                return
            with closing(self._conn()) as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS conversation_semantic_state (
                        session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        watermark INTEGER NOT NULL DEFAULT 0,
                        source_revision INTEGER NOT NULL DEFAULT 0,
                        index_revision INTEGER NOT NULL DEFAULT 0,
                        dimensions INTEGER,
                        PRIMARY KEY(session_id,fingerprint)
                    );
                    CREATE TABLE IF NOT EXISTS conversation_semantic_dirty (
                        session_id TEXT NOT NULL, fingerprint TEXT NOT NULL, seq INTEGER NOT NULL,
                        generation INTEGER NOT NULL, PRIMARY KEY(session_id,fingerprint,seq)
                    );
                    CREATE INDEX IF NOT EXISTS conversation_semantic_dirty_order
                        ON conversation_semantic_dirty(session_id,fingerprint,seq);
                    CREATE TABLE IF NOT EXISTS conversation_semantic_vectors (
                        session_id TEXT NOT NULL, seq INTEGER NOT NULL,
                        fingerprint TEXT NOT NULL, source_hash TEXT NOT NULL,
                        vector BLOB NOT NULL, dimensions INTEGER NOT NULL,
                        PRIMARY KEY(session_id,seq,fingerprint)
                    );
                    CREATE INDEX IF NOT EXISTS conversation_semantic_vectors_order
                        ON conversation_semantic_vectors(session_id,fingerprint,seq);
                    CREATE TRIGGER IF NOT EXISTS conversation_semantic_new_session
                    AFTER INSERT ON agent_sessions BEGIN
                      INSERT OR IGNORE INTO conversation_semantic_state(session_id,fingerprint)
                      SELECT NEW.session_id,fingerprint FROM conversation_semantic_state
                      GROUP BY fingerprint;
                    END;
                    CREATE TRIGGER IF NOT EXISTS conversation_semantic_insert
                    AFTER INSERT ON agent_session_context_messages BEGIN
                      INSERT INTO conversation_semantic_state(session_id,fingerprint,source_revision)
                      SELECT NEW.session_id,fingerprint,1 FROM conversation_semantic_state
                      WHERE session_id=NEW.session_id GROUP BY fingerprint
                      ON CONFLICT(session_id,fingerprint) DO UPDATE SET source_revision=source_revision+1;
                      INSERT INTO conversation_semantic_dirty(session_id,fingerprint,seq,generation)
                      SELECT session_id,fingerprint,NEW.seq,source_revision FROM conversation_semantic_state
                      WHERE session_id=NEW.session_id
                      ON CONFLICT(session_id,fingerprint,seq) DO UPDATE SET generation=excluded.generation;
                      INSERT INTO conversation_semantic_dirty(session_id,fingerprint,seq,generation)
                      SELECT m.session_id,s.fingerprint,m.seq,s.source_revision FROM agent_session_context_messages m
                      JOIN conversation_semantic_state s ON s.session_id=m.session_id
                      WHERE m.session_id=NEW.session_id AND NEW.trace_id IS NOT NULL
                        AND m.trace_id=NEW.trace_id AND m.role='agent'
                      ON CONFLICT(session_id,fingerprint,seq) DO UPDATE SET generation=excluded.generation;
                    END;
                    CREATE TRIGGER IF NOT EXISTS conversation_semantic_update
                    AFTER UPDATE OF role,content,created_at,trace_id,session_id
                    ON agent_session_context_messages BEGIN
                      INSERT INTO conversation_semantic_state(session_id,fingerprint,source_revision)
                      SELECT OLD.session_id,fingerprint,1 FROM conversation_semantic_state
                      WHERE session_id=OLD.session_id GROUP BY fingerprint
                      ON CONFLICT(session_id,fingerprint) DO UPDATE SET source_revision=source_revision+1;
                      INSERT INTO conversation_semantic_dirty(session_id,fingerprint,seq,generation)
                      SELECT session_id,fingerprint,OLD.seq,source_revision FROM conversation_semantic_state
                      WHERE session_id=OLD.session_id
                      ON CONFLICT(session_id,fingerprint,seq) DO UPDATE SET generation=excluded.generation;
                      INSERT INTO conversation_semantic_dirty(session_id,fingerprint,seq,generation)
                      SELECT m.session_id,s.fingerprint,m.seq,s.source_revision FROM agent_session_context_messages m
                      JOIN conversation_semantic_state s ON s.session_id=m.session_id
                      WHERE m.session_id=OLD.session_id AND OLD.trace_id IS NOT NULL
                        AND m.trace_id=OLD.trace_id AND m.role='agent'
                      ON CONFLICT(session_id,fingerprint,seq) DO UPDATE SET generation=excluded.generation;
                      INSERT INTO conversation_semantic_state(session_id,fingerprint,source_revision)
                      SELECT NEW.session_id,fingerprint,1 FROM conversation_semantic_state
                      WHERE session_id=NEW.session_id GROUP BY fingerprint
                      ON CONFLICT(session_id,fingerprint) DO UPDATE SET source_revision=source_revision+1;
                      INSERT INTO conversation_semantic_dirty(session_id,fingerprint,seq,generation)
                      SELECT session_id,fingerprint,NEW.seq,source_revision FROM conversation_semantic_state
                      WHERE session_id=NEW.session_id
                      ON CONFLICT(session_id,fingerprint,seq) DO UPDATE SET generation=excluded.generation;
                      INSERT INTO conversation_semantic_dirty(session_id,fingerprint,seq,generation)
                      SELECT m.session_id,s.fingerprint,m.seq,s.source_revision FROM agent_session_context_messages m
                      JOIN conversation_semantic_state s ON s.session_id=m.session_id
                      WHERE m.session_id=NEW.session_id AND NEW.trace_id IS NOT NULL
                        AND m.trace_id=NEW.trace_id AND m.role='agent'
                      ON CONFLICT(session_id,fingerprint,seq) DO UPDATE SET generation=excluded.generation;
                    END;
                    CREATE TRIGGER IF NOT EXISTS conversation_semantic_delete
                    AFTER DELETE ON agent_session_context_messages BEGIN
                      INSERT INTO conversation_semantic_state(session_id,fingerprint,source_revision)
                      SELECT OLD.session_id,fingerprint,1 FROM conversation_semantic_state
                      WHERE session_id=OLD.session_id GROUP BY fingerprint
                      ON CONFLICT(session_id,fingerprint) DO UPDATE SET source_revision=source_revision+1;
                      INSERT INTO conversation_semantic_dirty(session_id,fingerprint,seq,generation)
                      SELECT session_id,fingerprint,OLD.seq,source_revision FROM conversation_semantic_state
                      WHERE session_id=OLD.session_id
                      ON CONFLICT(session_id,fingerprint,seq) DO UPDATE SET generation=excluded.generation;
                      INSERT INTO conversation_semantic_dirty(session_id,fingerprint,seq,generation)
                      SELECT m.session_id,s.fingerprint,m.seq,s.source_revision FROM agent_session_context_messages m
                      JOIN conversation_semantic_state s ON s.session_id=m.session_id
                      WHERE m.session_id=OLD.session_id AND OLD.trace_id IS NOT NULL
                        AND m.trace_id=OLD.trace_id AND m.role='agent'
                      ON CONFLICT(session_id,fingerprint,seq) DO UPDATE SET generation=excluded.generation;
                    END;
                """)
                # Establish a scoped state row for a newly selected fingerprint.
                conn.execute("INSERT OR IGNORE INTO conversation_semantic_state(session_id,fingerprint) SELECT DISTINCT session_id,? FROM agent_sessions", (self._fingerprint,))
                # Cold-start data predates these triggers; watermark-based batches cover it.
                conn.commit()
            self._schema_ready = True

    @staticmethod
    def _eligible_sql() -> str:
        return """(m.role='user' OR (m.role='agent' AND m.trace_id IS NOT NULL
            AND EXISTS (SELECT 1 FROM agent_session_context_messages u
              WHERE u.session_id=m.session_id AND u.trace_id=m.trace_id AND u.role='user')
            AND NOT EXISTS (SELECT 1 FROM agent_session_context_messages t
              WHERE t.session_id=m.session_id AND t.trace_id=m.trace_id AND t.role='tool')))"""

    @staticmethod
    def _hash(row: Any) -> str:
        raw = json.dumps([row["session_id"], row["seq"], row["role"], row["content"],
                          row["created_at"], row["trace_id"]], ensure_ascii=False,
                         separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _pack(vector: Any) -> tuple[bytes, int, list[float]]:
        values = [float(value) for value in vector]
        if not values or any(not math.isfinite(value) for value in values):
            raise ValueError("invalid embedding vector")
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            raise ValueError("zero embedding vector")
        normalized = [value / norm for value in values]
        return struct.pack(f"<{len(values)}f", *normalized), len(values), normalized

    @staticmethod
    def _unpack(blob: bytes, dimensions: int) -> list[float]:
        return list(struct.unpack(f"<{dimensions}f", blob))

    def _session_ok(self, conn: sqlite3.Connection, sid: str) -> bool:
        row = conn.execute("SELECT kind,status FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()
        return bool(row and row["status"] != "deleted" and row["kind"] in {"ordinary", "manager"})

    def _status(self, conn: sqlite3.Connection, sid: str, state: str,
                indexed: int = 0, unavailable: bool = False) -> dict[str, Any]:
        row = conn.execute("SELECT watermark,source_revision,index_revision FROM conversation_semantic_state WHERE session_id=? AND fingerprint=?", (sid, self._fingerprint)).fetchone()
        watermark = int(row["watermark"]) if row else 0
        pending_dirty = conn.execute("SELECT 1 FROM conversation_semantic_dirty WHERE session_id=? AND fingerprint=? LIMIT 1", (sid, self._fingerprint)).fetchone()
        pending_cold = conn.execute("SELECT 1 FROM agent_session_context_messages WHERE session_id=? AND seq>? LIMIT 1", (sid, watermark)).fetchone()
        pending = bool(pending_dirty or pending_cold)
        generation = int(row["source_revision"]) if row else 0
        cache_key = (sid, generation)
        with self._lock:
            eligible_count = self._eligible_count_cache.get(cache_key)
            if eligible_count is not None:
                self._eligible_count_cache.move_to_end(cache_key)
        if eligible_count is None:
            eligible_count = conn.execute(f"SELECT COUNT(*) FROM agent_session_context_messages m WHERE m.session_id=? AND {self._eligible_sql()}", (sid,)).fetchone()[0]
            with self._lock:
                self._eligible_count_cache[cache_key] = int(eligible_count)
                while len(self._eligible_count_cache) > 64:
                    self._eligible_count_cache.popitem(last=False)
        count = conn.execute("SELECT COUNT(*) FROM conversation_semantic_vectors WHERE session_id=? AND fingerprint=?", (sid, self._fingerprint)).fetchone()[0]
        capped = max(0, int(eligible_count) - self._max_candidates)
        if pending and state == "ready":
            state = "warming"
        return {"state": state, "indexed": indexed, "indexed_count": int(count),
                "eligible_count": int(eligible_count), "coverage_capped": capped > 0,
                "omitted_count": capped, "pending": pending,
                "complete": not pending and not unavailable and not capped,
                "watermark": watermark, "generation": generation,
                "index_revision": int(row["index_revision"]) if row else 0,
                "unavailable": unavailable}

    def sync_batch(self, session_id: str) -> dict[str, Any]:
        """Process at most batch_size vectors, leaving remaining work queued."""
        self.ensure_schema()
        sid = str(session_id)
        try:
            with closing(self._conn()) as conn:
                conn.row_factory = sqlite3.Row
                if not self._session_ok(conn, sid):
                    return {"state": "unavailable", "unavailable": True, "indexed": 0, "pending": False}
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT OR IGNORE INTO conversation_semantic_state(session_id,fingerprint) VALUES(?,?)", (sid, self._fingerprint))
                state = conn.execute("SELECT * FROM conversation_semantic_state WHERE session_id=? AND fingerprint=?", (sid, self._fingerprint)).fetchone()
                watermark = int(state["watermark"])
                # Queue every bounded cold-scan row before advancing its watermark.
                scan = conn.execute("SELECT session_id,seq,role,content,created_at,trace_id FROM agent_session_context_messages WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?", (sid, watermark, self._batch_size)).fetchall()
                scan_high = max((int(row["seq"]) for row in scan), default=watermark)
                for source_row in scan:
                    conn.execute("""INSERT INTO conversation_semantic_dirty
                        (session_id,fingerprint,seq,generation) VALUES(?,?,?,?)
                        ON CONFLICT(session_id,fingerprint,seq) DO NOTHING""",
                        (sid, self._fingerprint, int(source_row["seq"]), int(state["source_revision"])))
                dirty = conn.execute("SELECT d.seq,d.generation FROM conversation_semantic_dirty d WHERE d.session_id=? AND d.fingerprint=? ORDER BY d.seq LIMIT ?", (sid, self._fingerprint, self._batch_size)).fetchall()
                dirty_gen = {int(row["seq"]): int(row["generation"]) for row in dirty}
                candidates: dict[int, Any] = {}
                changed_index = False
                for seq, dirty_generation in dirty_gen.items():
                    row = conn.execute("SELECT session_id,seq,role,content,created_at,trace_id FROM agent_session_context_messages WHERE session_id=? AND seq=?", (sid, seq)).fetchone()
                    if row is None:
                        removed = conn.execute("DELETE FROM conversation_semantic_vectors WHERE session_id=? AND seq=? AND fingerprint=?", (sid, seq, self._fingerprint)).rowcount
                        changed_index = changed_index or bool(removed)
                        conn.execute("DELETE FROM conversation_semantic_dirty WHERE session_id=? AND fingerprint=? AND seq=? AND generation=?", (sid, self._fingerprint, seq, dirty_generation))
                    elif conn.execute(f"SELECT 1 FROM agent_session_context_messages m WHERE m.session_id=? AND m.seq=? AND {self._eligible_sql()}", (sid, seq)).fetchone():
                        existing = conn.execute("SELECT source_hash FROM conversation_semantic_vectors WHERE session_id=? AND seq=? AND fingerprint=?", (sid, seq, self._fingerprint)).fetchone()
                        if existing and existing["source_hash"] == self._hash(row):
                            conn.execute("DELETE FROM conversation_semantic_dirty WHERE session_id=? AND fingerprint=? AND seq=? AND generation=?", (sid, self._fingerprint, seq, dirty_generation))
                        else:
                            candidates[seq] = row
                    else:
                        removed = conn.execute("DELETE FROM conversation_semantic_vectors WHERE session_id=? AND seq=? AND fingerprint=?", (sid, seq, self._fingerprint)).rowcount
                        changed_index = changed_index or bool(removed)
                        conn.execute("DELETE FROM conversation_semantic_dirty WHERE session_id=? AND fingerprint=? AND seq=? AND generation=?", (sid, self._fingerprint, seq, dirty_generation))
                count = conn.execute("SELECT COUNT(*) FROM conversation_semantic_vectors WHERE session_id=? AND fingerprint=?", (sid, self._fingerprint)).fetchone()[0]
                room = max(0, self._max_candidates - int(count))
                ordered = []
                for seq, row in sorted(candidates.items()):
                    exists = conn.execute("SELECT 1 FROM conversation_semantic_vectors WHERE session_id=? AND seq=? AND fingerprint=?", (sid, seq, self._fingerprint)).fetchone()
                    if exists or room:
                        ordered.append((seq, row))
                        if not exists:
                            room -= 1
                    elif seq in dirty_gen:
                        conn.execute("DELETE FROM conversation_semantic_dirty WHERE session_id=? AND fingerprint=? AND seq=? AND generation=?", (sid, self._fingerprint, seq, dirty_gen[seq]))
                    if len(ordered) >= self._batch_size:
                        break
                prepared_rows = [(seq, row, self._hash(row), dirty_gen[seq]) for seq, row in ordered]
                conn.execute("UPDATE conversation_semantic_state SET watermark=MAX(watermark,?) WHERE session_id=? AND fingerprint=?", (scan_high, sid, self._fingerprint))
                generation = int(state["source_revision"])
                conn.commit()

            vectors: list[Any] = []
            if prepared_rows:
                # Bound provider input while protecting content hashes over the complete source.
                vectors = self._provider.embed_passages([str(row["content"])[:2000] for _, row, _, _ in prepared_rows])
                if len(vectors) != len(prepared_rows):
                    raise ValueError("embedding count mismatch")
            packed = [(entry, self._pack(vector)) for entry, vector in zip(prepared_rows, vectors)]
            with closing(self._conn()) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                state_now = conn.execute("SELECT * FROM conversation_semantic_state WHERE session_id=? AND fingerprint=?", (sid, self._fingerprint)).fetchone()
                if not self._session_ok(conn, sid):
                    conn.rollback()
                    return {"state": "unavailable", "unavailable": True, "indexed": 0, "pending": False}
                published = 0
                dimensions = state_now["dimensions"]
                for ((seq, source, source_hash, source_generation), (blob, dims, _normalized)) in packed:
                    if self._expected_dimensions is not None and dims != self._expected_dimensions:
                        conn.rollback()
                        raise ValueError("embedding dimensions do not match model metadata")
                    if dimensions is not None and int(dimensions) != dims:
                        conn.rollback()
                        raise ValueError("embedding dimensions changed")
                    live = conn.execute(f"SELECT m.session_id,m.seq,m.role,m.content,m.created_at,m.trace_id FROM agent_session_context_messages m WHERE m.session_id=? AND m.seq=? AND {self._eligible_sql()}", (sid, seq)).fetchone()
                    if live is None or self._hash(live) != source_hash:
                        continue
                    conn.execute("INSERT INTO conversation_semantic_vectors(session_id,seq,fingerprint,source_hash,vector,dimensions) VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,seq,fingerprint) DO UPDATE SET source_hash=excluded.source_hash,vector=excluded.vector,dimensions=excluded.dimensions", (sid, seq, self._fingerprint, source_hash, blob, dims))
                    dimensions = dims
                    if source_generation is not None:
                        conn.execute("DELETE FROM conversation_semantic_dirty WHERE session_id=? AND fingerprint=? AND seq=? AND generation=?", (sid, self._fingerprint, seq, source_generation))
                    published += 1
                conn.execute("UPDATE conversation_semantic_state SET dimensions=COALESCE(dimensions,?),index_revision=index_revision+? WHERE session_id=? AND fingerprint=?", (dimensions, int(published > 0 or changed_index), sid, self._fingerprint))
                # The source-generation fence also catches edits to related trace eligibility.
                same_generation = int(state_now["source_revision"]) == generation
                conn.commit()
                with self._lock:
                    if published or changed_index:
                        self._matrix_cache.pop(sid, None)
                status = self._status(conn, sid, "warming" if not same_generation else "ready", published)
                if status["pending"]:
                    status["state"] = "warming"
                return status
        except Exception:  # noqa: BLE001 - model faults become a stable unavailable status.
            with closing(self._conn()) as conn:
                conn.row_factory = sqlite3.Row
                return self._status(conn, sid, "unavailable", unavailable=True)

    def _matrix(self, conn: sqlite3.Connection, sid: str, source_revision: int,
                index_revision: int) -> tuple[list[int], Any]:
        with self._lock:
            cached = self._matrix_cache.get(sid)
            if cached and cached[0] == source_revision and cached[1] == index_revision:
                self._matrix_cache.move_to_end(sid)
                return cached[2], cached[3]
            # Keep the lock across loading so concurrent searches share one build.
            rows = conn.execute("""SELECT v.seq,v.vector,v.dimensions
                FROM conversation_semantic_vectors v WHERE v.session_id=? AND v.fingerprint=?
                AND NOT EXISTS (SELECT 1 FROM conversation_semantic_dirty d
                  WHERE d.session_id=v.session_id AND d.fingerprint=v.fingerprint AND d.seq=v.seq)
                ORDER BY v.seq DESC LIMIT ?""", (sid, self._fingerprint, self._max_candidates)).fetchall()
            seqs = [int(row["seq"]) for row in rows]
            dimensions = int(rows[0]["dimensions"]) if rows else 0
            if rows and any(int(row["dimensions"]) != dimensions for row in rows):
                raise ValueError("stored embedding dimensions are inconsistent")
            packed = b"".join(row["vector"] for row in rows)
            expected_size = len(rows) * dimensions * 4
            if len(packed) != expected_size:
                raise ValueError("stored embedding size is invalid")
            if np is not None and rows:
                matrix = np.frombuffer(packed, dtype="<f4").reshape(len(rows), dimensions)
                matrix.setflags(write=False)
                matrix_bytes = int(matrix.nbytes)
            elif rows:
                matrix = array("f")
                matrix.frombytes(packed)
                matrix_bytes = len(matrix) * matrix.itemsize
            else:
                matrix = np.empty((0, 0), dtype=np.float32) if np is not None else array("f")
                matrix_bytes = 0
            if matrix_bytes <= self._max_matrix_cache_bytes:
                self._matrix_cache[sid] = (source_revision, index_revision, seqs, matrix, matrix_bytes)
                self._matrix_cache.move_to_end(sid)
                total_bytes = sum(entry[4] for entry in self._matrix_cache.values())
                while (len(self._matrix_cache) > self._matrix_sessions
                       or total_bytes > self._max_matrix_cache_bytes):
                    _, removed = self._matrix_cache.popitem(last=False)
                    total_bytes -= removed[4]
            else:
                self._matrix_cache.pop(sid, None)
            return seqs, matrix

    def search(self, session_id: str, query: str, limit: int = 8, offset: int = 0) -> dict[str, Any]:
        """Search only the published index; never performs passage sync here."""
        self.ensure_schema()
        if not isinstance(query, str) or not 0 < len(query) <= 500 or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("invalid semantic search arguments")
        sid = str(session_id)
        try:
            with closing(self._conn()) as conn:
                conn.row_factory = sqlite3.Row
                if not self._session_ok(conn, sid):
                    return {"messages": [], "status": {"state": "unavailable", "unavailable": True, "pending": False}}
                conn.execute("BEGIN")
                state = conn.execute("SELECT * FROM conversation_semantic_state WHERE session_id=? AND fingerprint=?", (sid, self._fingerprint)).fetchone()
                if state is None:
                    return {"messages": [], "status": self._status(conn, sid, "warming")}
                rows = conn.execute("SELECT COUNT(*) FROM conversation_semantic_vectors WHERE session_id=? AND fingerprint=?", (sid, self._fingerprint)).fetchone()[0]
                if not rows:
                    status = self._status(conn, sid, "warming")
                    conn.commit()
                    return {"messages": [], "status": status}
                generation = int(state["source_revision"])
                index_revision = int(state["index_revision"])
                seqs, matrix = self._matrix(conn, sid, generation, index_revision)
                if not seqs:
                    status = self._status(conn, sid, "warming")
                    conn.commit()
                    return {"messages": [], "status": status}
                # Query embedding can be slow. Do not hold a SQLite read snapshot
                # while calling the injected provider.
                conn.commit()
                with self._lock:
                    cached_query = self._query_cache.get(query)
                    if cached_query is not None and cached_query[0] == self._fingerprint:
                        query_vector = cached_query[1]
                        self._query_cache.move_to_end(query)
                        query_status = "hit"
                    else:
                        query_vector = self._pack(self._provider.embed_query(query))[2]
                        self._query_cache[query] = (self._fingerprint, query_vector)
                        while len(self._query_cache) > 64:
                            self._query_cache.popitem(last=False)
                        query_status = "miss"
                if np is not None and len(seqs):
                    if matrix.shape[1] != len(query_vector):
                        raise ValueError("query embedding dimensions do not match the index")
                    scores = matrix @ np.asarray(query_vector, dtype=np.float32)
                    order = np.argsort(-scores)[:offset + limit]
                    ranked = [(seqs[int(i)], float(scores[int(i)])) for i in order]
                else:
                    if seqs and len(matrix) != len(seqs) * len(query_vector):
                        raise ValueError("query embedding dimensions do not match the index")
                    scored = []
                    dimensions = len(query_vector)
                    for row_index, seq in enumerate(seqs):
                        start = row_index * dimensions
                        vector = matrix[start:start + dimensions]
                        scored.append((seq, sum(a * b for a, b in zip(vector, query_vector))))
                    ranked = sorted(scored, key=lambda pair: (-pair[1], -pair[0]))[:offset + limit]
                output = []
                for seq, score in ranked[offset:offset + limit]:
                    live = conn.execute(f"SELECT m.session_id,m.seq,m.role,m.content,m.created_at,m.trace_id FROM agent_session_context_messages m WHERE m.session_id=? AND m.seq=? AND {self._eligible_sql()}", (sid, seq)).fetchone()
                    vector = conn.execute("SELECT source_hash FROM conversation_semantic_vectors WHERE session_id=? AND seq=? AND fingerprint=?", (sid, seq, self._fingerprint)).fetchone()
                    if live is None or vector is None or self._hash(live) != vector["source_hash"]:
                        continue
                    output.append({"seq": seq, "hash": vector["source_hash"],
                                   "content_hash": vector["source_hash"], "score": score,
                                   "role": live["role"], "created_at": live["created_at"], "trace_id": live["trace_id"]})
                conn.commit()
                # A new connection observes writes that committed while the
                # candidate rows were being revalidated in the prior snapshot.
                with closing(self._conn()) as check_conn:
                    check_conn.row_factory = sqlite3.Row
                    fresh = check_conn.execute("SELECT source_revision,index_revision FROM conversation_semantic_state WHERE session_id=? AND fingerprint=?", (sid, self._fingerprint)).fetchone()
                    stale = not fresh or int(fresh["source_revision"]) != generation or int(fresh["index_revision"]) != index_revision
                    status = self._status(check_conn, sid, "warming" if stale else "ready")
                status["query_cache"] = query_status
                return {"messages": [] if stale else output, "status": status}
        except Exception:  # noqa: BLE001 - model faults become a stable unavailable status.
            with closing(self._conn()) as conn:
                conn.row_factory = sqlite3.Row
                return {"messages": [], "status": self._status(conn, sid, "unavailable", unavailable=True)}
