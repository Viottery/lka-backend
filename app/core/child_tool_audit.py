"""Versioned tool-effect classification stored with completed child runs."""

from __future__ import annotations

from typing import Any

from app.core.tools import effective_tool_read_only

CHILD_TOOL_AUDIT_PROTOCOL = "child_tool_audit_v1"


def build_child_tool_audit(tool_events: list[Any], registry: Any) -> dict[str, Any]:
    """Capture invocation-time read-only classifications from the live registry."""

    invocations: list[dict[str, Any]] = []
    complete = True
    seen_invocation_ids: set[str] = set()
    for event in tool_events:
        invocation_id = event.result.get("invocation_id")
        spec = registry.get_tool_or_none(event.tool_name) if registry is not None else None
        read_only = (
            effective_tool_read_only(spec, event.input)
            if spec is not None
            else None
        )
        status = event.result.get("status")
        execution_started = event.result.get("execution_started")
        if (
            not isinstance(invocation_id, str)
            or not invocation_id
            or invocation_id in seen_invocation_ids
            or not isinstance(event.tool_name, str)
            or not event.tool_name
            or not isinstance(read_only, bool)
            or not isinstance(status, str)
            or (execution_started is not None and not isinstance(execution_started, bool))
        ):
            complete = False
        if isinstance(invocation_id, str):
            seen_invocation_ids.add(invocation_id)
        classification = {
            "invocation_id": invocation_id,
            "tool_name": event.tool_name,
            "read_only": read_only,
            "status": status,
        }
        if isinstance(execution_started, bool):
            classification["execution_started"] = execution_started
        invocations.append(classification)
    return {
        "protocol_version": CHILD_TOOL_AUDIT_PROTOCOL,
        "complete": complete,
        "invocations": invocations,
    }
