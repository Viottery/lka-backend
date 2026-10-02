"""Mail persistence, search, and matter extraction services."""

from __future__ import annotations

import base64
import hmac
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha1, sha256

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _parse_mail_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("mail date boundaries must be ISO timestamps with a timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("mail date boundaries must include a timezone")
    return parsed.astimezone(UTC)


class MailAccountInput(BaseModel):
    provider: str = "local_json"
    email_address: str
    display_name: str | None = None


class MailAttachmentInput(BaseModel):
    external_id: str
    name: str
    content_type: str | None = None
    size: int | None = None


class MailMessageInput(BaseModel):
    external_id: str
    folder: str = "Inbox"
    subject: str = ""
    sender: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    received_at: str | None = None
    body_text: str = ""
    attachments: list[MailAttachmentInput] = Field(default_factory=list)


class MailImportResult(BaseModel):
    account_id: str
    imported_messages: int
    imported_attachments: int


class MailSearchResultItem(BaseModel):
    message_id: str
    subject: str
    sender: str
    folder: str
    received_at: str | None = None
    snippet: str
    document_id: str | None = None
    chunk_id: str | None = None
    source_ref: str | None = None
    excerpt_truncated: bool = False


class MailSearchResult(BaseModel):
    query: str
    messages: list[MailSearchResultItem]
    requested_limit: int = 10
    applied_limit: int = 10
    returned_count: int = 0
    possible_more: bool = False


class MailListCard(BaseModel):
    message_id: str
    subject: str
    sender: str
    folder: str
    received_at: str | None = None
    subject_truncated: bool = False
    sender_truncated: bool = False
    folder_truncated: bool = False


class MailListResult(BaseModel):
    total_matches: int
    requested_range: dict[str, int]
    returned_count: int
    has_more: bool
    next_range: dict[str, int] | None = None
    applied_limit: int
    coverage: dict[str, int]
    listing_id: str
    messages: list[MailListCard]


_MAIL_LIST_TOKEN_SECRET = os.urandom(32)


def _listing_token(payload: dict[str, object]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(_MAIL_LIST_TOKEN_SECRET, body, sha256).digest()
    return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")


def _decode_listing_token(token: str) -> dict[str, object]:
    try:
        if len(token) > 2048:
            raise ValueError
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if len(raw) <= 32:
            raise ValueError
        body, signature = raw[:-32], raw[-32:]
        expected = hmac.new(_MAIL_LIST_TOKEN_SECRET, body, sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise TypeError("listing token payload must be an object")
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, base64.binascii.Error) as exc:
        raise ValueError("Invalid listing_id; start a new listing.") from exc


class MailMessageRecord(BaseModel):
    message_id: str
    account_id: str = Field(exclude=True)
    subject: str
    sender: str
    folder: str
    recipients: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    received_at: str | None = None
    body_text: str
    attachments: list[MailAttachmentInput] = Field(default_factory=list)


class MailMirrorRecord(MailMessageRecord):
    """A persisted message with the stable identifiers needed by a mirror."""

    account_id: str
    external_id: str


class MailMatter(BaseModel):
    matter_id: str
    title: str
    summary: str
    status: str
    priority: str


class MailMatterList(BaseModel):
    matters: list[MailMatter]


class MailMatterDraft(BaseModel):
    title: str
    summary: str
    status: str = "open"
    priority: str = "normal"
    source_message_ids: list[str] = Field(default_factory=list)


class MailService:
    """Service for the first local-mail MVP slice."""

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]) -> None:
        self._conn_factory = conn_factory

    def list_authorized_account_ids(self) -> tuple[str, ...]:
        """Return the currently configured local mail-account inventory."""
        conn = self._conn_factory()
        try:
            rows = conn.execute("SELECT account_id FROM mail_accounts ORDER BY account_id").fetchall()
        finally:
            conn.close()
        return tuple(str(row["account_id"]) for row in rows)

    def list_messages(
        self, *, received_from: str, received_before: str, start_rank: int = 1,
        end_rank: int | None = None, folder: str | None = None,
        account_ids: list[str] | None = None, listing_id: str | None = None,
    ) -> MailListResult:
        """List a bounded rank page of metadata cards in a half-open date interval."""
        start = _parse_mail_timestamp(received_from)
        end = _parse_mail_timestamp(received_before)
        if end <= start:
            raise ValueError("received_before must be later than received_from")
        if end_rank is None and start_rank == 1:
            last = 20
        else:
            last = start_rank + 19 if end_rank is None else end_rank
        if start_rank < 1 or last < start_rank or last - start_rank + 1 > 20:
            raise ValueError("rank range must be 1-based, inclusive, and at most 20 messages")
        # SQLite date functions normalize offsets but have millisecond precision;
        # leave a small margin and apply exact Python bounds to the reduced rows.
        filters = [
            "received_at IS NOT NULL",
            "julianday(received_at) >= julianday(?) - 0.00002",
            "julianday(received_at) < julianday(?) + 0.00002",
        ]
        params: list[object] = [start.isoformat(), end.isoformat()]
        if folder is not None:
            filters.append("lower(folder) = lower(?)")
            params.append(folder)
        if account_ids is not None and account_ids:
            filters.append("account_id IN (" + ",".join("?" for _ in account_ids) + ")")
            params.extend(account_ids)
        where = " AND ".join(filters)
        if account_ids is not None and not account_ids:
            rows = []
        else:
            conn = self._conn_factory()
            try:
                rows = conn.execute(
                    f"SELECT message_id, subject, sender, folder, received_at FROM mail_messages WHERE {where}",
                    params,
                ).fetchall()
            finally:
                conn.close()
        eligible = []
        for row in rows:
            try:
                received = _parse_mail_timestamp(str(row["received_at"]))
            except ValueError:
                continue
            if start <= received < end:
                eligible.append((received, row))
        eligible.sort(key=lambda item: (item[0], str(item[1]["message_id"])), reverse=True)
        normalized_filters = {
            "from": start.isoformat(), "before": end.isoformat(),
            "folder": folder.casefold() if folder is not None else None,
            "accounts": sorted(account_ids) if account_ids is not None else None,
        }
        fingerprint = sha256(json.dumps({
            "filters": normalized_filters,
            "ordered": [(str(row["message_id"]), received.isoformat()) for received, row in eligible],
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        token_payload = {"filters": normalized_filters, "fingerprint": fingerprint}
        if listing_id is not None:
            existing = _decode_listing_token(listing_id)
            if existing != token_payload:
                raise ValueError("Stale listing: mailbox contents or listing filters changed. Start a new listing.")
            effective_listing_id = listing_id
        else:
            effective_listing_id = _listing_token(token_payload)
        total = len(eligible)
        selected = eligible[start_rank - 1:last]
        cards = [
            MailListCard(
                message_id=str(row["message_id"]),
                subject=str(row["subject"] or "")[:180],
                sender=str(row["sender"] or "")[:120],
                folder=str(row["folder"] or "")[:80],
                received_at=row["received_at"],
                subject_truncated=len(str(row["subject"] or "")) > 180,
                sender_truncated=len(str(row["sender"] or "")) > 120,
                folder_truncated=len(str(row["folder"] or "")) > 80,
            )
            for _, row in selected
        ]
        returned = len(cards)
        next_start = start_rank + returned
        has_more = next_start <= total
        return MailListResult(
            total_matches=total, requested_range={"start_rank": start_rank, "end_rank": last},
            returned_count=returned, has_more=has_more,
            next_range={"start_rank": next_start, "end_rank": min(next_start + 19, total)} if has_more else None,
            applied_limit=20,
            coverage={"start_rank": start_rank if returned else 0, "end_rank": start_rank + returned - 1 if returned else 0},
            listing_id=effective_listing_id,
            messages=cards,
        )

    def import_messages(
        self,
        *,
        account: MailAccountInput,
        messages: list[MailMessageInput],
    ) -> MailImportResult:
        account_id = _stable_id("mail_account", account.provider, account.email_address.lower())
        now = _now_iso()
        imported_attachments = 0
        conn = self._conn_factory()
        try:
            conn.execute(
                """
                INSERT INTO mail_accounts(
                    account_id, provider, email_address, display_name, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, email_address) DO UPDATE SET
                    display_name=excluded.display_name,
                    updated_at=excluded.updated_at
                """,
                (
                    account_id,
                    account.provider,
                    account.email_address.lower(),
                    account.display_name,
                    now,
                    now,
                ),
            )

            for message in messages:
                message_id = _stable_id("mail_msg", account_id, message.external_id)
                recipients_json = json.dumps(message.to, ensure_ascii=False, sort_keys=True)
                cc_json = json.dumps(message.cc, ensure_ascii=False, sort_keys=True)
                conn.execute(
                    """
                    INSERT INTO mail_messages(
                        message_id, account_id, external_id, folder, subject, sender,
                        recipients, cc, received_at, body_text, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, external_id) DO UPDATE SET
                        folder=excluded.folder,
                        subject=excluded.subject,
                        sender=excluded.sender,
                        recipients=excluded.recipients,
                        cc=excluded.cc,
                        received_at=excluded.received_at,
                        body_text=excluded.body_text,
                        updated_at=excluded.updated_at
                    """,
                    (
                        message_id,
                        account_id,
                        message.external_id,
                        message.folder,
                        message.subject,
                        message.sender,
                        recipients_json,
                        cc_json,
                        message.received_at,
                        message.body_text,
                        now,
                        now,
                    ),
                )
                conn.execute("DELETE FROM mail_messages_fts WHERE message_id = ?", (message_id,))
                conn.execute(
                    """
                    INSERT INTO mail_messages_fts(message_id, subject, sender, folder, body_text)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        message.subject,
                        message.sender,
                        message.folder,
                        message.body_text,
                    ),
                )
                conn.execute("DELETE FROM mail_chunks WHERE message_id = ?", (message_id,))
                for chunk_index, chunk_text in enumerate(self._chunk_text(message.body_text)):
                    conn.execute(
                        """
                        INSERT INTO mail_chunks(
                            chunk_id, message_id, chunk_index, text, created_at
                        )
                        VALUES(?, ?, ?, ?, ?)
                        """,
                        (
                            _stable_id("mail_chunk", message_id, str(chunk_index)),
                            message_id,
                            chunk_index,
                            chunk_text,
                            now,
                        ),
                    )

                for attachment in message.attachments:
                    attachment_id = _stable_id(
                        "mail_att",
                        message_id,
                        attachment.external_id,
                    )
                    conn.execute(
                        """
                        INSERT INTO mail_attachments(
                            attachment_id, message_id, external_id, name,
                            content_type, size, created_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(message_id, external_id) DO UPDATE SET
                            name=excluded.name,
                            content_type=excluded.content_type,
                            size=excluded.size
                        """,
                        (
                            attachment_id,
                            message_id,
                            attachment.external_id,
                            attachment.name,
                            attachment.content_type,
                            attachment.size,
                            now,
                        ),
                    )
                    imported_attachments += 1

            conn.commit()
        finally:
            conn.close()

        return MailImportResult(
            account_id=account_id,
            imported_messages=len(messages),
            imported_attachments=imported_attachments,
        )

    def search_messages(self, *, query: str, limit: int = 10) -> MailSearchResult:
        normalized_query = query.strip()
        requested_limit = max(1, limit)
        applied_limit = min(requested_limit, 100)
        conn = self._conn_factory()
        try:
            if normalized_query:
                rows = conn.execute(
                    """
                    SELECT
                        m.message_id,
                        m.subject,
                        m.sender,
                        m.folder,
                        m.received_at,
                        snippet(mail_messages_fts, 4, '[', ']', '...', 12) AS snippet
                    FROM mail_messages_fts
                    JOIN mail_messages m ON m.message_id = mail_messages_fts.message_id
                    WHERE mail_messages_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (self._fts_query(normalized_query), applied_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT message_id, subject, sender, folder, received_at, body_text AS snippet
                    FROM mail_messages
                    ORDER BY received_at DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (applied_limit,),
                ).fetchall()
        finally:
            conn.close()

        return MailSearchResult(
            query=query,
            requested_limit=requested_limit,
            applied_limit=applied_limit,
            returned_count=len(rows),
            possible_more=len(rows) >= applied_limit,
            messages=[
                MailSearchResultItem(
                    message_id=row["message_id"],
                    subject=row["subject"],
                    sender=row["sender"],
                    folder=row["folder"],
                    received_at=row["received_at"],
                    snippet=self._trim_snippet(row["snippet"]),
                )
                for row in rows
            ],
        )

    def get_message_summaries(self, message_ids: list[str]) -> dict[str, MailSearchResultItem]:
        """Return mail card metadata without loading message bodies."""

        if not message_ids:
            return {}
        placeholders = ", ".join("?" for _ in message_ids)
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"""
                SELECT message_id, subject, sender, folder, received_at
                FROM mail_messages
                WHERE message_id IN ({placeholders})
                """,
                message_ids,
            ).fetchall()
        finally:
            conn.close()
        return {
            str(row["message_id"]): MailSearchResultItem(
                message_id=str(row["message_id"]),
                subject=str(row["subject"] or ""),
                sender=str(row["sender"] or ""),
                folder=str(row["folder"] or ""),
                received_at=str(row["received_at"] or ""),
                snippet="",
            )
            for row in rows
        }

    def list_matters(self, *, limit: int = 50) -> MailMatterList:
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                """
                SELECT matter_id, title, summary, status, priority
                FROM mail_matters
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()

        return MailMatterList(
            matters=[
                MailMatter(
                    matter_id=row["matter_id"],
                    title=row["title"],
                    summary=row["summary"],
                    status=row["status"],
                    priority=row["priority"],
                )
                for row in rows
            ]
        )

    def _fts_query(self, query: str) -> str:
        terms = [term.strip().replace('"', "") for term in query.split() if term.strip()]
        if not terms:
            return '""'
        return " OR ".join(f'"{term}"' for term in terms)

    def load_messages(
        self,
        message_ids: list[str],
        *,
        account_ids: list[str] | None = None,
    ) -> list[MailMessageRecord]:
        if not message_ids:
            return []

        placeholders = ", ".join("?" for _ in message_ids)
        account_clause = ""
        params: list[str] = list(message_ids)
        if account_ids:
            account_clause = f" AND account_id IN ({', '.join('?' for _ in account_ids)})"
            params.extend(account_ids)
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"""
                SELECT
                    message_id, account_id, subject, sender, folder, recipients, cc,
                    received_at, body_text
                FROM mail_messages
                WHERE message_id IN ({placeholders}) {account_clause}
                """,
                params,
            ).fetchall()
            attachment_rows = conn.execute(
                f"""
                SELECT message_id, external_id, name, content_type, size
                FROM mail_attachments
                WHERE message_id IN ({placeholders})
                ORDER BY created_at ASC
                """,
                message_ids,
            ).fetchall()
        finally:
            conn.close()

        attachments_by_message: dict[str, list[MailAttachmentInput]] = {}
        for row in attachment_rows:
            attachments_by_message.setdefault(row["message_id"], []).append(
                MailAttachmentInput(
                    external_id=row["external_id"],
                    name=row["name"],
                    content_type=row["content_type"],
                    size=row["size"],
                )
            )

        records_by_id = {
            row["message_id"]: MailMessageRecord(
                message_id=row["message_id"],
                account_id=row["account_id"],
                subject=row["subject"],
                sender=row["sender"],
                folder=row["folder"],
                recipients=self._json_list(row["recipients"]),
                cc=self._json_list(row["cc"]),
                received_at=row["received_at"],
                body_text=row["body_text"],
                attachments=attachments_by_message.get(row["message_id"], []),
            )
            for row in rows
        }
        return [records_by_id[message_id] for message_id in message_ids if message_id in records_by_id]

    def list_messages_for_mirror(
        self,
        *,
        account_id: str | None = None,
        external_ids: list[str] | None = None,
    ) -> list[MailMirrorRecord]:
        """Return persisted messages selected for projection into local knowledge."""

        clauses: list[str] = []
        params: list[str] = []
        if account_id:
            clauses.append("m.account_id = ?")
            params.append(account_id)
        if external_ids is not None:
            if not external_ids:
                return []
            placeholders = ", ".join("?" for _ in external_ids)
            clauses.append(f"m.external_id IN ({placeholders})")
            params.extend(external_ids)
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"""
                SELECT m.message_id, m.account_id, m.external_id, m.subject, m.sender, m.folder,
                    m.recipients, m.cc, m.received_at, m.body_text
                FROM mail_messages m
                {where_clause}
                ORDER BY m.received_at DESC, m.updated_at DESC
                """,
                params,
            ).fetchall()
            message_ids = [str(row["message_id"]) for row in rows]
            attachments_by_message = self._attachments_by_message(conn, message_ids)
        finally:
            conn.close()
        return [
            MailMirrorRecord(
                message_id=row["message_id"],
                account_id=row["account_id"],
                external_id=row["external_id"],
                subject=row["subject"],
                sender=row["sender"],
                folder=row["folder"],
                recipients=self._json_list(row["recipients"]),
                cc=self._json_list(row["cc"]),
                received_at=row["received_at"],
                body_text=row["body_text"],
                attachments=attachments_by_message.get(row["message_id"], []),
            )
            for row in rows
        ]

    def _attachments_by_message(
        self,
        conn: sqlite3.Connection,
        message_ids: list[str],
    ) -> dict[str, list[MailAttachmentInput]]:
        if not message_ids:
            return {}
        placeholders = ", ".join("?" for _ in message_ids)
        attachment_rows = conn.execute(
            f"""
            SELECT message_id, external_id, name, content_type, size
            FROM mail_attachments
            WHERE message_id IN ({placeholders})
            ORDER BY created_at ASC
            """,
            message_ids,
        ).fetchall()
        attachments_by_message: dict[str, list[MailAttachmentInput]] = {}
        for row in attachment_rows:
            attachments_by_message.setdefault(row["message_id"], []).append(
                MailAttachmentInput(
                    external_id=row["external_id"],
                    name=row["name"],
                    content_type=row["content_type"],
                    size=row["size"],
                )
            )
        return attachments_by_message

    def draft_matters_locally(self, messages: list[MailMessageRecord]) -> list[MailMatterDraft]:
        return [
            MailMatterDraft(
                title=message.subject or "Untitled mail matter",
                summary=self._trim_snippet(message.body_text) or message.subject,
                priority=self._priority_for(message.subject, message.body_text),
                source_message_ids=[message.message_id],
            )
            for message in messages
        ]

    def persist_matter_drafts(
        self,
        *,
        drafts: list[MailMatterDraft],
        provider: str,
        link_reason: str,
    ) -> int:
        now = _now_iso()
        matters_created = 0
        conn = self._conn_factory()
        try:
            for index, draft in enumerate(drafts):
                source_message_ids = draft.source_message_ids
                matter_id = (
                    _stable_id("mail_matter", *source_message_ids)
                    if source_message_ids
                    else _stable_id("mail_matter", provider, str(index), now)
                )
                title = draft.title.strip() or "Untitled mail matter"
                summary = draft.summary.strip() or title
                status = draft.status.strip() or "open"
                priority = self._normalized_priority(draft.priority, title, summary)
                conn.execute(
                    """
                    INSERT INTO mail_matters(
                        matter_id, title, summary, status, priority, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(matter_id) DO UPDATE SET
                        title=excluded.title,
                        summary=excluded.summary,
                        priority=excluded.priority,
                        updated_at=excluded.updated_at
                    """,
                    (matter_id, title, summary, status, priority, now, now),
                )
                for message_id in source_message_ids:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO mail_matter_links(
                            matter_id, message_id, reason, created_at
                        )
                        VALUES(?, ?, ?, ?)
                        """,
                        (matter_id, message_id, link_reason, now),
                    )
                matters_created += 1
            conn.commit()
        finally:
            conn.close()
        return matters_created

    def _json_list(self, value: str) -> list[str]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [str(item) for item in payload]

    def _trim_snippet(self, snippet: str | None) -> str:
        if not snippet:
            return ""
        return snippet if len(snippet) <= 280 else f"{snippet[:277]}..."

    def _chunk_text(self, text: str, chunk_size: int = 1200) -> list[str]:
        clean_text = text.strip()
        if not clean_text:
            return [""]
        return [
            clean_text[index : index + chunk_size]
            for index in range(0, len(clean_text), chunk_size)
        ]

    def _priority_for(self, title: str, summary: str) -> str:
        text = f"{title} {summary}".lower()
        urgent_terms = ["urgent", "asap", "deadline", "overdue", "紧急", "截止", "逾期"]
        return "high" if any(term in text for term in urgent_terms) else "normal"

    def _normalized_priority(self, priority: str, title: str, summary: str) -> str:
        normalized = priority.strip().lower()
        if normalized in {"low", "normal", "high"}:
            return normalized
        return self._priority_for(title, summary)
