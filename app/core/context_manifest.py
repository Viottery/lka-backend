"""Content-free audit facts for a fitted model request."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from app.core.prompt_tokens import PromptTokenCounter

_SECTION_NAMES = frozenset({
    "system_prompt", "user_prompt", "messages", "session_context_window",
    "session_history", "memory", "guidance", "knowledge", "tool_results",
    "observations", "instructions", "task", "project", "mail", "messages_context",
    "web", "workspace", "projects", "recall", "other",
    "user_input", "route", "package_catalog", "expanded_tools", "working_state",
    "progress", "input_sources", "continuation_handles", "task_completion",
    "task_working_state", "_task_working_state", "route_context", "completed_tool_calls",
})
_MAX_SECTIONS = 16
_MAX_SECTION_BYTES = 32_768
_SAFE_BUDGET_KEYS = frozenset({
    "input_budget", "max_input_tokens", "output_reserve", "safety_margin",
    "available_tokens", "total_budget", "fit_attempt", "fit_round",
})
_SUMMARY_METHODS = frozenset({
    "atomic_records", "session_handoff", "conversation_intent", "local_emergency_view",
    "model", "local", "local_fallback", "unknown",
})


def _token_counter(counter: Any) -> Callable[[str], int]:
    if counter is None:
        counter = PromptTokenCounter()
    method = getattr(counter, "count_text", None)
    if callable(method):
        return lambda text: _count_value(method(text))
    if callable(counter):
        return lambda text: _count_value(counter(text))
    raise TypeError("token_counter must be callable or provide count_text")


def _count_value(value: Any) -> int:
    count = getattr(value, "count", value)
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("token counter must return an integer or count result")
    return max(0, count)


def _safe_budget(metadata: Mapping[str, Any] | None) -> dict[str, int | float | bool]:
    result = {}
    for key in _SAFE_BUDGET_KEYS:
        value = (metadata or {}).get(key)
        if isinstance(value, (bool, int, float)):
            result[key] = value
    return result


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError):
        return b""


def _session_facts(section: Any, count_text: Callable[[str], int]) -> dict[str, Any]:
    if not isinstance(section, Mapping):
        return {}
    messages = section.get("recent_messages")
    messages = messages if isinstance(messages, list) else []
    roles, seqs = [], []
    for message in messages[:4096]:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if isinstance(role, str) and role in {"user", "agent", "assistant", "tool", "system"}:
            roles.append(role)
        seq = message.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            seqs.append(seq)
    summary = section.get("summary")
    metadata = section.get("summary_metadata")
    recall = section.get("recalled_memories", section.get("recall"))
    recall = recall if isinstance(recall, Mapping) else {}
    components = {}
    for key in ("recent_messages", "summary", "agent_instructions", "recalled_memories",
                "cached_tool_observations"):
        value = section.get(key)
        if value is None:
            continue
        raw = _json_bytes(value)
        sample = raw[:8192].decode("utf-8", errors="ignore")
        components[key] = {"bytes": len(raw), "tokens": count_text(sample),
                           "token_estimate_capped": len(raw) > 8192}
    return {
        "recent_messages": {"count": len(messages), "roles": roles,
                            "min_seq": min(seqs) if seqs else None,
                            "max_seq": max(seqs) if seqs else None},
        "summary": {"kind": metadata.get("method") if isinstance(metadata, Mapping)
                    and isinstance(metadata.get("method"), str)
                    and metadata.get("method") in _SUMMARY_METHODS else "unknown",
                    "bytes": len(summary.encode("utf-8")) if isinstance(summary, str) else 0},
        "recall_counts": {
            "selected": len(recall["items"]) if isinstance(recall.get("items"), list)
            else _nonnegative_count(recall.get("selected_count")),
            "omitted": _nonnegative_count(recall.get("omitted_count")),
        },
        "components": components,
        "retained_from_seq": _nonnegative_count(metadata.get("retained_from_seq"))
        if isinstance(metadata, Mapping) else 0,
        "retained_to_seq": _nonnegative_count(metadata.get("retained_to_seq"))
        if isinstance(metadata, Mapping) else 0,
        "source_range_before_prompt_fit": bool(metadata.get("source_range_before_prompt_fit"))
        if isinstance(metadata, Mapping) else False,
    }


def _nonnegative_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def build_context_manifest(
    *, system_prompt: str, user_prompt: str, tools: Any = None,
    token_counter: Any = None, budget_metadata: Mapping[str, Any] | None = None,
    request_count: Any = None,
) -> dict[str, Any]:
    """Describe final request size and known payload sections without retaining content."""
    if not isinstance(system_prompt, str) or not isinstance(user_prompt, str):
        raise TypeError("prompts must be strings")
    count_text = _token_counter(token_counter)
    if request_count is not None:
        total = request_count
    elif hasattr(token_counter, "count_request"):
        total = token_counter.count_request(system_prompt, user_prompt, tools or [])
    elif callable(token_counter):
        tool_text = "".join(_json_bytes(tool).decode("utf-8", errors="ignore") for tool in (tools or []))
        total = count_text(system_prompt + user_prompt + tool_text)
    else:
        total = PromptTokenCounter().count_request(system_prompt, user_prompt, tools or [])
    try:
        parsed = json.loads(user_prompt)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    sections: dict[str, Any] = {}
    other_bytes = 0
    other_sample = bytearray()
    if isinstance(parsed, Mapping):
        for raw_key, value in parsed.items():
            name = raw_key if isinstance(raw_key, str) and raw_key in _SECTION_NAMES else "other"
            encoded = _json_bytes(value)
            if name == "other":
                other_bytes += len(encoded)
                remaining = _MAX_SECTION_BYTES - len(other_sample)
                if remaining > 0:
                    other_sample.extend(encoded[:remaining])
                continue
            if name not in sections and len(sections) >= _MAX_SECTIONS - 1:
                other_bytes += len(encoded)
                remaining = _MAX_SECTION_BYTES - len(other_sample)
                if remaining > 0:
                    other_sample.extend(encoded[:remaining])
                continue
            if name in sections:
                continue
            sample = encoded[:_MAX_SECTION_BYTES].decode("utf-8", errors="ignore")
            section: dict[str, Any] = {
                "bytes": len(encoded), "tokens": count_text(sample),
                "token_estimate_capped": len(encoded) > _MAX_SECTION_BYTES,
            }
            if name == "session_context_window":
                section.update(_session_facts(value, count_text))
            sections[name] = section
    if other_bytes:
        sections["other"] = {"bytes": other_bytes,
                              "tokens": count_text(other_sample.decode("utf-8", errors="ignore")),
                              "token_estimate_capped": other_bytes > _MAX_SECTION_BYTES}
    return {
        "request_tokens": _count_value(total),
        "request_count_method": getattr(total, "method", "provided_counter" if callable(token_counter) else "unknown"),
        "request_count_conservative": getattr(total, "conservative", None),
        "request_count_approximate": getattr(total, "approximate", False),
        "system_prompt_bytes": len(system_prompt.encode("utf-8")),
        "user_prompt_bytes": len(user_prompt.encode("utf-8")),
        "system_prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "user_prompt_sha256": hashlib.sha256(user_prompt.encode("utf-8")).hexdigest(),
        "tools_count": len(tools) if isinstance(tools, (list, tuple)) else 0,
        "budget": _safe_budget(budget_metadata),
        "sections": sections,
        "section_estimates_additive": False,
    }
