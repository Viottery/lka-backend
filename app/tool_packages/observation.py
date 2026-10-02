"""Bounded access to cached tool results from the current Agent run."""

from __future__ import annotations

import json
from typing import Any

from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec

OBSERVATION_PACKAGE = ToolPackageSpec(
    name="observation",
    description="Read bounded portions of cached tool results from this run.",
    risk="low",
    requires_expansion=True,
    routing_hints=["Do not select this package as the initial data source; handles exist only after a tool runs in the current run."],
    decision_hints=["Expand only after an observation provides _result_cache.artifact_id; read relevant paths and continue with offset only when needed."],
)

_MAX_STRING_CHARS = 4000
_PREVIEW_ITEMS = 5
_PREVIEW_DEPTH = 3
_PAGE_PREVIEW_CHARS = 5_000


class ObservationReadTool:
    def __init__(self, store: Any) -> None:
        self.store = store

    spec = ToolSpec(
        name="observation.read",
        package="observation",
        type="local_tool",
        description=(
            "Read a bounded page from a cached tool_result artifact belonging to the current run. "
            "Path is a JSON Pointer rooted at the stored ToolResult dictionary."
        ),
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_db"],
        input_schema={
            "type": "object",
            "required": ["artifact_id"],
            "properties": {
                "artifact_id": {"type": "string"},
                "path": {"type": "string", "default": ""},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
        },
        output_schema={"path": "string", "value_type": "string"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        def rejected(message: str) -> ToolResult:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="rejected",
                error=message,
            )

        if not context.run_id:
            return rejected("A current run is required to read tool result artifacts.")
        if (
            context.tool_view is not None
            and context.tool_view.child_run_id is not None
            and context.tool_view.child_run_id != context.run_id
        ):
            return rejected("The child view and current run do not match.")
        artifact_id = invocation.input.get("artifact_id")
        path = invocation.input.get("path", "")
        offset = invocation.input.get("offset", 0)
        limit = invocation.input.get("limit", 5)
        if not isinstance(artifact_id, str) or not artifact_id:
            return rejected("artifact_id must be a non-empty string.")
        if not isinstance(path, str):
            return rejected("path must be a JSON Pointer string.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return rejected("offset must be an integer greater than or equal to zero.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            return rejected("limit must be an integer from 1 through 20.")

        payload = self.store.load_tool_result_artifact(artifact_id, context.run_id)
        if payload is None:
            return rejected("Tool result artifact was not found in the current run.")
        try:
            value = _resolve_pointer(payload, path)
        except (KeyError, IndexError, ValueError, TypeError):
            return rejected("path does not identify a value in the tool result.")

        output = {"path": path, "value_type": _value_type(value)}
        if isinstance(value, list):
            items: list[Any] = []
            used = 0
            for item in value[offset:offset + limit]:
                item_preview = _preview(item)
                size = len(json.dumps(item_preview, ensure_ascii=False, default=str))
                if items and used + size > _PAGE_PREVIEW_CHARS:
                    break
                if size > _PAGE_PREVIEW_CHARS:
                    item_preview = {"type": _value_type(item), "path": f"{path}/{offset + len(items)}",
                                    "preview_omitted": True}
                    size = len(json.dumps(item_preview))
                items.append(item_preview)
                used += size
            end = offset + len(items)
            output.update({
                "items": items,
                "total": len(value),
                "has_more": end < len(value),
                "next_offset": end if end < len(value) else None,
            })
        elif isinstance(value, dict):
            keys = list(value.keys())
            entries: dict[str, Any] = {}
            used = 0
            for key in keys[offset:offset + limit]:
                item_preview = _preview(value[key])
                size = len(json.dumps({key: item_preview}, ensure_ascii=False, default=str))
                if entries and used + size > _PAGE_PREVIEW_CHARS:
                    break
                if size > _PAGE_PREVIEW_CHARS:
                    item_preview = {"type": _value_type(value[key]), "preview_omitted": True}
                    size = len(json.dumps({key: item_preview}))
                entries[key] = item_preview
                used += size
            end = offset + len(entries)
            page_keys = list(entries)
            output.update({
                "entries": entries,
                "total": len(keys),
                "keys": page_keys,
                "has_more": end < len(keys),
                "next_offset": end if end < len(keys) else None,
            })
        elif isinstance(value, str):
            end = min(offset + _MAX_STRING_CHARS, len(value))
            output.update({
                "text": value[offset:end],
                "total": len(value),
                "has_more": end < len(value),
                "next_offset": end if end < len(value) else None,
            })
        else:
            output["value"] = value

        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=output,
        )


def _resolve_pointer(root: dict[str, Any], pointer: str) -> Any:
    if pointer == "":
        return root
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must start with '/'.")
    value: Any = root
    for raw_token in pointer[1:].split("/"):
        token_chars: list[str] = []
        index = 0
        while index < len(raw_token):
            char = raw_token[index]
            if char == "~":
                if index + 1 >= len(raw_token) or raw_token[index + 1] not in "01":
                    raise ValueError("Invalid JSON Pointer escape.")
                token_chars.append("~" if raw_token[index + 1] == "0" else "/")
                index += 2
            else:
                token_chars.append(char)
                index += 1
        token = "".join(token_chars)
        if isinstance(value, dict):
            value = value[token]
        elif isinstance(value, list):
            if not token.isdecimal() or (len(token) > 1 and token.startswith("0")):
                raise IndexError("Invalid array index.")
            value = value[int(token)]
        else:
            raise TypeError("Cannot descend into scalar value.")
    return value


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    return "object"


def _preview(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_CHARS:
            return value
        return {"preview": value[:_MAX_STRING_CHARS], "total_chars": len(value), "truncated": True}
    if isinstance(value, list):
        return {
            "type": "array",
            "total": len(value),
            "preview": [_preview(item, depth + 1) for item in value[:_PREVIEW_ITEMS]] if depth < _PREVIEW_DEPTH else [],
            "truncated": len(value) > _PREVIEW_ITEMS or depth >= _PREVIEW_DEPTH and bool(value),
        }
    if isinstance(value, dict):
        keys = list(value)[:_PREVIEW_ITEMS]
        return {
            "type": "object",
            "total_keys": len(value),
            "preview": {
                key: _preview(value[key], depth + 1) for key in keys
            } if depth < _PREVIEW_DEPTH else {},
            "truncated": len(value) > _PREVIEW_ITEMS or depth >= _PREVIEW_DEPTH and bool(value),
        }
    return value
