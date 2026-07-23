"""Tool invocation contracts for runtime debug execution."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.context import TaskContext


class ToolSpec(BaseModel):
    name: str
    type: str
    description: str
    risk: str = "low"
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class ToolInvocation(BaseModel):
    invocation_id: str
    tool: ToolSpec
    session_id: str
    context_id: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    invocation_id: str
    tool_name: str
    status: str
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


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
