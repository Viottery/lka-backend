"""Exact bounded grouping over arrays in current-run cached tool results."""

from __future__ import annotations

import json
import math
from typing import Any

from app.core.tools import ToolContext, ToolInvocation, ToolResult, ToolSpec

_MAX_RECORDS = 100_000
_MAX_GROUPS = 5_000
_MAX_OUTPUT_BYTES = 5_500
_MAX_GROUP_KEY_CHARS = 1_024
_MAX_GROUP_KEY_BYTES = 3_000
_MAX_PATH_CHARS = 2_048
_MAX_SAMPLES = 2
_SAMPLE_STRING_CHARS = 256


class ObservationGroupTool:
    """Count exact typed scalar values in a cached array, then page the groups."""

    def __init__(self, store: Any) -> None:
        self.store = store

    spec = ToolSpec(
        name="observation.group",
        package="observation",
        type="local_tool",
        description=(
            "Group a raw cached array of objects by one shallow scalar field. Counts cover the "
            "complete scan before group paging. Missing values form their own group; non-object "
            "records and non-scalar field values are reported as skipped."
        ),
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_db"],
        input_schema={
            "type": "object",
            "required": ["artifact_id", "path", "field"],
            "properties": {
                "artifact_id": {"type": "string", "minLength": 1},
                "path": {"type": "string", "maxLength": _MAX_PATH_CHARS},
                "field": {"type": "string", "minLength": 1, "maxLength": 64},
                "sample_fields": {
                    "type": "array", "minItems": 1, "maxItems": 6,
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
        },
        output_schema={
            "path": "string", "field": "string", "total_records": "integer",
            "scanned": "integer", "groups_total": "integer", "groups": "array",
            "next_offset": "integer|null", "scan_complete": "boolean",
        },
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
            return rejected("A current run is required to group tool result artifacts.")
        if (
            context.tool_view is not None
            and context.tool_view.child_run_id is not None
            and context.tool_view.child_run_id != context.run_id
        ):
            return rejected("The child view and current run do not match.")

        tool_input = invocation.input
        artifact_id = tool_input.get("artifact_id")
        path = tool_input.get("path")
        field = tool_input.get("field")
        offset = tool_input.get("offset", 0)
        limit = tool_input.get("limit", 10)
        sample_fields = tool_input.get("sample_fields")
        if not isinstance(artifact_id, str) or not artifact_id:
            return rejected("artifact_id must be a non-empty string.")
        if not isinstance(path, str) or len(path) > _MAX_PATH_CHARS:
            return rejected(f"path must be a JSON Pointer string of at most {_MAX_PATH_CHARS} characters.")
        if len(json.dumps(path, ensure_ascii=False).encode("utf-8")) > 1_000:
            return rejected("path is too large to include in the bounded group output.")
        if not isinstance(field, str) or not 1 <= len(field) <= 64:
            return rejected("field must contain 1 through 64 characters.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return rejected("offset must be an integer greater than or equal to zero.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            return rejected("limit must be an integer from 1 through 20.")
        if "sample_fields" in tool_input and (
            not isinstance(sample_fields, list)
            or not 1 <= len(sample_fields) <= 6
            or any(not isinstance(item, str) or not 1 <= len(item) <= 64 for item in sample_fields)
            or len(set(sample_fields)) != len(sample_fields)
        ):
            return rejected("sample_fields must contain 1 through 6 distinct field names of at most 64 characters.")

        payload = self.store.load_tool_result_artifact(artifact_id, context.run_id)
        if payload is None:
            return rejected("Tool result artifact was not found in the current run.")
        try:
            records = _resolve_pointer(payload, path)
        except (KeyError, IndexError, ValueError, TypeError):
            return rejected("path does not identify a value in the tool result.")
        if not isinstance(records, list):
            return rejected("path must identify an array.")
        if len(records) > _MAX_RECORDS:
            return rejected(f"array exceeds the {_MAX_RECORDS} record limit; no partial counts returned.")

        grouped: dict[tuple[str, Any], dict[str, Any]] = {}
        skipped_non_object = 0
        skipped_non_scalar = 0
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                skipped_non_object += 1
                continue
            if field not in record:
                identity = ("missing", None)
                key = {"type": "missing"}
            else:
                value = record[field]
                scalar_type = _scalar_type(value)
                if scalar_type is None:
                    skipped_non_scalar += 1
                    continue
                if isinstance(value, str) and len(value) > _MAX_GROUP_KEY_CHARS:
                    return rejected(f"group key in field {field!r} exceeds {_MAX_GROUP_KEY_CHARS} characters.")
                identity = (scalar_type, value)
                key = {"type": scalar_type, "value": value}
            key_bytes = len(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if key_bytes > _MAX_GROUP_KEY_BYTES:
                return rejected("group key is too large to return within the bounded output limit.")
            group = grouped.get(identity)
            if group is None:
                if len(grouped) >= _MAX_GROUPS:
                    return rejected(f"array exceeds the {_MAX_GROUPS} group limit; no partial counts returned.")
                group = {"key": key, "count": 0, "samples": []}
                grouped[identity] = group
            group["count"] += 1
            if sample_fields is not None and len(group["samples"]) < _MAX_SAMPLES:
                group["samples"].append(_sample_record(record, sample_fields, index))

        groups = sorted(grouped.values(), key=_group_sort_key)
        groups_total = len(groups)
        selected = groups[offset:offset + limit]
        output: dict[str, Any] = {
            "path": path,
            "field": field,
            "total_records": len(records),
            "scanned": len(records),
            "skipped_non_object": skipped_non_object,
            "skipped_non_scalar": skipped_non_scalar,
            "groups_total": groups_total,
            "groups": [],
            "scan_complete": True,
        }
        shown_samples = [_render_group(group, sample_fields) for group in selected]
        visible_count = len(selected)
        while True:
            output["groups"] = shown_samples[:visible_count]
            next_offset = offset + visible_count
            output["next_offset"] = next_offset if next_offset < groups_total else None
            if _serialized_size(output) <= _MAX_OUTPUT_BYTES:
                break
            removable = next(
                (
                    i for i in range(visible_count - 1, -1, -1)
                    if shown_samples[i].get("samples")
                ),
                None,
            )
            if removable is not None:
                shown_samples[removable]["samples"].pop()
                shown_samples[removable]["samples_omitted"] += 1
            elif visible_count > 1:
                visible_count -= 1
            else:
                return rejected("A group key cannot fit within the bounded output limit.")

        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=output,
        )


def _scalar_type(value: Any) -> str | None:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float" if math.isfinite(value) else None
    if isinstance(value, str):
        return "string"
    return None


def _group_sort_key(group: dict[str, Any]) -> tuple[Any, ...]:
    key = group["key"]
    kind = key["type"]
    order = {"missing": 0, "null": 1, "bool": 2, "int": 3, "float": 4, "string": 5}
    value = key.get("value")
    stable_value = value if kind in {"bool", "int", "float", "string"} else ""
    return (-group["count"], order[kind], stable_value)


def _sample_record(record: dict[str, Any], fields: list[str], index: int) -> dict[str, Any]:
    selected = {name: _bounded_value(record[name]) for name in fields if name in record}
    missing = [name for name in fields if name not in record]
    return {"index": index, "fields": selected, "missing_fields": missing}


def _bounded_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= _SAMPLE_STRING_CHARS:
            return value
        return {"value": value[:_SAMPLE_STRING_CHARS], "truncated": True}
    if isinstance(value, (dict, list)):
        return {"_type": "object" if isinstance(value, dict) else "array", "preview_omitted": True}
    if _scalar_type(value) is not None:
        return value
    return {"_type": type(value).__name__, "preview_omitted": True}


def _render_group(group: dict[str, Any], sample_fields: list[str] | None) -> dict[str, Any]:
    samples = list(group["samples"])
    result = {"key": group["key"], "count": group["count"]}
    if sample_fields is not None:
        result["samples"] = samples
        result["samples_omitted"] = 0
    return result


def _serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _resolve_pointer(root: Any, pointer: str) -> Any:
    if pointer == "":
        return root
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must start with '/'.")
    value = root
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
            raise TypeError("Cannot descend into scalar JSON value.")
    return value
