"""Tool package and invocation contracts for agent execution."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.core.context import TaskContext


class ToolPackageSpec(BaseModel):
    name: str
    description: str
    risk: str = "low"
    requires_expansion: bool = True
    tool_names: list[str] = Field(default_factory=list)


class ToolSpec(BaseModel):
    name: str
    type: str
    description: str
    package: str | None = None
    risk: str = "low"
    requires_confirmation: bool = False
    side_effects: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class ToolInvocation(BaseModel):
    invocation_id: str
    tool: ToolSpec
    session_id: str
    context_id: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolContext(BaseModel):
    session_id: str
    trace_id: str | None = None
    context_id: str | None = None


class ToolResult(BaseModel):
    invocation_id: str
    tool_name: str
    status: str
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Tool(Protocol):
    spec: ToolSpec

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        ...


class ToolRegistry:
    """Static package-aware registry for MVP tools."""

    def __init__(self) -> None:
        self._packages: dict[str, ToolPackageSpec] = {}
        self._tools: dict[str, Tool] = {}

    def register_package(self, package: ToolPackageSpec) -> None:
        self._packages[package.name] = package

    def register_tool(self, tool: Tool) -> None:
        self._tools[tool.spec.name] = tool
        if tool.spec.package and tool.spec.package in self._packages:
            package = self._packages[tool.spec.package]
            if tool.spec.name not in package.tool_names:
                package.tool_names.append(tool.spec.name)

    def list_packages(self) -> list[ToolPackageSpec]:
        return list(self._packages.values())

    def list_tools(self, *, package: str | None = None) -> list[ToolSpec]:
        tools = [tool.spec for tool in self._tools.values()]
        if package is None:
            return tools
        return [tool for tool in tools if tool.package == package]

    def get_tool(self, name: str) -> Tool:
        return self._tools[name]


class ToolExecutor:
    """Execute registered tools and normalize failures into ToolResult."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def execute(
        self,
        *,
        invocation_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        tool = self.registry.get_tool(tool_name)
        invocation = ToolInvocation(
            invocation_id=invocation_id,
            tool=tool.spec,
            session_id=context.session_id,
            context_id=context.context_id or "",
            input=tool_input,
        )
        try:
            return tool.invoke(invocation=invocation, context=context)
        except Exception as exc:
            return ToolResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                status="failed",
                error=str(exc),
            )


class MockToolExecutor:
    """A read-only tool executor used only by the debug runtime path."""

    spec = ToolSpec(
        name="runtime_debug_echo",
        type="local_tool",
        description="Echo structured task context metadata for runtime debugging.",
        risk="low",
        requires_confirmation=False,
        input_schema={"goal_summary": "string", "related_file_count": "integer"},
        output_schema={"message": "string", "context_id": "string"},
    )

    def invoke(self, *, invocation_id: str, task_context: TaskContext) -> ToolResult:
        return ToolResult(
            invocation_id=invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={
                "message": "Mock tool completed without modifying files.",
                "context_id": task_context.context_id,
                "related_file_count": len(task_context.related_files),
            },
        )
