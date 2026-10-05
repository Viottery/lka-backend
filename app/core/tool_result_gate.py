"""Deterministic, schema-agnostic views over JSON tool results."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import islice
from typing import Any

_DELIVERY_LIMIT = 24
_MISSING = object()


def _delivery_origin_valid(artifact_id: Any, content_hash: Any, path: Any) -> bool:
    return (
        isinstance(artifact_id, str) and 0 < len(artifact_id) <= 200
        and isinstance(content_hash, str) and len(content_hash) == 64
        and all(char in "0123456789abcdef" for char in content_hash)
        and isinstance(path, str) and len(path) <= 512
        and (not path or path.startswith("/"))
    )


def _delivery_pointer(value: Any, path: str) -> Any:
    if not isinstance(path, str) or len(path) > 1024 or (path and not path.startswith("/")):
        return _MISSING
    if not path:
        return value
    parts = path[1:].split("/")
    if len(parts) > _DELIVERY_LIMIT:
        return _MISSING
    for part in parts:
        # Only valid JSON Pointer escapes; do not normalize source identifiers.
        if any(part[index + 1:index + 2] not in ("0", "1")
               for index, char in enumerate(part) if char == "~"):
            return _MISSING
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(key, _MISSING)
        elif (
            isinstance(value, list) and key.isascii() and key.isdecimal() and len(key) <= 10
            and (key == "0" or not key.startswith("0"))
        ):
            index = int(key)
            value = value[index] if index < len(value) else _MISSING
        else:
            return _MISSING
    return value


def _delivery_ranges(ranges: list[list[int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if start == end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def bind_context_delivery(
    raw_payload: Any, view_payload: Any, artifact_id: str,
    content_hash: str, observation_id: str,
) -> list[dict[str, Any]]:
    """Bind a server projection of long cached strings, never an upstream source.

    view_payload is the entire observation envelope, including its result.
    Only exact strings or the exact preview() head/tail structure yield ranges.
    Walk/entry counts are bounded; untracked values cannot gain completeness.
    """
    if (
        not _delivery_origin_valid(artifact_id, content_hash, "")
        or not isinstance(observation_id, str) or not 0 < len(observation_id) <= 200
        or not isinstance(raw_payload, dict) or raw_payload.get("invocation_id") != observation_id
    ):
        return []
    bindings: list[dict[str, Any]] = []
    view_result = view_payload.get("result", _MISSING) if isinstance(view_payload, dict) else _MISSING
    stack = [(raw_payload, view_result, "", "/result")]
    visited = 0
    while stack and visited < _DELIVERY_LIMIT:
        raw, view, path, view_path = stack.pop()
        visited += 1
        if len(path) > 512 or len(view_path) > 1024:
            continue
        if isinstance(raw, str) and len(raw) >= 700:
            binding: dict[str, Any] = {
                "observation_id": observation_id, "artifact_id": artifact_id,
                "content_hash": content_hash, "path": path,
                "total_chars": len(raw), "fragments": [],
            }
            if isinstance(view, str) and view == raw:
                binding["fragments"] = [{
                    "view_path": view_path, "start": 0, "end": len(raw), "text": raw,
                }]
            elif isinstance(view, dict) and view == preview(raw, path=path):
                binding["fragments"] = [
                    {"view_path": pointer(view_path, field), "start": start,
                     "end": end, "text": raw[start:end]}
                    for field, (start, end) in (
                        ("head", (0, 350)), ("tail", (len(raw) - 150, len(raw))),
                    )
                ]
            elif view is not _MISSING:
                binding["projection_unknown"] = True
            bindings.append(binding)
        elif isinstance(raw, dict):
            room = _DELIVERY_LIMIT - visited - len(stack)
            children = [
                (item, view.get(key, _MISSING) if isinstance(view, dict) else _MISSING,
                 pointer(path, key), pointer(view_path, key))
                for key, item in islice(raw.items(), room)
            ]
            stack.extend(reversed(children))
        elif isinstance(raw, list):
            room = _DELIVERY_LIMIT - visited - len(stack)
            if isinstance(view, list):
                items, items_path = view, view_path
            elif (
                isinstance(view, dict) and view.get("_type") == "array"
                and type(view.get("total_count")) is int and view["total_count"] == len(raw)
                and isinstance(view.get("items"), list)
            ):
                items, items_path = view["items"], pointer(view_path, "items")
            else:
                items, items_path = [], view_path
            stack.extend(reversed([
                (item, items[index] if index < len(items) else _MISSING,
                 pointer(path, index), pointer(items_path, index))
                for index, item in islice(enumerate(raw), room)
            ]))
    return bindings


def summarize_context_delivery(
    *, prompt_payload: dict[str, Any], bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Check trusted bindings against the final fitted prompt, without mutations.

    Tool JSON, descriptive delivery fields, scan flags and cache handles are
    never authority. Matching is by invocation ID, not drifting array indexes.
    Complete means only this version of this cached string was delivered.
    """
    if not isinstance(bindings, list):
        return []
    bounded_bindings = bindings[:_DELIVERY_LIMIT]
    needed_ids = {
        binding["observation_id"] for binding in bounded_bindings
        if isinstance(binding, dict) and isinstance(binding.get("observation_id"), str)
        and 0 < len(binding["observation_id"]) <= 200
    }
    observations = prompt_payload.get("observations") if isinstance(prompt_payload, dict) else None
    by_id: dict[str, Any] = {}
    if isinstance(observations, list) and needed_ids:
        # Scan only ID fields throughout the actual fitted list. The last 24
        # observations need not contain the 24 bound IDs, and an earlier
        # duplicate/contradiction must not escape ambiguity detection.
        for observation in observations:
            result = observation.get("result") if isinstance(observation, dict) else None
            identity = observation.get("_observation_id") if isinstance(observation, dict) else None
            raw_identity = result.get("invocation_id") if isinstance(result, dict) else None
            if identity is None:
                identity = raw_identity
            if isinstance(identity, str) and 0 < len(identity) <= 200 and identity in needed_ids:
                by_id[identity] = (
                    _MISSING if identity in by_id or raw_identity not in (None, identity) else observation
                )
            if (
                isinstance(raw_identity, str) and 0 < len(raw_identity) <= 200
                and raw_identity in needed_ids and identity != raw_identity
            ):
                by_id[raw_identity] = _MISSING
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for binding in bounded_bindings:
        if not isinstance(binding, dict):
            continue
        artifact_id, content_hash, path = (binding.get(key) for key in ("artifact_id", "content_hash", "path"))
        total = binding.get("total_chars")
        if not _delivery_origin_valid(artifact_id, content_hash, path) or type(total) is not int or total < 0:
            continue
        group = groups.setdefault((artifact_id, content_hash, path), {
            "total": total, "ranges": [], "unknown": False, "seen_empty": False,
        })
        if total != group["total"]:
            group["unknown"] = True
            continue
        identity = binding.get("observation_id")
        if not isinstance(identity, str) or not 0 < len(identity) <= 200:
            group["unknown"] = True
            continue
        observation = by_id.get(identity)
        if observation is None:
            continue  # Evicted; an artifact continuation handle is not text.
        if observation is _MISSING:
            group["unknown"] = True  # Ambiguous duplicate invocation ID.
            continue
        if binding.get("projection_unknown") is not None and binding.get("projection_unknown") is not False:
            group["unknown"] = True
        fragments = binding.get("fragments")
        if not isinstance(fragments, list) or len(fragments) > _DELIVERY_LIMIT:
            group["unknown"] = True
            continue
        for fragment in fragments:
            if not isinstance(fragment, dict):
                group["unknown"] = True
                continue
            start, end, text, view_path = (fragment.get(key) for key in ("start", "end", "text", "view_path"))
            if (
                type(start) is not int or type(end) is not int or not 0 <= start <= end <= total
                or not isinstance(text, str) or len(text) != end - start
                or not isinstance(view_path, str) or not view_path.startswith("/result/")
            ):
                group["unknown"] = True
                continue
            visible = _delivery_pointer(observation, view_path)
            if visible is _MISSING:
                continue
            if not isinstance(visible, str) or visible != text:
                group["unknown"] = True
                continue
            group["ranges"].append([start, end])
            if total == 0:
                group["seen_empty"] = True
    summaries = []
    for (artifact_id, content_hash, path), group in groups.items():
        ranges = _delivery_ranges(group["ranges"])
        if len(ranges) > _DELIVERY_LIMIT:
            ranges = ranges[:_DELIVERY_LIMIT]
            group["unknown"] = True
        covered = sum(end - start for start, end in ranges)
        complete = covered == group["total"] and (group["total"] > 0 or group["seen_empty"])
        summaries.append({
            "scope": "cached_value", "artifact_id": artifact_id, "content_hash": content_hash,
            "path": path, "unit": "unicode_codepoints", "total_chars": group["total"],
            "ranges": ranges, "covered_chars": covered,
            "coverage": "unknown" if group["unknown"] else "complete" if complete else "partial",
            "upstream_coverage": "unknown",
        })
    return summaries


def pointer(parent: str, part: str | int) -> str:
    escaped = str(part).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}"


def preview(value: Any, *, path: str = "", depth: int = 0) -> Any:
    """Expose structure and stable paths without pretending a sample is complete."""
    if isinstance(value, str):
        if len(value) <= 700:
            return value
        return {
            "_type": "string", "path": path, "total_chars": len(value),
            "head": value[:350], "tail": value[-150:],
            "head_range": [0, 350], "tail_range": [len(value) - 150, len(value)],
            "omitted_chars": len(value) - 500, "_partial": True,
        }
    if isinstance(value, list):
        if depth >= 4:
            return {"_type": "array", "path": path, "total_count": len(value), "_partial": True}
        visible = min(len(value), 4)
        return {
            "_type": "array", "path": path, "total_count": len(value),
            "visible_count": visible, "omitted_count": len(value) - visible,
            "_partial": len(value) > visible,
            "items": [preview(item, path=pointer(path, i), depth=depth + 1)
                      for i, item in enumerate(value[:visible])],
        }
    if isinstance(value, dict):
        if depth >= 4:
            return {"_type": "object", "path": path, "total_keys": len(value), "_partial": True}
        keys = list(value)
        shown = keys[:12]
        result = {key: preview(value[key], path=pointer(path, key), depth=depth + 1)
                  for key in shown}
        if len(keys) > len(shown):
            result["_omitted_keys"] = len(keys) - len(shown)
            result["_path"] = path
            result["_partial"] = True
        return result
    return value


def needs_gate(value: Any) -> bool:
    """Use a cheap bounded walk; only large serialized results need a handle."""
    if len(json.dumps(value, ensure_ascii=False, default=str)) > 6_000:
        return True
    if isinstance(value, list):
        return len(value) > 20 or any(needs_gate(item) for item in value)
    if isinstance(value, dict):
        return any(needs_gate(item) for item in value.values())
    return isinstance(value, str) and len(value) > 4_000


def preview_text_fields(value: Any) -> Any:
    """Preview long strings without sampling structural/status fields.

    For previously accepted small results in a control working set, arrays and
    dictionaries must keep their status, gaps and counts. A bounded walk fails
    closed rather than omit unknown fields. Standard gate behavior is unchanged.
    """
    remaining = 512

    def visit(item: Any, path: str, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 32:
            raise ValueError("control text preview exceeds bounded walk")
        if isinstance(item, str):
            return preview(item, path=path)
        if isinstance(item, dict):
            return {key: visit(child, pointer(path, key), depth + 1) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child, pointer(path, index), depth + 1) for index, child in enumerate(item)]
        return item

    return visit(value, "", 0)


def bounded_preview(value: Any) -> Any:
    result = preview(value)
    if len(json.dumps(result, ensure_ascii=False, default=str)) <= 7_000:
        return result
    # Adversarially wide/nested output must not evict the cache handle itself.
    if isinstance(value, dict):
        return {
            "_type": "object", "path": "", "total_keys": len(value),
            "fields": [
                {"key": str(key)[:100], "path": pointer("", key),
                 "type": type(item).__name__,
                 "size": len(item) if isinstance(item, (str, list, dict)) else None}
                for key, item in list(value.items())[:20]
            ],
            "omitted_keys": max(0, len(value) - 20),
        }
    return {"_type": type(value).__name__, "path": "",
            "size": len(value) if isinstance(value, (str, list)) else None}


def historical_cache_status(record: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Describe a session cache as history; TTL never upgrades it to current evidence."""
    as_of = record.get("created_at")
    age_seconds: int | None = None
    age_status = "unknown_age"
    if isinstance(as_of, str):
        try:
            created = datetime.fromisoformat(as_of)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_seconds = max(0, int(((now or datetime.now(UTC)) - created).total_seconds()))
            ttl = record.get("ttl_seconds")
            if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl >= 0:
                age_status = "within_ttl" if age_seconds <= ttl else "expired"
            else:
                age_status = "unknown_ttl"
        except (ValueError, OverflowError):
            age_status = "invalid_timestamp"
    source_version = record.get("source_version")
    freshness = (
        "unknown_source_version"
        if not isinstance(source_version, str) or not source_version
        else "source_version_not_revalidated"
    )
    return {
        "as_of": as_of,
        "age_seconds": age_seconds,
        "age_status": age_status,
        "freshness": freshness,
        "source_version": source_version,
        "cache_version": record.get("cache_version"),
        "permission_version": record.get("permission_version"),
        "workspace_version": record.get("workspace_version"),
        "run_id": record.get("run_id"),
        "current_evidence": False,
        "historical_only": True,
        "replay_policy": "historical_only",
    }


def cache_scope_compatible(
    cached: dict[str, Any],
    current: dict[str, Any],
    *,
    tool_name: str,
) -> bool:
    """Fail closed unless cached read scope is contained in the current grant."""
    old_scope = cached.get("scope")
    if not isinstance(old_scope, dict):
        return False
    if cached.get("permission_version") != current.get("permission_version"):
        return False
    if tool_name not in set(current.get("allowed_tools", [])):
        return False

    if old_scope.get("full_data_authority") and not current.get("full_data_authority"):
        return False
    for field in ("allowed_source_ids", "allowed_account_ids"):
        old_values = set(old_scope.get(field, []))
        current_values = set(current.get(field, []))
        if not old_values.issubset(current_values):
            return False
        if not old_values and current_values and old_scope.get("full_data_authority"):
            return False

    if old_scope.get("workspace_sensitive"):
        if cached.get("workspace_version") != current.get("workspace_version"):
            return False
        if old_scope.get("workspace_root") != current.get("workspace_root"):
            return False
        old_paths = set(old_scope.get("allowed_paths", []))
        current_paths = set(current.get("allowed_paths", []))
        if not old_paths.issubset(current_paths):
            return False
        if not old_paths and old_scope.get("full_workspace_authority"):
            return False
    return True
