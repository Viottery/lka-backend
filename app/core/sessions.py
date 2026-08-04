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
    created_at: str
    updated_at: str


class AgentSessionMessage(BaseModel):
    message_id: str
    session_id: str
    role: SessionRole
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class AgentSessionList(BaseModel):
    sessions: list[AgentSession]


class AgentSessionDetail(BaseModel):
    session: AgentSession
    messages: list[AgentSessionMessage]


class SessionService:
    """Deterministic storage service for parallel and multi-turn agent sessions."""

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

    def _default_title(self, *, title: str | None, initial_message: str | None) -> str:
        if title and title.strip():
            return title.strip()
        if initial_message and initial_message.strip():
            return initial_message.strip()[:60]
        return "New Session"

    def _session_from_row(self, row: sqlite3.Row) -> AgentSession:
        return AgentSession(
            session_id=row["session_id"],
            title=row["title"],
            status=row["status"],
            metadata=self._json_dict(row["metadata"]),
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
