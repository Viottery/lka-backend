"""Bounded access to cached tool results from the current Agent run."""

from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from itertools import islice
from typing import Any

from app.core.tool_result_gate import preview
from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec

OBSERVATION_PACKAGE = ToolPackageSpec(
    name="observation",
    description="Search text, count groups, and read selected fields or bounded pages from cached results in this run.",
    risk="low",
    requires_expansion=True,
    routing_hints=["Do not select this package as the initial data source; handles exist only after a tool runs in the current run."],
    decision_hints=[
        "Expand only after an observation provides _result_cache.artifact_id; paths refer to the raw stored ToolResult, not its structural preview wrappers.",
        "Use search to locate literal terms in long cached text, then read its returned path/character offset for more context; a bounded search is not proof of full coverage.",
        "When repeated labels or links crowd search hits, use distinct_contexts=true to page through different text windows, then read from snippet_start. complete describes the cached text only, not an upstream truncated source.",
        "_delivery_view describes original-character windows of a cached value, not fetched or fully read upstream documents. Later prompt trimming can reduce that view further.",
        "For an array of records, use read with fields to retain the requested shallow keys rather than generic first-key previews. Follow next_offset to cover further records; report incomplete coverage when stopping early.",
        "For exact counts by a scalar field, use group on the cached raw array instead of adding counts from remembered previews. Group counts cover that entire array before group paging; they do not cover other artifacts or source pages. Follow next_offset to see every group and inspect skipped record counts.",
    ],
)

_MAX_STRING_CHARS = 4000
_PREVIEW_ITEMS = 5
_PREVIEW_DEPTH = 3
_PAGE_PREVIEW_CHARS = 5_000
_SEARCH_MAX_NODES = 10_000
_SEARCH_MAX_CHARS = 200_000
_SEARCH_MAX_PATH_CHARS = 512
_SEARCH_SNIPPET_CHARS = 500
_DELIVERY_LIMIT = 24
_DELIVERY_MISSING = object()


def _artifact_content_hash(payload: dict[str, Any]) -> str:
    # Exactly SqliteAgentRunStore's existing serialization; not a text-only hash.
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), default=str)
    return sha256(serialized.encode("utf-8")).hexdigest()


def _cached_text_description(
    artifact_id: str, content_hash: str, path: str, text: str,
    windows: list[list[int]], *, empty_delivered: bool = False,
) -> dict[str, Any]:
    ranges: list[list[int]] = []
    for start, end in sorted(windows):
        if start == end:
            continue
        if ranges and start <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])
    covered = sum(end - start for start, end in ranges)
    return {
        "scope": "cached_value", "artifact_id": artifact_id, "content_hash": content_hash,
        "path": path, "unit": "unicode_codepoints", "total_chars": len(text),
        "ranges": ranges, "covered_chars": covered,
        "coverage": "complete" if covered == len(text) and (text or empty_delivered) else "partial",
        "upstream_coverage": "unknown",
    }


def _projected_text_fragments(
    raw_text: str, view: Any, *, view_path: str, raw_path: str, source_start: int,
) -> tuple[list[dict[str, Any]], bool]:
    if view is _DELIVERY_MISSING:
        return [], False
    if isinstance(view, str) and view == raw_text:
        return [{"view_path": view_path, "start": source_start,
                 "end": source_start + len(raw_text), "text": raw_text}], False
    if isinstance(view, dict) and view == preview(raw_text, path=raw_path):
        return [{"view_path": f"{view_path}/{field}", "start": source_start + start,
                 "end": source_start + end, "text": raw_text[start:end]}
                for field, (start, end) in (
                    ("head", (0, 350)), ("tail", (len(raw_text) - 150, len(raw_text))),
                )], False
    return [], True


def _context_delivery_bindings(
    tool: Any, *, result_payload: dict[str, Any], view_payload: dict[str, Any], context: ToolContext,
    check_cancel: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Called only through a registered backend object; revalidate cached origin.

    Descriptive tool JSON is not trusted by core. This method reloads the owning
    run's artifact and proves every returned body window against that version.
    Missing/stale/malformed mapping returns no authority, never a generic fallback.
    """
    if (
        not context.run_id or not isinstance(result_payload, dict)
        or result_payload.get("status") != "completed"
        or result_payload.get("tool_name") != tool.spec.name
        or (context.tool_view is not None and context.tool_view.child_run_id is not None
            and context.tool_view.child_run_id != context.run_id)
        or (context.tool_view is not None and not context.tool_view.allows_tool(
            tool_name=tool.spec.name, package=tool.spec.package, read_only=True))
    ):
        return []
    identity = result_payload.get("invocation_id")
    output = result_payload.get("output")
    if not isinstance(identity, str) or not 0 < len(identity) <= 200 or not isinstance(output, dict):
        return []
    descriptions = output.get("_delivery_view")
    if not isinstance(descriptions, list) or len(descriptions) > _DELIVERY_LIMIT:
        return []
    view_result = view_payload.get("result") if isinstance(view_payload, dict) else None
    if (
        isinstance(view_payload, dict) and view_payload.get("_observation_id") not in (None, identity)
        or isinstance(view_result, dict) and view_result.get("invocation_id") not in (None, identity)
    ):
        return []
    view_output = view_result.get("output") if isinstance(view_result, dict) else None
    bindings: list[dict[str, Any]] = []
    cached_versions: dict[str, tuple[dict[str, Any], str]] = {}
    for description in descriptions:
        if not isinstance(description, dict):
            continue
        described_ranges = description.get("ranges")
        if (
            type(description.get("total_chars")) is not int
            or type(description.get("covered_chars")) is not int
            or not isinstance(described_ranges, list) or len(described_ranges) > _DELIVERY_LIMIT
            or any(not isinstance(interval, list) or len(interval) != 2
                   or any(type(index) is not int for index in interval)
                   for interval in described_ranges)
        ):
            continue
        artifact_id, path = description.get("artifact_id"), description.get("path")
        if (
            not isinstance(artifact_id, str) or not 0 < len(artifact_id) <= 200
            or not isinstance(path, str) or len(path) > 512
        ):
            continue
        if artifact_id not in cached_versions:
            if check_cancel is not None:
                check_cancel()
            cached = tool.store.load_tool_result_artifact(artifact_id, context.run_id)
            if check_cancel is not None:
                check_cancel()
            if cached is None:
                continue
            cached_versions[artifact_id] = cached, _artifact_content_hash(cached)
        cached, content_hash = cached_versions[artifact_id]
        if content_hash != description.get("content_hash"):
            continue
        try:
            text = _resolve_pointer(cached, path)
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if not isinstance(text, str):
            continue
        windows: list[list[int]] = []
        fragments: list[dict[str, Any]] = []
        unknown = False
        empty_delivered = False
        if isinstance(tool, ObservationReadTool) and output.get("value_type") == "string":
            if output.get("path") != path or type(output.get("total")) is not int or output["total"] != len(text):
                continue
            body = output.get("text")
            ranges = description.get("ranges")
            if not isinstance(body, str) or not isinstance(ranges, list) or len(ranges) > 1:
                continue
            if ranges:
                interval = ranges[0]
                if (
                    not isinstance(interval, list) or len(interval) != 2
                    or any(type(index) is not int for index in interval)
                    or not 0 <= interval[0] < interval[1] <= len(text)
                    or body != text[interval[0]:interval[1]]
                ):
                    continue
                start = interval[0]
                windows = [interval]
            elif not body:
                start = len(text)
            else:
                continue
            empty_delivered = not text
            view = view_output.get("text", _DELIVERY_MISSING) if isinstance(view_output, dict) else _DELIVERY_MISSING
            fragments, unknown = _projected_text_fragments(
                body, view, view_path="/result/output/text", raw_path="/output/text", source_start=start)
        elif isinstance(tool, ObservationSearchTool) and isinstance(output.get("matches"), list):
            matches = output["matches"]
            if len(matches) > _DELIVERY_LIMIT:
                continue
            projected = view_output.get("matches") if isinstance(view_output, dict) else None
            items_path = "/result/output/matches"
            if isinstance(projected, dict) and projected.get("_type") == "array" and isinstance(projected.get("items"), list):
                projected = projected["items"]
                items_path += "/items"
            valid = True
            for index, match in enumerate(matches):
                if not isinstance(match, dict) or match.get("path") != path:
                    continue
                start, end, body = (match.get(key) for key in ("snippet_start", "snippet_end", "snippet"))
                if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text)
                    or not isinstance(body, str) or body != text[start:end]):
                    valid = False
                    break
                windows.append([start, end])
                item = projected[index] if isinstance(projected, list) and index < len(projected) else None
                view = item.get("snippet", _DELIVERY_MISSING) if isinstance(item, dict) else _DELIVERY_MISSING
                new_fragments, changed = _projected_text_fragments(
                    body, view, view_path=f"{items_path}/{index}/snippet",
                    raw_path=f"/output/matches/{index}/snippet", source_start=start)
                fragments.extend(new_fragments)
                unknown = unknown or changed
            if not valid:
                continue
        else:
            continue
        expected = _cached_text_description(artifact_id, description["content_hash"], path, text,
                                            windows, empty_delivered=empty_delivered)
        if description != expected:
            continue
        binding = {"observation_id": identity, "artifact_id": artifact_id,
                   "content_hash": description["content_hash"], "path": path,
                   "total_chars": len(text), "fragments": fragments}
        if unknown:
            binding["projection_unknown"] = True
        bindings.append(binding)
    return bindings


class ObservationReadTool:
    def __init__(self, store: Any) -> None:
        self.store = store

    def context_delivery_bindings(self, *, result_payload, view_payload, context, check_cancel=None):
        return _context_delivery_bindings(
            self, result_payload=result_payload, view_payload=view_payload, context=context,
            check_cancel=check_cancel)

    spec = ToolSpec(
        name="observation.read",
        package="observation",
        type="local_tool",
        description=(
            "Read a bounded page from a cached tool_result artifact belonging to the current run. "
            "Path is a JSON Pointer rooted at the raw stored ToolResult dictionary, not its preview. "
            "For an array of records, fields selects relevant shallow keys. Offset is an array index, "
            "object-key index, or character offset for strings; inspect has_more and next_offset."
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
                "fields": {
                    "type": "array", "minItems": 1, "maxItems": 12,
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                    "description": "Optional shallow field projection for list items that are objects.",
                },
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
        fields = invocation.input.get("fields")
        if not isinstance(artifact_id, str) or not artifact_id:
            return rejected("artifact_id must be a non-empty string.")
        if not isinstance(path, str):
            return rejected("path must be a JSON Pointer string.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return rejected("offset must be an integer greater than or equal to zero.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            return rejected("limit must be an integer from 1 through 20.")
        if "fields" in invocation.input and (
            not isinstance(fields, list)
            or not 1 <= len(fields) <= 12
            or any(not isinstance(field, str) or not field or len(field) > 64 for field in fields)
            or len(set(fields)) != len(fields)
        ):
            return rejected("fields must contain 1 through 12 distinct non-empty field names of at most 64 characters.")

        payload = self.store.load_tool_result_artifact(artifact_id, context.run_id)
        if payload is None:
            return rejected("Tool result artifact was not found in the current run.")
        try:
            value = _resolve_pointer(payload, path)
        except (KeyError, IndexError, ValueError, TypeError):
            return rejected("path does not identify a value in the tool result.")
        if fields is not None and not isinstance(value, list):
            return rejected("fields projection requires path to identify an array.")

        output = {"path": path, "value_type": _value_type(value)}
        if isinstance(value, list):
            items: list[Any] = []
            used = 0
            for item in value[offset:offset + limit]:
                if fields is not None:
                    field_budget = max(64, min(500, 2_200 // len(fields)))
                    if isinstance(item, dict):
                        projected = {
                            field: _projection_value(item[field], field_budget)
                            for field in fields if field in item
                        }
                        missing = [field for field in fields if field not in item]
                        item_preview = {
                            "index": offset + len(items),
                            "fields": projected,
                            "missing_fields": missing,
                        }
                    else:
                        item_preview = {
                            "index": offset + len(items),
                            "item_type": _value_type(item),
                            "fields": {},
                            "missing_fields": fields,
                            "value": _projection_value(item, field_budget),
                        }
                else:
                    item_preview = _preview(item)
                size = len(json.dumps(item_preview, ensure_ascii=False, default=str))
                if items and used + size > _PAGE_PREVIEW_CHARS:
                    break
                if size > _PAGE_PREVIEW_CHARS:
                    if fields is not None:
                        item_preview = {
                            "index": offset + len(items),
                            "fields": {
                                field: {"_type": "omitted", "_partial": True}
                                for field in fields if isinstance(item, dict) and field in item
                            },
                            "missing_fields": [
                                field for field in fields if not isinstance(item, dict) or field not in item
                            ],
                            "preview_omitted": True,
                        }
                    else:
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
            if fields is not None:
                output["projected_fields"] = fields
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
                "_delivery_view": [_cached_text_description(
                    artifact_id, _artifact_content_hash(payload), path, value,
                    [[min(offset, len(value)), end]], empty_delivered=not value)],
            })
        else:
            output["value"] = value

        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=output,
        )


class ObservationSearchTool:
    """Find literal text in a bounded portion of a current-run cached result."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def context_delivery_bindings(self, *, result_payload, view_payload, context, check_cancel=None):
        return _context_delivery_bindings(
            self, result_payload=result_payload, view_payload=view_payload, context=context,
            check_cancel=check_cancel)

    spec = ToolSpec(
        name="observation.search",
        package="observation",
        type="local_tool",
        description=(
            "Search cached tool_result text from the current run using a case-insensitive literal. "
            "Returns JSON Pointer paths and character offsets for follow-up with observation.read. "
            "Use distinct_contexts=true to skip nearby hits well covered by the preceding snippet "
            "in the same string; offset/limit then count context windows rather than occurrences. "
            "Search is bounded; inspect complete and scan_limited before treating no matches as conclusive."
        ),
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_local_db"],
        input_schema={
            "type": "object",
            "required": ["artifact_id", "query"],
            "properties": {
                "artifact_id": {"type": "string"},
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "distinct_contexts": {"type": "boolean", "default": False},
                "path": {"type": "string", "default": ""},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
        },
        output_schema={
            "matches": "array of literal matches with JSON Pointer and character offsets",
            "complete": "boolean",
            "scan_limited": "boolean",
            "has_more": "boolean or null when unknown",
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
            return rejected("A current run is required to search tool result artifacts.")
        if (
            context.tool_view is not None
            and context.tool_view.child_run_id is not None
            and context.tool_view.child_run_id != context.run_id
        ):
            return rejected("The child view and current run do not match.")
        artifact_id = invocation.input.get("artifact_id")
        query = invocation.input.get("query")
        distinct_contexts = invocation.input.get("distinct_contexts", False)
        path = invocation.input.get("path", "")
        offset = invocation.input.get("offset", 0)
        limit = invocation.input.get("limit", 5)
        if not isinstance(artifact_id, str) or not artifact_id:
            return rejected("artifact_id must be a non-empty string.")
        if not isinstance(query, str) or not query or len(query) > 200:
            return rejected("query must be a non-empty string of at most 200 characters.")
        if not isinstance(distinct_contexts, bool):
            return rejected("distinct_contexts must be a boolean.")
        if not isinstance(path, str):
            return rejected("path must be a JSON Pointer string.")
        if len(path) > _SEARCH_MAX_PATH_CHARS or len(json.dumps(path, ensure_ascii=False)) > 512:
            return rejected("path exceeds the maximum searchable JSON Pointer length.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return rejected("offset must be an integer greater than or equal to zero.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            return rejected("limit must be an integer from 1 through 10.")

        payload = self.store.load_tool_result_artifact(artifact_id, context.run_id)
        if payload is None:
            return rejected("Tool result artifact was not found in the current run.")
        try:
            root = _resolve_pointer(payload, path)
        except (KeyError, IndexError, ValueError, TypeError):
            return rejected("path does not identify a value in the tool result.")

        folded_query = query.casefold()
        wanted = offset + limit + 1
        total_matches = 0
        matches: list[dict[str, Any]] = []
        delivery_values = {path: root} if isinstance(root, str) else {}
        nodes_scanned = 0
        nodes_scheduled = 1
        chars_scanned = 0
        scan_limited = False
        complete = False
        seen_containers: set[int] = set()
        stack: list[tuple[Any, str]] = [(root, path)]

        while stack:
            value, value_path = stack.pop()
            nodes_scanned += 1
            if nodes_scanned > _SEARCH_MAX_NODES:
                scan_limited = True
                break
            if isinstance(value, str):
                remaining = _SEARCH_MAX_CHARS - chars_scanned
                if remaining <= 0:
                    scan_limited = True
                    break
                scanned_text = value[:remaining]
                chars_scanned += len(scanned_text)
                for match in _literal_matches(scanned_text, folded_query, value_path,
                                              distinct_contexts=distinct_contexts):
                    if total_matches >= offset:
                        matches.append(match)
                        if len(delivery_values) < _DELIVERY_LIMIT:
                            delivery_values.setdefault(value_path, value)
                    total_matches += 1
                    if total_matches >= wanted:
                        break
                if total_matches >= wanted:
                    complete = False
                    break
                if len(scanned_text) < len(value):
                    scan_limited = True
                    break
            elif isinstance(value, (dict, list)):
                identity = id(value)
                if identity in seen_containers:
                    continue
                seen_containers.add(identity)
                remaining_nodes = _SEARCH_MAX_NODES - nodes_scheduled
                if remaining_nodes <= 0:
                    if len(value) > 0:
                        scan_limited = True
                    continue
                source = value.items() if isinstance(value, dict) else enumerate(value)
                raw_children = list(islice(source, remaining_nodes + 1))
                if len(raw_children) > remaining_nodes:
                    scan_limited = True
                    raw_children = raw_children[:remaining_nodes]
                children = [
                    (item, _pointer_child(value_path, key))
                    for key, item in raw_children
                ]
                if any(
                    len(child_path) > _SEARCH_MAX_PATH_CHARS
                    or len(json.dumps(child_path, ensure_ascii=False)) > 512
                    for _, child_path in children
                ):
                    scan_limited = True
                    children = [
                        (child, child_path)
                        for child, child_path in children
                        if len(child_path) <= _SEARCH_MAX_PATH_CHARS
                        and len(json.dumps(child_path, ensure_ascii=False)) <= 512
                    ]
                nodes_scheduled += len(children)
                stack.extend(reversed(children))
        else:
            complete = not scan_limited

        has_more: bool | None
        if len(matches) > limit:
            has_more = True
        elif complete:
            has_more = False
        else:
            has_more = None
        output = {
            "path": path,
            "query": query,
            "distinct_contexts": distinct_contexts,
            "matches": matches[:limit],
            "offset": offset,
            "next_offset": offset + len(matches[:limit]) if has_more is True else None,
            "has_more": has_more,
            "complete": complete,
            "scan_limited": scan_limited,
            "nodes_scanned": min(nodes_scanned, _SEARCH_MAX_NODES),
            "chars_scanned": chars_scanned,
            "total_matches": total_matches if complete else None,
        }
        # Distinct-context suppression was based on these full windows. Shrink
        # them afterwards and skipped neighbors can lose their only visible
        # context. Page fewer intact windows instead; legacy occurrence mode
        # retains its existing shrink behavior.
        while not distinct_contexts and len(json.dumps(output, ensure_ascii=False)) > 5_500 and any(
            len(match["snippet"]) > match["match_end"] - match["match_start"]
            for match in output["matches"]
        ):
            largest = max(
                output["matches"],
                key=lambda match: len(match["snippet"])
                - (match["match_end"] - match["match_start"]),
            )
            _shrink_snippet(largest)
        while len(json.dumps(output, ensure_ascii=False)) > 5_500 and len(output["matches"]) > 1:
            output["matches"].pop()
            output["has_more"] = True
            output["next_offset"] = offset + len(output["matches"])
            if distinct_contexts:
                output["complete"] = False
                output["total_matches"] = None
        # Preserve the existing literal windows and bounds. Descriptive metadata
        # may use leftover capacity, never evict evidence or rescan a wide object
        # that supplied no text window. Missing mappings remain non-authoritative.
        descriptions = output["_delivery_view"] = []
        delivered_values = {
            value_path: text for value_path, text in delivery_values.items()
            if value_path == path or any(match["path"] == value_path for match in output["matches"])
        }
        if delivered_values:
            content_hash = _artifact_content_hash(payload)
            for value_path, text in delivered_values.items():
                description = _cached_text_description(
                    artifact_id, content_hash, value_path, text,
                    [[match["snippet_start"], match["snippet_end"]]
                     for match in output["matches"] if match["path"] == value_path],
                )
                descriptions.append(description)
                if len(json.dumps(output, ensure_ascii=False)) > 5_500:
                    descriptions.pop()
        if len(json.dumps(output, ensure_ascii=False)) > 5_500:
            output.pop("_delivery_view")
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=output,
        )


def _pointer_child(parent: str, part: str | int) -> str:
    escaped = str(part).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}"


def _shrink_snippet(match: dict[str, Any]) -> None:
    start = match["snippet_start"]
    end = match["snippet_end"]
    match_start = match["match_start"]
    match_end = match["match_end"]
    target = max(match_end - match_start, len(match["snippet"]) - 25)
    left_context = (target - (match_end - match_start)) // 2
    new_start = max(start, match_start - left_context)
    new_end = min(end, new_start + target)
    if new_end < match_end:
        new_start = max(start, match_end - target)
        new_end = match_end
    match["snippet_start"] = new_start
    match["snippet_end"] = new_end
    match["snippet"] = match["snippet"][new_start - start:new_end - start]


def _literal_matches(text: str, folded_query: str, path: str, *, distinct_contexts: bool = False):
    """Map casefolded literal hits back to stable offsets in the original string."""
    folded_parts: list[str] = []
    source_indexes: list[int] = []
    for index, char in enumerate(text):
        folded_char = char.casefold()
        folded_parts.append(folded_char)
        source_indexes.extend([index] * len(folded_char))
    folded_text = "".join(folded_parts)
    cursor = 0
    covered_end = -1
    while True:
        start = folded_text.find(folded_query, cursor)
        if start < 0:
            break
        folded_end = start + len(folded_query)
        if folded_end > len(source_indexes):
            break
        source_start = source_indexes[start]
        source_end = source_indexes[folded_end - 1] + 1
        cursor = max(start + 1, folded_end)
        # Keep boundary hits: their following assertion may lie beyond the
        # preceding window even when the query token itself was visible there.
        if distinct_contexts and source_end + (_SEARCH_SNIPPET_CHARS // 4) <= covered_end:
            continue
        snippet_start = max(0, source_start - (_SEARCH_SNIPPET_CHARS // 2))
        snippet_end = min(len(text), snippet_start + _SEARCH_SNIPPET_CHARS)
        snippet_start = max(0, snippet_end - _SEARCH_SNIPPET_CHARS)
        covered_end = snippet_end
        yield {
            "path": path,
            "match_start": source_start,
            "match_end": source_end,
            "snippet_start": snippet_start,
            "snippet_end": snippet_end,
            "snippet": text[snippet_start:snippet_end],
        }


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


def _projection_value(value: Any, max_string_chars: int) -> Any:
    """Keep projected fields useful while making nested values explicitly partial."""
    if isinstance(value, str):
        if len(value) <= max_string_chars:
            return value
        return {
            "_type": "string",
            "preview": value[:max_string_chars],
            "total_chars": len(value),
            "truncated": True,
        }
    if isinstance(value, dict):
        return {"_type": "object", "total_keys": len(value), "_partial": bool(value)}
    if isinstance(value, list):
        return {"_type": "array", "total_items": len(value), "_partial": bool(value)}
    return value
