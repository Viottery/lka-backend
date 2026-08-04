"""Mail persistence, search, and matter extraction services."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

from pydantic import BaseModel, Field

from app.core.llm import LLMClientError, TextLLMClient


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


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


class MailSearchResult(BaseModel):
    query: str
    messages: list[MailSearchResultItem]


class MailMessageRecord(BaseModel):
    message_id: str
    subject: str
    sender: str
    folder: str
    recipients: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    received_at: str | None = None
    body_text: str
    attachments: list[MailAttachmentInput] = Field(default_factory=list)


class MailMatter(BaseModel):
    matter_id: str
    title: str
    summary: str
    status: str
    priority: str


class MailProcessResult(BaseModel):
    run_id: str
    status: str
    processed_messages: int
    matters_created: int


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
                    (self._fts_query(normalized_query), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT message_id, subject, sender, folder, received_at, body_text AS snippet
                    FROM mail_messages
                    ORDER BY received_at DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        finally:
            conn.close()

        return MailSearchResult(
            query=query,
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

    def process_messages(
        self,
        *,
        query: str | None = None,
        limit: int = 10,
        llm_client: TextLLMClient | None = None,
    ) -> MailProcessResult:
        messages = self._messages_for_processing(query=query, limit=limit)
        now = _now_iso()
        run_id = _stable_id("mail_run", query or "", now)
        provider = "local_heuristic"
        if llm_client is not None and messages:
            try:
                drafts = self._draft_matters_with_llm(
                    query=query,
                    messages=messages,
                    llm_client=llm_client,
                )
                provider = "llm"
            except LLMClientError:
                drafts = self._draft_matters_locally(messages)
                provider = "local_heuristic_after_llm_error"
        else:
            drafts = self._draft_matters_locally(messages)

        matters_created = 0
        conn = self._conn_factory()
        try:
            for index, draft in enumerate(drafts):
                source_message_ids = [
                    message_id
                    for message_id in draft.source_message_ids
                    if any(message.message_id == message_id for message in messages)
                ]
                if not source_message_ids and index < len(messages):
                    source_message_ids = [messages[index].message_id]
                matter_id = (
                    _stable_id("mail_matter", *source_message_ids)
                    if source_message_ids
                    else _stable_id("mail_matter", run_id, str(index))
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
                    (
                        matter_id,
                        title,
                        summary,
                        status,
                        priority,
                        now,
                        now,
                    ),
                )
                for message_id in source_message_ids:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO mail_matter_links(
                            matter_id, message_id, reason, created_at
                        )
                        VALUES(?, ?, ?, ?)
                        """,
                        (
                            matter_id,
                            message_id,
                            "LLM mail processing run." if provider == "llm" else "Local mail processing run.",
                            now,
                        ),
                    )
                matters_created += 1

            conn.execute(
                """
                INSERT INTO mail_processing_runs(
                    run_id, query, status, processed_messages,
                    matters_created, provider, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    query,
                    "completed",
                    len(messages),
                    matters_created,
                    provider,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return MailProcessResult(
            run_id=run_id,
            status="completed",
            processed_messages=len(messages),
            matters_created=matters_created,
        )

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

    def _messages_for_processing(
        self,
        *,
        query: str | None,
        limit: int,
    ) -> list[MailMessageRecord]:
        search_result = self.search_messages(query=query or "", limit=limit)
        return self._load_messages([message.message_id for message in search_result.messages])

    def _load_messages(self, message_ids: list[str]) -> list[MailMessageRecord]:
        if not message_ids:
            return []

        placeholders = ", ".join("?" for _ in message_ids)
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"""
                SELECT
                    message_id, subject, sender, folder, recipients, cc,
                    received_at, body_text
                FROM mail_messages
                WHERE message_id IN ({placeholders})
                """,
                message_ids,
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

    def _draft_matters_locally(self, messages: list[MailMessageRecord]) -> list[MailMatterDraft]:
        return [
            MailMatterDraft(
                title=message.subject or "Untitled mail matter",
                summary=self._trim_snippet(message.body_text) or message.subject,
                priority=self._priority_for(message.subject, message.body_text),
                source_message_ids=[message.message_id],
            )
            for message in messages
        ]

    def _draft_matters_with_llm(
        self,
        *,
        query: str | None,
        messages: list[MailMessageRecord],
        llm_client: TextLLMClient,
    ) -> list[MailMatterDraft]:
        response = llm_client.complete_text(
            system_prompt=(
                "You are the mail matter organizer for Local Knowledge Agent OS. "
                "Read the complete email bodies provided by the user. Extract actionable "
                "matters. Return only strict JSON with this shape: "
                '{"matters":[{"title":"...","summary":"...","status":"open",'
                '"priority":"low|normal|high","source_message_ids":["mail_msg_..."]}]}. '
                "Do not omit important deadlines, missing documents, or sender requests."
            ),
            user_prompt=self._mail_llm_prompt(query=query, messages=messages),
            prompt_summary=f"mail_process query={query or ''} messages={len(messages)}",
            temperature=0.0,
            max_output_tokens=None,
        )
        drafts = self._parse_llm_matter_drafts(response.content)
        if not drafts:
            raise LLMClientError("LLM response did not contain any mail matters.")
        return drafts

    def _mail_llm_prompt(self, *, query: str | None, messages: list[MailMessageRecord]) -> str:
        sections = [
            f"Query: {query or '(none)'}",
            "Use every complete email body below. Do not rely on snippets.",
        ]
        for index, message in enumerate(messages, start=1):
            attachments = [
                {
                    "external_id": attachment.external_id,
                    "name": attachment.name,
                    "content_type": attachment.content_type,
                    "size": attachment.size,
                }
                for attachment in message.attachments
            ]
            sections.append(
                "\n".join(
                    [
                        f"EMAIL {index}",
                        f"message_id: {message.message_id}",
                        f"folder: {message.folder}",
                        f"subject: {message.subject}",
                        f"sender: {message.sender}",
                        f"to: {', '.join(message.recipients)}",
                        f"cc: {', '.join(message.cc)}",
                        f"received_at: {message.received_at or ''}",
                        f"attachments: {json.dumps(attachments, ensure_ascii=False)}",
                        "body_text:",
                        message.body_text,
                        "END_EMAIL",
                    ]
                )
            )
        return "\n\n".join(sections)

    def _parse_llm_matter_drafts(self, content: str) -> list[MailMatterDraft]:
        payload = self._parse_json_object(content)
        matters = payload.get("matters") if isinstance(payload, dict) else None
        if not isinstance(matters, list):
            return []

        drafts: list[MailMatterDraft] = []
        for item in matters:
            if not isinstance(item, dict):
                continue
            try:
                drafts.append(MailMatterDraft.model_validate(item))
            except ValueError:
                continue
        return drafts

    def _parse_json_object(self, content: str) -> Any:
        clean_content = content.strip()
        if clean_content.startswith("```"):
            clean_content = clean_content.strip("`").strip()
            if clean_content.startswith("json"):
                clean_content = clean_content[4:].strip()
        try:
            return json.loads(clean_content)
        except json.JSONDecodeError:
            start = clean_content.find("{")
            end = clean_content.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return {}
            try:
                return json.loads(clean_content[start : end + 1])
            except json.JSONDecodeError:
                return {}

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
