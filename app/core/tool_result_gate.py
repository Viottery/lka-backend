"""Deterministic, schema-agnostic views over JSON tool results."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


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
