"""Matter tool package adapters around MatterService."""

from __future__ import annotations

from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec
from app.domains.matters import (
    MatterCreateInput,
    MatterService,
    MatterSourceLinkInput,
    MatterUpdateInput,
)


def _child_scope(
    context: ToolContext,
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None, bool]:
    view = context.tool_view
    if view is None or getattr(view, "child_run_id", None) is None:
        return None, None, True
    return (
        tuple(getattr(view, "allowed_source_ids", ())),
        tuple(getattr(view, "allowed_account_ids", ())),
        bool(getattr(view, "full_data_authority", False)),
    )


_MATTER_CHILD_SCOPE = {
    "scope_uses_sources": True,
    "scope_uses_accounts": True,
    "scope_filtering_required": True,
}


MATTER_PACKAGE = ToolPackageSpec(
    name="matter",
    description="Manage local todo tasks and independent project plans, including deadlines, progress, optional directory associations and evidence links.",
    risk="low_to_medium",
    requires_expansion=True,
    routing_hints=[
        "Use this package when the user asks to manage todo lists, split goals into project plans and tasks, or inspect, create, update, or link local tasks, events, reminders, or matters.",
    ],
    capability_hints=[
        {"required_tools": ["matter.search"], "routing_hints": ["Find existing local tasks, appointments and matters."],
         "decision_hints": ["Search existing Matters before creating a potential duplicate. Search previews are not complete source evidence."]},
        {"required_tools": ["management.create_matter", "management.update_matter"],
         "routing_hints": ["Capture or adjust a single fully specified Matter through the supervisor writers."],
         "decision_hints": ["Use management.create_matter/update_matter for a simple single record; dispatch source investigation and bulk analysis to an execution session. Preserve expected_revision and publication checks. For unknown duration keep the exact start in summary with scheduled_on; never invent scheduled_end."]},
    ],
    decision_hints=[
        "Use the writer names actually exposed by this session. Dedicated equivalents may replace create/update; a missing standard writer does not prove all writing is forbidden. Inspect the catalog before claiming a permission failure.",
        "Before create or create_many, call matter.search with the candidate title or key subject unless recent observations already prove no duplicate risk.",
        "If matter.search returns a likely duplicate, update or link the existing matter instead of creating a new one.",
        "Choose only writer names exposed by the current session; tool descriptions specify whether they create, update, batch or attach evidence.",
        "Use kind=plan for an independent plan, then create kind=task records with its parent_matter_id. A plan may optionally reference an existing directory project_id; tasks inherit that project.",
        "Get the current record before editing and pass its expected_revision to avoid overwriting concurrent edits. Completing a plan does not complete its tasks.",
        "Scheduling changes planned work, never the deadline: use scheduled_on for a day and scheduled_start/scheduled_end for one time block; due_at remains the deadline. Clear both time endpoints together when removing a block.",
        "Use inbox=true for unorganized captured tasks and inbox=false after organizing. A task without dates is not automatically an inbox task. Plans may store ordered sections even when they have no tasks.",
        "Message-derived matters require their dedicated human review workflow; ordinary tool approval cannot authorize their mutation.",
        "Follow every tool input_schema exactly, especially required fields and allowed_values.",
    ],
)

MATTER_STATUS_VALUES = ["open", "in_progress", "waiting", "done", "cancelled"]
MATTER_PRIORITY_VALUES = ["low", "normal", "high", "urgent"]
MATTER_SOURCE_LINK_SCHEMA = {
    "type": "object",
    "required": ["source_type", "source_id", "reason"],
    "properties": {
        "source_type": {
            "type": "string",
            "description": "Evidence source type, for example mail_message.",
        },
        "source_id": {
            "type": "string",
            "description": "Stable id of the evidence source.",
        },
        "reason": {
            "type": "string",
            "description": "Why this source supports the matter.",
        },
    },
}
MATTER_SCHEDULING_FIELDS = {
    "scheduled_on": {"type": ["string", "null"], "description": "Planned execution day, YYYY-MM-DD. Separate from due_at. Null clears this day."},
    "scheduled_start": {"type": ["string", "null"], "description": "Single time block start, ISO datetime with timezone. Supply with scheduled_end; clear both with null."},
    "scheduled_end": {"type": ["string", "null"], "description": "Time block end, ISO datetime with timezone, strictly after scheduled_start."},
    "inbox": {"type": "boolean", "default": False, "description": "True means captured for later organization; independent of dates and status."},
    "section": {"type": "string", "default": "", "maxLength": 120, "description": "Stage label for grouping tasks, or empty string to clear."},
    "sort_order": {"type": "number", "default": 0, "description": "Finite manual ordering position; smaller values appear first."},
    "sections": {"type": "array", "items": {"type": "string"}, "description": "Plan stage names in display order (unique, 1..120 characters, at most 100). Empty stages are retained. Only plans may have a nonempty list."},
}
MATTER_PLAN_FIELDS = {
    "parent_matter_id": {"type": ["string", "null"], "description": "Plan id for a task; null detaches it. Plans cannot have parents."},
    "project_id": {"type": ["string", "null"], "description": "Existing directory project id, optional. A child task inherits its plan project and explicit mismatches are rejected."},
}
MATTER_FILTER_FIELDS = {
    "visibility": {"type": "string", "allowed_values": ["user", "internal", "all"], "default": "user",
                   "description": "Presentation scope; source/account restrictions still apply."},
    "kind": {"type": ["string", "null"], "allowed_values": ["task", "plan"]},
    **MATTER_PLAN_FIELDS,
}
MATTER_VISIBILITY_FIELD = {"type": "string", "allowed_values": ["user", "internal"],
                           "description": "Default user. Internal records require the server feature flag; this grants no source authority."}
MATTER_CREATE_SCHEMA = {
    "type": "object",
    "required": ["title", "summary"],
    "properties": {
        "kind": {"type": "string", "allowed_values": ["task", "plan"], "default": "task"},
        "visibility": MATTER_VISIBILITY_FIELD,
        **MATTER_PLAN_FIELDS,
        **MATTER_SCHEDULING_FIELDS,
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "status": {
            "type": "string",
            "allowed_values": MATTER_STATUS_VALUES,
            "default": "open",
        },
        "priority": {
            "type": "string",
            "allowed_values": MATTER_PRIORITY_VALUES,
            "default": "normal",
        },
        "due_at": {
            "type": ["string", "null"],
            "description": "ISO calendar date or ISO 8601 datetime with an explicit timezone.",
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "source_links": {
            "type": "array",
            "items": MATTER_SOURCE_LINK_SCHEMA,
            "description": "Evidence links such as loaded mail message ids.",
        },
        "metadata": {"type": "object"},
    },
    "examples": [
        {
            "title": "Prepare application materials",
            "summary": "Collect required documents before the submission deadline.",
            "status": "open",
            "priority": "normal",
            "due_at": "2026-08-14T13:00:00+08:00",
            "tags": ["application"],
            "source_links": [
                {
                    "source_type": "local_evidence",
                    "source_id": "evidence_001",
                    "reason": "Extracted from loaded local evidence.",
                }
            ],
            "metadata": {"created_from": "agent_turn"},
        }
    ],
}


class CreateMatterTool:
    def __init__(self, matter_service: MatterService) -> None:
        self.matter_service = matter_service

    spec = ToolSpec(
        name="matter.create",
        package="matter",
        type="local_tool",
        description="Create a local task or independent plan with optional deadline, plan/directory association and evidence links.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        effect_domains=("matter",),
        side_effects=["write_local_db"],
        **_MATTER_CHILD_SCOPE,
        input_schema=MATTER_CREATE_SCHEMA,
        output_schema={"matter": "object"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        source_ids, account_ids, full_authority = _child_scope(context)
        if (
            source_ids is not None
            and not invocation.input.get("source_links")
            and not full_authority
        ):
            raise PermissionError("Unlinked matters are outside the child source/account scope.")
        matter = self.matter_service.create_matter(
            MatterCreateInput.model_validate(invocation.input),
            source_ids=source_ids, account_ids=account_ids,
            allow_unattributed=full_authority,
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"matter": matter.model_dump(mode="json")},
        )


class CreateManyMattersTool:
    def __init__(self, matter_service: MatterService) -> None:
        self.matter_service = matter_service

    spec = ToolSpec(
        name="matter.create_many",
        effect_domains=("matter",),
        package="matter",
        type="local_tool",
        description="Create multiple independent local matters in one controlled batch.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["write_local_db"],
        **_MATTER_CHILD_SCOPE,
        input_schema={
            "type": "object",
            "required": ["matters"],
            "properties": {
                "matters": {
                    "type": "array",
                    "items": MATTER_CREATE_SCHEMA,
                    "description": (
                        "Batch of independent matters extracted from explicit evidence."
                    ),
                }
            },
            "examples": [{"matters": MATTER_CREATE_SCHEMA["examples"]}],
        },
        output_schema={"matters_created": "integer", "matters": "array"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        source_ids, account_ids, full_authority = _child_scope(context)
        payloads = [
            MatterCreateInput.model_validate(payload)
            for payload in invocation.input.get("matters", []) if isinstance(payload, dict)
        ]
        if source_ids is not None:
            if not full_authority and any(not payload.source_links for payload in payloads):
                raise PermissionError("Unlinked matters are outside the child source/account scope.")
            for payload in payloads:
                self.matter_service.validate_source_links_scope(
                    source_links=payload.source_links,
                    source_ids=source_ids,
                    account_ids=account_ids, allow_unattributed=full_authority,
                )
        created = [
            self.matter_service.create_matter(
                payload,
                source_ids=source_ids,
                account_ids=account_ids,
                allow_unattributed=full_authority,
            ) for payload in payloads
        ]
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={
                "matters_created": len(created),
                "matters": [matter.model_dump(mode="json") for matter in created],
            },
        )


class SearchMattersTool:
    def __init__(self, matter_service: MatterService) -> None:
        self.matter_service = matter_service

    spec = ToolSpec(
        name="matter.search",
        package="matter",
        type="local_tool",
        description="Search independent local matters with SQLite FTS.",
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_db"],
        **_MATTER_CHILD_SCOPE,
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                **MATTER_FILTER_FIELDS,
                "limit": {"type": "integer", "default": 10, "minimum": 1},
            },
        },
        output_schema={"query": "string", "matters": "array"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        source_ids, account_ids, full_authority = _child_scope(context)
        result = self.matter_service.search_matters(
            query=str(invocation.input.get("query") or ""),
            visibility=invocation.input.get("visibility", "user"),
            kind=invocation.input.get("kind"),
            parent_matter_id=invocation.input.get("parent_matter_id"),
            project_id=invocation.input.get("project_id"),
            limit=int(invocation.input.get("limit") or 10),
            source_ids=source_ids, account_ids=account_ids,
            allow_unattributed=full_authority,
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=result.model_dump(mode="json"),
        )


class GetMatterTool:
    def __init__(self, matter_service: MatterService) -> None:
        self.matter_service = matter_service

    spec = ToolSpec(
        name="matter.get",
        package="matter",
        type="local_tool",
        description="Read a complete task or plan by id, including its current revision and plan progress.",
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_db"],
        **_MATTER_CHILD_SCOPE,
        input_schema={"type": "object", "required": ["matter_id"], "properties": {"matter_id": {"type": "string"}}},
        output_schema={"matter": "object"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        source_ids, account_ids, full_authority = _child_scope(context)
        record = self.matter_service.get_matter(
            matter_id=str(invocation.input["matter_id"]), source_ids=source_ids,
            account_ids=account_ids, allow_unattributed=full_authority,
        )
        return ToolResult(
            invocation_id=invocation.invocation_id, tool_name=self.spec.name,
            status="completed", output={"matter": record.model_dump(mode="json")},
        )


class ListMattersTool:
    def __init__(self, matter_service: MatterService) -> None:
        self.matter_service = matter_service

    spec = ToolSpec(
        name="matter.list",
        package="matter",
        type="local_tool",
        description="List local tasks and plans by manual order, deadline, and recent updates. Filter planned execution dates separately from deadlines.",
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_db"],
        **_MATTER_CHILD_SCOPE,
        input_schema={
            "type": "object",
            "properties": {
                **MATTER_FILTER_FIELDS,
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "q": {"type": ["string", "null"], "description": "Literal title or summary substring."},
                "scheduled_on": {"type": ["string", "null"], "description": "Exact planned calendar day, YYYY-MM-DD."},
                "scheduled_before": {"type": ["string", "null"], "description": "Exclusive planned day upper bound, YYYY-MM-DD."},
                "scheduled_after": {"type": ["string", "null"], "description": "Inclusive planned day lower bound, YYYY-MM-DD."},
                "inbox": {"type": ["boolean", "null"], "description": "Filter explicit inbox classification."},
                "section": {"type": ["string", "null"], "description": "Exact task stage label; empty string selects ungrouped."},
                "unscheduled": {"type": "boolean", "default": False, "description": "Only records without a deadline."},
                "due_before": {"type": ["string", "null"], "description": "Exclusive ISO date or timezone datetime deadline upper bound."},
                "due_after": {"type": ["string", "null"], "description": "Inclusive ISO date or timezone datetime deadline lower bound."},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                "status": {
                    "type": ["string", "null"],
                    "allowed_values": [*MATTER_STATUS_VALUES, "active"],
                },
            },
        },
        output_schema={"matters": "array", "next_offset": "integer|null", "total_count": "integer"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        source_ids, account_ids, full_authority = _child_scope(context)
        status_value = invocation.input.get("status")
        result = self.matter_service.list_matters(
            limit=int(invocation.input.get("limit") or 50),
            status=str(status_value) if status_value else None,
            visibility=invocation.input.get("visibility", "user"),
            offset=int(invocation.input.get("offset") or 0),
            q=invocation.input.get("q"),
            kind=invocation.input.get("kind"),
            parent_matter_id=invocation.input.get("parent_matter_id"),
            project_id=invocation.input.get("project_id"),
            due_before=invocation.input.get("due_before"),
            due_after=invocation.input.get("due_after"),
            unscheduled=bool(invocation.input.get("unscheduled", False)),
            scheduled_on=invocation.input.get("scheduled_on"),
            scheduled_before=invocation.input.get("scheduled_before"),
            scheduled_after=invocation.input.get("scheduled_after"),
            inbox=invocation.input.get("inbox"),
            section=invocation.input.get("section"),
            source_ids=source_ids, account_ids=account_ids,
            allow_unattributed=full_authority,
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=result.model_dump(mode="json"),
        )


class UpdateMatterTool:
    def __init__(self, matter_service: MatterService) -> None:
        self.matter_service = matter_service

    spec = ToolSpec(
        name="matter.update",
        effect_domains=("matter",),
        package="matter",
        type="local_tool",
        description="Update an independent local matter by id.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["write_local_db"],
        **_MATTER_CHILD_SCOPE,
        input_schema={
            "type": "object",
            "required": ["matter_id"],
            "properties": {
                "matter_id": {"type": "string"},
                "visibility": MATTER_VISIBILITY_FIELD,
                **MATTER_PLAN_FIELDS,
                **MATTER_SCHEDULING_FIELDS,
                "expected_revision": {"type": ["integer", "null"], "minimum": 1},
                "title": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
                "status": {
                    "type": ["string", "null"],
                    "allowed_values": MATTER_STATUS_VALUES,
                },
                "priority": {
                    "type": ["string", "null"],
                    "allowed_values": MATTER_PRIORITY_VALUES,
                },
                "due_at": {"type": ["string", "null"]},
                "tags": {"type": ["array", "null"], "items": {"type": "string"}},
                "metadata": {"type": ["object", "null"]},
            },
        },
        output_schema={"matter": "object"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        source_ids, account_ids, full_authority = _child_scope(context)
        matter_id = str(invocation.input.get("matter_id") or "")
        payload = MatterUpdateInput.model_validate(
            {
                key: value
                for key, value in invocation.input.items()
                if key != "matter_id"
            }
        )
        matter = self.matter_service.update_matter(
            matter_id=matter_id,
            payload=payload,
            source_ids=source_ids, account_ids=account_ids,
            allow_unattributed=full_authority,
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"matter": matter.model_dump(mode="json")},
        )


class LinkMatterSourceTool:
    def __init__(self, matter_service: MatterService) -> None:
        self.matter_service = matter_service

    spec = ToolSpec(
        name="matter.link_source",
        effect_domains=("matter",),
        package="matter",
        type="local_tool",
        description="Link an existing matter to a source object such as a mail message.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["write_local_db"],
        **_MATTER_CHILD_SCOPE,
        input_schema={
            "type": "object",
            "required": ["matter_id", "source_type", "source_id", "reason"],
            "properties": {
                "matter_id": {"type": "string"},
                **MATTER_SOURCE_LINK_SCHEMA["properties"],
            },
        },
        output_schema={"matter": "object"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        source_ids, account_ids, full_authority = _child_scope(context)
        matter = self.matter_service.link_source(
            matter_id=str(invocation.input.get("matter_id") or ""),
            source_link=MatterSourceLinkInput(
                source_type=str(invocation.input.get("source_type") or "unknown"),
                source_id=str(invocation.input.get("source_id") or ""),
                reason=str(invocation.input.get("reason") or ""),
            ),
            source_ids=source_ids, account_ids=account_ids,
            allow_unattributed=full_authority,
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"matter": matter.model_dump(mode="json")},
        )
