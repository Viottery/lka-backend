"""Mail tool package adapters around MailService."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.domains.mail import MailMatterDraft, MailService
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
        "Search candidate messages before loading full message records.",
        "Load full messages for relevant message ids before writing the final answer.",
        "Synchronize mail first only when the user asks to sync, asks for the latest mailbox state, or local mail may be stale.",
        "This package reads and syncs mail evidence; use another registered persistence package for tasks, events, or matters.",
    ],
    observation_cache={
        "tool_names": ["mail.load_messages"],
        "description": "Reuse loaded complete mail records from the same session when relevant.",
    },
)


class SearchMailTool:
    def __init__(self, mail_service: MailService) -> None:
        self.mail_service = mail_service

    spec = ToolSpec(
        name="mail.search",
        package="mail",
        type="local_tool",
        description=(
            "Search local persisted mail with SQLite FTS and return candidate message ids. "
            "Use an empty query string to match all local mail, ordered by newest first."
        ),
        risk="low",
        requires_confirmation=False,
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
            },
        },
        output_schema={"query": "string", "messages": "array"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        result = self.mail_service.search_messages(
            query=str(invocation.input.get("query") or ""),
            limit=int(invocation.input.get("limit") or 10),
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
        description="Load complete local mail records, including full body text and attachment metadata.",
        risk="low",
        requires_confirmation=False,
        side_effects=["read_local_db"],
        input_schema={
            "type": "object",
            "required": ["message_ids"],
            "properties": {
                "message_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
        output_schema={"messages": "array"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        message_ids = [str(message_id) for message_id in invocation.input.get("message_ids", [])]
        messages = self.mail_service.load_messages(message_ids)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"messages": [message.model_dump(mode="json") for message in messages]},
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
