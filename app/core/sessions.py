"""Persistent agent session and message management."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any, Literal

from pydantic import BaseModel, Field


SessionRole = Literal["user", "agent", "system", "tool"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


class AgentSession(BaseModel):
    session_id: str
    title: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    workspace: "SessionWorkspace | None" = None
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

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]) -> None:
        self._conn_factory = conn_factory

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

    def list_sessions(self, *, limit: int = 50) -> AgentSessionList:
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                """
                SELECT session_id, title, status, metadata, created_at, updated_at
                FROM agent_sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
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

    def get_session_or_none(self, *, session_id: str) -> AgentSession | None:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                SELECT session_id, title, status, metadata, created_at, updated_at
                FROM agent_sessions
                WHERE session_id = ?
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
    ) -> AgentSession:
        session = self.get_session_or_none(session_id=session_id)
        if session is None:
            raise KeyError(f"Session not found: {session_id}")
        metadata = dict(session.metadata)
        metadata["workspace"] = workspace.model_dump(mode="json")
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
    ) -> AgentSessionMessage:
        self.ensure_session(session_id=session_id)
        now = _now_iso()
        message_id = _stable_id("session_msg", session_id, role, content, now)
        payload = payload or {}

        conn = self._conn_factory()
        try:
            conn.execute(
                """
                INSERT INTO agent_session_messages(
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
            conn.commit()
        finally:
            conn.close()

        return AgentSessionMessage(
            message_id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            payload=payload,
            created_at=now,
        )

    def get_context_window(
        self,
        *,
        session_id: str,
        token_budget: int | None = None,
    ) -> AgentSessionContextWindow:
        self.ensure_session(session_id=session_id)
        budget = token_budget or self.default_context_token_budget
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                SELECT session_id, token_budget, summary, recent_messages, token_estimate, updated_at
                FROM agent_session_context_windows
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
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
            )
        return AgentSessionContextWindow(
            session_id=row["session_id"],
            token_budget=row["token_budget"],
            summary=row["summary"],
            recent_messages=self._recent_messages_from_json(row["recent_messages"]),
            token_estimate=row["token_estimate"],
            updated_at=row["updated_at"],
        )

    def record_context_exchange(
        self,
        *,
        session_id: str,
        user_input: str,
        agent_answer: str,
        trace_id: str,
        token_budget: int | None = None,
        context_summarizer: Callable[
            [str, list[SessionRecentMessage], list[SessionRecentMessage], int],
            str | None,
        ]
        | None = None,
    ) -> AgentSessionContextWindow:
        window = self.get_context_window(
            session_id=session_id,
            token_budget=token_budget,
        )
        now = _now_iso()
        messages = [
            *window.recent_messages,
            SessionRecentMessage(
                role="user",
                content=user_input,
                created_at=now,
                trace_id=trace_id,
            ),
            SessionRecentMessage(
                role="agent",
                content=agent_answer,
                created_at=now,
                trace_id=trace_id,
            ),
        ]
        summary, kept_messages, token_estimate = self._fit_context_window(
            summary=window.summary,
            messages=messages,
            token_budget=window.token_budget,
            context_summarizer=context_summarizer,
        )
        updated = AgentSessionContextWindow(
            session_id=session_id,
            token_budget=window.token_budget,
            summary=summary,
            recent_messages=kept_messages,
            token_estimate=token_estimate,
            updated_at=now,
        )
        self._upsert_context_window(updated)
        return updated

    def _upsert_context_window(self, window: AgentSessionContextWindow) -> None:
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
            current_summary = (
                context_summarizer(summary, messages_to_summarize, kept, token_budget)
                or self._summarize_messages_locally(summary, messages_to_summarize)
            )
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
        text = summary + "\n" + "\n".join(message.content for message in messages)
        return self._estimate_tokens(text)

    def _estimate_tokens(self, text: str) -> int:
        return max(1, (len(text) + 3) // 4) if text else 0

    def _compact_text(self, text: str, *, max_chars: int) -> str:
        compact = " ".join(text.split())
        if len(compact) <= max_chars:
            return compact
        return compact[: max_chars - 3].rstrip() + "..."

    def _trim_summary_for_budget(
        self,
        *,
        summary: str,
        messages: list[SessionRecentMessage],
        token_budget: int,
    ) -> str:
        message_tokens = self._context_token_estimate("", messages)
        available_summary_tokens = max(token_budget - message_tokens, 0)
        max_summary_chars = available_summary_tokens * 4
        if max_summary_chars <= 0:
            return ""
        if len(summary) <= max_summary_chars:
            return summary
        return summary[-max_summary_chars:].lstrip()

    def _recent_messages_from_json(self, value: str) -> list[SessionRecentMessage]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [
            SessionRecentMessage.model_validate(item)
            for item in payload
            if isinstance(item, dict)
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
