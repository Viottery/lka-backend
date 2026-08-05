"""Matter tool package adapters around MatterService."""

from __future__ import annotations

from app.core.matters import (
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
)


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
        side_effects=["write_local_db"],
        input_schema={
            "title": "string",
            "summary": "string",
            "status": "string",
            "priority": "string",
            "due_at": "string|null",
            "tags": "array",
            "source_links": "array",
            "metadata": "object",
        },
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
        side_effects=["write_local_db"],
        input_schema={"matters": "array"},
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
        side_effects=["read_local_db"],
        input_schema={"query": "string", "limit": "integer"},
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
        side_effects=["read_local_db"],
        input_schema={"limit": "integer", "status": "string|null"},
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
        side_effects=["write_local_db"],
        input_schema={
            "matter_id": "string",
            "title": "string|null",
            "summary": "string|null",
            "status": "string|null",
            "priority": "string|null",
            "due_at": "string|null",
            "tags": "array|null",
            "metadata": "object|null",
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
        side_effects=["write_local_db"],
        input_schema={
            "matter_id": "string",
            "source_type": "string",
            "source_id": "string",
            "reason": "string",
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
