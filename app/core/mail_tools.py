"""Mail tool package adapters around MailService."""

from __future__ import annotations

from app.core.mail import MailMatterDraft, MailService
from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec


MAIL_PACKAGE = ToolPackageSpec(
    name="mail",
    description="Search, load, and persist local mail knowledge and mail matters.",
    risk="low_to_medium",
    requires_expansion=True,
)


class SearchMailTool:
    def __init__(self, mail_service: MailService) -> None:
        self.mail_service = mail_service

    spec = ToolSpec(
        name="mail.search",
        package="mail",
        type="local_tool",
        description="Search local persisted mail with SQLite FTS and return candidate message ids.",
        risk="low",
        requires_confirmation=False,
        side_effects=["read_local_db"],
        input_schema={"query": "string", "limit": "integer"},
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
        input_schema={"message_ids": "array"},
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
        input_schema={"drafts": "array", "provider": "string", "link_reason": "string"},
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
            provider=str(invocation.input.get("provider") or "agent_loop"),
            link_reason=str(invocation.input.get("link_reason") or "Agent mail processing run."),
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"matters_created": matters_created},
        )
