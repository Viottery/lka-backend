"""Runtime utility tools that expose deterministic environment context."""

from __future__ import annotations

from app.core.runtime_context import current_time_payload
from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec


RUNTIME_PACKAGE = ToolPackageSpec(
    name="runtime",
    description="Read deterministic runtime context such as the current time.",
    risk="low",
    requires_expansion=True,
    routing_hints=[
        "Use this package when a direct answer requires deterministic runtime context.",
    ],
    decision_hints=[
        "Use runtime tools when the session context does not already contain sufficient deterministic runtime data.",
    ],
)


class RuntimeNowTool:
    spec = ToolSpec(
        name="runtime.now",
        package="runtime",
        type="local_tool",
        description="Return the current UTC time and configured local time.",
        risk="low",
        requires_confirmation=False,
        side_effects=[],
        input_schema={
            "type": "object",
            "properties": {
                "timezone": {"type": "string"},
            },
        },
        output_schema={
            "utc": "string",
            "local": "string",
            "timezone": "string",
            "date": "string",
        },
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        timezone_name = str(invocation.input.get("timezone") or "Asia/Shanghai")
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=current_time_payload(timezone_name=timezone_name),
        )
