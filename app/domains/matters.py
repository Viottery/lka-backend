"""Independent matter persistence and search service."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha1, sha256
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.domains.mail_knowledge import MailKnowledgeMirror
from app.storage.commit_signals import notify_after_commit


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def validate_matter_date(value: str | None) -> str | None:
    """Accept an ISO calendar date or an instant with an explicit timezone."""
    if value is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        date.fromisoformat(value)
        return value
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("Expected an ISO date or timezone-aware datetime")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Datetime must include a timezone")
    return value


def validate_scheduled_on(value: str | None) -> str | None:
    if value is not None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("scheduled_on must be an ISO calendar date")
        date.fromisoformat(value)
    return value


def validate_scheduled_time(value: str | None) -> str | None:
    validate_matter_date(value)
    if value is not None and len(value) == 10:
        raise ValueError("Time blocks require timezone-aware datetimes")
    return value


def validate_sections(value: list[str]) -> list[str]:
    if len(value) > 100:
        raise ValueError("At most 100 plan sections are supported")
    normalized = [item.strip() for item in value]
    if any(not item or len(item) > 120 for item in normalized):
        raise ValueError("Section names must be 1..120 characters")
    if len(set(normalized)) != len(normalized):
        raise ValueError("Section names must be unique")
    return normalized


def validate_planning_state(*, kind: str, scheduled_start: str | None, scheduled_end: str | None, sections: list[str]) -> None:
    if (scheduled_start is None) != (scheduled_end is None):
        raise ValueError("scheduled_start and scheduled_end must both be supplied or both cleared")
    if scheduled_start is not None and datetime.fromisoformat(scheduled_start) >= datetime.fromisoformat(scheduled_end):
        raise ValueError("scheduled_start must be earlier than scheduled_end")
    if kind != "plan" and sections:
        raise ValueError("Only plans may define sections")


class MatterConflictError(ValueError):
    """A matter changed after the caller read its revision."""


class MatterPlanNotEmptyError(ValueError):
    """A plan cannot be deleted while it still has tasks."""


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
    visibility: Literal["user", "internal"] = "user"
    scheduled_on: str | None = None
    scheduled_start: str | None = None
    scheduled_end: str | None = None
    inbox: bool = Field(default=False, strict=True)
    section: str = Field(default="", max_length=120)
    sort_order: float = Field(default=0, allow_inf_nan=False)
    sections: list[str] = Field(default_factory=list)
    kind: Literal["task", "plan"] = "task"
    parent_matter_id: str | None = None
    project_id: str | None = None
    title: str
    summary: str = ""
    status: str = "open"
    priority: str = "normal"
    due_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_links: list[MatterSourceLinkInput] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _validate_due_at = field_validator("due_at")(validate_matter_date)
    _validate_scheduled_on = field_validator("scheduled_on")(validate_scheduled_on)
    _validate_scheduled_times = field_validator("scheduled_start", "scheduled_end")(validate_scheduled_time)
    _validate_sections = field_validator("sections")(validate_sections)


class MatterUpdateInput(BaseModel):
    visibility: Literal["user", "internal"] = "user"
    scheduled_on: str | None = None
    scheduled_start: str | None = None
    scheduled_end: str | None = None
    inbox: bool = Field(default=False, strict=True)
    section: str = Field(default="", max_length=120)
    sort_order: float = Field(default=0, allow_inf_nan=False)
    sections: list[str] = Field(default_factory=list)
    parent_matter_id: str | None = None
    project_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    title: str | None = None
    summary: str | None = None
    status: str | None = None
    priority: str | None = None
    due_at: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None

    _validate_due_at = field_validator("due_at")(validate_matter_date)
    _validate_scheduled_on = field_validator("scheduled_on")(validate_scheduled_on)
    _validate_scheduled_times = field_validator("scheduled_start", "scheduled_end")(validate_scheduled_time)
    _validate_sections = field_validator("sections")(validate_sections)


class MatterRecord(BaseModel):
    visibility: Literal["user", "internal"] = "user"
    scheduled_on: str | None = None
    scheduled_start: str | None = None
    scheduled_end: str | None = None
    inbox: bool = Field(default=False, strict=True)
    section: str = Field(default="", max_length=120)
    sort_order: float = Field(default=0, allow_inf_nan=False)
    sections: list[str] = Field(default_factory=list)
    kind: Literal["task", "plan"] = "task"
    parent_matter_id: str | None = None
    project_id: str | None = None
    progress: dict[str, int] = Field(default_factory=lambda: {"total": 0, "done": 0, "cancelled": 0})
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
    revision: int = 1
    deleted_at: str | None = None


class MatterList(BaseModel):
    matters: list[MatterRecord]
    next_offset: int | None = None
    total_count: int = 0


class MatterSyncChange(BaseModel):
    sequence: int
    matter: MatterRecord


class MatterSyncPage(BaseModel):
    changes: list[MatterSyncChange]
    cursor: int
    has_more: bool


class MatterSearchResultItem(BaseModel):
    visibility: Literal["user", "internal"] = "user"
    scheduled_on: str | None = None
    scheduled_start: str | None = None
    scheduled_end: str | None = None
    inbox: bool = Field(default=False, strict=True)
    section: str = Field(default="", max_length=120)
    sort_order: float = Field(default=0, allow_inf_nan=False)
    sections: list[str] = Field(default_factory=list)
    kind: Literal["task", "plan"] = "task"
    parent_matter_id: str | None = None
    project_id: str | None = None
    matter_id: str
    title: str
    summary: str
    status: str
    priority: str
    due_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    snippet: str
    revision: int = 1


class MatterSearchResult(BaseModel):
    query: str
    matters: list[MatterSearchResultItem]


@dataclass(frozen=True)
class _MessageMutationReceipt:
    token: object
    conn: sqlite3.Connection
    action: str
    matter_id: str | None
    digest: str


class MatterService:
    """Deterministic local matter service independent of any source provider."""

    VALID_STATUSES = {"open", "in_progress", "waiting", "done", "cancelled"}
    VALID_PRIORITIES = {"low", "normal", "high", "urgent"}

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection], *,
                 internal_items_enabled: Callable[[], bool] | None = None) -> None:
        self._conn_factory = conn_factory
        self.internal_items_enabled = internal_items_enabled or (lambda: False)
        self.__receipt_token = object()
        with self._write_connection() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(matters)")}
            if "revision" not in columns:
                conn.execute("ALTER TABLE matters ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            for column, declaration in (
                ("visibility", "TEXT NOT NULL DEFAULT 'user' CHECK(visibility IN ('user','internal'))"),
                ("kind", "TEXT NOT NULL DEFAULT 'task'"),
                ("parent_matter_id", "TEXT"),
                ("project_id", "TEXT"),
                ("scheduled_on", "TEXT"),
                ("scheduled_start", "TEXT"),
                ("scheduled_end", "TEXT"),
                ("inbox", "INTEGER NOT NULL DEFAULT 0"),
                ("section", "TEXT NOT NULL DEFAULT ''"),
                ("sort_order", "REAL NOT NULL DEFAULT 0"),
                ("sections", "TEXT NOT NULL DEFAULT '[]'"),
                ("deleted_at", "TEXT"),
            ):
                if column not in columns:
                    conn.execute(f"ALTER TABLE matters ADD COLUMN {column} {declaration}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_matters_parent ON matters(parent_matter_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_matters_project ON matters(project_id, kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_matters_schedule ON matters(scheduled_on)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS matter_changes("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, matter_id TEXT NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_matter_changes_id ON matter_changes(matter_id)")
            for trigger, table, event, identity in (
                ("matter_change_insert", "matters", "INSERT", "NEW.matter_id"),
                ("matter_change_update", "matters", "UPDATE", "NEW.matter_id"),
                ("matter_link_change_insert", "matter_source_links", "INSERT", "NEW.matter_id"),
                ("matter_link_change_update", "matter_source_links", "UPDATE", "NEW.matter_id"),
                ("matter_link_change_delete", "matter_source_links", "DELETE", "OLD.matter_id"),
            ):
                conn.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger} AFTER {event} ON {table} "
                    f"BEGIN INSERT INTO matter_changes(matter_id) VALUES({identity}); END"
                )
            # Seed unchanged legacy rows once; triggers and seed share this transaction.
            conn.execute(
                "INSERT INTO matter_changes(matter_id) SELECT m.matter_id FROM matters m "
                "WHERE NOT EXISTS(SELECT 1 FROM matter_changes c WHERE c.matter_id=m.matter_id) "
                "ORDER BY m.created_at,m.matter_id"
            )


    @contextmanager
    def _write_connection(self, conn: sqlite3.Connection | None = None):
        owns = conn is None
        if owns:
            conn = self._conn_factory()
            conn.execute("BEGIN IMMEDIATE")
        elif not conn.in_transaction:
            raise ValueError("Caller-owned matter connection requires an active transaction.")
        try:
            yield conn
            if owns:
                conn.commit()
        except BaseException:
            if owns:
                conn.rollback()
            raise
        finally:
            if owns:
                conn.close()

    def _issue_message_receipt(self, conn, *, action: str, matter_id: str | None, payload: Any):
        """Private capability used only by the authenticated proposal transaction."""
        return _MessageMutationReceipt(
            self.__receipt_token, conn, action, matter_id, self._mutation_digest(payload)
        )

    @staticmethod
    def _mutation_digest(payload: Any) -> str:
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        return sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()

    def _check_message_receipt(self, conn, *, action, matter_id, payload, receipt, links=()):
        existing = (
            conn.execute(
                "SELECT 1 FROM matter_source_links WHERE matter_id=? "
                "AND lower(trim(source_type))='message_history_message' LIMIT 1",
                (matter_id,),
            ).fetchone()
            if matter_id
            else None
        )
        requires = existing or any(
            link.source_type.strip().lower() == "message_history_message" for link in links
        )
        # A real message identity cannot be disguised as "local_evidence" or
        # another source type to evade the dedicated proposal transaction.
        has_messages = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_history_messages'"
        ).fetchone()
        if has_messages:
            for link in links:
                if conn.execute("SELECT 1 FROM message_history_messages WHERE internal_message_id=?",
                                (link.source_id,)).fetchone():
                    if link.source_type != "message_history_message":
                        raise PermissionError("message_source_requires_canonical_type")
                    requires = True
        if not requires:
            return
        if not (
            isinstance(receipt, _MessageMutationReceipt)
            and receipt.token is self.__receipt_token
            and receipt.conn is conn
            and conn.in_transaction
            and receipt.action == action
            and receipt.matter_id == (None if action == "create" else matter_id)
            and receipt.digest == self._mutation_digest(payload)
        ):
            raise PermissionError("message_matter_proposal_receipt_required")

    def _validate_plan_links(
        self, conn, *, kind, parent_matter_id, project_id, project_explicit,
        source_ids, account_ids, allow_unattributed,
    ):
        if parent_matter_id is not None:
            if kind != "task":
                raise ValueError("Only tasks can belong to a plan")
            parent = self.get_matter(
                matter_id=parent_matter_id, conn=conn, source_ids=source_ids,
                account_ids=account_ids, allow_unattributed=allow_unattributed,
            )
            if parent.kind != "plan":
                raise ValueError("Task parent must be a plan")
            if project_explicit and project_id != parent.project_id:
                raise ValueError("Task project must match its plan project")
            project_id = parent.project_id
        if project_id is not None and not conn.execute(
            "SELECT 1 FROM project_profiles WHERE project_id=?", (project_id,),
        ).fetchone():
            raise ValueError("Unknown directory project")
        return project_id

    def _plan_progress(self, conn, matter_id, *, source_ids=None, account_ids=None, allow_unattributed=True):
        rows = conn.execute(
            "SELECT matter_id, status FROM matters WHERE parent_matter_id=? AND kind='task' AND visibility='user' AND deleted_at IS NULL", (matter_id,),
        ).fetchall()
        rows = [row for row in rows if self._matter_in_scope(
            conn, matter_id=row["matter_id"], source_ids=source_ids,
            account_ids=account_ids, allow_unattributed=allow_unattributed,
        )]
        return {"total": len(rows), "done": sum(row["status"] == "done" for row in rows),
                "cancelled": sum(row["status"] == "cancelled" for row in rows)}

    @notify_after_commit
    def create_matter(
        self,
        payload: MatterCreateInput,
        *,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
        conn: sqlite3.Connection | None = None,
        matter_id: str | None = None,
        _receipt: Any = None,
    ) -> MatterRecord:
        if (
            (source_ids is not None or account_ids is not None)
            and not payload.source_links
            and not allow_unattributed
        ):
            raise PermissionError("Unlinked matters are outside the child source/account scope.")
        if payload.visibility == "internal" and not self.internal_items_enabled():
            raise PermissionError("Internal matter creation is disabled")
        if matter_id is not None and (not isinstance(matter_id, str) or not matter_id.strip()):
            raise ValueError("matter_id must be a non-empty server-assigned ID")
        now = _now_iso()
        title = payload.title.strip() or "Untitled matter"
        summary = payload.summary.strip() or title
        status = self._normalized_status(payload.status)
        priority = self._normalized_priority(payload.priority)
        tags = self._normalized_tags(payload.tags)
        matter_id = matter_id or _stable_id(
            "matter",
            title,
            summary,
            payload.due_at,
            json.dumps(tags, ensure_ascii=False, sort_keys=True),
            now,
        )
        with self._write_connection(conn) as transaction:
            self._check_message_receipt(
                transaction,
                action="create",
                matter_id=matter_id,
                payload=payload,
                receipt=_receipt,
                links=payload.source_links,
            )
            if source_ids is not None or account_ids is not None:
                self.validate_source_links_scope(
                    source_links=payload.source_links,
                    source_ids=source_ids,
                    account_ids=account_ids,
                    allow_unattributed=allow_unattributed,
                    conn=transaction,
                )
            validate_planning_state(
                kind=payload.kind, scheduled_start=payload.scheduled_start,
                scheduled_end=payload.scheduled_end, sections=payload.sections,
            )
            project_id = self._validate_plan_links(
                transaction, kind=payload.kind, parent_matter_id=payload.parent_matter_id,
                project_id=payload.project_id, project_explicit="project_id" in payload.model_fields_set,
                source_ids=source_ids, account_ids=account_ids, allow_unattributed=allow_unattributed,
            )
            transaction.execute(
                """
                INSERT INTO matters(
                    matter_id, title, summary, status, priority, due_at,
                    tags, metadata, created_at, updated_at, kind, parent_matter_id, project_id,
                    scheduled_on, scheduled_start, scheduled_end, inbox, section, sort_order, sections, visibility
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    payload.kind,
                    payload.parent_matter_id,
                    project_id,
                    payload.scheduled_on,
                    payload.scheduled_start,
                    payload.scheduled_end,
                    int(payload.inbox),
                    payload.section,
                    payload.sort_order,
                    self._json_dumps(payload.sections),
                    payload.visibility,
                ),
            )
            self._upsert_fts(transaction, matter_id, title, summary, tags)
            self._replace_source_links(
                transaction,
                matter_id=matter_id,
                source_links=payload.source_links,
                now=now,
            )
            return self.get_matter(matter_id=matter_id, conn=transaction)

    def get_matter(
        self, *, matter_id: str, conn: sqlite3.Connection | None = None,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
        include_deleted: bool = False,
    ) -> MatterRecord:
        owns = conn is None
        conn = conn or self._conn_factory()
        try:
            if owns:
                conn.execute("BEGIN")
            row = conn.execute(
                """
                SELECT matter_id, title, summary, status, priority, due_at,
                       tags, metadata, created_at, updated_at, revision, kind, parent_matter_id, project_id, scheduled_on, scheduled_start, scheduled_end, inbox, section, sort_order, sections, visibility, deleted_at
                FROM matters
                WHERE matter_id = ?
                """,
                (matter_id,),
            ).fetchone()
            if row is None or (row["deleted_at"] is not None and not include_deleted):
                raise KeyError(f"Matter not found: {matter_id}")
            if not self._matter_in_scope(
                conn, matter_id=matter_id, source_ids=source_ids,
                account_ids=account_ids, allow_unattributed=allow_unattributed,
            ):
                raise PermissionError("Matter is outside the child source/account scope.")
            source_links = self._load_source_links(conn, matter_id=matter_id)
            record = self._record_from_row(row, source_links=source_links)
            if record.kind == "plan":
                record.progress = self._plan_progress(
                    conn, matter_id, source_ids=source_ids, account_ids=account_ids,
                    allow_unattributed=allow_unattributed,
                )
        finally:
            if owns:
                conn.close()
        return record

    def sync_matters(self, *, after: int = 0, limit: int = 200) -> MatterSyncPage:
        """Read a bounded change page, hydrating current records in one snapshot.

        Sequence entries may repeat a matter. Soft-deleted matters stay in the
        feed with ``deleted_at`` set so caches can drop them; restore clears it. Clients derive parent progress from
        their full task cache because child changes do not journal parent records.
        """
        if after < 0 or not 1 <= limit <= 500:
            raise ValueError("Sync requires after >= 0 and limit in 1..500")
        conn = self._conn_factory()
        try:
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT sequence,matter_id FROM matter_changes WHERE sequence>? ORDER BY sequence LIMIT ?",
                (after, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            records: dict[str, MatterRecord] = {}
            changes = []
            for row in page:
                matter_id = row["matter_id"]
                if matter_id not in records:
                    records[matter_id] = self.get_matter(matter_id=matter_id, conn=conn, include_deleted=True)
                changes.append(MatterSyncChange(sequence=row["sequence"], matter=records[matter_id]))
            return MatterSyncPage(
                changes=changes,
                cursor=page[-1]["sequence"] if page else after,
                has_more=has_more,
            )
        finally:
            conn.close()

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
                    and MailKnowledgeMirror.source_id_for_account(owner["account_id"])
                    not in sources
                ):
                    return False
                if account_ids is not None and owner["account_id"] not in accounts:
                    return False
            elif source_type == "message_history_message":
                owner = self._message_source_owner(conn, source_id)
                if owner is None or not owner["record_enabled"]:
                    return False
                if source_ids is not None and owner["source_id"] not in sources:
                    return False
                if account_ids is not None and owner["account_scope_id"] not in accounts:
                    return False
            else:
                return False
        return True

    @staticmethod
    def _message_source_owner(conn, message_id):
        # Tables may be absent for legacy consumers that initialize only matters.
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='message_history_messages'"
        ).fetchone():
            return None
        return conn.execute(
            "SELECT p.source_id,p.account_scope_id,p.record_enabled FROM message_history_messages m "
            "JOIN message_history_policies p ON p.conversation_key=m.conversation_key "
            "WHERE m.internal_message_id=?",
            (message_id,),
        ).fetchone()

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
                        raise PermissionError(
                            "Matter source ownership cannot be verified for this child run."
                        )
                    if (
                        source_ids is not None
                        and MailKnowledgeMirror.source_id_for_account(owner["account_id"])
                        not in sources
                    ):
                        raise PermissionError(
                            "Matter source link is outside the child source scope."
                        )
                    if account_ids is not None and owner["account_id"] not in accounts:
                        raise PermissionError(
                            "Matter source link is outside the child account scope."
                        )
                elif kind == "message_history_message":
                    owner = self._message_source_owner(conn, source_id)
                    if owner is None or not owner["record_enabled"]:
                        raise PermissionError("message_history_not_allowed")
                    if source_ids is not None and owner["source_id"] not in sources:
                        raise PermissionError(
                            "Matter source link is outside the child source scope."
                        )
                    if account_ids is not None and owner["account_scope_id"] not in accounts:
                        raise PermissionError(
                            "Matter source link is outside the child account scope."
                        )
                else:
                    raise PermissionError(
                        "Matter source type cannot be verified for this child scope."
                    )
        finally:
            if owns_connection:
                conn.close()

    def list_matters(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        q: str | None = None,
        kind: Literal["task", "plan"] | None = None,
        parent_matter_id: str | None = None,
        project_id: str | None = None,
        due_before: str | None = None,
        due_after: str | None = None,
        unscheduled: bool = False,
        scheduled_on: str | None = None,
        scheduled_before: str | None = None,
        scheduled_after: str | None = None,
        inbox: bool | None = None,
        section: str | None = None,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
        visibility: Literal["user", "internal", "all"] = "user",
        conn: sqlite3.Connection | None = None,
    ) -> MatterList:
        if visibility not in {"user", "internal", "all"}:
            raise ValueError("visibility must be user, internal or all")
        page_size = max(1, min(int(limit), 200))
        start = max(0, int(offset))
        conditions: list[str] = ["deleted_at IS NULL"]
        params: list[Any] = []
        if visibility != "all":
            conditions.append("visibility = ?")
            params.append(visibility)
        if status == "active":
            conditions.append("status IN ('open', 'in_progress', 'waiting')")
        elif status:
            conditions.append("status = ?")
            params.append(self._normalized_status(status))
        for column, value in (("kind", kind), ("parent_matter_id", parent_matter_id), ("project_id", project_id)):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(value)
        if q and q.strip():
            conditions.append("(instr(lower(title), lower(?)) > 0 OR instr(lower(summary), lower(?)) > 0)")
            params.extend((q.strip(), q.strip()))
        if unscheduled:
            conditions.append("due_at IS NULL")
        for column, value in (("inbox", inbox), ("section", section)):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(value)
        for bound, operator in ((scheduled_on, "="), (scheduled_before, "<"), (scheduled_after, ">=")):
            if bound is not None:
                validate_scheduled_on(bound)
                conditions.append(f"scheduled_on {operator} ?")
                params.append(bound)

        for bound, operator in ((due_before, "<"), (due_after, ">=")):
            if bound is not None:
                validate_matter_date(bound)
                # Calendar deadlines follow the caller's local day; timed deadlines
                # remain absolute instants, including when offsets differ.
                calendar_bound = datetime.fromisoformat(bound).date().isoformat()
                conditions.append(
                    f"((length(due_at) = 10 AND julianday(due_at) {operator} julianday(?)) "
                    f"OR (length(due_at) != 10 AND julianday(due_at) {operator} julianday(?)))"
                )
                params.extend((calendar_bound, bound))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        scoped = source_ids is not None or account_ids is not None
        owns = conn is None
        conn = conn if conn is not None else self._conn_factory()
        try:
            # Count and page share a snapshot; child authority is filtered before pagination.
            if owns:
                conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT matter_id, title, summary, status, priority, due_at, "
                "tags, metadata, created_at, updated_at, revision, kind, parent_matter_id, project_id, scheduled_on, scheduled_start, scheduled_end, inbox, section, sort_order, sections, visibility, deleted_at FROM matters" + where +
                " ORDER BY sort_order ASC, julianday(due_at) IS NULL, julianday(due_at) ASC, updated_at DESC, matter_id ASC" +
                ("" if scoped else " LIMIT ? OFFSET ?"),
                params if scoped else [*params, page_size, start],
            ).fetchall()
            if scoped:
                rows = [row for row in rows if self._matter_in_scope(
                    conn, matter_id=row["matter_id"], source_ids=source_ids,
                    account_ids=account_ids, allow_unattributed=allow_unattributed,
                )]
                total_count = len(rows)
                rows = rows[start:start + page_size]
            else:
                total_count = conn.execute("SELECT COUNT(*) FROM matters" + where, params).fetchone()[0]
            links_by_matter = self._load_source_links_for_rows(conn, rows)
            records = [self._record_from_row(row, source_links=links_by_matter.get(row["matter_id"], [])) for row in rows]
            for record in records:
                if record.kind == "plan":
                    record.progress = self._plan_progress(
                        conn, record.matter_id, source_ids=source_ids, account_ids=account_ids,
                        allow_unattributed=allow_unattributed,
                    )
            return MatterList(
                matters=records,
                total_count=total_count,
                next_offset=start + page_size if start + page_size < total_count else None,
            )
        finally:
            if owns:
                conn.close()

    def search_matters(
        self,
        *,
        query: str,
        limit: int = 10,
        kind: Literal["task", "plan"] | None = None,
        parent_matter_id: str | None = None,
        project_id: str | None = None,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
        visibility: Literal["user", "internal", "all"] = "user",
        conn: sqlite3.Connection | None = None,
    ) -> MatterSearchResult:
        if visibility not in {"user", "internal", "all"}:
            raise ValueError("visibility must be user, internal or all")
        normalized_query = query.strip()
        owns = conn is None
        conn = conn if conn is not None else self._conn_factory()
        try:
            if owns:
                conn.execute("BEGIN")
            filters: list[str] = ["deleted_at IS NULL"]
            filter_params: list[Any] = []
            if visibility != "all":
                filters.append("visibility = ?")
                filter_params.append(visibility)
            for column, value in (("kind", kind), ("parent_matter_id", parent_matter_id), ("project_id", project_id)):
                if value is not None:
                    filters.append(f"{column} = ?")
                    filter_params.append(value)
            filter_sql = " AND " + " AND ".join(filters) if filters else ""
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
                        m.revision, m.kind, m.parent_matter_id, m.project_id, m.scheduled_on, m.scheduled_start, m.scheduled_end, m.inbox, m.section, m.sort_order, m.sections, m.visibility,
                        snippet(matters_fts, 2, '[', ']', '...', 12) AS snippet
                    FROM matters_fts
                    JOIN matters m ON m.matter_id = matters_fts.matter_id
                    WHERE matters_fts MATCH ? {filter_sql}
                    ORDER BY rank
                    {limit_sql}
                    """,
                    [self._fts_query(normalized_query), *filter_params, limit]
                    if limit_sql
                    else [self._fts_query(normalized_query), *filter_params],
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT matter_id, title, summary, status, priority, due_at, tags, revision, kind, parent_matter_id, project_id, scheduled_on, scheduled_start, scheduled_end, inbox, section, sort_order, sections, visibility,
                           summary AS snippet
                    FROM matters
                    WHERE 1=1 {filter_sql}
                    ORDER BY
                        due_at IS NULL,
                        due_at ASC,
                        updated_at DESC
                    {limit_sql}
                    """,
                    [*filter_params, limit] if limit_sql else filter_params,
                ).fetchall()
            rows = [
                row
                for row in rows
                if self._matter_in_scope(
                    conn,
                    matter_id=row["matter_id"],
                    source_ids=source_ids,
                    account_ids=account_ids,
                    allow_unattributed=allow_unattributed,
                )
            ][:limit]
        finally:
            if owns:
                conn.close()

        return MatterSearchResult(
            query=query,
            matters=[
                MatterSearchResultItem(
                    visibility=row["visibility"],
                    scheduled_on=row["scheduled_on"], scheduled_start=row["scheduled_start"], scheduled_end=row["scheduled_end"],
                    inbox=bool(row["inbox"]), section=row["section"], sort_order=row["sort_order"], sections=self._json_list(row["sections"]),
                    kind=row["kind"], parent_matter_id=row["parent_matter_id"], project_id=row["project_id"],
                    matter_id=row["matter_id"],
                    title=row["title"],
                    summary=row["summary"],
                    status=row["status"],
                    priority=row["priority"],
                    due_at=row["due_at"],
                    tags=self._json_list(row["tags"]),
                    snippet=self._trim_snippet(row["snippet"]),
                    revision=row["revision"],
                )
                for row in rows
            ],
        )

    @notify_after_commit
    def update_matter(
        self,
        *,
        matter_id: str,
        payload: MatterUpdateInput,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
        conn: sqlite3.Connection | None = None,
        _receipt: Any = None,
    ) -> MatterRecord:
        with self._write_connection(conn) as transaction:
            self._check_message_receipt(
                transaction, action="update", matter_id=matter_id, payload=payload, receipt=_receipt
            )
            return self._update_matter_transaction(
                transaction,
                matter_id=matter_id,
                payload=payload,
                source_ids=source_ids,
                account_ids=account_ids,
                allow_unattributed=allow_unattributed,
            )

    def _update_matter_transaction(
        self, conn, *, matter_id, payload, source_ids, account_ids, allow_unattributed
    ):
        current = self.get_matter(matter_id=matter_id, conn=conn)
        title = payload.title.strip() if payload.title is not None else current.title
        summary = payload.summary.strip() if payload.summary is not None else current.summary
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
        due_at = payload.due_at if "due_at" in payload.model_fields_set else current.due_at
        tags = self._normalized_tags(payload.tags) if payload.tags is not None else current.tags
        metadata = payload.metadata if payload.metadata is not None else current.metadata
        now = _now_iso()
        if (source_ids is not None or account_ids is not None) and not self._matter_in_scope(
            conn,
            matter_id=matter_id,
            source_ids=source_ids,
            account_ids=account_ids,
            allow_unattributed=allow_unattributed,
        ):
            raise PermissionError("Matter is outside the child source/account scope.")
        if payload.expected_revision is not None and payload.expected_revision != current.revision:
            raise MatterConflictError(f"Matter revision conflict: {matter_id}")
        visibility = payload.visibility if "visibility" in payload.model_fields_set else current.visibility
        if current.visibility == "user" and visibility == "internal" and not self.internal_items_enabled():
            raise PermissionError("Internal matter creation is disabled")
        parent_matter_id = payload.parent_matter_id if "parent_matter_id" in payload.model_fields_set else current.parent_matter_id
        project_id = payload.project_id if "project_id" in payload.model_fields_set else current.project_id
        project_id = self._validate_plan_links(
            conn, kind=current.kind, parent_matter_id=parent_matter_id, project_id=project_id,
            project_explicit="project_id" in payload.model_fields_set,
            source_ids=source_ids, account_ids=account_ids, allow_unattributed=allow_unattributed,
        )
        if current.kind == "plan" and current.project_id != project_id and conn.execute(
            "SELECT 1 FROM matters WHERE parent_matter_id=? AND deleted_at IS NULL LIMIT 1", (matter_id,),
        ).fetchone():
            raise ValueError("Detach all tasks before changing the plan project")
        planning = {
            field: getattr(payload, field) if field in payload.model_fields_set else getattr(current, field)
            for field in ("scheduled_on", "scheduled_start", "scheduled_end", "inbox", "section", "sort_order", "sections")
        }
        validate_planning_state(
            kind=current.kind, scheduled_start=planning["scheduled_start"],
            scheduled_end=planning["scheduled_end"], sections=planning["sections"],
        )
        conn.execute(
            """
                UPDATE matters
                SET title = ?, summary = ?, status = ?, priority = ?, due_at = ?,
                    tags = ?, metadata = ?, updated_at = ?, parent_matter_id = ?, project_id = ?,
                    scheduled_on = ?, scheduled_start = ?, scheduled_end = ?, inbox = ?, section = ?, sort_order = ?, sections = ?, visibility = ?, revision = revision + 1
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
                parent_matter_id,
                project_id,
                planning["scheduled_on"],
                planning["scheduled_start"],
                planning["scheduled_end"],
                int(planning["inbox"]),
                planning["section"],
                planning["sort_order"],
                self._json_dumps(planning["sections"]),
                visibility,
                matter_id,
            ),
        )
        self._upsert_fts(conn, matter_id, title, summary, tags)
        return self.get_matter(matter_id=matter_id, conn=conn, source_ids=source_ids, account_ids=account_ids, allow_unattributed=allow_unattributed)

    def link_source(
        self,
        *,
        matter_id: str,
        source_link: MatterSourceLinkInput,
        source_ids: tuple[str, ...] | None = None,
        account_ids: tuple[str, ...] | None = None,
        allow_unattributed: bool = True,
        conn: sqlite3.Connection | None = None,
        _receipt: Any = None,
    ) -> MatterRecord:
        now = _now_iso()
        with self._write_connection(conn) as transaction:
            self.get_matter(matter_id=matter_id, conn=transaction)
            self._check_message_receipt(
                transaction,
                action="link",
                matter_id=matter_id,
                payload=source_link,
                receipt=_receipt,
                links=[source_link],
            )
            if source_ids is not None or account_ids is not None:
                self.validate_source_links_scope(
                    source_links=[source_link],
                    source_ids=source_ids,
                    account_ids=account_ids,
                    allow_unattributed=allow_unattributed,
                    conn=transaction,
                )
                if not self._matter_in_scope(
                    transaction,
                    matter_id=matter_id,
                    source_ids=source_ids,
                    account_ids=account_ids,
                    allow_unattributed=allow_unattributed,
                ):
                    raise PermissionError("Matter is outside the child source/account scope.")
            transaction.execute(
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
            transaction.execute(
                "UPDATE matters SET revision=revision+1, updated_at=? WHERE matter_id=?",
                (now, matter_id),
            )
            return self.get_matter(matter_id=matter_id, conn=transaction, source_ids=source_ids, account_ids=account_ids, allow_unattributed=allow_unattributed)

    @notify_after_commit
    def delete_matter(
        self, *, matter_id: str, expected_revision: int | None = None,
        conn: sqlite3.Connection | None = None, _receipt: Any = None,
    ) -> MatterRecord:
        """Soft-delete a matter; it leaves lists/search but stays restorable.

        Plans with remaining tasks are refused so tasks are never orphaned.
        """
        with self._write_connection(conn) as transaction:
            current = self.get_matter(matter_id=matter_id, conn=transaction)
            self._check_message_receipt(
                transaction, action="delete", matter_id=matter_id,
                payload={"expected_revision": expected_revision}, receipt=_receipt,
            )
            if expected_revision is not None and expected_revision != current.revision:
                raise MatterConflictError(f"Matter revision conflict: {matter_id}")
            if current.kind == "plan" and transaction.execute(
                "SELECT 1 FROM matters WHERE parent_matter_id=? AND deleted_at IS NULL LIMIT 1", (matter_id,),
            ).fetchone():
                raise MatterPlanNotEmptyError(f"Plan still has tasks: {matter_id}")
            now = _now_iso()
            transaction.execute(
                "UPDATE matters SET deleted_at=?, updated_at=?, revision=revision+1 WHERE matter_id=?",
                (now, now, matter_id),
            )
            transaction.execute("DELETE FROM matters_fts WHERE matter_id = ?", (matter_id,))
            return self.get_matter(matter_id=matter_id, conn=transaction, include_deleted=True)

    @notify_after_commit
    def restore_matter(
        self, *, matter_id: str, conn: sqlite3.Connection | None = None, _receipt: Any = None,
    ) -> MatterRecord:
        """Undo a soft delete. A task whose plan is gone comes back standalone."""
        with self._write_connection(conn) as transaction:
            current = self.get_matter(matter_id=matter_id, conn=transaction, include_deleted=True)
            if current.deleted_at is None:
                return current
            self._check_message_receipt(
                transaction, action="restore", matter_id=matter_id, payload={}, receipt=_receipt,
            )
            parent = current.parent_matter_id
            if parent and not transaction.execute(
                "SELECT 1 FROM matters WHERE matter_id=? AND deleted_at IS NULL", (parent,),
            ).fetchone():
                parent = None
            now = _now_iso()
            transaction.execute(
                "UPDATE matters SET deleted_at=NULL, parent_matter_id=?, updated_at=?, revision=revision+1 WHERE matter_id=?",
                (parent, now, matter_id),
            )
            self._upsert_fts(transaction, matter_id, current.title, current.summary, current.tags)
            return self.get_matter(matter_id=matter_id, conn=transaction)

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
            links_by_matter.setdefault(row["matter_id"], []).append(self._source_link_from_row(row))
        return links_by_matter

    def _record_from_row(
        self,
        row: sqlite3.Row,
        *,
        source_links: list[MatterSourceLink],
    ) -> MatterRecord:
        return MatterRecord(
            visibility=row["visibility"],
            scheduled_on=row["scheduled_on"], scheduled_start=row["scheduled_start"], scheduled_end=row["scheduled_end"],
            inbox=bool(row["inbox"]), section=row["section"], sort_order=row["sort_order"], sections=self._json_list(row["sections"]),
            kind=row["kind"], parent_matter_id=row["parent_matter_id"], project_id=row["project_id"],
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
            revision=row["revision"],
            deleted_at=row["deleted_at"] if "deleted_at" in row.keys() else None,  # noqa: SIM118 - sqlite3.Row membership tests values
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
