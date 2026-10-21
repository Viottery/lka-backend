"""Explicit PDF tools, with invocation-scoped workspace authorization."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pypdf.errors import PdfReadError

from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec
from app.domains.documents import DocumentService
from app.platform.workspace_access import approved_path, require_child_path_scope
from app.tool_packages.filesystem import FileAccessPolicy

DOCUMENTS_PACKAGE = ToolPackageSpec(
    name="documents", risk="medium", requires_expansion=True,
    description="Read native PDFs locally by page, search their text, and render pages for visual inspection.",
    routing_hints=["Use for PDF files, including scanned pages and visually complex documents."],
    decision_hints=[
        "Open a PDF first and pass its sha256 as expected_sha256 on all later calls.",
        "Reading and searching are lazy and bounded. Follow next cursors without skipping page offsets.",
        "Text extraction cannot guarantee correct reading order or recover charts and images. No text can mean a scanned page.",
        "Use plain extraction for speed; layout extraction preserves approximate spacing, not full graphical fidelity.",
        "Render selected relevant pages to inspect diagrams, tables and scanned content. Rendering is not OCR or visual understanding.",
        "After rendering, expand vision and call vision.inspect_image using output_path and image_sha256 when visual understanding is needed.",
        "Rendering creates a PNG at an explicit output_path and requires write review. It never overwrites existing files.",
        "All paths are relative to the session workspace. Outside targets require exact-path approval each invocation.",
    ],
)

_PATH = {"type": "string"}
_INT = lambda minimum, maximum: {"type": "integer", "minimum": minimum, "maximum": maximum}


class DocumentTool:
    def __init__(self, operation: str, policy: FileAccessPolicy, service: DocumentService) -> None:
        self.operation, self.policy, self.service = operation, policy, service
        required = ["path"]
        properties: dict[str, Any] = {"path": _PATH}
        if operation != "open":
            required.append("expected_sha256")
            properties["expected_sha256"] = {"type": "string", "minLength": 64, "maxLength": 64}
        if operation in ("read_pages", "find"):
            properties.update(start_page=_INT(1, 20000), start_char=_INT(0, 2000000))
        if operation == "read_pages":
            properties.update(max_pages=_INT(1, 10), max_chars=_INT(1, 64000),
                              mode={"type": "string", "enum": ["plain", "layout"]})
        elif operation == "find":
            required.append("query")
            properties.update(query={"type": "string", "minLength": 1, "maxLength": 1000},
                              max_pages=_INT(1, 50), max_matches=_INT(1, 100))
        elif operation == "render_page":
            required.extend(["page", "output_path"])
            properties.update(page=_INT(1, 20000), output_path=_PATH, dpi=_INT(72, 200))
        writing = operation == "render_page"
        self.spec = ToolSpec(
            name=f"documents.{operation}", package="documents", type="local_tool",
            description={"open": "Inspect PDF metadata and outline without extracting or rendering pages. Returns source sha256.",
                         "read_pages": "Read bounded PDF text by 1-based page and character offset; returns continuation and fidelity flags.",
                         "find": "Search exact case-sensitive text lazily over a bounded page range; returns page offsets and continuation.",
                         "render_page": "Create a PNG of a PDF page at an explicit output path for later visual inspection; no overwrite."}[operation],
            risk="medium" if writing else "low", read_only=not writing, requires_confirmation=False,
            unrestricted_execution=True, scope_uses_workspace=True,
            scope_path_fields=("path", "output_path") if writing else ("path",),
            side_effects=["read_local_file", "write_local_file"] if writing else ["read_local_file"],
            input_schema={"type": "object", "required": required, "properties": properties, "additionalProperties": False},
            output_schema={"path": "string", "sha256": "string"},
            output_selected_content=operation == "read_pages",
            output_preview_priority_fields=["path", "sha256", "next", "truncated", "pages"] if operation == "read_pages" else [],
        )

    def _resolve(self, value: str, context: ToolContext, *, authorize: bool) -> Path:
        policy = self.policy.for_session_workspace(context.workspace_root)
        target = policy.resolve(value, allow_outside=True)
        require_child_path_scope(target, context)
        if authorize and not policy.is_allowed(target) and not approved_path(target, context):
            raise PermissionError("path is outside allowed workspace roots and was not approved for this invocation")
        return target

    def workspace_write_targets(self, tool_input: dict[str, Any], context: ToolContext) -> list[str]:
        if self.operation != "render_page":
            return []
        try:
            return [str(self._resolve(str(tool_input.get("output_path") or ""), context, authorize=False))]
        except (OSError, ValueError, PermissionError):
            return []

    def workspace_access_request(self, tool_input: dict[str, Any], context: ToolContext) -> list[str]:
        targets = []
        try:
            policy = self.policy.for_session_workspace(context.workspace_root)
        except (OSError, ValueError, PermissionError):
            return []
        for field in self.spec.scope_path_fields:
            try:
                target = self._resolve(str(tool_input.get(field) or ""), context, authorize=False)
                if not policy.is_allowed(target):
                    targets.append(str(target))
            except (OSError, ValueError, PermissionError):
                continue
        return targets

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            args = dict(invocation.input)
            path = self._resolve(str(args.pop("path", "")), context, authorize=True)
            if self.operation == "render_page":
                args["output_path"] = self._resolve(str(args["output_path"]), context, authorize=True)
            output = getattr(self.service, self.operation)(path, **args)
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="completed", output=output)
        except (OSError, ValueError, PdfReadError, RuntimeError, KeyError, TypeError, IndexError, ArithmeticError, AttributeError) as exc:
            # Malformed document/parser and renderer errors must fail explicitly.
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="failed", error=str(exc))


def create_documents_tools(workspace_roots: list[Path] | None = None) -> list[DocumentTool]:
    service = DocumentService()
    policy = FileAccessPolicy.from_workspace_roots(workspace_roots)
    return [DocumentTool(operation, policy, service) for operation in ("open", "read_pages", "find", "render_page")]
