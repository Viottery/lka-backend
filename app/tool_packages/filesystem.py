"""Agent-visible explicit file read/edit tools."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec
from app.platform.workspace_access import approved_path, is_path_within, require_child_path_scope

DEFAULT_READ_MAX_LINES = 200
DEFAULT_READ_MAX_BYTES = 32_768
MAX_READ_LINES = 1_000
MAX_READ_BYTES = 65_536
MAX_TEXT_FILE_BYTES = 2_000_000
MAX_EDIT_FILE_BYTES = 1_000_000


FILESYSTEM_PACKAGE = ToolPackageSpec(
    name="filesystem",
    description=(
        "List directories, read, create and edit specific text files, using the session workspace as the default root. "
        "An outside-workspace target requires exact-path approval for that invocation. This package "
        "is for explicit file access, not workspace indexing, search, or summaries."
    ),
    risk="medium",
    requires_expansion=True,
    routing_hints=[
        "Use this package when the user asks to read a known file path.",
        "Use this package when the user asks to make a targeted edit to a known text file.",
        "Use list_directory for bounded directory discovery; text search belongs to shell commands or indexed retrieval.",
    ],
    decision_hints=[
        "Read a file before editing it so you can provide expected_sha256.",
        "Use list_directory for a directory, not read_file. Listing is shallow; enter relevant subdirectories rather than reading an entire tree.",
        "Use relative paths from the default workspace root, or the injected $workspace_root variable.",
        "For an outside-workspace file, provide its path; the normal per-call review evaluates that exact target.",
        "Use edit_file for targeted old_text to new_text replacements only.",
        "If old_text is not unique, read more context and retry with a longer unique snippet.",
        "Do not use edit_file to create new files or replace large files wholesale.",
        "Use create_file to save a new UTF-8 text file or Python script. The parent directory must exist; existing files are never overwritten.",
    ],
)


@dataclass(frozen=True)
class FileAccessPolicy:
    roots: list[Path]
    allow_session_root_override: bool = False

    @classmethod
    def from_workspace_roots(cls, roots: list[Path] | None) -> "FileAccessPolicy":
        configured = [root.expanduser().resolve(strict=False) for root in roots or []]
        if not configured:
            configured = [Path.cwd().resolve(strict=False)]
        return cls(roots=configured, allow_session_root_override=not bool(roots))

    def for_session_workspace(self, workspace_root: str | None) -> "FileAccessPolicy":
        if not workspace_root:
            return self
        root = Path(workspace_root).expanduser().resolve(strict=False)
        if not self.allow_session_root_override and not self._is_allowed(root):
            raise PermissionError("session workspace is outside allowed workspace roots")
        return FileAccessPolicy(roots=[root])

    def resolve(self, path_value: str, *, allow_outside: bool = False) -> Path:
        if not path_value.strip():
            raise ValueError("path is required.")
        path = Path(self.expand_workspace_variables(path_value)).expanduser()
        if not path.is_absolute():
            path = self.default_root / path
        resolved = path.resolve(strict=False)
        if not self._is_allowed(resolved):
            if allow_outside:
                return resolved
            allowed = ", ".join(root.as_posix() for root in self.roots)
            raise PermissionError(f"path is outside allowed workspace roots: {allowed}")
        return resolved

    def is_allowed(self, path: Path) -> bool:
        return self._is_allowed(path.resolve(strict=False))

    @property
    def default_root(self) -> Path:
        return self.roots[0]

    def expand_workspace_variables(self, value: str) -> str:
        root = self.default_root.as_posix()
        return (
            value.replace("$workspace_root", root)
            .replace("${workspace_root}", root)
            .replace("$WORKSPACE_ROOT", root)
            .replace("${WORKSPACE_ROOT}", root)
            .replace("$LKA_WORKSPACE_ROOT", root)
            .replace("${LKA_WORKSPACE_ROOT}", root)
        )

    def _is_allowed(self, path: Path) -> bool:
        for root in self.roots:
            try:
                path.relative_to(root)
            except ValueError:
                continue
            return True
        return False


class ReadFileTool:
    def __init__(self, policy: FileAccessPolicy) -> None:
        self.policy = policy

    def workspace_access_request(self, tool_input: dict[str, Any], context: ToolContext) -> list[str]:
        return workspace_access_request(tool_input, context, self.policy)

    spec = ToolSpec(
        name="filesystem.read_file",
        unrestricted_execution=True,
        package="filesystem",
        type="local_tool",
        description=(
            "Read a bounded UTF-8 text file slice by path. Returns line metadata, "
            "truncation status, and full-file sha256 for later edit_file calls. "
            "An outside-workspace path requires approval for this invocation."
        ),
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_file"],
        scope_path_fields=("path",),
        scope_uses_workspace=True,
        input_schema={
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1},
                "max_bytes": {"type": "integer", "minimum": 1},
            },
        },
        output_schema={
            "path": "string",
            "resolved_path": "string",
            "sha256": "string",
            "file_size_bytes": "integer",
            "start_line": "integer",
            "end_line": "integer",
            "total_lines": "integer",
            "returned_lines": "integer",
            "truncated": "boolean",
            "content": "string",
        },
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            policy = self.policy.for_session_workspace(context.workspace_root)
            requested_path = str(invocation.input.get("path") or "")
            resolved = policy.resolve(requested_path, allow_outside=True)
            _require_child_path_scope(resolved, context)
            _require_approved_external_path(resolved, policy, context)
            payload = read_file_slice(
                path=resolved,
                requested_path=requested_path,
                start_line=int(invocation.input.get("start_line") or 1),
                max_lines=int(invocation.input.get("max_lines") or DEFAULT_READ_MAX_LINES),
                max_bytes=int(invocation.input.get("max_bytes") or DEFAULT_READ_MAX_BYTES),
            )
        except (OSError, UnicodeError, ValueError, PermissionError) as exc:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="failed",
                error=str(exc),
            )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=payload,
        )


class ListDirectoryTool(ReadFileTool):
    """Shallow, bounded discovery; no symlink traversal or implied text coverage."""

    spec = ToolSpec(
        name="filesystem.list_directory", package="filesystem", type="local_tool",
        description="List bounded immediate directory entries, without reading file contents or following symlinks. "
                    "Pass snapshot_id as expected_snapshot on subsequent pages; narrow path if enumeration is truncated.",
        risk="low", read_only=True, unrestricted_execution=True,
        requires_confirmation=False, side_effects=["read_local_file"],
        scope_path_fields=("path",), scope_uses_workspace=True,
        input_schema={"type": "object", "properties": {
            "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "expected_snapshot": {"type": "string", "minLength": 64, "maxLength": 64}},
            "additionalProperties": False},
        output_schema={"entries": "array", "snapshot_id": "string", "enumeration_complete": "boolean"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            policy = self.policy.for_session_workspace(context.workspace_root)
            requested = str(invocation.input.get("path", "."))
            path = policy.resolve(requested, allow_outside=True)
            _require_child_path_scope(path, context)
            _require_approved_external_path(path, policy, context)
            offset, limit = int(invocation.input.get("offset", 0)), int(invocation.input.get("limit", 100))
            if offset < 0 or not 1 <= limit <= 200:
                raise ValueError("Invalid directory page")
            entries = []
            with os.scandir(path) as iterator:
                for entry in islice(iterator, 2001):
                    kind = ("symlink" if entry.is_symlink() else "directory" if entry.is_dir(follow_symlinks=False)
                            else "file" if entry.is_file(follow_symlinks=False) else "other")
                    entries.append({"name": entry.name, "kind": kind})
            complete = len(entries) <= 2000
            entries = sorted(entries[:2000], key=lambda item: item["name"])
            signature = hashlib.sha256((str(path) + repr(entries) + str(complete)).encode()).hexdigest()
            expected = invocation.input.get("expected_snapshot")
            if offset and not expected:
                raise ValueError("Continuation requires expected_snapshot")
            if expected is not None and expected != signature:
                raise ValueError("Directory changed; restart enumeration")
            end = min(offset + limit, len(entries))
            payload = {"path": requested, "resolved_path": str(path), "entries": entries[offset:end],
                       "snapshot_id": signature, "offset": offset,
                       "next_offset": end if end < len(entries) else None,
                       "has_more": end < len(entries), "enumeration_complete": complete,
                       "enumerated_entries": len(entries), "truncated": not complete,
                       "evidence_scope": "Immediate entry names only; file contents and subdirectories are not read."}
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="completed", output=payload)
        except (OSError, ValueError, PermissionError) as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="failed", error=str(exc))


class CreateFileTool:
    def __init__(self, policy: FileAccessPolicy) -> None:
        self.policy = policy

    def workspace_access_request(self, tool_input: dict[str, Any], context: ToolContext) -> list[str]:
        return workspace_access_request(tool_input, context, self.policy)

    spec = ToolSpec(
        name="filesystem.create_file", unrestricted_execution=True, package="filesystem",
        type="local_tool", description="Create a new UTF-8 text file exclusively, including reusable .py scripts. "
        "Requires an existing parent directory; never overwrites a file. Outside-workspace targets need exact-path approval.",
        risk="medium", requires_confirmation=False, read_only=False,
        side_effects=["write_local_file"], scope_path_fields=("path",), scope_uses_workspace=True,
        input_schema={"type": "object", "required": ["path", "content"],
                      "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
        output_schema={"path": "string", "resolved_path": "string", "sha256": "string",
                       "bytes_written": "integer", "created": "boolean"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            policy = self.policy.for_session_workspace(context.workspace_root)
            requested = str(invocation.input.get("path") or "")
            path = policy.resolve(requested, allow_outside=True)
            _require_child_path_scope(path, context)
            _require_approved_external_path(path, policy, context)
            requested_path = Path(policy.expand_workspace_variables(requested)).expanduser()
            if not requested_path.is_absolute():
                requested_path = policy.default_root / requested_path
            # resolve() follows even a dangling leaf symlink. It is already a
            # user-owned directory entry; do not create its missing target.
            if requested_path.is_symlink():
                raise FileExistsError("Target already exists as a symlink")
            raw = str(invocation.input.get("content", "")).encode("utf-8")
            if len(raw) > MAX_EDIT_FILE_BYTES:
                raise ValueError(f"New file exceeds {MAX_EDIT_FILE_BYTES} bytes")
            if b"\x00" in raw:
                raise ValueError("New file must be text without NUL bytes")
            if not path.parent.is_dir():
                raise FileNotFoundError("Parent directory must already exist")
            # O_EXCL rejects a competing creator and avoids truncating user files.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            output = {"path": requested, "resolved_path": str(path),
                      "sha256": hashlib.sha256(raw).hexdigest(), "bytes_written": len(raw), "created": True}
        except (OSError, UnicodeError, ValueError, PermissionError) as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="failed", error=str(exc))
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                          status="completed", output=output)


class EditFileTool:
    def __init__(self, policy: FileAccessPolicy) -> None:
        self.policy = policy

    def workspace_access_request(self, tool_input: dict[str, Any], context: ToolContext) -> list[str]:
        return workspace_access_request(tool_input, context, self.policy)

    spec = ToolSpec(
        name="filesystem.edit_file",
        unrestricted_execution=True,
        package="filesystem",
        type="local_tool",
        description=(
            "Apply targeted old_text to new_text replacements to an existing UTF-8 text file. "
            "Requires expected_sha256 from read_file and requires each old_text to match exactly once by default. "
            "An outside-workspace path requires approval for this invocation."
        ),
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["write_local_file"],
        scope_path_fields=("path",),
        scope_uses_workspace=True,
        input_schema={
            "type": "object",
            "required": ["path", "expected_sha256", "edits"],
            "properties": {
                "path": {"type": "string"},
                "expected_sha256": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["old_text", "new_text"],
                        "properties": {
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                            "expected_occurrences": {"type": "integer", "minimum": 1},
                        },
                    },
                },
            },
        },
        output_schema={
            "path": "string",
            "resolved_path": "string",
            "sha256_before": "string",
            "sha256_after": "string",
            "bytes_written": "integer",
            "changed": "boolean",
            "edit_count": "integer",
        },
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            policy = self.policy.for_session_workspace(context.workspace_root)
            requested_path = str(invocation.input.get("path") or "")
            resolved = policy.resolve(requested_path, allow_outside=True)
            _require_child_path_scope(resolved, context)
            _require_approved_external_path(resolved, policy, context)
            payload = edit_file(
                path=resolved,
                requested_path=requested_path,
                expected_sha256=str(invocation.input.get("expected_sha256") or ""),
                edits=invocation.input.get("edits"),
            )
        except (OSError, UnicodeError, ValueError, PermissionError) as exc:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="failed",
                error=str(exc),
            )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=payload,
        )


def _require_child_path_scope(path: Path, context: ToolContext) -> None:
    """Recheck the snapshot path ceiling even when the base policy is broader."""

    require_child_path_scope(path, context)


def workspace_access_request(tool_input: dict[str, Any], context: ToolContext, policy: FileAccessPolicy) -> list[str]:
    """Return canonical out-of-root targets for executor review without reading them."""

    try:
        active = policy.for_session_workspace(context.workspace_root)
        target = active.resolve(str(tool_input.get("path") or ""), allow_outside=True)
        _require_child_path_scope(target, context)
    except (OSError, ValueError, PermissionError):
        return []
    return [] if active.is_allowed(target) else [str(target)]


def _require_approved_external_path(path: Path, policy: FileAccessPolicy, context: ToolContext) -> None:
    active_policy = policy.for_session_workspace(context.workspace_root)
    if active_policy.is_allowed(path):
        return
    # Re-resolve immediately before use so a symlink retarget invalidates the grant.
    canonical = path.expanduser().resolve(strict=False)
    if canonical != path or not approved_path(canonical, context):
        raise PermissionError("path is outside allowed workspace roots and was not approved for this invocation")


_is_path_within = is_path_within


def read_file_slice(
    *,
    path: Path,
    requested_path: str,
    start_line: int,
    max_lines: int,
    max_bytes: int,
) -> dict[str, Any]:
    content, raw = _read_text_file(path=path, max_file_bytes=MAX_TEXT_FILE_BYTES)
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    start = max(1, start_line)
    line_limit = min(max(1, max_lines), MAX_READ_LINES)
    byte_limit = min(max(1, max_bytes), MAX_READ_BYTES)
    if start > total_lines:
        return {
            "path": requested_path,
            "resolved_path": path.as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "file_size_bytes": len(raw),
            "start_line": start,
            "end_line": total_lines,
            "total_lines": total_lines,
            "returned_lines": 0,
            "truncated": False,
            "content": "",
        }
    selected: list[str] = []
    used_bytes = 0
    end_line = start - 1
    truncated_by_bytes = False
    for line_number, line in enumerate(lines[start - 1 :], start=start):
        if len(selected) >= line_limit:
            break
        encoded_len = len(line.encode("utf-8"))
        if selected and used_bytes + encoded_len > byte_limit:
            truncated_by_bytes = True
            break
        if not selected and encoded_len > byte_limit:
            selected.append(line.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore"))
            used_bytes = byte_limit
            end_line = line_number
            truncated_by_bytes = True
            break
        selected.append(line)
        used_bytes += encoded_len
        end_line = line_number
    truncated = end_line < total_lines or truncated_by_bytes
    return {
        "path": requested_path,
        "resolved_path": path.as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "file_size_bytes": len(raw),
        "start_line": start,
        "end_line": end_line,
        "total_lines": total_lines,
        "returned_lines": len(selected),
        "truncated": truncated,
        "content": "".join(selected),
    }


def edit_file(
    *,
    path: Path,
    requested_path: str,
    expected_sha256: str,
    edits: Any,
) -> dict[str, Any]:
    content, raw = _read_text_file(path=path, max_file_bytes=MAX_EDIT_FILE_BYTES)
    sha_before = hashlib.sha256(raw).hexdigest()
    if not expected_sha256:
        raise ValueError("expected_sha256 is required.")
    if expected_sha256 != sha_before:
        raise ValueError("expected_sha256 does not match current file content.")
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty array.")

    updated = content
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"edits[{index}] must be an object.")
        old_text = edit.get("old_text")
        new_text = edit.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            raise ValueError(f"edits[{index}].old_text must be a non-empty string.")
        if not isinstance(new_text, str):
            raise ValueError(f"edits[{index}].new_text must be a string.")
        expected_occurrences = int(edit.get("expected_occurrences") or 1)
        actual_occurrences = updated.count(old_text)
        if actual_occurrences != expected_occurrences:
            raise ValueError(
                f"edits[{index}].old_text matched {actual_occurrences} times; "
                f"expected {expected_occurrences}."
            )
        updated = updated.replace(old_text, new_text, expected_occurrences)

    changed = updated != content
    encoded = updated.encode("utf-8")
    if changed:
        _atomic_write(path=path, data=encoded)
    return {
        "path": requested_path,
        "resolved_path": path.as_posix(),
        "sha256_before": sha_before,
        "sha256_after": hashlib.sha256(encoded).hexdigest(),
        "bytes_written": len(encoded) if changed else 0,
        "changed": changed,
        "edit_count": len(edits),
    }


def _read_text_file(*, path: Path, max_file_bytes: int) -> tuple[str, bytes]:
    if not path.exists():
        raise FileNotFoundError(f"file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"path is not a file: {path}")
    size = path.stat().st_size
    if size > max_file_bytes:
        raise ValueError(f"file is too large: {size} bytes > {max_file_bytes} bytes.")
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError("binary files are not supported.")
    try:
        return raw.decode("utf-8"), raw
    except UnicodeDecodeError as exc:
        raise UnicodeError("file is not valid UTF-8 text.") from exc


def _atomic_write(*, path: Path, data: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
