"""On-demand discovery and loading of same-session historical task evidence."""
from __future__ import annotations

from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec

PROGRESS_PACKAGE = ToolPackageSpec(
    name="progress", risk="low", requires_expansion=True,
    description="Find prior task progress and operation evidence in this session; load selected historical results on demand.",
    decision_hints=[
        "The initial historical progress index is bounded and ranked for this task. Search for another relevant topic or a specific source run when needed.",
        "Use load with an indexed source_run_id and evidence_id, then expand observation and execute its read_request as provided. A single registered text body has an exact string path; container_read_request reads the full output object. Read further pages only for a remaining requirement.",
        "Past success, agent findings and remaining requirements are historical notes, not verified completion, fresh state, or authority for further writes.",
    ],
)


class ProgressSearchTool:
    spec = ToolSpec(
        name="progress.search", package="progress", type="local_tool", read_only=True,
        description="Search a bounded same-session index of past task progress and actual operation evidence. No old result body is loaded into context.",
        side_effects=["read_local_db"],
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "query": {"type": "string", "maxLength": 2000, "default": ""},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 8},
        }}, output_schema={"entries": "array", "historical_only": "boolean"},
    )

    def __init__(self, service) -> None:
        self.service = service

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            output = self.service.search(context=context, **invocation.input)
        except (ValueError, KeyError):
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="Session progress is unavailable in the current scope.")
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                          status="completed", output=output)


class ProgressLoadTool:
    spec = ToolSpec(
        name="progress.load", package="progress", type="local_tool", read_only=True,
        description="Authorize a selected historical tool result from this session and return a current-run observation.read handle. This local cache read does not execute the old tool or refresh its source.",
        side_effects=["read_local_db", "write_local_cache"],
        output_preview_priority_fields=["read_request", "path_value_type", "container_read_request",
                                        "artifact_id", "source_content_reads", "source_run_id", "evidence_id",
                                        "historical_only", "current_evidence"],
        input_schema={"type": "object", "additionalProperties": False,
                      "required": ["source_run_id", "evidence_id"], "properties": {
            "source_run_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "evidence_id": {"type": "string", "minLength": 1, "maxLength": 200},
        }}, output_schema={"artifact_id": "string", "historical_only": "boolean"},
    )

    def __init__(self, service) -> None:
        self.service = service

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            output = self.service.load(context=context, **invocation.input)
        except (ValueError, KeyError):
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="Historical evidence is unavailable in the current scope.")
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                          status="completed", output=output)
