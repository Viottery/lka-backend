"""Explicit foreground configuration of durable management capture policies."""
from __future__ import annotations

from app.core.tools import ToolResult, ToolSpec


class ManagementPolicyTool:
    def __init__(self, tasks, attention, schedules, action, *, schema, read_only, description):
        self.tasks, self.attention, self.schedules, self.action = tasks, attention, schedules, action
        self.spec = ToolSpec(
            name="management." + action, package="management", type="local_tool",
            description=description, risk="low" if read_only else "medium", read_only=read_only,
            requires_confirmation=False, input_schema=schema, output_schema={"type": "object"},
            effect_domains=() if read_only else ("agent_control",),
            side_effects=[] if read_only else ["Changes future management capture policies"],
        )

    def invoke(self, *, invocation, context):
        mutate = self.spec.read_only is not True
        if mutate and not context.safety_review_approved:
            raise ValueError("Policy writes require the tool safety review gate")
        allowed = self.spec.input_schema.get("properties", {})
        if set(invocation.input) - allowed.keys():
            raise ValueError("Unknown management policy input fields")
        run = self.tasks.caller(context, mutate=mutate)
        if mutate and run.metadata.get("manager_execution_id"):
            raise ValueError("Future capture policies require a foreground user-delegated turn; autonomous runs cannot change them")
        if self.action == "read_policies":
            output = {"attention": self.attention.get(context.session_id),
                      **self.schedules.list_schedules(context.session_id, **invocation.input)}
        else:
            service = self.attention if self.action == "configure_attention" else self.schedules
            output = service.configure(context.session_id, **invocation.input,
                authority_check=lambda conn: self.tasks.caller(context, mutate=True, conn=conn))
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="completed", output=output)


def management_policy_tools(tasks, attention, schedules):
    from app.domains.management_schedules import ManagementScheduleInput

    schedule_schema = ManagementScheduleInput.model_json_schema()
    schedule_schema["properties"]["command_id"] = {"type": "string", "minLength": 1, "maxLength": 128}
    schedule_schema.setdefault("required", []).append("command_id")
    return [
        ManagementPolicyTool(tasks, attention, schedules, "read_policies", read_only=True,
            description="Read opted-in recent Matter attention policy and bounded recurring schedules before configuring them.",
            schema={"type": "object", "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False}),
        ManagementPolicyTool(tasks, attention, schedules, "configure_attention", read_only=False,
            description="Explicitly enable/pause user Matter reminders in a foreground delegated turn. Read policy first; expected_revision=0 creates. Deduplicates due soon, overdue and today's schedule facts. No policy means no reminders.",
            schema={"type": "object", "properties": {
                "expected_revision": {"type": "integer", "minimum": 0},
                "enabled": {"type": "boolean"}, "timezone": {"type": "string", "maxLength": 128},
                "horizon_days": {"type": "integer", "minimum": 1, "maximum": 90}},
                "required": ["expected_revision", "enabled"], "additionalProperties": False}),
        ManagementPolicyTool(tasks, attention, schedules, "configure_schedule", read_only=False,
            description="Create/revise one-shot, daily or weekly timezone-aware goal checks in a foreground user-delegated turn. Use stable command_id; update requires schedule_id/expected_revision. Missed occurrences coalesce once; pause/disable prevents backfill. This schedules a bounded manager turn, not external delivery.",
            schema=schedule_schema),
    ]
