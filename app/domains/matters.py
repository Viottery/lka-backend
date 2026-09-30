"""Independent matter persistence and search service."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

from pydantic import BaseModel, Field

from app.domains.mail_knowledge import MailKnowledgeMirror


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


class MatterSourceLinkInput(BaseModel):
    source_type: str
    source_id: str
    reason: str = ""


class MatterSourceLink(BaseModel):
    source_type: str
    source_id: str
    reason: str = ""
    created_at: str


class MatterCreateInput(BaseModel):
    title: str
    summary: str = ""
    status: str = "open"
    priority: str = "normal"
    due_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_links: list[MatterSourceLinkInput] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MatterUpdateInput(BaseModel):
    title: str | None = None
    summary: str | None = None
    status: str | None = None
    priority: str | None = None
    due_at: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class MatterRecord(BaseModel):
    matter_id: str
    title: str
    summary: str
    status: str
    priority: str
    due_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_links: list[MatterSourceLink] = Field(default_factory=list)
    created_at: str
    updated_at: str


class MatterList(BaseModel):
    matters: list[MatterRecord]


class MatterSearchResultItem(BaseModel):
    matter_id: str
    title: str
    summary: str
    status: str
    priority: str
    due_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    snippet: str


class MatterSearchResult(BaseModel):
    query: str
    matters: list[MatterSearchResultItem]


class MatterService:
    """Deterministic local matter service independent of any source provider."""

    VALID_STATUSES = {"open", "in_progress", "waiting", "done", "cancelled"}
    VALID_PRIORITIES = {"low", "normal", "high", "urgent"}

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]) -> None:
        self._conn_factory = conn_factory

    def create_matter(
        self, payload: MatterCreateInput, *,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
    ) -> MatterRecord:
        if (
            (source_ids is not None or account_ids is not None)
            and not payload.source_links
            and not allow_unattributed
        ):
            raise PermissionError("Unlinked matters are outside the child source/account scope.")
        now = _now_iso()
        title = payload.title.strip() or "Untitled matter"
        summary = payload.summary.strip() or title
        status = self._normalized_status(payload.status)
        priority = self._normalized_priority(payload.priority)
        tags = self._normalized_tags(payload.tags)
        matter_id = _stable_id(
            "matter",
            title,
            summary,
            payload.due_at,
            json.dumps(tags, ensure_ascii=False, sort_keys=True),
            now,
        )
        conn = self._conn_factory()
        try:
            if source_ids is not None or account_ids is not None:
                conn.execute("BEGIN IMMEDIATE")
                self.validate_source_links_scope(
                    source_links=payload.source_links, source_ids=source_ids,
                    account_ids=account_ids, allow_unattributed=allow_unattributed,
                    conn=conn,
                )
            conn.execute(
                """
                INSERT INTO matters(
                    matter_id, title, summary, status, priority, due_at,
                    tags, metadata, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(matter_id) DO UPDATE SET
                    title=excluded.title,
                    summary=excluded.summary,
                    status=excluded.status,
                    priority=excluded.priority,
                    due_at=excluded.due_at,
                    tags=excluded.tags,
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at
                """,
                (
                    matter_id,
                    title,
                    summary,
                    status,
                    priority,
                    payload.due_at,
                    self._json_dumps(tags),
                    self._json_dumps(payload.metadata),
                    now,
                    now,
                ),
            )
            self._upsert_fts(conn, matter_id, title, summary, tags)
            self._replace_source_links(
                conn,
                matter_id=matter_id,
                source_links=payload.source_links,
                now=now,
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_matter(matter_id=matter_id)

    def get_matter(self, *, matter_id: str) -> MatterRecord:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                SELECT matter_id, title, summary, status, priority, due_at,
                       tags, metadata, created_at, updated_at
                FROM matters
                WHERE matter_id = ?
                """,
                (matter_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Matter not found: {matter_id}")
            source_links = self._load_source_links(conn, matter_id=matter_id)
        finally:
            conn.close()
        return self._record_from_row(row, source_links=source_links)

    def _matter_in_scope(
        self,
        conn: sqlite3.Connection,
        *,
        matter_id: str,
        source_ids: tuple[str, ...] | None,
        account_ids: tuple[str, ...] | None,
        allow_unattributed: bool,
    ) -> bool:
        if source_ids is None and account_ids is None:
            return True
        links = conn.execute(
            "SELECT source_type, source_id FROM matter_source_links WHERE matter_id = ?",
            (matter_id,),
        ).fetchall()
        if not links:
            return allow_unattributed
        sources = set(source_ids or ())
        accounts = set(account_ids or ())
        for link in links:
            source_type = str(link["source_type"]).strip().lower()
            source_id = str(link["source_id"]).strip()
            if source_type == "mail_message":
                owner = conn.execute(
                    "SELECT account_id FROM mail_messages WHERE message_id = ?",
                    (source_id,),
                ).fetchone()
                if owner is None:
                    return False
                if (
                    source_ids is not None
                    and MailKnowledgeMirror.source_id_for_account(owner["account_id"]) not in sources
                ):
                    return False
                if account_ids is not None and owner["account_id"] not in accounts:
                    return False
            else:
                return False
        return True

    def validate_source_links_scope(
        self,
        *,
        source_links: list[MatterSourceLinkInput],
        source_ids: tuple[str, ...] | None,
        account_ids: tuple[str, ...] | None,
        allow_unattributed: bool,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        sources = set(source_ids or ())
        accounts = set(account_ids or ())
        owns_connection = conn is None
        if conn is None:
            conn = self._conn_factory()
        try:
            for link in source_links:
                kind = link.source_type.strip().lower()
                source_id = link.source_id.strip()
                if kind == "mail_message":
                    owner = conn.execute(
                        "SELECT account_id FROM mail_messages WHERE message_id = ?",
                        (source_id,),
                    ).fetchone()
                    if owner is None:
                        raise PermissionError("Matter source ownership cannot be verified for this child run.")
                    if (
                        source_ids is not None
                        and MailKnowledgeMirror.source_id_for_account(owner["account_id"]) not in sources
                    ):
                        raise PermissionError("Matter source link is outside the child source scope.")
                    if account_ids is not None and owner["account_id"] not in accounts:
                        raise PermissionError("Matter source link is outside the child account scope.")
                else:
                    raise PermissionError("Matter source type cannot be verified for this child scope.")
        finally:
            if owns_connection:
                conn.close()

    def list_matters(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
    ) -> MatterList:
        conn = self._conn_factory()
        try:
            if source_ids is not None or account_ids is not None:
                conn.execute("BEGIN")
            limit_sql = "LIMIT ?" if source_ids is None and account_ids is None else ""
            if status:
                rows = conn.execute(
                    f"""
                    SELECT matter_id, title, summary, status, priority, due_at,
                           tags, metadata, created_at, updated_at
                    FROM matters
                    WHERE status = ?
                    ORDER BY
                        due_at IS NULL,
                        due_at ASC,
                        updated_at DESC
                    {limit_sql}
                    """,
                    (self._normalized_status(status), limit)
                    if limit_sql
                    else (self._normalized_status(status),),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT matter_id, title, summary, status, priority, due_at,
                           tags, metadata, created_at, updated_at
                    FROM matters
                    ORDER BY
                        due_at IS NULL,
                        due_at ASC,
                        updated_at DESC
                    {limit_sql}
                    """,
                    (limit,) if limit_sql else (),
                ).fetchall()
            rows = [
                row for row in rows
                if self._matter_in_scope(
                    conn, matter_id=row["matter_id"], source_ids=source_ids,
                    account_ids=account_ids, allow_unattributed=allow_unattributed,
                )
            ][:limit]
            links_by_matter = self._load_source_links_for_rows(conn, rows)
        finally:
            conn.close()
        return MatterList(
            matters=[
                self._record_from_row(
                    row,
                    source_links=links_by_matter.get(row["matter_id"], []),
                )
                for row in rows
            ]
        )

    def search_matters(
        self, *, query: str, limit: int = 10,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
    ) -> MatterSearchResult:
        normalized_query = query.strip()
        conn = self._conn_factory()
        try:
            if source_ids is not None or account_ids is not None:
                conn.execute("BEGIN")
            limit_sql = "LIMIT ?" if source_ids is None and account_ids is None else ""
            if normalized_query:
                rows = conn.execute(
                    f"""
                    SELECT
                        m.matter_id,
                        m.title,
                        m.summary,
                        m.status,
                        m.priority,
                        m.due_at,
                        m.tags,
                        snippet(matters_fts, 2, '[', ']', '...', 12) AS snippet
                    FROM matters_fts
                    JOIN matters m ON m.matter_id = matters_fts.matter_id
                    WHERE matters_fts MATCH ?
                    ORDER BY rank
                    {limit_sql}
                    """,
                    (self._fts_query(normalized_query), limit)
                    if limit_sql
                    else (self._fts_query(normalized_query),),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT matter_id, title, summary, status, priority, due_at, tags,
                           summary AS snippet
                    FROM matters
                    ORDER BY
                        due_at IS NULL,
                        due_at ASC,
                        updated_at DESC
                    {limit_sql}
                    """,
                    (limit,) if limit_sql else (),
                ).fetchall()
            rows = [
                row for row in rows
                if self._matter_in_scope(
                    conn, matter_id=row["matter_id"], source_ids=source_ids,
                    account_ids=account_ids, allow_unattributed=allow_unattributed,
                )
            ][:limit]
        finally:
            conn.close()

        return MatterSearchResult(
            query=query,
            matters=[
                MatterSearchResultItem(
                    matter_id=row["matter_id"],
                    title=row["title"],
                    summary=row["summary"],
                    status=row["status"],
                    priority=row["priority"],
                    due_at=row["due_at"],
                    tags=self._json_list(row["tags"]),
                    snippet=self._trim_snippet(row["snippet"]),
                )
                for row in rows
            ],
        )

    def update_matter(
        self,
        *,
        matter_id: str,
        payload: MatterUpdateInput,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
    ) -> MatterRecord:
        current = self.get_matter(matter_id=matter_id)
        title = payload.title.strip() if payload.title is not None else current.title
        summary = (
            payload.summary.strip()
            if payload.summary is not None
            else current.summary
        )
        status = (
            self._normalized_status(payload.status)
            if payload.status is not None
            else current.status
        )
        priority = (
            self._normalized_priority(payload.priority)
            if payload.priority is not None
            else current.priority
        )
        due_at = payload.due_at if payload.due_at is not None else current.due_at
        tags = (
            self._normalized_tags(payload.tags)
            if payload.tags is not None
            else current.tags
        )
        metadata = payload.metadata if payload.metadata is not None else current.metadata
        now = _now_iso()
        conn = self._conn_factory()
        try:
            if source_ids is not None or account_ids is not None:
                conn.execute("BEGIN IMMEDIATE")
                if not self._matter_in_scope(
                    conn, matter_id=matter_id, source_ids=source_ids,
                    account_ids=account_ids, allow_unattributed=allow_unattributed,
                ):
                    raise PermissionError("Matter is outside the child source/account scope.")
            conn.execute(
                """
                UPDATE matters
                SET title = ?, summary = ?, status = ?, priority = ?, due_at = ?,
                    tags = ?, metadata = ?, updated_at = ?
                WHERE matter_id = ?
                """,
                (
                    title or "Untitled matter",
                    summary or title or "Untitled matter",
                    status,
                    priority,
                    due_at,
                    self._json_dumps(tags),
                    self._json_dumps(metadata),
                    now,
                    matter_id,
                ),
            )
            self._upsert_fts(conn, matter_id, title, summary, tags)
            conn.commit()
        finally:
            conn.close()
        updated = self.get_matter(matter_id=matter_id)
        if source_ids is not None or account_ids is not None:
            conn = self._conn_factory()
            try:
                if not self._matter_in_scope(
                    conn, matter_id=matter_id, source_ids=source_ids,
                    account_ids=account_ids, allow_unattributed=allow_unattributed,
                ):
                    raise PermissionError("Matter is outside the child source/account scope.")
            finally:
                conn.close()
        return updated

    def link_source(
        self,
        *,
        matter_id: str,
        source_link: MatterSourceLinkInput,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
    ) -> MatterRecord:
        self.get_matter(matter_id=matter_id)
        now = _now_iso()
        conn = self._conn_factory()
        try:
            if source_ids is not None or account_ids is not None:
                conn.execute("BEGIN IMMEDIATE")
                self.validate_source_links_scope(
                    source_links=[source_link], source_ids=source_ids,
                    account_ids=account_ids, allow_unattributed=allow_unattributed,
                    conn=conn,
                )
                if not self._matter_in_scope(
                    conn, matter_id=matter_id, source_ids=source_ids,
                    account_ids=account_ids, allow_unattributed=allow_unattributed,
                ):
                    raise PermissionError("Matter is outside the child source/account scope.")
            conn.execute(
                """
                INSERT INTO matter_source_links(
                    matter_id, source_type, source_id, reason, created_at
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(matter_id, source_type, source_id) DO UPDATE SET
                    reason=excluded.reason
                """,
                (
                    matter_id,
                    source_link.source_type.strip() or "unknown",
                    source_link.source_id.strip(),
                    source_link.reason.strip(),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        linked = self.get_matter(matter_id=matter_id)
        if source_ids is not None or account_ids is not None:
            conn = self._conn_factory()
            try:
                if not self._matter_in_scope(
                    conn, matter_id=matter_id, source_ids=source_ids,
                    account_ids=account_ids, allow_unattributed=allow_unattributed,
                ):
                    raise PermissionError("Matter is outside the child source/account scope.")
            finally:
                conn.close()
        return linked

    def _upsert_fts(
        self,
        conn: sqlite3.Connection,
        matter_id: str,
        title: str,
        summary: str,
        tags: list[str],
    ) -> None:
        conn.execute("DELETE FROM matters_fts WHERE matter_id = ?", (matter_id,))
        conn.execute(
            """
            INSERT INTO matters_fts(matter_id, title, summary, tags)
            VALUES(?, ?, ?, ?)
            """,
            (matter_id, title, summary, " ".join(tags)),
        )

    def _replace_source_links(
        self,
        conn: sqlite3.Connection,
        *,
        matter_id: str,
        source_links: list[MatterSourceLinkInput],
        now: str,
    ) -> None:
        for source_link in source_links:
            if not source_link.source_id.strip():
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO matter_source_links(
                    matter_id, source_type, source_id, reason, created_at
                )
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    matter_id,
                    source_link.source_type.strip() or "unknown",
                    source_link.source_id.strip(),
                    source_link.reason.strip(),
                    now,
                ),
            )

    def _load_source_links(
        self,
        conn: sqlite3.Connection,
        *,
        matter_id: str,
    ) -> list[MatterSourceLink]:
        rows = conn.execute(
            """
            SELECT source_type, source_id, reason, created_at
            FROM matter_source_links
            WHERE matter_id = ?
            ORDER BY created_at ASC
            """,
            (matter_id,),
        ).fetchall()
        return [self._source_link_from_row(row) for row in rows]

    def _load_source_links_for_rows(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
    ) -> dict[str, list[MatterSourceLink]]:
        matter_ids = [row["matter_id"] for row in rows]
        if not matter_ids:
            return {}
        placeholders = ", ".join("?" for _ in matter_ids)
        link_rows = conn.execute(
            f"""
            SELECT matter_id, source_type, source_id, reason, created_at
            FROM matter_source_links
            WHERE matter_id IN ({placeholders})
            ORDER BY created_at ASC
            """,
            matter_ids,
        ).fetchall()
        links_by_matter: dict[str, list[MatterSourceLink]] = {}
        for row in link_rows:
            links_by_matter.setdefault(row["matter_id"], []).append(
                self._source_link_from_row(row)
            )
        return links_by_matter

    def _record_from_row(
        self,
        row: sqlite3.Row,
        *,
        source_links: list[MatterSourceLink],
    ) -> MatterRecord:
        return MatterRecord(
            matter_id=row["matter_id"],
            title=row["title"],
            summary=row["summary"],
            status=row["status"],
            priority=row["priority"],
            due_at=row["due_at"],
            tags=self._json_list(row["tags"]),
            metadata=self._json_dict(row["metadata"]),
            source_links=source_links,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _source_link_from_row(self, row: sqlite3.Row) -> MatterSourceLink:
        return MatterSourceLink(
            source_type=row["source_type"],
            source_id=row["source_id"],
            reason=row["reason"],
            created_at=row["created_at"],
        )

    def _fts_query(self, query: str) -> str:
        terms = [term.strip().replace('"', "") for term in query.split() if term.strip()]
        if not terms:
            return '""'
        return " OR ".join(f'"{term}"' for term in terms)

    def _normalized_status(self, status: str | None) -> str:
        normalized = (status or "open").strip().lower()
        return normalized if normalized in self.VALID_STATUSES else "open"

    def _normalized_priority(self, priority: str | None) -> str:
        normalized = (priority or "normal").strip().lower()
        return normalized if normalized in self.VALID_PRIORITIES else "normal"

    def _normalized_tags(self, tags: list[str] | None) -> list[str]:
        if not tags:
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            clean_tag = str(tag).strip()
            if not clean_tag or clean_tag.lower() in seen:
                continue
            normalized.append(clean_tag)
            seen.add(clean_tag.lower())
        return normalized

    def _json_dumps(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _json_list(self, value: str) -> list[str]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [str(item) for item in payload]

    def _json_dict(self, value: str) -> dict[str, Any]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _trim_snippet(self, snippet: str | None) -> str:
        if not snippet:
            return ""
        return snippet if len(snippet) <= 280 else f"{snippet[:277]}..."
