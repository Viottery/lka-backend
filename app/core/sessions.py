"""Persistent agent session and message management."""

from __future__ import annotations

import inspect
import json
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from hashlib import sha1
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.prompt_tokens import PromptTokenCounter

SessionRole = Literal["user", "agent", "system", "tool"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


class AgentSession(BaseModel):
    session_id: str
    title: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    workspace: SessionWorkspace | None = None
    project_id: str | None = None
    created_at: str
    updated_at: str


class SessionWorkspace(BaseModel):
    """A backend-local directory selected for one Agent session."""

    path: str
    platform: Literal["linux", "windows", "macos"]
    backend_path: str


class AgentSessionMessage(BaseModel):
    message_id: str
    session_id: str
    role: SessionRole
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class SessionRecentMessage(BaseModel):
    role: SessionRole
    content: str
    created_at: str
    trace_id: str | None = None


class AgentSessionContextWindow(BaseModel):
    session_id: str
    token_budget: int = 65_536
    summary: str = ""
    summary_metadata: dict[str, Any] = Field(default_factory=dict)
    recent_messages: list[SessionRecentMessage] = Field(default_factory=list)
    token_estimate: int = 0
    updated_at: str


class AgentSessionList(BaseModel):
    sessions: list[AgentSession]


class AgentSessionDetail(BaseModel):
    session: AgentSession
    messages: list[AgentSessionMessage]


class SessionService:
    """Deterministic storage service for parallel and multi-turn agent sessions."""

    default_context_token_budget = 65_536

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        *,
        context_token_counter: PromptTokenCounter | None = None,
    ) -> None:
        self._conn_factory = conn_factory
        self._context_token_counter = context_token_counter
        self._turn_context_counter: ContextVar[PromptTokenCounter | None] = ContextVar(
            "session_context_counter", default=None
        )

    @contextmanager
    def use_context_token_counter(self, counter: PromptTokenCounter):
        """Use the selected turn model's tokenizer without changing background jobs."""
        token = self._turn_context_counter.set(counter)
        try:
            yield
        finally:
            self._turn_context_counter.reset(token)

    def _active_context_token_counter(self) -> PromptTokenCounter | None:
        return self._turn_context_counter.get() or self._context_token_counter

    @property
    def context_token_count_method(self) -> str:
        counter = self._active_context_token_counter()
        if counter is None:
            return "legacy_chars_per_4"
        return counter.count_text("").method

    def create_session(
        self,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        initial_message: str | None = None,
    ) -> AgentSessionDetail:
        now = _now_iso()
        clean_title = self._default_title(title=title, initial_message=initial_message)
        session_id = _stable_id("session", clean_title, now)
        metadata_payload = metadata or {}

        conn = self._conn_factory()
        try:
            conn.execute(
                """
                INSERT INTO agent_sessions(session_id, title, status, metadata, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    clean_title,
                    "active",
                    json.dumps(metadata_payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        if initial_message:
            self.append_message(
                session_id=session_id,
                role="user",
                content=initial_message,
                payload={"source": "session_create"},
            )
        return self.get_session(session_id=session_id)

    def ensure_session(
        self,
        *,
        session_id: str | None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentSession:
        if session_id:
            existing = self.get_session_or_none(session_id=session_id)
            if existing is not None:
                return existing
            now = _now_iso()
            conn = self._conn_factory()
            try:
                tombstone = conn.execute(
                    "SELECT 1 FROM agent_sessions WHERE session_id = ? AND status = 'deleted'",
                    (session_id,),
                ).fetchone()
                if tombstone is not None:
                    raise KeyError(f"Session is deleted: {session_id}")
                conn.execute(
                    """
                    INSERT INTO agent_sessions(
                        session_id, title, status, metadata, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        title or session_id,
                        "active",
                        json.dumps(metadata or {}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            created = self.get_session_or_none(session_id=session_id)
            if created is None:
                raise RuntimeError(f"Failed to create session: {session_id}")
            return created

        return self.create_session(title=title, metadata=metadata).session

    def list_sessions(
        self, *, limit: int = 50, offset: int = 0, q: str | None = None,
        project_id: str | None = None,
    ) -> AgentSessionList:
        return self._list_sessions(
            status_clause="status != 'deleted'", limit=limit, offset=offset, q=q,
            project_id=project_id,
        )

    def list_deleted_sessions(
        self, *, limit: int = 50, offset: int = 0, q: str | None = None
    ) -> AgentSessionList:
        return self._list_sessions(
            status_clause="status = 'deleted'", limit=limit, offset=offset, q=q
        )

    def _list_sessions(
        self, *, status_clause: str, limit: int, offset: int, q: str | None,
        project_id: str | None = None,
    ) -> AgentSessionList:
        query = q.strip() if q else ""
        search_clause = ""
        params: list[Any] = []
        if query:
            # Treat q as literal substring text; LIKE metacharacters are escaped.
            escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped_query}%"
            search_clause = """
                AND (
                    title LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM agent_session_messages AS message
                        WHERE message.session_id = agent_sessions.session_id
                          AND message.content LIKE ? ESCAPE '\\'
                    )
                )
            """
            params.extend((pattern, pattern))
        if project_id is not None:
            search_clause += """ AND (
                json_extract(metadata,'$.project_id')=? OR (
                    json_extract(metadata,'$.project_id') IS NULL AND
                    json_extract(metadata,'$.workspace.backend_path') IN (
                        SELECT path_key FROM memory_project_paths WHERE project_id=? AND active=1
                    )
                ))"""
            params.extend((project_id, project_id))
        params.extend((limit, offset))
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"""
                SELECT session_id, title, status, metadata, created_at, updated_at
                FROM agent_sessions
                WHERE {status_clause}
                {search_clause}
                ORDER BY updated_at DESC, session_id DESC
                LIMIT ?
                OFFSET ?
                """,
                params,
            ).fetchall()
        finally:
            conn.close()
        return AgentSessionList(sessions=[self._session_from_row(row) for row in rows])

    def get_session(self, *, session_id: str) -> AgentSessionDetail:
        session = self.get_session_or_none(session_id=session_id)
        if session is None:
            raise KeyError(f"Session not found: {session_id}")

        conn = self._conn_factory()
        try:
            rows = conn.execute(
                """
                SELECT message_id, session_id, role, content, payload, created_at
                FROM agent_session_messages
                WHERE session_id = ?
                ORDER BY created_at ASC
                """,
                (session_id,),
            ).fetchall()
        finally:
            conn.close()

        return AgentSessionDetail(
            session=session,
            messages=[self._message_from_row(row) for row in rows],
        )

    def rename_session(self, *, session_id: str, title: str) -> AgentSessionDetail:
        """Persist a user-selected title without changing other session metadata."""

        clean_title = title.strip()
        if not clean_title or len(clean_title) > 40:
            raise ValueError("title must contain 1 to 40 non-whitespace characters")

        conn = self._conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT metadata FROM agent_sessions
                WHERE session_id = ? AND status != 'deleted'
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Session not found: {session_id}")

            metadata = json.loads(row["metadata"] or "{}")
            metadata["title_is_custom"] = True
            cursor = conn.execute(
                """
                UPDATE agent_sessions
                SET title = ?, metadata = ?, updated_at = ?
                WHERE session_id = ? AND status != 'deleted'
                """,
                (
                    clean_title,
                    json.dumps(metadata, ensure_ascii=False),
                    _now_iso(),
                    session_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Session not found: {session_id}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return self.get_session(session_id=session_id)

    def delete_session(self, *, session_id: str) -> bool:
        """Soft delete a session while retaining its local audit and run data."""

        now = _now_iso()
        conn = self._conn_factory()
        try:
            cursor = conn.execute(
                """
                UPDATE agent_sessions
                SET status = 'deleted', updated_at = ?
                WHERE session_id = ? AND status != 'deleted'
                """,
                (now, session_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def restore_session(self, *, session_id: str) -> bool:
        """Restore a soft-deleted session without changing its associated data."""

        now = _now_iso()
        conn = self._conn_factory()
        try:
            cursor = conn.execute(
                """
                UPDATE agent_sessions
                SET status = 'active', updated_at = ?
                WHERE session_id = ? AND status = 'deleted'
                """,
                (now, session_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_session_or_none(self, *, session_id: str) -> AgentSession | None:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                SELECT session_id, title, status, metadata, created_at, updated_at
                FROM agent_sessions
                WHERE session_id = ? AND status != 'deleted'
                """,
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._session_from_row(row) if row else None

    def set_workspace(
        self,
        *,
        session_id: str,
        workspace: SessionWorkspace,
        project_id: str | None = None,
    ) -> AgentSession:
        session = self.get_session_or_none(session_id=session_id)
        if session is None:
            raise KeyError(f"Session not found: {session_id}")
        metadata = dict(session.metadata)
        metadata["workspace"] = workspace.model_dump(mode="json")
        if project_id is not None:
            metadata["project_id"] = project_id
        else:
            metadata.pop("project_id", None)
        now = _now_iso()
        conn = self._conn_factory()
        try:
            conn.execute(
                """
                UPDATE agent_sessions
                SET metadata = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (json.dumps(metadata, ensure_ascii=False), now, session_id),
            )
            conn.commit()
        finally:
            conn.close()
        updated = self.get_session_or_none(session_id=session_id)
        if updated is None:
            raise RuntimeError(f"Failed to update session workspace: {session_id}")
        return updated

    def append_message(
        self,
        *,
        session_id: str,
        role: SessionRole,
        content: str,
        payload: dict[str, Any] | None = None,
        message_id: str | None = None,
        persisted_message_callback: Callable[[sqlite3.Connection, AgentSessionMessage], None]
        | None = None,
    ) -> AgentSessionMessage:
        """Persist a message and optionally enqueue work in the same transaction.

        The callback runs only for `role='agent'`, after insertion and before commit. It
        receives the live connection and persisted message; callback failure rolls both
        message and outbox writes back. Use an idempotency key in the callback's outbox.
        """
        self.ensure_session(session_id=session_id)
        now = _now_iso()
        message_id = message_id or _stable_id("session_msg", session_id, role, content, now)
        payload = payload or {}

        conn = self._conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_session_messages(
                    message_id, session_id, role, content, payload, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    role,
                    content,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE agent_sessions
                SET updated_at = ?
                WHERE session_id = ?
                """,
                (now, session_id),
            )
            persisted = AgentSessionMessage(
                message_id=message_id, session_id=session_id, role=role, content=content,
                payload=payload, created_at=now,
            )
            if role == "agent" and persisted_message_callback is not None:
                persisted_message_callback(conn, persisted)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return persisted

    def get_context_window(
        self,
        *,
        session_id: str,
        token_budget: int | None = None,
    ) -> AgentSessionContextWindow:
        """Return the published summary plus every raw message beyond its watermark."""
        window, _ = self._get_context_window_snapshot(
            session_id=session_id, token_budget=token_budget,
        )
        return window

    def _get_context_window_snapshot(
        self, *, session_id: str, token_budget: int | None = None,
    ) -> tuple[AgentSessionContextWindow, dict[str, Any] | None]:
        """Read the summary, watermark and raw sequence in one SQLite snapshot."""

        self.ensure_session(session_id=session_id)
        budget = token_budget if token_budget is not None else self.default_context_token_budget
        conn = self._conn_factory()
        try:
            self._ensure_context_state_table(conn)
            conn.execute("BEGIN")
            row = conn.execute(
                """
                SELECT session_id, token_budget, summary, recent_messages, token_estimate, updated_at
                FROM agent_session_context_windows
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            state_row = conn.execute(
                "SELECT revision, next_seq, covered_seq, summary_metadata "
                "FROM agent_session_context_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            state = dict(state_row) if state_row else None
            # Sequenced raw messages are authoritative once a legacy window
            # has been adopted. Do not deduplicate distinct sequence entries.
            messages = (
                self._context_messages_from_connection(
                    conn, session_id=session_id, after_seq=state["covered_seq"],
                ) if state else
                self._recent_messages_from_json(row["recent_messages"]) if row else []
            )
        finally:
            conn.close()

        if row is None:
            return AgentSessionContextWindow(
                session_id=session_id,
                token_budget=budget,
                summary="",
                recent_messages=[],
                token_estimate=0,
                updated_at=_now_iso(),
            ), state
        summary = row["summary"]
        return AgentSessionContextWindow(
            session_id=row["session_id"],
            token_budget=row["token_budget"],
            summary=summary,
            summary_metadata=json.loads(state["summary_metadata"]) if state else {},
            recent_messages=messages,
            token_estimate=self._context_token_estimate(summary, messages),
            updated_at=row["updated_at"],
        ), state

    def get_prompt_context_window(
        self, *, session_id: str, token_budget: int | None = None,
    ) -> AgentSessionContextWindow:
        """Return a bounded prompt projection without changing the full raw window."""
        raw, state = self._get_context_window_snapshot(session_id=session_id)
        budget = token_budget if token_budget is not None else raw.token_budget
        if raw.token_estimate <= budget:
            return raw.model_copy(update={"token_budget": budget})

        summary, recent, estimate = self._fit_context_window(
            summary=raw.summary, messages=raw.recent_messages, token_budget=budget,
        )
        if estimate > budget:
            messages = raw.recent_messages
            prior = self._summarize_messages_locally(raw.summary, messages[:-2])
            user_line = ""
            agent_line = ""
            if messages:
                current_user = messages[-2] if len(messages) > 1 else messages[-1]
                user_line = "user: " + self._compact_text(current_user.content, max_chars=500)
            if len(messages) > 1:
                agent_line = "agent: " + self._compact_text(messages[-1].content, max_chars=500)

            user_budget = int(budget * 0.65)
            agent_budget = int(budget * 0.10)
            prior_budget = max(0, budget - user_budget - agent_budget - 4)
            prior = self._trim_summary_for_budget(
                summary=prior, messages=[], token_budget=prior_budget,
            )
            user_line = self._trim_summary_for_budget(
                summary=user_line, messages=[], token_budget=user_budget,
            )
            agent_line = self._trim_summary_for_budget(
                summary=agent_line, messages=[], token_budget=agent_budget,
            )
            summary = "\n".join(part for part in (prior, user_line, agent_line) if part)
            estimate = self._context_token_estimate(summary, [])
            if estimate > budget:
                summary = self._trim_summary_for_budget(
                    summary=summary, messages=[], token_budget=budget,
                )
                estimate = self._context_token_estimate(summary, [])
            if estimate > budget:
                summary, estimate = "", 0
            recent = []

        metadata = {
            **raw.summary_metadata,
            "emergency_view": True,
            "method": "local_emergency_view",
            "lossy_fallback_possible": True,
            "raw_interval": {
                "from_seq": state["covered_seq"] + 1 if state else 1,
                "to_seq": state["next_seq"] - 1 if state else len(raw.recent_messages),
            },
            "input_trace_ids": list(dict.fromkeys(
                message.trace_id for message in raw.recent_messages if message.trace_id
            ))[:64],
        }
        return raw.model_copy(update={
            "token_budget": budget, "summary": summary,
            "summary_metadata": metadata, "recent_messages": recent,
            "token_estimate": estimate,
        })

    def record_context_exchange(
        self,
        *,
        session_id: str,
        user_input: str,
        agent_answer: str,
        trace_id: str,
        effect_id: str | None = None,
        token_budget: int | None = None,
        context_summarizer: Callable[
            [str, list[SessionRecentMessage], list[SessionRecentMessage], int],
            str | None,
        ]
        | None = None,
        background_enqueue: Callable[..., Any] | None = None,
    ) -> AgentSessionContextWindow:
        """Atomically append an exchange and update its context window.

        `background_enqueue` opts into asynchronous precompaction. It receives
        `(session_id, revision, target_seq)`; callbacks accepting `conn=` (or a fourth
        positional connection argument) run inside the same SQLite transaction and should
        insert an idempotent outbox item. Other callbacks are invoked after commit and are
        best effort; durable delivery requires the transactional form. The published summary
        and its covered sequence remain unchanged in background mode, so readers always see
        the raw tail. Transactional queues also handle hard overflow, with a local prompt
        projection until publication. Without a transactional queue, hard-threshold
        compaction remains synchronous through `context_summarizer`.
        """
        transactional_enqueue = (
            background_enqueue is not None
            and self._callback_accepts_connection(background_enqueue)
        )
        replay_window = (
            self.get_prompt_context_window if transactional_enqueue else self.get_context_window
        )
        if effect_id is not None:
            conn = self._conn_factory()
            try:
                existing = conn.execute(
                    "SELECT 1 FROM agent_session_effects WHERE effect_id = ?", (effect_id,)
                ).fetchone()
            finally:
                conn.close()
            if existing is not None:
                return replay_window(session_id=session_id, token_budget=token_budget)
        now = _now_iso()
        conn = self._conn_factory()
        post_commit_enqueue: tuple[int, int] | None = None
        sync_compaction: tuple[int, int, int, str, list[SessionRecentMessage], int] | None = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_context_state_table(conn)
            if effect_id is not None and conn.execute(
                "SELECT 1 FROM agent_session_effects WHERE effect_id = ?", (effect_id,)
            ).fetchone():
                conn.rollback()
                return replay_window(session_id=session_id, token_budget=token_budget)
            row = conn.execute(
                "SELECT token_budget, summary, recent_messages, token_estimate FROM agent_session_context_windows WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            state = conn.execute(
                "SELECT revision, next_seq, covered_seq FROM agent_session_context_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            budget = (
                token_budget if token_budget is not None
                else row["token_budget"] if row else self.default_context_token_budget
            )
            summary = row["summary"] if row else ""
            existing = self._recent_messages_from_json(row["recent_messages"]) if row else []
            revision = state["revision"] if state else 0
            next_seq = state["next_seq"] if state else 1
            covered_seq = state["covered_seq"] if state else 0
            if state is None:
                # Adopt pre-existing recent messages as sequence-addressed raw tail.
                for seq, message in enumerate(existing, start=1):
                    conn.execute(
                        "INSERT OR IGNORE INTO agent_session_context_messages(session_id, seq, role, content, created_at, trace_id) VALUES(?, ?, ?, ?, ?, ?)",
                        (session_id, seq, message.role, message.content, message.created_at, message.trace_id),
                    )
                next_seq = len(existing) + 1
            user_message = SessionRecentMessage(
                role="user",
                content=user_input,
                created_at=now,
                trace_id=trace_id,
            )
            agent_message = SessionRecentMessage(
                role="agent",
                content=agent_answer,
                created_at=now,
                trace_id=trace_id,
            )
            for seq, message in ((next_seq, user_message), (next_seq + 1, agent_message)):
                conn.execute(
                    "INSERT INTO agent_session_context_messages(session_id, seq, role, content, created_at, trace_id) VALUES(?, ?, ?, ?, ?, ?)",
                    (session_id, seq, message.role, message.content, message.created_at, message.trace_id),
                )
            conn.execute(
                "INSERT INTO agent_session_context_state(session_id, revision, next_seq, covered_seq) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision, next_seq=excluded.next_seq",
                (session_id, revision + 1, next_seq + 2, covered_seq),
            )
            all_messages = self._context_messages_from_connection(
                conn, session_id=session_id, after_seq=covered_seq
            )
            estimate = self._context_token_estimate(summary, all_messages)
            hard_async = estimate > budget and transactional_enqueue
            background_mode = (
                background_enqueue is not None
                and estimate >= int(budget * 0.70)
                and (estimate <= budget or hard_async)
            )
            if background_mode or estimate > budget:
                kept_summary = summary
                kept_messages = all_messages
                token_estimate = estimate
                if estimate > budget and not hard_async:
                    target_seq = next_seq - 1 if len(all_messages) > 2 else next_seq + 1
                    sync_compaction = (
                        revision + 1, covered_seq, target_seq,
                        summary, all_messages, budget,
                    )
            else:
                kept_summary, kept_messages, token_estimate = self._fit_context_window(
                    summary=summary, messages=all_messages, token_budget=budget,
                    context_summarizer=None,
                )
            updated = AgentSessionContextWindow(
                session_id=session_id, token_budget=budget, summary=kept_summary,
                recent_messages=kept_messages, token_estimate=token_estimate, updated_at=now,
            )
            conn.execute(
                "INSERT INTO agent_session_context_windows(session_id, token_budget, summary, recent_messages, token_estimate, updated_at) VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET token_budget=excluded.token_budget, summary=excluded.summary, recent_messages=excluded.recent_messages, token_estimate=excluded.token_estimate, updated_at=excluded.updated_at",
                (session_id, budget, kept_summary,
                 json.dumps([m.model_dump(mode="json") for m in kept_messages], ensure_ascii=False),
                 token_estimate, now),
            )
            if effect_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO agent_session_effects(effect_id, session_id, effect_type, created_at) VALUES(?, ?, 'context_exchange', ?)",
                    (effect_id, session_id, now),
                )
            if background_mode:
                target_seq = next_seq + 1
                if transactional_enqueue:
                    self._invoke_enqueue(background_enqueue, session_id, revision + 1, target_seq, conn)
                else:
                    post_commit_enqueue = (revision + 1, target_seq)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if post_commit_enqueue is not None and background_enqueue is not None:
            self._invoke_enqueue(background_enqueue, session_id, *post_commit_enqueue, None)
        if sync_compaction is not None:
            expected_revision, expected_covered, target_seq, prior_summary, raw, hard_budget = sync_compaction
            try:
                if len(raw) > 2:
                    new_summary, _, _ = self._fit_context_window(
                        summary=prior_summary, messages=raw, token_budget=hard_budget,
                        context_summarizer=context_summarizer,
                    )
                else:
                    new_summary = (
                        context_summarizer(prior_summary, raw, [], hard_budget)
                        if context_summarizer is not None else None
                    ) or self._summarize_messages_locally(prior_summary, raw)
                    new_summary = self._trim_summary_for_budget(
                        summary=new_summary, messages=[], token_budget=hard_budget,
                    )
            except Exception:  # noqa: BLE001 - preserve turn on compressor failure.
                new_summary, _, _ = self._fit_context_window(
                    summary=prior_summary, messages=raw, token_budget=hard_budget,
                    context_summarizer=None,
                )
            try:
                self.publish_context_summary(
                    session_id=session_id, expected_revision=expected_revision,
                    expected_covered_seq=expected_covered, target_seq=target_seq,
                    summary=new_summary, token_budget=hard_budget,
                )
            except sqlite3.Error:
                # Raw sequenced messages were already committed. Another turn
                # can retry; losing an answer is worse than delayed compaction.
                pass
            return self.get_context_window(session_id=session_id, token_budget=hard_budget)
        if background_mode and estimate > budget:
            return self.get_prompt_context_window(session_id=session_id, token_budget=budget)
        return updated

    def _ensure_context_state_table(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS agent_session_context_state (session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, next_seq INTEGER NOT NULL, covered_seq INTEGER NOT NULL DEFAULT 0)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_session_context_state)")}
        if "summary_revision" not in columns:
            self._add_context_column(conn, "summary_revision", "INTEGER NOT NULL DEFAULT 0")
        if "summary_metadata" not in columns:
            self._add_context_column(conn, "summary_metadata", "TEXT NOT NULL DEFAULT '{}' ")
        conn.execute("CREATE TABLE IF NOT EXISTS agent_session_context_messages (session_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, trace_id TEXT, PRIMARY KEY(session_id, seq))")

    @staticmethod
    def _add_context_column(conn, name: str, declaration: str) -> None:
        try:
            conn.execute(f"ALTER TABLE agent_session_context_state ADD COLUMN {name} {declaration}")
        except sqlite3.OperationalError:
            # Another connection may have performed the additive migration
            # between PRAGMA and ALTER. Do not suppress actual storage errors.
            if name not in {row[1] for row in conn.execute("PRAGMA table_info(agent_session_context_state)")}:
                raise

    def _read_context_state(self, *, conn_factory, session_id: str):
        conn = conn_factory()
        try:
            self._ensure_context_state_table(conn)
            row = conn.execute("SELECT revision, next_seq, covered_seq,summary_metadata FROM agent_session_context_state WHERE session_id = ?", (session_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _messages_after_seq(self, *, session_id: str, covered_seq: int) -> list[SessionRecentMessage]:
        conn = self._conn_factory()
        try:
            self._ensure_context_state_table(conn)
            rows = conn.execute("SELECT role, content, created_at, trace_id FROM agent_session_context_messages WHERE session_id = ? AND seq > ? ORDER BY seq", (session_id, covered_seq)).fetchall()
            return [SessionRecentMessage(role=r["role"], content=r["content"], created_at=r["created_at"], trace_id=r["trace_id"]) for r in rows]
        finally:
            conn.close()

    def _callback_accepts_connection(self, callback: Callable[..., Any]) -> bool:
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return False
        try:
            signature.bind("session", 1, 2, conn=None)
        except TypeError:
            try:
                signature.bind("session", 1, 2, None)
            except TypeError:
                return False
        return True

    def _invoke_enqueue(self, callback, session_id, revision, target_seq, conn) -> None:
        if conn is not None:
            try:
                inspect.signature(callback).bind(session_id, revision, target_seq, conn=conn)
            except TypeError:
                callback(session_id, revision, target_seq, conn)
            else:
                # A TypeError from inside the callback must roll back, not
                # invoke the callback a second time with positional arguments.
                callback(session_id, revision, target_seq, conn=conn)
        else:
            callback(session_id, revision, target_seq)

    def publish_context_summary(
        self, *, session_id: str, expected_revision: int, expected_covered_seq: int,
        target_seq: int,
        summary: str, token_budget: int | None = None,
        expected_summary_revision: int | None = None,
        publication_lease: tuple[str, str, int] | None = None,
        summary_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Publish a worker result through its fixed target watermark and revision CAS.

        `target_seq` must be the sequence captured when that job was enqueued. Newer
        messages are stored as raw tail and are never folded into the worker's summary.
        """
        conn = self._conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_context_state_table(conn)
            state = conn.execute("SELECT revision, covered_seq,summary_revision FROM agent_session_context_state WHERE session_id = ?", (session_id,)).fetchone()
            valid_revision = state is not None and (
                state["revision"] == expected_revision if expected_summary_revision is None
                else state["summary_revision"] == expected_summary_revision
            )
            if not valid_revision or state["covered_seq"] != expected_covered_seq:
                conn.rollback()
                return False
            if publication_lease is not None:
                now = _now_iso()
                owned = conn.execute(
                    "SELECT 1 FROM background_jobs WHERE job_id=? AND lease_owner=? AND lease_epoch=? AND status='running' AND lease_expires_at>? AND (deadline IS NULL OR deadline>?)",
                    (*publication_lease, now, now),
                ).fetchone()
                if owned is None:
                    conn.rollback()
                    return False
            current_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM agent_session_context_messages WHERE session_id = ?", (session_id,)).fetchone()["seq"]
            if target_seq < expected_covered_seq or target_seq > current_seq:
                conn.rollback()
                return False
            tail_rows = conn.execute("SELECT role, content, created_at, trace_id FROM agent_session_context_messages WHERE session_id = ? AND seq > ? ORDER BY seq", (session_id, target_seq)).fetchall()
            tail = [SessionRecentMessage(role=r["role"], content=r["content"], created_at=r["created_at"], trace_id=r["trace_id"]) for r in tail_rows]
            budget = token_budget if token_budget is not None else self.default_context_token_budget
            estimate = self._context_token_estimate(summary, tail)
            metadata = summary_metadata or {"method": "synchronous_or_local", "lossy_fallback_possible": True}
            encoded_metadata = json.dumps(metadata, ensure_ascii=False)
            if len(encoded_metadata.encode("utf-8")) > 8192:
                raise ValueError("summary metadata exceeds bounded diagnostic budget")
            conn.execute("UPDATE agent_session_context_state SET covered_seq = ?, revision = revision + 1,summary_revision=summary_revision+1,summary_metadata=? WHERE session_id = ? AND covered_seq = ?", (target_seq, encoded_metadata, session_id, expected_covered_seq))
            conn.execute("INSERT INTO agent_session_context_windows(session_id, token_budget, summary, recent_messages, token_estimate, updated_at) VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET token_budget=excluded.token_budget, summary=excluded.summary, recent_messages=excluded.recent_messages, token_estimate=excluded.token_estimate, updated_at=excluded.updated_at", (session_id, budget, summary, json.dumps([m.model_dump(mode="json") for m in tail], ensure_ascii=False), estimate, _now_iso()))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _context_messages_from_connection(
        self, conn: sqlite3.Connection, *, session_id: str, after_seq: int
    ) -> list[SessionRecentMessage]:
        rows = conn.execute(
            "SELECT role, content, created_at, trace_id FROM agent_session_context_messages "
            "WHERE session_id = ? AND seq > ? ORDER BY seq",
            (session_id, after_seq),
        ).fetchall()
        return [
            SessionRecentMessage(
                role=row["role"], content=row["content"],
                created_at=row["created_at"], trace_id=row["trace_id"],
            )
            for row in rows
        ]

    def _upsert_context_window(
        self, window: AgentSessionContextWindow, *, effect_id: str | None = None
    ) -> None:
        conn = self._conn_factory()
        try:
            conn.execute(
                """
                INSERT INTO agent_session_context_windows(
                    session_id, token_budget, summary, recent_messages, token_estimate, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    token_budget=excluded.token_budget,
                    summary=excluded.summary,
                    recent_messages=excluded.recent_messages,
                    token_estimate=excluded.token_estimate,
                    updated_at=excluded.updated_at
                """,
                (
                    window.session_id,
                    window.token_budget,
                    window.summary,
                    json.dumps(
                        [message.model_dump(mode="json") for message in window.recent_messages],
                        ensure_ascii=False,
                    ),
                    window.token_estimate,
                    window.updated_at,
                ),
            )
            if effect_id is not None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_session_effects(
                        effect_id, session_id, effect_type, created_at
                    ) VALUES (?, ?, 'context_exchange', ?)
                    """,
                    (effect_id, window.session_id, window.updated_at),
                )
            conn.commit()
        finally:
            conn.close()

    def _fit_context_window(
        self,
        *,
        summary: str,
        messages: list[SessionRecentMessage],
        token_budget: int,
        context_summarizer: Callable[
            [str, list[SessionRecentMessage], list[SessionRecentMessage], int],
            str | None,
        ]
        | None = None,
    ) -> tuple[str, list[SessionRecentMessage], int]:
        if self._context_token_estimate(summary, messages) <= token_budget:
            return summary, messages, self._context_token_estimate(summary, messages)

        kept = messages[-2:] if len(messages) > 2 else list(messages)
        messages_to_summarize = messages[:-2] if len(messages) > 2 else []
        if context_summarizer is not None and messages_to_summarize:
            current_summary = context_summarizer(
                summary, messages_to_summarize, kept, token_budget
            ) or self._summarize_messages_locally(summary, messages_to_summarize)
        else:
            current_summary = self._summarize_messages_locally(summary, messages_to_summarize)
        current_summary = self._trim_summary_for_budget(
            summary=current_summary,
            messages=kept,
            token_budget=token_budget,
        )
        return (
            current_summary,
            kept,
            self._context_token_estimate(current_summary, kept),
        )

    def _summarize_messages_locally(
        self,
        summary: str,
        messages: list[SessionRecentMessage],
    ) -> str:
        current_summary = summary
        for message in messages:
            current_summary = self._append_summary_message(current_summary, message)
        return current_summary

    def _append_summary_message(self, summary: str, message: SessionRecentMessage) -> str:
        prefix = f"{message.created_at} {message.role}: "
        line = prefix + self._compact_text(message.content, max_chars=500)
        return f"{summary.rstrip()}\n{line}".strip()

    def _context_token_estimate(
        self,
        summary: str,
        messages: list[SessionRecentMessage],
    ) -> int:
        if not summary and not messages:
            return 0
        text = summary + "\n" + "\n".join(message.content for message in messages)
        counter = self._active_context_token_counter()
        if counter is not None:
            return counter.count_text(text).count
        return self._estimate_tokens(text)

    def _estimate_tokens(self, text: str) -> int:
        return max(1, (len(text) + 3) // 4) if text else 0

    def _compact_text(self, text: str, *, max_chars: int) -> str:
        compact = " ".join(text.split())
        if len(compact) <= max_chars:
            return compact
        return self._bounded_excerpt(compact, max_chars=max_chars)

    @staticmethod
    def _bounded_excerpt(
        text: str, *, max_chars: int, tail_fraction: float = 0.5,
    ) -> str:
        """Keep bounded excerpts from both ends and disclose the omitted span."""
        if max_chars <= 0:
            return ""
        if len(text) <= max_chars:
            return text
        tail_fraction = min(1.0, max(0.0, tail_fraction))
        retained = max(0, max_chars - 32)
        while retained >= 0:
            tail_chars = int(retained * tail_fraction)
            # Preserve the opening fact without consuming the whole excerpt:
            # even small windows reserve at least a quarter for the newest text.
            head_chars = max(retained - tail_chars, min(80, int(retained * 0.75)))
            tail_chars = retained - head_chars
            omitted = len(text) - retained
            marker = f" [... {omitted} chars omitted ...] "
            if head_chars + len(marker) + tail_chars <= max_chars:
                head = text[:head_chars].rstrip()
                tail = text[len(text) - tail_chars:].lstrip() if tail_chars else ""
                return f"{head}{marker}{tail}"
            retained -= 1
        # A very small budget cannot fit the counted marker; retain an explicit
        # omission signal instead of returning a misleading raw prefix.
        return "…"

    def _trim_summary_for_budget(
        self,
        *,
        summary: str,
        messages: list[SessionRecentMessage],
        token_budget: int,
    ) -> str:
        message_tokens = self._context_token_estimate("", messages)
        available_summary_tokens = max(token_budget - message_tokens, 0)
        if available_summary_tokens <= 0:
            return ""
        if self._context_token_estimate(summary, messages) <= token_budget:
            return summary

        # Preserve the beginning for durable context and favor the ending, where
        # later summaries normally carry the newest state. Measure each excerpt
        # with the active counter because tokenization is not additive at joins.
        low, high = 0, len(summary)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = self._bounded_excerpt(
                summary, max_chars=middle, tail_fraction=0.6,
            )
            if self._context_token_estimate(candidate, messages) <= token_budget:
                low = middle
            else:
                high = middle - 1
        if low == 0:
            return ""
        result = self._bounded_excerpt(summary, max_chars=low, tail_fraction=0.6)
        # Tokenizers need not be monotonic in character length. Never publish
        # a candidate that fails the final exact measurement.
        return result if self._context_token_estimate(result, messages) <= token_budget else ""

    def _recent_messages_from_json(self, value: str) -> list[SessionRecentMessage]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [
            SessionRecentMessage.model_validate(item) for item in payload if isinstance(item, dict)
        ]

    def _default_title(self, *, title: str | None, initial_message: str | None) -> str:
        if title and title.strip():
            return title.strip()
        if initial_message and initial_message.strip():
            return initial_message.strip()[:60]
        return "New Session"

    def _session_from_row(self, row: sqlite3.Row) -> AgentSession:
        metadata = self._json_dict(row["metadata"])
        workspace_value = metadata.get("workspace")
        workspace = (
            SessionWorkspace.model_validate(workspace_value)
            if isinstance(workspace_value, dict)
            else None
        )
        return AgentSession(
            session_id=row["session_id"],
            title=row["title"],
            status=row["status"],
            metadata=metadata,
            workspace=workspace,
            project_id=metadata.get("project_id"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _message_from_row(self, row: sqlite3.Row) -> AgentSessionMessage:
        return AgentSessionMessage(
            message_id=row["message_id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            payload=self._json_dict(row["payload"]),
            created_at=row["created_at"],
        )

    def _json_dict(self, value: str) -> dict[str, Any]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
