"""Mail tool package adapters around MailService."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from typing import Any

from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec
from app.domains.mail import MailMatterDraft, MailService
from app.domains.mail_knowledge import MailKnowledgeMirror

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
        "Use mail.list for chronological metadata-only mailbox review over a date interval and bounded rank pages.",
        "For exhaustive date-interval listing, follow next_range with the same listing_id until has_more=false or budget; if stopping early, report partial coverage. A single 20-card page is not complete enumeration.",
        "Use mail.search for content discovery; its empty-query time order is a quick peek, not exhaustive enumeration. The 100-result cap and possible_more signal do not provide an exact total.",
        "mail.sync next_link/delta_link track remote provider sync progress, not mail.list pagination; use listing_id for local pages.",
        "Do not request complete message bodies in bulk. Use the returned evidence snippets unless exact source text is essential.",
        "After search identifies a small set of relevant messages, load those messages together with mail.load_messages instead of loading them one at a time or searching the same query again. Reuse loaded records from this session when they still answer the question.",
        "Treat search excerpts and truncated loaded text as partial evidence; preserve which messages and text ranges were actually read. Do not describe an excerpt or truncated result as the full message. Keep the source's date precision (calendar date versus timestamp) and surface conflicting message dates or displayed timestamps for reconciliation rather than silently choosing one.",
        "When automatic mail synchronization is enabled, normally read local mail without calling mail.sync first, including routine requests for recent mail. Call mail.sync only for an explicit sync/immediate-refresh request, or when automatic synchronization is unavailable and local mail may be stale.",
        "This package reads and syncs mail evidence; use another registered persistence package for tasks, events, or matters.",
    ],
    capability_hints=[
        {"required_tools": ["mail.list"],
         "routing_hints": ["Read a chronological interval of authorized local mail."],
         "decision_hints": ["mail.list returns metadata cards; follow next_range with the same listing_id until has_more=false or report partial coverage. Read current source evidence instead of substituting old conversation summaries."]},
        {"required_tools": ["mail.search"],
         "routing_hints": ["Search currently authorized mail evidence."],
         "decision_hints": ["mail.search snippets are selected evidence; possible_more is not an exact total. An empty-query quick peek is not complete enumeration. Once a few relevant IDs are identified, batch-load those records and reuse the loaded result; state the actual read coverage and any truncation."]},
        {"required_tools": ["mail.load_messages"],
         "decision_hints": ["Load a small set of already identified relevant messages together when needed to verify requirements, dates and responsibility; do not bulk-read all bodies or repeat an equivalent search. Preserve exact read coverage and truncation. Keep date-only evidence as a calendar date, not an invented end-of-day timestamp, and report conflicts among source dates/timestamps."]},
    ],
    observation_cache={
        "tool_names": ["mail.load_messages"],
        "description": "Reuse loaded complete mail records from the same session when relevant.",
    },
)


def build_mail_package(*, automatic_sync_enabled: bool, sync_interval_seconds: int) -> ToolPackageSpec:
    package = MAIL_PACKAGE.model_copy(deep=True)
    if automatic_sync_enabled:
        package.decision_hints.append(
            f"Automatic mail synchronization is enabled; the backend polls every {sync_interval_seconds} seconds. "
            "This is the configured schedule, not proof that the latest sync succeeded or that local mail is fully current."
        )
    else:
        package.decision_hints.append("Automatic mail synchronization is disabled in the current backend configuration.")
    return package


class ListMailTool:
    def __init__(self, mail_service: MailService, mail_knowledge_mirror: MailKnowledgeMirror) -> None:
        self.mail_service = mail_service
        self.mail_knowledge_mirror = mail_knowledge_mirror

    spec = ToolSpec(
        name="mail.list", package="mail", type="local_tool",
        description=(
            "List mailbox metadata cards in received_at descending then message_id descending order. "
            "Requires a timezone-qualified ISO [received_from, received_before) interval, optionally a folder, "
            "and returns at most 20 1-based inclusive ranks. Cards never include body text or snippets. "
            "Subjects/senders/folders are capped at 180/120/80 characters with truncation flags; stored originals are unchanged. "
            "The first page returns an opaque listing_id; pass it on later pages to keep ranks stable. "
            "If eligible mail changes, the ID is rejected as stale and listing must restart. The HMAC token is process-local and becomes invalid after backend restart. The ID is not authorization."
        ),
        risk="low", requires_confirmation=False, read_only=True,
        side_effects=["read_local_db"], scope_uses_sources=True, scope_uses_accounts=True,
        scope_filtering_required=True,
        input_schema={"type": "object", "required": ["received_from", "received_before"], "properties": {
            "received_from": {"type": "string", "format": "date-time"},
            "received_before": {"type": "string", "format": "date-time"},
            "folder": {"type": "string"},
            "start_rank": {"type": "integer", "minimum": 1},
            "end_rank": {"type": "integer", "minimum": 1},
            "listing_id": {"type": "string", "description": "Opaque snapshot token returned by the first page; pass unchanged for later pages."},
        }},
        output_schema={"total_matches": "integer", "requested_range": "object", "returned_count": "integer", "has_more": "boolean", "next_range": "object|null", "applied_limit": "integer", "coverage": "object", "listing_id": "string", "messages": "array"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        scope = context.tool_view
        session_id = context.session_id
        if scope is not None and scope.child_run_id is not None and (
            not scope.allowed_source_ids or not scope.allowed_account_ids
        ):
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="Child mail listing requires explicit source and account grants.")
        accounts = set(self.mail_knowledge_mirror.active_account_ids(session_id=session_id))
        if scope is not None and scope.child_run_id is not None:
            accounts.intersection_update(scope.allowed_account_ids)
            granted_sources = set(scope.allowed_source_ids)
            accounts = {
                account for account in accounts
                if MailKnowledgeMirror.source_id_for_account(account) in granted_sources
            }
        try:
            result = self.mail_service.list_messages(
                received_from=str(invocation.input["received_from"]),
                received_before=str(invocation.input["received_before"]),
                start_rank=int(invocation.input.get("start_rank", 1)),
                end_rank=int(invocation.input["end_rank"]) if invocation.input.get("end_rank") is not None else None,
                folder=str(invocation.input["folder"]) if invocation.input.get("folder") is not None else None,
                account_ids=sorted(accounts),
                listing_id=str(invocation.input["listing_id"]) if invocation.input.get("listing_id") is not None else None,
            )
        except ValueError as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error=str(exc))
        output = result.model_dump(mode="json")
        # Opaque authorization inventory permits later revocation checks without
        # exposing account IDs or invalidating history when new mail arrives.
        output["_authorization_source_tokens"] = [self._account_token(account) for account in accounts]
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                          status="completed", output=output)

    @staticmethod
    def _account_token(account_id):
        return sha256(("mail-authorization-v1:" + account_id).encode()).hexdigest()

    def allow_cached_observation(self, *, tool_input, result, context):
        """Revalidate the complete listing snapshot against current mail grants."""
        try:
            output = result.get("output") if isinstance(result, dict) else None
            if not isinstance(tool_input, dict) or not isinstance(output, dict):
                return False
            tokens = output.get("_authorization_source_tokens")
            if isinstance(tokens, list):
                if (any(not isinstance(token, str) or len(token) != 64 for token in tokens)
                        or len(tokens) != len(set(tokens))):
                    return False
            else:
                tokens = None  # Legacy rows lack the invocation-time account inventory.
            accounts = sorted(self.mail_knowledge_mirror.active_account_ids(session_id=context.session_id))
            scope = context.tool_view
            if scope is not None:
                if (scope.child_run_id is not None and not scope.full_data_authority
                        and not scope.allowed_account_ids and not scope.allowed_source_ids):
                    return False
                if not scope.full_data_authority and scope.allowed_account_ids:
                    accounts = [account for account in accounts if account in scope.allowed_account_ids]
                if not scope.full_data_authority and scope.allowed_source_ids:
                    accounts = [account for account in accounts
                                if MailKnowledgeMirror.source_id_for_account(account) in scope.allowed_source_ids]
            if tokens is not None:
                active_tokens = {self._account_token(account) for account in accounts}
                if not set(tokens).issubset(active_tokens):
                    return False
            messages = output.get("messages")
            if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
                return False
            message_ids = [item.get("message_id") for item in messages]
            if any(not isinstance(message_id, str) for message_id in message_ids):
                return False
            owners = self.mail_service.get_message_account_ids(message_ids, account_ids=accounts)
            if set(owners) != set(message_ids):
                return False
            source_ids = set(self.mail_knowledge_mirror.active_source_ids(session_id=context.session_id))
            owners_valid = all(
                MailKnowledgeMirror.source_id_for_account(owners[item["message_id"]]) in source_ids
                and (tokens is None or self._account_token(owners[item["message_id"]]) in set(tokens))
                for item in messages
            )
            if not owners_valid:
                return False
            if tokens is not None:
                return True
            # Legacy limitation: no invocation-time account inventory was saved.
            # Permit only if current authorized data produces the same complete
            # visible page; process-local listing_id is deliberately excluded.
            current = self.mail_service.list_messages(
                received_from=tool_input.get("received_from"),
                received_before=tool_input.get("received_before"),
                start_rank=tool_input.get("start_rank", 1), end_rank=tool_input.get("end_rank"),
                folder=tool_input.get("folder"), account_ids=accounts,
            ).model_dump(mode="json")
            visible = ("total_matches", "requested_range", "returned_count", "has_more", "next_range",
                       "applied_limit", "coverage", "messages")
            return all(current.get(key) == output.get(key) for key in visible)
        except (KeyError, TypeError, ValueError):
            return False


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
            "Reports requested_limit, applied_limit (maximum 100), returned_count, and possible_more; possible_more means the retrieval page filled its cap and does not report an exact total. "
            "An empty query with order_by=source_time_desc is only a quick peek, not exhaustive enumeration. Use mail.list for complete bounded chronological metadata pages."
        ),
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_db"],
        scope_uses_sources=True,
        scope_uses_accounts=True,
        scope_filtering_required=True,
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
        scope = context.tool_view
        if scope is not None and scope.child_run_id is not None and (
            not scope.allowed_source_ids or not scope.allowed_account_ids
        ):
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="rejected",
                error="Child mail search requires explicit source and account grants.",
            )
        result = self.mail_knowledge_mirror.search(
            query=str(invocation.input.get("query") or ""),
            limit=int(invocation.input.get("limit") or 10),
            mode=str(invocation.input.get("mode") or "") or None,
            order_by=str(invocation.input.get("order_by") or "relevance"),
            max_snippet_chars=int(invocation.input.get("max_snippet_chars") or 420),
            source_ids=list(scope.allowed_source_ids) if scope and scope.allowed_source_ids else None,
            account_ids=list(scope.allowed_account_ids) if scope and scope.allowed_account_ids else None,
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=result.model_dump(mode="json"),
        )


class LoadMailMessagesTool:
    def __init__(self, mail_service: MailService, mail_knowledge_mirror: MailKnowledgeMirror | None = None) -> None:
        self.mail_service = mail_service
        self.mail_knowledge_mirror = mail_knowledge_mirror

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
        scope_uses_sources=True,
        scope_uses_accounts=True,
        scope_filtering_required=True,
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
        scope = context.tool_view
        if scope is not None and scope.child_run_id is not None and (
            not scope.allowed_source_ids or not scope.allowed_account_ids
        ):
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="rejected",
                error="Child mail loading requires explicit source and account grants.",
            )
        message_ids = [str(message_id) for message_id in invocation.input.get("message_ids", [])][:3]
        max_chars = int(invocation.input.get("max_chars_per_message") or 12000)
        messages = self.mail_service.load_messages(
            message_ids,
            account_ids=list(scope.allowed_account_ids) if scope and scope.allowed_account_ids else None,
        )
        if scope is not None and scope.child_run_id is not None:
            permitted_sources = set(scope.allowed_source_ids)
            messages = [
                message for message in messages
                if MailKnowledgeMirror.source_id_for_account(message.account_id) in permitted_sources
            ]
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

    def allow_cached_observation(self, *, tool_input, result, context):
        """Ensure every historical message remains inside active account sources."""
        try:
            if self.mail_knowledge_mirror is None or not isinstance(tool_input, dict) or not isinstance(result, dict):
                return False
            requested = tool_input.get("message_ids")
            output = result.get("output")
            if (not isinstance(requested, list) or not requested or len(requested) > 3
                    or any(not isinstance(item, str) or not item for item in requested)
                    or len(set(requested)) != len(requested) or not isinstance(output, dict)
                    or not isinstance(output.get("messages"), list)):
                return False
            accounts = set(self.mail_knowledge_mirror.active_account_ids(session_id=context.session_id))
            scope = context.tool_view
            if scope is not None:
                if (scope.child_run_id is not None and not scope.full_data_authority
                        and not scope.allowed_account_ids and not scope.allowed_source_ids):
                    return False
                if not scope.full_data_authority and scope.allowed_account_ids:
                    accounts.intersection_update(scope.allowed_account_ids)
                if not scope.full_data_authority and scope.allowed_source_ids:
                    accounts = {account for account in accounts
                                if MailKnowledgeMirror.source_id_for_account(account) in scope.allowed_source_ids}
            authorized_by_id = self.mail_service.get_message_account_ids(requested, account_ids=sorted(accounts))
            active_sources = set(self.mail_knowledge_mirror.active_source_ids(session_id=context.session_id))
            delivered = [item for item in output["messages"] if isinstance(item, dict)]
            if len(delivered) != len(output["messages"]):
                return False
            delivered_ids = [item.get("message_id") for item in delivered]
            if len(delivered_ids) != len(set(delivered_ids)) or set(delivered_ids) != set(authorized_by_id):
                return False
            for item in delivered:
                account_id = authorized_by_id.get(item["message_id"])
                if (account_id is None or MailKnowledgeMirror.source_id_for_account(account_id)
                        not in active_sources):
                    return False
            return True
        except (KeyError, TypeError, ValueError):
            return False


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
        scope_uses_accounts=True,
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
            "messages into the local mail store. next_link and delta_link indicate remote sync progress; they are not mail.list page tokens."
        ),
        risk="low_to_medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["read_remote_mail", "write_local_db"],
        scope_uses_accounts=True,
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
