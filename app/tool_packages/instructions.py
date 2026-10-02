"""Agent-visible read and controlled update of persistent guidance files."""

from __future__ import annotations

from app.core.instruction_files import InstructionFiles
from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec

INSTRUCTIONS_PACKAGE = ToolPackageSpec(
    name="instructions",
    description="Read paginated AGENTS.md guidance or update global/watch guidance.",
    risk="medium",
    requires_expansion=True,
    routing_hints=["Use when the user asks to inspect or maintain durable Agent preferences or watch guidance, or a loaded AGENTS.md preview says more content is available."],
    decision_hints=["Follow next_offset to read more pages; read the current document hash before updating; guidance never expands tool permissions."],
)


class ReadInstructionsTool:
    def __init__(self, files: InstructionFiles) -> None:
        self.files = files

    spec = ToolSpec(
        name="instructions.read", package="instructions", type="local_tool",
        description="Read one UTF-8 page of global or watch AGENTS.md. Follow next_offset until truncated is false; sha256 identifies the whole file.",
        read_only=True, risk="low", requires_confirmation=False,
        side_effects=["read_local_file"],
        input_schema={"type": "object", "required": ["kind"], "properties": {
            "kind": {"type": "string", "enum": ["global", "watch"]},
            "offset": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": "integer", "minimum": 4, "maximum": 16384}}},
        output_schema={"kind": "string", "path": "string", "content": "string",
                       "sha256": "string", "next_offset": "integer|null", "truncated": "boolean"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            output = self.files.read(str(invocation.input["kind"]),
                                     offset=invocation.input.get("offset", 0),
                                     max_bytes=invocation.input.get("max_bytes", 4_096))
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="completed", output=output)
        except (KeyError, OSError, UnicodeError, ValueError) as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="failed", error=str(exc))


class ReadProjectInstructionsTool:
    def __init__(self, files: InstructionFiles) -> None:
        self.files = files

    spec = ToolSpec(
        name="instructions.read_project", package="instructions", type="local_tool",
        description="Read one UTF-8 page of an AGENTS.md in the selected workspace instruction chain. Use the path from agent_instructions and follow next_offset.",
        read_only=True, risk="low", requires_confirmation=False,
        side_effects=["read_local_file"], scope_uses_workspace=True,
        scope_filtering_required=True,
        input_schema={"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": "integer", "minimum": 4, "maximum": 16384}}},
        output_schema={"kind": "string", "path": "string", "content": "string",
                       "next_offset": "integer|null", "truncated": "boolean"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            output = self.files.read_project(
                str(invocation.input["path"]), workspace_root=context.workspace_root,
                offset=invocation.input.get("offset", 0),
                max_bytes=invocation.input.get("max_bytes", 4_096),
            )
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="completed", output=output)
        except (KeyError, OSError, UnicodeError, ValueError, PermissionError) as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="failed", error=str(exc))


class SearchInstructionsTool:
    def __init__(self, files: InstructionFiles) -> None:
        self.files = files

    spec = ToolSpec(
        name="instructions.search", package="instructions", type="local_tool",
        description="Search the automatically rebuilt chunk index of global or watch AGENTS.md; returns offsets to read in full.",
        read_only=True, risk="low", requires_confirmation=False,
        side_effects=["read_local_file"],
        input_schema={"type": "object", "required": ["kind", "query"], "properties": {
            "kind": {"type": "string", "enum": ["global", "watch"]},
            "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
        output_schema={"matches": "array", "total_matches": "integer", "sha256": "string"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            output = self.files.search(kind=str(invocation.input["kind"]),
                                       query=str(invocation.input["query"]),
                                       limit=invocation.input.get("limit", 8))
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="completed", output=output)
        except (KeyError, OSError, UnicodeError, ValueError, PermissionError) as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="failed", error=str(exc))


class SearchProjectInstructionsTool:
    def __init__(self, files: InstructionFiles) -> None:
        self.files = files

    spec = ToolSpec(
        name="instructions.search_project", package="instructions", type="local_tool",
        description="Search the chunk index of a project AGENTS.md in the selected workspace instruction chain.",
        read_only=True, risk="low", requires_confirmation=False,
        side_effects=["read_local_file"], scope_uses_workspace=True,
        scope_filtering_required=True,
        input_schema={"type": "object", "required": ["path", "query"], "properties": {
            "path": {"type": "string"}, "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
        output_schema={"matches": "array", "total_matches": "integer", "sha256": "string"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            output = self.files.search(kind="project", path=str(invocation.input["path"]),
                                       workspace_root=context.workspace_root,
                                       query=str(invocation.input["query"]),
                                       limit=invocation.input.get("limit", 8))
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="completed", output=output)
        except (KeyError, OSError, UnicodeError, ValueError, PermissionError) as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="failed", error=str(exc))


class UpdateInstructionsTool:
    def __init__(self, files: InstructionFiles) -> None:
        self.files = files

    spec = ToolSpec(
        name="instructions.update", package="instructions", type="local_tool",
        description="Replace a global or watch AGENTS.md after reading its whole-file sha256. Persistent write subject to the normal safety review gate.",
        read_only=False, risk="medium", requires_confirmation=False,
        side_effects=["write_local_file"],
        input_schema={"type": "object", "required": ["kind", "content", "expected_sha256"],
                      "properties": {"kind": {"type": "string", "enum": ["global", "watch"]},
                                     "content": {"type": "string", "maxLength": 1000000},
                                     "expected_sha256": {"type": "string"}}},
        output_schema={"kind": "string", "path": "string", "content": "string", "sha256": "string"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            output = self.files.update(str(invocation.input["kind"]),
                                       content=str(invocation.input["content"]),
                                       expected_sha256=str(invocation.input["expected_sha256"]))
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="completed", output=output)
        except (KeyError, OSError, UnicodeError, ValueError) as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="failed", error=str(exc))
