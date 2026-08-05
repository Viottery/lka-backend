"""Runtime utility tools that expose deterministic environment context."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec


RUNTIME_PACKAGE = ToolPackageSpec(
    name="runtime",
    description="Read deterministic runtime context such as the current time.",
    risk="low",
    requires_expansion=True,
)


def current_time_payload(*, timezone_name: str = "Asia/Shanghai") -> dict[str, str]:
    utc_now = datetime.now(UTC)
    try:
        local_zone = ZoneInfo(timezone_name)
    except Exception:
        local_zone = ZoneInfo("Asia/Shanghai")
        timezone_name = "Asia/Shanghai"
    local_now = utc_now.astimezone(local_zone)
    return {
        "utc": utc_now.isoformat(),
        "local": local_now.isoformat(),
        "timezone": timezone_name,
        "date": local_now.date().isoformat(),
    }


class RuntimeNowTool:
    spec = ToolSpec(
        name="runtime.now",
        package="runtime",
        type="local_tool",
        description="Return the current UTC time and configured local time.",
        risk="low",
        requires_confirmation=False,
        side_effects=[],
        input_schema={"timezone": "string"},
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
