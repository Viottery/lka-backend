"""Bounded read-only mail tools intended for specialist agents."""

from __future__ import annotations

from typing import Any

from app.core.tools import ToolContext, ToolInvocation, ToolResult, ToolSpec
from app.domains.mail import MailService
from app.domains.mail_knowledge import MailKnowledgeMirror


def _missing_child_grants(context: ToolContext) -> bool:
    scope = context.tool_view
    return bool(
        scope is not None
        and scope.child_run_id is not None
        and (not scope.allowed_source_ids or not scope.allowed_account_ids)
    )


def _listing_accounts(mail_service: MailService, mirror: MailKnowledgeMirror, context: ToolContext) -> list[str]:
    scope = context.tool_view
    if scope is not None and scope.child_run_id is not None:
        # The child has an isolated session ID; parent session-scoped sources
        # are intentionally not copied. Its immutable ToolView is the grant.
        accounts = set(mail_service.list_authorized_account_ids())
        accounts.intersection_update(scope.allowed_account_ids)
        sources = set(scope.allowed_source_ids)
        accounts = {
            account for account in accounts
            if MailKnowledgeMirror.source_id_for_account(account) in sources
        }
    else:
        accounts = set(mirror.active_account_ids(session_id=context.session_id))
    return sorted(accounts)


class MailSnapshotTool:
    """Enumerate chronological mail metadata with an explicit coverage boundary."""

    def __init__(self, mail_service: MailService, mail_knowledge_mirror: MailKnowledgeMirror) -> None:
        self.mail_service = mail_service
        self.mail_knowledge_mirror = mail_knowledge_mirror

    spec = ToolSpec(
        name="mail.snapshot", package="mail", type="local_tool",
        description=(
            "Enumerate chronological metadata cards for a half-open timezone-qualified date interval. "
            "Reads in stable pages of 20 and returns at most 500 messages per invocation (default 300). "
            "Pass next_range.start_rank and the same listing_id to continue; complete means the final rank "
            "was reached, not that earlier ranks were read by this invocation. Cards never include body text."
        ),
        risk="low", requires_confirmation=False, read_only=True,
        side_effects=["read_local_db"], scope_uses_sources=True, scope_uses_accounts=True,
        scope_filtering_required=True,
        input_schema={"type": "object", "required": ["received_from", "received_before"], "properties": {
            "received_from": {"type": "string", "format": "date-time"},
            "received_before": {"type": "string", "format": "date-time"},
            "folder": {"type": "string"},
            "start_rank": {"type": "integer", "minimum": 1, "default": 1},
            "listing_id": {"type": "string"},
            "max_messages": {"type": "integer", "minimum": 1, "maximum": 500, "default": 300},
        }},
        output_schema={"total_matches": "integer", "messages": "array", "returned_count": "integer",
                       "complete": "boolean", "next_range": "object|null", "coverage": "object",
                       "listing_id": "string"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        if _missing_child_grants(context):
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="Child mail listing requires explicit source and account grants.")
        max_messages = int(invocation.input.get("max_messages", 300))
        if not 1 <= max_messages <= 500:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="max_messages must be between 1 and 500.")
        start = int(invocation.input.get("start_rank", 1))
        if start < 1:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="start_rank must be positive.")
        first_rank = start
        listing_id: str | None = str(invocation.input["listing_id"]) if invocation.input.get("listing_id") else None
        messages: list[dict[str, Any]] = []
        first: dict[str, Any] | None = None
        try:
            while len(messages) < max_messages:
                end = min(start + 19, first_rank + max_messages - 1)
                page = self.mail_service.list_messages(
                    received_from=str(invocation.input["received_from"]),
                    received_before=str(invocation.input["received_before"]),
                    start_rank=start, end_rank=end,
                    folder=str(invocation.input["folder"]) if invocation.input.get("folder") is not None else None,
                    account_ids=_listing_accounts(self.mail_service, self.mail_knowledge_mirror, context),
                    listing_id=listing_id,
                ).model_dump(mode="json")
                if first is None:
                    first = page
                    listing_id = str(page["listing_id"])
                elif page["listing_id"] != listing_id or page["total_matches"] != first["total_matches"]:
                    raise ValueError("Mail snapshot changed between pages; restart the snapshot.")
                messages.extend(page["messages"])
                if not page["has_more"]:
                    break
                start += page["returned_count"]
                if page["returned_count"] == 0:
                    raise ValueError("Mail snapshot page made no progress.")
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error=str(exc))
        assert first is not None
        returned = len(messages)
        total = int(first["total_matches"])
        covered_end = first_rank + returned - 1
        complete = covered_end >= total
        next_range = None if complete else {"start_rank": covered_end + 1, "end_rank": min(covered_end + 20, total)}
        coverage = {"start_rank": first_rank if returned else 0, "end_rank": covered_end if returned else 0}
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="completed",
                          output={"total_matches": total, "messages": messages, "returned_count": returned,
                                  "complete": complete, "next_range": next_range, "coverage": coverage,
                                  "listing_id": str(listing_id)})


class MailBatchLoadTool:
    """Load a bounded batch of identified mail records."""

    def __init__(self, mail_service: MailService) -> None:
        self.mail_service = mail_service

    spec = ToolSpec(
        name="mail.batch_load", package="mail", type="local_tool",
        description=(
            "Load source text and metadata for up to 100 identified messages. Each body is limited to "
            "1500 characters by default; truncated bodies end with an explicit marker. Child calls require "
            "both source and account grants and only return records within both grants."
        ),
        risk="low", requires_confirmation=False, read_only=True,
        side_effects=["read_local_db"], scope_uses_sources=True, scope_uses_accounts=True,
        scope_filtering_required=True,
        input_schema={"type": "object", "required": ["message_ids"], "properties": {
            "message_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
            "max_chars_per_message": {"type": "integer", "minimum": 1, "maximum": 1500, "default": 1500},
        }},
        output_schema={"messages": "array", "missing_ids": "array", "returned_count": "integer"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        if _missing_child_grants(context):
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="Child mail loading requires explicit source and account grants.")
        message_ids = list(dict.fromkeys(str(item) for item in invocation.input.get("message_ids", [])))
        max_chars = int(invocation.input.get("max_chars_per_message", 1500))
        if not 1 <= len(message_ids) <= 100:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="message_ids must contain between 1 and 100 IDs.")
        if not 1 <= max_chars <= 1500:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="max_chars_per_message must be between 1 and 1500.")
        scope = context.tool_view
        account_ids = list(scope.allowed_account_ids) if scope is not None and scope.child_run_id is not None else None
        records = self.mail_service.load_messages(message_ids, account_ids=account_ids)
        if scope is not None and scope.child_run_id is not None:
            sources = set(scope.allowed_source_ids)
            records = [r for r in records if MailKnowledgeMirror.source_id_for_account(r.account_id) in sources]
        by_id = {record.message_id: record for record in records}
        output_messages: list[dict[str, Any]] = []
        for message_id in message_ids:
            record = by_id.get(message_id)
            if record is None:
                continue
            payload = record.model_dump(mode="json")
            body = str(payload.get("body_text") or "")
            payload["body_truncated"] = len(body) > max_chars
            payload["body_text"] = body[:max_chars] + ("\n...[truncated]" if len(body) > max_chars else "")
            output_messages.append(payload)
        missing = [message_id for message_id in message_ids if message_id not in by_id]
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="completed",
                          output={"messages": output_messages, "missing_ids": missing,
                                  "returned_count": len(output_messages)})
