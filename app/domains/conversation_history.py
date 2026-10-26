"""Bounded, source-linked access to one live conversation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from threading import RLock
from typing import Any


class ConversationHistoryService:
    """Incremental lexical index and bounded source reader for one session."""

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        *,
        semantic_index: Any = None,
        index_batch: int = 256,
    ) -> None:
        self._conn = conn_factory
        self.semantic_index = semantic_index
        self.index_batch = max(1, min(int(index_batch), 1024))
        self._schema_lock = RLock()
        self._schema_ready = False
        self._index_lock = RLock()

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with closing(self._conn()) as conn:
                conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_history_fts_map (
                    rowid INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    source_hash TEXT NOT NULL,
                    UNIQUE(session_id, seq)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS conversation_history_fts
                    USING fts5(content, tokenize='trigram');
                CREATE TABLE IF NOT EXISTS conversation_history_index_state (
                    session_id TEXT PRIMARY KEY,
                    last_seq INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS conversation_history_dirty (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    PRIMARY KEY(session_id, seq)
                );
                CREATE INDEX IF NOT EXISTS conversation_history_map_session_seq
                    ON conversation_history_fts_map(session_id, seq);
                CREATE TRIGGER IF NOT EXISTS conversation_history_source_insert
                AFTER INSERT ON agent_session_context_messages BEGIN
                    INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
                    VALUES(NEW.session_id,NEW.seq);
                    INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
                    SELECT session_id,seq FROM agent_session_context_messages
                    WHERE session_id=NEW.session_id AND trace_id=NEW.trace_id AND role='agent';
                END;
                CREATE TRIGGER IF NOT EXISTS conversation_history_source_update
                AFTER UPDATE OF role,content,created_at,trace_id ON agent_session_context_messages BEGIN
                    INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
                    VALUES(OLD.session_id,OLD.seq);
                    INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
                    SELECT session_id,seq FROM agent_session_context_messages
                    WHERE session_id=OLD.session_id AND trace_id=OLD.trace_id AND role='agent';
                    INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
                    SELECT session_id,seq FROM agent_session_context_messages
                    WHERE session_id=NEW.session_id AND trace_id=NEW.trace_id AND role='agent';
                END;
                CREATE TRIGGER IF NOT EXISTS conversation_history_source_delete
                AFTER DELETE ON agent_session_context_messages BEGIN
                    DELETE FROM conversation_history_dirty WHERE session_id=OLD.session_id AND seq=OLD.seq;
                    DELETE FROM conversation_history_fts
                    WHERE rowid=(SELECT rowid FROM conversation_history_fts_map
                                 WHERE session_id=OLD.session_id AND seq=OLD.seq);
                    DELETE FROM conversation_history_fts_map
                    WHERE session_id=OLD.session_id AND seq=OLD.seq;
                    INSERT OR IGNORE INTO conversation_history_dirty(session_id,seq)
                    SELECT session_id,seq FROM agent_session_context_messages
                    WHERE session_id=OLD.session_id AND trace_id=OLD.trace_id AND role='agent';
                END;
                    """
                )
                conn.commit()
            self._schema_ready = True

    @staticmethod
    def _hash(row: Any) -> str:
        encoded = json.dumps(
            [row["session_id"], row["seq"], row["role"], row["content"],
             row["created_at"], row["trace_id"]],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT session_id,kind,status FROM agent_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if not row or row["status"] == "deleted" or row["kind"] not in {"ordinary", "manager"}:
            raise KeyError("Current session is unavailable")
        return dict(row)

    @staticmethod
    def _lane_row(conn: sqlite3.Connection, session_id: str, seq: int) -> Any:
        return conn.execute(
            """SELECT m.session_id,m.seq,m.role,m.content,m.created_at,m.trace_id
               FROM agent_session_context_messages m
               WHERE m.session_id=? AND m.seq=? AND (
                 m.role='user' OR (m.role='agent' AND m.trace_id IS NOT NULL AND EXISTS (
                   SELECT 1 FROM agent_session_context_messages u
                   WHERE u.session_id=m.session_id AND u.trace_id=m.trace_id AND u.role='user'
                 ) AND NOT EXISTS (
                   SELECT 1 FROM agent_session_context_messages t
                   WHERE t.session_id=m.session_id AND t.trace_id=m.trace_id AND t.role='tool'
                 ))
               )""",
            (session_id, seq),
        ).fetchone()

    def _index(self, conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
        state = conn.execute(
            "SELECT last_seq FROM conversation_history_index_state WHERE session_id=?",
            (session_id,),
        ).fetchone()
        watermark = int(state[0]) if state else 0
        dirty = conn.execute(
            "SELECT seq FROM conversation_history_dirty WHERE session_id=? ORDER BY seq LIMIT ?",
            (session_id, self.index_batch),
        ).fetchall()
        dirty_seqs = {int(row[0]) for row in dirty}
        new_rows = conn.execute(
            """SELECT session_id,seq,role,content,created_at,trace_id
               FROM agent_session_context_messages
               WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?""",
            (session_id, watermark, self.index_batch),
        ).fetchall()
        touched = dirty_seqs | {int(row["seq"]) for row in new_rows}
        for seq in sorted(touched):
            conn.execute(
                "DELETE FROM conversation_history_fts WHERE rowid=(SELECT rowid FROM conversation_history_fts_map WHERE session_id=? AND seq=?)",
                (session_id, seq),
            )
            conn.execute(
                "DELETE FROM conversation_history_fts_map WHERE session_id=? AND seq=?",
                (session_id, seq),
            )
            conn.execute(
                "DELETE FROM conversation_history_dirty WHERE session_id=? AND seq=?",
                (session_id, seq),
            )
            row = conn.execute(
                """SELECT session_id,seq,role,content,created_at,trace_id
                   FROM agent_session_context_messages WHERE session_id=? AND seq=?""",
                (session_id, seq),
            ).fetchone()
            if row is None or self._lane_row(conn, session_id, seq) is None:
                continue
            cur = conn.execute(
                "INSERT INTO conversation_history_fts(content) VALUES(?)",
                (row["content"],),
            )
            conn.execute(
                "INSERT INTO conversation_history_fts_map(rowid,session_id,seq,source_hash) VALUES(?,?,?,?)",
                (cur.lastrowid, session_id, seq, self._hash(row)),
            )
        if new_rows:
            watermark = max(watermark, int(new_rows[-1]["seq"]))
        if touched or state is None:
            conn.execute(
                """INSERT INTO conversation_history_index_state(session_id,last_seq) VALUES(?,?)
                   ON CONFLICT(session_id) DO UPDATE SET last_seq=excluded.last_seq""",
                (session_id, watermark),
            )
        high = conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM agent_session_context_messages WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT seq FROM conversation_history_dirty WHERE session_id=? ORDER BY seq LIMIT 1",
            (session_id,),
        ).fetchone()
        return {"indexed": len(touched), "watermark": watermark,
                "complete": watermark >= high and not pending,
                "next_dirty_seq": int(pending[0]) if pending else 0,
                "next_watermark": None if watermark >= high and not pending else watermark}

    def _refresh_index(self, session_id: str) -> dict[str, Any]:
        """Advance the FTS index in a serialized bounded write transaction."""
        with self._index_lock, closing(self._conn()) as conn:
            conn.row_factory = sqlite3.Row
            try:
                # Warm searches need no SQLite write reservation. A later
                # source change is detected again in the read snapshot below.
                conn.execute("BEGIN")
                self._session(conn, session_id)
                previous = conn.execute(
                    "SELECT last_seq FROM conversation_history_index_state WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                watermark = int(previous[0]) if previous else 0
                pending = conn.execute(
                    "SELECT 1 FROM conversation_history_dirty WHERE session_id=? LIMIT 1",
                    (session_id,),
                ).fetchone()
                newer = conn.execute(
                    "SELECT 1 FROM agent_session_context_messages WHERE session_id=? AND seq>? LIMIT 1",
                    (session_id, watermark),
                ).fetchone()
                conn.rollback()
                if previous is not None and not pending and not newer:
                    return {"indexed": 0, "watermark": watermark,
                            "complete": True, "next_watermark": None}
                conn.execute("BEGIN IMMEDIATE")
                self._session(conn, session_id)
                state = self._index(conn, session_id)
                conn.commit()
                return state
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                return {"indexed": 0, "watermark": 0, "complete": False,
                        "next_watermark": 0, "busy": True}

    @staticmethod
    def _check(context: Any, query: str, limit: int, offset: int) -> None:
        view = context.tool_view
        if view is not None and (
            view.child_run_id is not None or not view.full_data_authority
        ):
            raise PermissionError("Restricted runs cannot read unrestricted conversation history")
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query must be a string of at most 500 characters")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be in 1..20")
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be in 0..1000000")

    @staticmethod
    def _match(conn: sqlite3.Connection, sid: str, query: str,
               limit: int, offset: int) -> tuple[list[Any], bool]:
        if len(query) < 3:
            rows = conn.execute(
                """WITH candidates AS (
                     SELECT f.rowid,m.seq,m.source_hash,f.content
                     FROM conversation_history_fts f
                     JOIN conversation_history_fts_map m ON m.rowid=f.rowid
                     WHERE m.session_id=? ORDER BY m.seq DESC LIMIT 5001
                   ), matches AS (
                     SELECT * FROM candidates
                     WHERE instr(lower(content),lower(?))>0
                   ), matches_page AS (
                     SELECT * FROM matches ORDER BY seq DESC LIMIT ? OFFSET ?
                   ), result AS (
                     SELECT rowid,seq,source_hash,content,
                            (SELECT COUNT(*) FROM candidates) AS candidate_count,0 AS dummy
                     FROM matches_page
                     UNION ALL
                     SELECT NULL,NULL,NULL,NULL,(SELECT COUNT(*) FROM candidates),1
                     WHERE NOT EXISTS(SELECT 1 FROM matches_page)
                   )
                   SELECT * FROM result ORDER BY dummy,seq DESC""",
                (sid, query, limit + 1, offset),
            ).fetchall()
            incomplete = bool(rows and rows[0]["candidate_count"] > 5000)
            return [row for row in rows if row["seq"] is not None], incomplete

        escaped = '"' + query.replace('"', '""') + '"'
        try:
            rows = conn.execute(
                """SELECT m.rowid,m.seq,m.source_hash,f.content
                   FROM conversation_history_fts f
                   JOIN conversation_history_fts_map m ON m.rowid=f.rowid
                   WHERE conversation_history_fts MATCH ? AND m.session_id=?
                   ORDER BY m.seq DESC LIMIT ? OFFSET ?""",
                (escaped, sid, limit + 1, offset),
            ).fetchall()
            return rows, False
        except sqlite3.OperationalError:
            return [], True

    def search(self, context: Any, *, query: str = "", limit: int = 8,
               offset: int = 0, around_chars: int = 140,
               mode: str = "lexical", index_cursor: str | None = None) -> dict[str, Any]:
        self._check(context, query, limit, offset)
        if index_cursor is not None and (
            not isinstance(index_cursor, str) or len(index_cursor) > 43
            or len(index_cursor.split(":")) != 2
            or not all(part.isascii() and part.isdecimal() for part in index_cursor.split(":"))
        ):
            raise ValueError("invalid index continuation")
        # The cursor identifies observed indexing progress, not a caller-chosen
        # source range. All advancement uses the current server-owned watermark.
        if mode not in {"lexical", "semantic", "hybrid"}:
            raise ValueError("mode must be lexical, semantic or hybrid")
        if type(around_chars) is not int or not 20 <= around_chars <= 500:
            raise ValueError("around_chars must be in 20..500")
        if mode != "lexical" and query:
            return self._augmented_search(context, query, limit, offset, around_chars, mode)
        self.ensure_schema()
        index_state = self._refresh_index(context.session_id)
        with closing(self._conn()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            self._session(conn, context.session_id)
            high = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM agent_session_context_messages WHERE session_id=?",
                (context.session_id,),
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT 1 FROM conversation_history_dirty WHERE session_id=? LIMIT 1",
                (context.session_id,),
            ).fetchone()
            index_state["complete"] = bool(
                index_state["complete"] and index_state["watermark"] >= high and not pending
            )
            if query:
                hits, incomplete = self._match(conn, context.session_id, query, limit, offset)
                incomplete = incomplete or not index_state["complete"]
                selected = []
                for hit in hits:
                    row = self._lane_row(conn, context.session_id, hit["seq"])
                    if row is not None and self._hash(row) == hit["source_hash"]:
                        selected.append(row)
            else:
                selected = conn.execute(
                    """SELECT m.session_id,m.seq,m.role,m.content,m.created_at,m.trace_id
                       FROM agent_session_context_messages m WHERE m.session_id=? AND (
                         m.role='user' OR (m.role='agent' AND m.trace_id IS NOT NULL AND EXISTS(
                           SELECT 1 FROM agent_session_context_messages u WHERE u.session_id=m.session_id AND u.trace_id=m.trace_id AND u.role='user'
                         ) AND NOT EXISTS(
                           SELECT 1 FROM agent_session_context_messages t WHERE t.session_id=m.session_id AND t.trace_id=m.trace_id AND t.role='tool'
                         ))) ORDER BY m.seq DESC LIMIT ? OFFSET ?""",
                    (context.session_id, limit + 1, offset),
                ).fetchall()
                incomplete = not index_state["complete"]
            more = len(selected) > limit
            messages = []
            for row in selected[:limit]:
                content = row["content"]
                pos = content.casefold().find(query.casefold()) if query else 0
                start = max(0, pos - around_chars // 2)
                messages.append({"seq": row["seq"], "role": row["role"],
                    "trace_id": row["trace_id"], "created_at": row["created_at"],
                    "source_ref": f"{context.session_id}:{row['seq']}",
                    "content_hash": self._hash(row),
                    "snippet": content[start:start + around_chars], "start_char": start,
                    "match": bool(query and pos >= 0)})
            return {"query": query, "messages": messages,
                    "next_offset": offset + limit if more else None,
                    "index": index_state, "incomplete": incomplete,
                    "next_index_cursor": (
                        f"{index_state['watermark']}:{index_state.get('next_dirty_seq', 0)}"
                        if not index_state["complete"] else None
                    ),
                    "retrieval": "lexical_fts5_trigram" if query else "recent_source_index",
                    "semantic": {"state": "not_requested"}}

    def _augmented_search(self, context, query, limit, offset, around_chars, mode):
        if self.semantic_index is None:
            result = self.search(context, query=query, limit=limit, offset=offset,
                                 around_chars=around_chars)
            result["semantic"] = {"state": "not_configured"}
            result["requested_mode"] = mode
            return result
        if mode == "hybrid" and offset >= 120:
            raise ValueError("hybrid offset must be below the 120-candidate ranking window; narrow the query")
        semantic = self.semantic_index.search(
            context.session_id, query,
            limit=100 if mode == "hybrid" else limit + 1,
            offset=0 if mode == "hybrid" else offset,
        )
        status = semantic.get("status", {"state": "unavailable"})
        # Cold or unavailable semantic indexes keep literal search usable.
        if (not semantic.get("messages") or mode == "semantic") and status.get("state") != "ready":
            result = self.search(context, query=query, limit=limit, offset=offset,
                                 around_chars=around_chars)
            result["semantic"] = status
            result["requested_mode"] = mode
            result["incomplete"] = result["incomplete"] or bool(
                status.get("pending") or status.get("coverage_capped")
            )
            return result
        lexical = (self.search(context, query=query, limit=20, around_chars=around_chars)
                   if mode == "hybrid" else None)
        scores, candidates = {}, {}
        sources = [semantic.get("messages", [])]
        if lexical is not None:
            sources.append(lexical["messages"])
        for hits in sources:
            for rank, hit in enumerate(hits, 1):
                seq = int(hit["seq"])
                scores[seq] = scores.get(seq, 0) + 1 / (60 + rank)
                candidates.setdefault(seq, hit)
        ranked = sorted(candidates, key=lambda seq: (-scores[seq], -seq))
        if mode == "hybrid":
            ranked = ranked[offset:]
        messages = []
        stale_candidates = False
        next_offset = None
        self.ensure_schema()
        with closing(self._conn()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            self._session(conn, context.session_id)
            for candidate_offset, seq in enumerate(ranked):
                row = self._lane_row(conn, context.session_id, seq)
                if row is None or self._hash(row) != candidates[seq]["content_hash"]:
                    stale_candidates = True
                    continue
                content = row["content"]
                pos = content.casefold().find(query.casefold())
                start = max(0, pos - around_chars // 2)
                messages.append({
                    "seq": seq, "role": row["role"], "trace_id": row["trace_id"],
                    "created_at": row["created_at"], "source_ref": f"{context.session_id}:{seq}",
                    "content_hash": self._hash(row), "snippet": content[start:start + around_chars],
                    "start_char": start, "match": pos >= 0, "rank_score": scores[seq],
                })
                if len(messages) > limit:
                    # Resume at the unconsumed lookahead candidate, counting
                    # raw ranks including any invalidated sources we skipped.
                    next_offset = offset + candidate_offset
                    break
        ranking_truncated = mode == "hybrid" and (
            status.get("indexed_count", 0) > 100 or bool(lexical and lexical.get("next_offset") is not None)
        )
        return {
            "query": query, "messages": messages[:limit], "retrieval": mode,
            "next_offset": next_offset,
            "semantic": status, "index": lexical["index"] if lexical else None,
            "incomplete": bool(stale_candidates or ranking_truncated
                               or status.get("pending") or status.get("coverage_capped")
                               or (lexical and lexical["incomplete"])),
            "ranking_window": 120 if mode == "hybrid" else None,
            "next_index_cursor": lexical.get("next_index_cursor") if lexical else None,
            "ranking_truncated": ranking_truncated,
            "stale_candidates_filtered": stale_candidates,
        }

    def read(
        self,
        context: Any,
        *,
        seq: int | None = None,
        around_seq: int | None = None,
        offset: int = 0,
        max_chars: int = 4000,
        limit: int = 6,
        cursor: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Read one message or one complete trace turn, with a lossless cursor."""
        view = context.tool_view
        if view is not None and (
            view.child_run_id is not None or not view.full_data_authority
        ):
            raise PermissionError("Restricted runs cannot read unrestricted conversation history")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if type(max_chars) is not int or not 1 <= max_chars <= 8000:
            raise ValueError("max_chars must be in 1..8000")
        if type(limit) is not int or not 1 <= limit <= 12:
            raise ValueError("limit must be in 1..12")
        if cursor is not None:
            if seq is not None or around_seq is not None:
                raise ValueError("cursor cannot be combined with seq or around_seq")
            required = {"anchor_seq", "message_offset", "char_offset"}
            if set(cursor) != required or any(type(cursor[key]) is not int or cursor[key] < 0 for key in required):
                raise ValueError("cursor requires non-negative anchor_seq, message_offset, char_offset")
            around_seq = cursor["anchor_seq"]
            offset = cursor["message_offset"]
            char_offset = cursor["char_offset"]
        else:
            if (seq is None) == (around_seq is None):
                raise ValueError("choose exactly one of seq or around_seq")
            char_offset = offset if seq is not None else 0

        target = seq if seq is not None else around_seq
        if type(target) is not int or target < 1:
            raise ValueError("sequence must be a positive integer")

        with closing(self._conn()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            self._session(conn, context.session_id)
            target_row = self._lane_row(conn, context.session_id, target)
            if target_row is None:
                raise KeyError(target)
            if seq is not None:
                rows = [target_row]
                total = 1
                anchor_trace = target_row["trace_id"]
            else:
                anchor_trace = target_row["trace_id"]
                if not anchor_trace:
                    raise ValueError("Selected source has no turn trace")
                rows = conn.execute(
                    """SELECT m.session_id,m.seq,m.role,m.content,m.created_at,m.trace_id
                       FROM agent_session_context_messages m
                       WHERE m.session_id=? AND m.trace_id=? AND m.role IN ('user','agent')
                         AND (m.role='user' OR NOT EXISTS (
                           SELECT 1 FROM agent_session_context_messages t
                           WHERE t.session_id=m.session_id AND t.trace_id=m.trace_id AND t.role='tool'
                         ))
                       ORDER BY m.seq LIMIT ? OFFSET ?""",
                    (context.session_id, anchor_trace, limit + 1, offset),
                ).fetchall()
                count_row = conn.execute(
                    """SELECT COUNT(*) FROM agent_session_context_messages m
                       WHERE m.session_id=? AND m.trace_id=? AND m.role IN ('user','agent')
                         AND (m.role='user' OR NOT EXISTS (
                           SELECT 1 FROM agent_session_context_messages t
                           WHERE t.session_id=m.session_id AND t.trace_id=m.trace_id AND t.role='tool'
                         ))""",
                    (context.session_id, anchor_trace),
                ).fetchone()
                total = int(count_row[0])
            has_message_page = offset + min(len(rows), limit) < total if seq is None else False
            if seq is None:
                rows = rows[:limit]

            output = []
            remaining = max_chars
            next_cursor = None
            next_seq = None
            next_char = None
            for index, row in enumerate(rows):
                body = row["content"]
                start = char_offset if index == 0 else 0
                if start > len(body):
                    raise ValueError("cursor character offset is beyond source length")
                end = min(len(body), start + remaining)
                part = body[start:end]
                output.append({
                    "seq": row["seq"],
                    "role": row["role"],
                    "trace_id": row["trace_id"],
                    "created_at": row["created_at"],
                    "source_ref": f"{context.session_id}:{row['seq']}",
                    "content_hash": self._hash(row),
                    "content": part,
                    "start_char": start,
                    "next_char": end if end < len(body) else None,
                })
                remaining -= len(part)
                if end < len(body):
                    if seq is not None:
                        next_seq, next_char = int(row["seq"]), end
                    else:
                        next_cursor = {
                            "anchor_seq": int(target),
                            "message_offset": offset + index,
                            "char_offset": end,
                        }
                    break
                if remaining == 0:
                    if seq is None and (index + 1 < len(rows) or has_message_page):
                        next_cursor = {
                            "anchor_seq": int(target),
                            "message_offset": offset + index + 1,
                            "char_offset": 0,
                        }
                    break
            if next_cursor is None and has_message_page:
                next_cursor = {
                    "anchor_seq": int(target),
                    "message_offset": offset + len(rows),
                    "char_offset": 0,
                }
            return {
                "messages": output,
                "cursor": next_cursor,
                "next_seq": next_seq,
                "next_offset": next_char,
                "truncated": next_cursor is not None or next_seq is not None,
                "source_ref": f"{context.session_id}:{target}",
            }
