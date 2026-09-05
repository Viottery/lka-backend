"""Matter tool package adapters around MatterService."""

from __future__ import annotations

from app.domains.matters import (
    MatterCreateInput,
    MatterService,
    MatterSourceLinkInput,
    MatterUpdateInput,
)
from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec


MATTER_PACKAGE = ToolPackageSpec(
    name="matter",
    description="Create, search, list, update, and link independent local matters.",
    risk="low_to_medium",
    requires_expansion=True,
    routing_hints=[
        "Use this package when the user asks to inspect, create, update, or link local tasks, events, reminders, or matters.",
    ],
    decision_hints=[
        "Before create or create_many, call matter.search with the candidate title or key subject unless recent observations already prove no duplicate risk.",
        "If matter.search returns a likely duplicate, update or link the existing matter instead of creating a new one.",
        "Use create for one record, create_many for a controlled batch, update to change existing records, and link_source to attach evidence.",
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
MATTER_CREATE_SCHEMA = {
    "type": "object",
    "required": ["title", "summary"],
    "properties": {
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
            "description": "ISO 8601 datetime with timezone when the time is known.",
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
        description="Create an independent local matter with optional source links.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["write_local_db"],
        input_schema=MATTER_CREATE_SCHEMA,
        output_schema={"matter": "object"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        matter = self.matter_service.create_matter(
            MatterCreateInput.model_validate(invocation.input)
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
        package="matter",
        type="local_tool",
        description="Create multiple independent local matters in one controlled batch.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["write_local_db"],
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
        created = [
            self.matter_service.create_matter(MatterCreateInput.model_validate(payload))
            for payload in invocation.input.get("matters", [])
            if isinstance(payload, dict)
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
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10, "minimum": 1},
            },
        },
        output_schema={"query": "string", "matters": "array"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        result = self.matter_service.search_matters(
            query=str(invocation.input.get("query") or ""),
            limit=int(invocation.input.get("limit") or 10),
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=result.model_dump(mode="json"),
        )


class ListMattersTool:
    def __init__(self, matter_service: MatterService) -> None:
        self.matter_service = matter_service

    spec = ToolSpec(
        name="matter.list",
        package="matter",
        type="local_tool",
        description="List independent local matters ordered by due date and recent updates.",
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_db"],
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50, "minimum": 1},
                "status": {
                    "type": ["string", "null"],
                    "allowed_values": MATTER_STATUS_VALUES,
                },
            },
        },
        output_schema={"matters": "array"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        status_value = invocation.input.get("status")
        result = self.matter_service.list_matters(
            limit=int(invocation.input.get("limit") or 50),
            status=str(status_value) if status_value else None,
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
        package="matter",
        type="local_tool",
        description="Update an independent local matter by id.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["write_local_db"],
        input_schema={
            "type": "object",
            "required": ["matter_id"],
            "properties": {
                "matter_id": {"type": "string"},
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
        package="matter",
        type="local_tool",
        description="Link an existing matter to a source object such as a mail message.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["write_local_db"],
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
        matter = self.matter_service.link_source(
            matter_id=str(invocation.input.get("matter_id") or ""),
            source_link=MatterSourceLinkInput(
                source_type=str(invocation.input.get("source_type") or "unknown"),
                source_id=str(invocation.input.get("source_id") or ""),
                reason=str(invocation.input.get("reason") or ""),
            ),
        )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"matter": matter.model_dump(mode="json")},
        )
