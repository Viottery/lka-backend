"""Mail tool package adapters around MailService."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.domains.mail import MailMatterDraft, MailService
from app.domains.mail_knowledge import MailKnowledgeMirror
from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec


MAIL_PACKAGE = ToolPackageSpec(
    name="mail",
    description="Search, load, and sync local mail knowledge. Matter persistence lives in the matter package.",
    risk="low_to_medium",
    requires_expansion=True,
    routing_hints=[
        "Use this package when the user needs evidence from local or synced email.",
        "Use this package when the user asks about mailbox contents, mail notifications, or mail-derived facts.",
    ],
    decision_hints=[
        "Use mail.search for locally indexed mail evidence; each result includes bounded relevant body text and provenance.",
        "Do not request complete message bodies in bulk. Use the returned evidence snippets unless exact source text is essential.",
        "Synchronize mail first only when the user asks to sync, asks for the latest mailbox state, or local mail may be stale.",
        "This package reads and syncs mail evidence; use another registered persistence package for tasks, events, or matters.",
    ],
    observation_cache={
        "tool_names": ["mail.load_messages"],
        "description": "Reuse loaded complete mail records from the same session when relevant.",
    },
)


class SearchMailTool:
    def __init__(self, mail_knowledge_mirror: MailKnowledgeMirror) -> None:
        self.mail_knowledge_mirror = mail_knowledge_mirror

    spec = ToolSpec(
        name="mail.search",
        package="mail",
        type="local_tool",
        description=(
            "Search locally indexed mail evidence through the knowledge retrieval layer. "
            "Returns mail cards with bounded relevant body excerpts and stable provenance; it never returns complete bodies in bulk. "
            "Use an empty query with order_by=source_time_desc to inspect the newest local mail."
        ),
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_db"],
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search text. Use an empty string to match all local mail; "
                        "combine with limit to fetch the latest messages."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum number of candidate messages to return.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["keyword", "semantic", "hybrid"],
                    "description": "Retrieval mode for content queries. source_time_desc uses local time ordering.",
                },
                "order_by": {
                    "type": "string",
                    "enum": ["relevance", "source_time_desc"],
                    "description": "Use source_time_desc with an empty query for newest-first mail.",
                },
                "max_snippet_chars": {
                    "type": "integer",
                    "minimum": 80,
                    "maximum": 1200,
                    "description": "Per-message evidence excerpt budget.",
                },
            },
        },
        output_schema={"query": "string", "messages": "array"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        result = self.mail_knowledge_mirror.search(
            query=str(invocation.input.get("query") or ""),
            limit=int(invocation.input.get("limit") or 10),
            mode=str(invocation.input.get("mode") or "") or None,
            order_by=str(invocation.input.get("order_by") or "relevance"),
            max_snippet_chars=int(invocation.input.get("max_snippet_chars") or 420),
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=result.model_dump(mode="json"),
        )


class LoadMailMessagesTool:
    def __init__(self, mail_service: MailService) -> None:
        self.mail_service = mail_service

    spec = ToolSpec(
        name="mail.load_messages",
        package="mail",
        type="local_tool",
        description=(
            "Load bounded source text for up to three already identified mail messages. "
            "Use only when mail.search evidence is insufficient for an exact question; do not use "
            "for bulk mail review."
        ),
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_db"],
        input_schema={
            "type": "object",
            "required": ["message_ids"],
            "properties": {
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 3,
                },
                "max_chars_per_message": {
                    "type": "integer",
                    "minimum": 80,
                    "maximum": 12000,
                },
            },
        },
        output_schema={"messages": "array"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        message_ids = [str(message_id) for message_id in invocation.input.get("message_ids", [])][:3]
        max_chars = int(invocation.input.get("max_chars_per_message") or 12000)
        messages = self.mail_service.load_messages(message_ids)
        output_messages = []
        for message in messages:
            payload = message.model_dump(mode="json")
            body_text = str(payload.get("body_text") or "")
            payload["body_text"] = body_text[:max_chars]
            payload["body_truncated"] = len(body_text) > max_chars
            output_messages.append(payload)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"messages": output_messages},
        )


class PersistMailMattersTool:
    def __init__(self, mail_service: MailService) -> None:
        self.mail_service = mail_service

    spec = ToolSpec(
        name="mail.persist_matters",
        package="mail",
        type="local_tool",
        description="Persist extracted mail matter drafts and link them to source messages.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["write_local_db"],
        input_schema={
            "type": "object",
            "required": ["drafts"],
            "properties": {
                "drafts": {"type": "array"},
                "provider": {"type": "string"},
                "link_reason": {"type": "string"},
            },
        },
        output_schema={"matters_created": "integer"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        drafts = [
            MailMatterDraft.model_validate(draft)
            for draft in invocation.input.get("drafts", [])
            if isinstance(draft, dict)
        ]
        matters_created = self.mail_service.persist_matter_drafts(
            drafts=drafts,
            provider=str(invocation.input.get("provider") or "tool_executor"),
            link_reason=str(invocation.input.get("link_reason") or "Mail tool invocation."),
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"matters_created": matters_created},
        )


class SyncMailTool:
    def __init__(self, sync_mail: Callable[..., Any]) -> None:
        self._sync_mail = sync_mail

    spec = ToolSpec(
        name="mail.sync",
        package="mail",
        type="local_tool",
        description=(
            "Synchronize the configured remote mail provider and import changed "
            "messages into the local mail store."
        ),
        risk="low_to_medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["read_remote_mail", "write_local_db"],
        input_schema={
            "type": "object",
            "properties": {
                "folder": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1},
                "max_pages": {"type": "integer", "minimum": 1},
            },
        },
        output_schema={
            "provider": "string",
            "folder": "string",
            "imported_messages": "integer",
            "imported_attachments": "integer",
            "status": "string",
            "sync_mode": "string",
            "next_link": "string|null",
            "delta_link": "string|null",
        },
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        folder_value = invocation.input.get("folder")
        result = self._sync_mail(
            folder=str(folder_value) if folder_value else None,
            limit=int(invocation.input.get("limit") or 25),
            max_pages=int(invocation.input.get("max_pages") or 1),
            trigger="tool",
        )
        output = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        output["provider"] = "outlook"
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status=str(output.get("status") or "completed"),
            output=output,
        )
