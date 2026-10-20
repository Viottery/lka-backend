"""Compact supervisor capture tools alongside the ordinary Matter readers."""
from app.tool_packages.management import COMMAND, TEXT, ManagementTool
from app.tool_packages.matter import MATTER_PRIORITY_VALUES, MATTER_STATUS_VALUES

NULL_TEXT = {"type": ["string", "null"], "maxLength": 80}
FIELDS = {
    "title": {"type": "string", "minLength": 1, "maxLength": 1000},
    "summary": {"type": "string", "maxLength": 16000},
    "due_at": {**NULL_TEXT, "description": "Deadline, not a scheduled time block."},
    "scheduled_on": {**NULL_TEXT, "description": "Planned day YYYY-MM-DD."},
    "scheduled_start": {**NULL_TEXT, "description": "Timezone-aware block start; supply or clear with scheduled_end. Never assume duration."},
    "scheduled_end": {**NULL_TEXT, "description": "Timezone-aware end after start; supply or clear with scheduled_start."},
    "priority": {"type": "string", "enum": MATTER_PRIORITY_VALUES},
    "status": {"type": "string", "enum": MATTER_STATUS_VALUES},
    "visibility": {"type": "string", "enum": ["user", "internal"], "description": "Default user: visible commitment; internal: abstract agent responsibility."},
    "tags": {"type": "array", "maxItems": 50, "items": {"type": "string"}},
    "inbox": {"type": "boolean"},
    "parent_matter_id": {"type": ["string", "null"], "maxLength": 256},
    "project_id": {"type": ["string", "null"], "maxLength": 256},
}


def management_capture_tools(service):
    instructions = (
        "Supervisor-only writer using the same safety, revision and atomic publication checks as organize_work. "
        "Read/search first to avoid duplicates or stale edits. An applied receipt already confirms the written revision; "
        "a proposed receipt takes effect only at successful publication. Do not reread merely to confirm an applied receipt. "
        "For a time with unknown duration, preserve the exact stated time in summary and scheduled_on; "
        "do not invent an end or convert an appointment into a deadline."
    )
    definitions = [
        ("create_matter", "Capture one Matter (default visibility=user). " + instructions,
         False, {"command_id": COMMAND, **FIELDS,
                 "kind": {"type": "string", "enum": ["task", "plan"]}}, ["command_id", "title"]),
        ("update_matter", "Revise or reschedule one existing Matter. " + instructions,
         False, {"command_id": COMMAND, "matter_id": TEXT,
                 "expected_revision": {"type": "integer", "minimum": 1},
                 "changes": {"type": "object", "properties": FIELDS, "minProperties": 1,
                             "additionalProperties": False}},
         ["command_id", "matter_id", "expected_revision", "changes"]),
    ]
    tools = [ManagementTool(service, definition) for definition in definitions]
    for tool in tools:
        tool.spec.package = "matter"
        tool.spec.effect_domains = ("matters",)
        tool.spec.side_effects = ["Creates or revises a Matter through management publication"]
    return tools
