"""Agent-visible read and controlled update of persistent guidance files."""

from __future__ import annotations

import re

from app.core.instruction_files import InstructionFiles
from app.core.sessions import SessionService
from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec

INSTRUCTIONS_PACKAGE = ToolPackageSpec(
    name="instructions",
    description="Read paginated AGENTS.md guidance or update global/watch guidance.",
    risk="medium",
    requires_expansion=True,
    routing_hints=["Use when the user asks to inspect or explicitly edit an AGENTS.md guidance file or watch guidance, or a loaded AGENTS.md preview says more content is available. Ordinary preferences and corrections are not requests to edit user-owned guidance files."],
    decision_hints=[
        "Follow next_offset to read more pages; read the current document hash before updating; guidance never expands tool permissions.",
        "Separate user-owned guidance from derived memory: background memory learning, when enabled, handles conversational preferences. Do not rewrite guidance merely to acknowledge a preference or forget a learned memory.",
    ],
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
    def __init__(self, files: InstructionFiles, sessions: SessionService | None = None) -> None:
        self.files = files
        self.sessions = sessions

    spec = ToolSpec(
        name="instructions.update", package="instructions", type="local_tool",
        description="Replace a global or watch AGENTS.md only when the user explicitly requests editing that persistent guidance file, after reading its whole-file sha256. Ordinary conversational preferences or requests to forget learned memory are not guidance-file edits. Preserve unrelated instructions. Persistent write subject to the normal safety review gate.",
        read_only=False, risk="medium", requires_confirmation=False,
        side_effects=["write_local_file"],
        input_schema={"type": "object", "required": ["kind", "content", "expected_sha256"],
                      "properties": {"kind": {"type": "string", "enum": ["global", "watch"]},
                                     "content": {"type": "string", "maxLength": 1000000},
                                     "expected_sha256": {"type": "string"}}},
        output_schema={"kind": "string", "path": "string", "content": "string", "sha256": "string"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        message = self.sessions.get_turn_user_message(
            session_id=context.session_id, trace_id=context.trace_id or "",
        ) if self.sessions is not None and context.tool_view is None else None
        if message is None or not _requests_guidance_edit(message.content):
            return ToolResult(
                invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                status="rejected", execution_started=False,
                error="The current user message must explicitly request editing a guidance file. "
                      "Conversational preferences belong in memory, not AGENTS.md.",
            )
        try:
            output = self.files.update(str(invocation.input["kind"]),
                                       content=str(invocation.input["content"]),
                                       expected_sha256=str(invocation.input["expected_sha256"]))
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="completed", output=output)
        except (KeyError, OSError, UnicodeError, ValueError) as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="failed", error=str(exc))


def _requests_guidance_edit(content: str) -> bool:
    """Conservative tool-owned target check, not a general intent classifier.

    Only the persisted current user input is examined. Ambiguous requests must
    be clarified rather than turning a learned preference into operating rules.
    """
    content = re.sub(r"[“\"](AGENTS?\.md)[”\"]", r"\1", content, flags=re.IGNORECASE)
    content = re.sub(r"```[\s\S]*?```|[“「『\"][\s\S]*?[”」』\"]", "", content)
    for clause in re.split(r"[。！？!?；;\n]", content):
        target = re.search(r"\bAGENTS?\.md\b|(?:全局|项目|关注)?指导文件", clause, re.IGNORECASE)
        edit = re.search(r"修改|编辑|更新|写入|写进|保存到|保存进|追加|维护|替换|重置|\b(?:edit|update|write|append|replace|reset)\b", clause, re.IGNORECASE)
        if target is not None and edit is not None and not re.search(
            r"不要|不应|不得|不能|禁止|无需|不必|别|不会|没有|写道|写着|引用|\b(?:not|never|don't|cannot|says|said|quoted)\b|[？?]|怎么|如何|whether|how\b", clause, re.IGNORECASE,
        ):
            return True
    return False
