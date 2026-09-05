"""Projection of persisted mail records into source-agnostic local knowledge."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.domains.knowledge import (
    KnowledgeDocumentInput,
    KnowledgeImportResult,
    KnowledgeService,
    KnowledgeSourceInput,
)
from app.domains.mail import MailMirrorRecord, MailSearchResult, MailSearchResultItem, MailService


class MailKnowledgeMirrorResult(BaseModel):
    scope: Literal["messages", "account", "all"]
    scanned_messages: int
    mirrored_messages: int
    imported_chunks: int
    document_ids: list[str] = Field(default_factory=list)


class MailKnowledgeMirror:
    """Maps mail evidence into the generic knowledge store without agent coupling."""

    def __init__(self, *, mail_service: MailService, knowledge_service: KnowledgeService) -> None:
        self._mail_service = mail_service
        self._knowledge_service = knowledge_service

    def sync(
        self,
        *,
        account_id: str | None = None,
        external_ids: list[str] | None = None,
    ) -> MailKnowledgeMirrorResult:
        records = self._mail_service.list_messages_for_mirror(
            account_id=account_id,
            external_ids=external_ids,
        )
        imported = [self._mirror_record(record) for record in records]
        scope: Literal["messages", "account", "all"]
        if external_ids is not None:
            scope = "messages"
        elif account_id:
            scope = "account"
        else:
            scope = "all"
        return MailKnowledgeMirrorResult(
            scope=scope,
            scanned_messages=len(records),
            mirrored_messages=len(imported),
            imported_chunks=sum(item.imported_chunks for item in imported),
            document_ids=[item.document_id for item in imported],
        )

    def search(
        self,
        *,
        query: str,
        limit: int = 10,
        mode: str | None = None,
        order_by: Literal["relevance", "source_time_desc"] = "relevance",
        max_snippet_chars: int = 420,
    ) -> MailSearchResult:
        """Search mail evidence through the local knowledge index.

        Mail remains the task-facing vocabulary, while retrieval, privacy filtering,
        chunking, and provenance remain owned by the source-agnostic knowledge layer.
        """

        resolved_mode = "keyword" if order_by == "source_time_desc" else mode
        result = self._knowledge_service.search(
            query=query,
            limit=limit,
            source_types=["mail_message"],
            max_snippet_chars=max_snippet_chars,
            tool_name="mail.search",
            mode=resolved_mode,
            sort_by=order_by,
            distinct_documents=True,
        )
        message_ids = [self._message_id_from_source_ref(item.source_ref) for item in result.results]
        summaries = self._mail_service.get_message_summaries(
            [message_id for message_id in message_ids if message_id]
        )
        messages: list[MailSearchResultItem] = []
        for item in result.results:
            message_id = self._message_id_from_source_ref(item.source_ref)
            if message_id is None:
                continue
            summary = summaries.get(message_id)
            if summary is None:
                continue
            messages.append(
                summary.model_copy(
                    update={
                        "snippet": item.snippet,
                        "document_id": item.document_id,
                        "chunk_id": item.chunk_id,
                        "source_ref": item.source_ref,
                        "excerpt_truncated": len(item.snippet) >= max_snippet_chars,
                    }
                )
            )
        return MailSearchResult(query=query, messages=messages)

    def _mirror_record(self, message: MailMirrorRecord) -> KnowledgeImportResult:
        source_uri = f"mail://{message.account_id}"
        document_uri = f"{source_uri}/messages/{message.message_id}"
        source_ref = f"mail_message:{message.message_id}"
        return self._knowledge_service.import_text_document(
            KnowledgeDocumentInput(
                source=KnowledgeSourceInput(
                    source_type="mail_message",
                    display_name="Local mail",
                    uri=source_uri,
                    metadata={"account_id": message.account_id},
                    sensitivity="personal",
                    remote_policy="redact",
                ),
                title=message.subject or "Untitled mail",
                uri=document_uri,
                text=self._document_text(message),
                mime_type="text/plain",
                source_ref=source_ref,
                metadata={
                    "mail_message_id": message.message_id,
                    "mail_account_id": message.account_id,
                    "mail_external_id": message.external_id,
                    "folder": message.folder,
                    "sender": message.sender,
                    "recipients": message.recipients,
                    "cc": message.cc,
                    "received_at": message.received_at,
                    "source_time": message.received_at,
                    "attachments": [item.model_dump(mode="json") for item in message.attachments],
                    "projection": "mail_knowledge_mirror_v1",
                },
            )
        )

    def _document_text(self, message: MailMirrorRecord) -> str:
        headers = [
            f"Subject: {message.subject}",
            f"From: {message.sender}",
            f"Folder: {message.folder}",
        ]
        if message.received_at:
            headers.append(f"Received-At: {message.received_at}")
        if message.recipients:
            headers.append(f"To: {', '.join(message.recipients)}")
        if message.cc:
            headers.append(f"Cc: {', '.join(message.cc)}")
        if message.attachments:
            headers.append(
                "Attachments: " + ", ".join(attachment.name for attachment in message.attachments)
            )
        return "\n".join(headers) + "\n\n" + message.body_text

    @staticmethod
    def _message_id_from_source_ref(source_ref: str) -> str | None:
        prefix = "mail_message:"
        if not source_ref.startswith(prefix):
            return None
        return source_ref.removeprefix(prefix).split("#", 1)[0] or None
