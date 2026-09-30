"""Read and select reviewed historical badcases without executing them.

Badcase manifests are explicitly curated JSON data. This module does not read
agent logs, resolve fixture paths, invoke a subject, or perform assertions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BADCASE_MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}$")
_METRIC = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ALLOWED_ROOT_KEYS = {"manifest_version", "manifest_id", "description", "cases"}
_ALLOWED_CASE_KEYS = {
    "case_id",
    "task",
    "agent_id",
    "executor_type",
    "severity",
    "tags",
    "fixture_ids",
    "privacy_reviewed",
    "checks",
}
_ALLOWED_CHECK_KEYS = {"metric", "operator", "value"}
_FORBIDDEN_KEYS = {
    "raw_log",
    "run_log",
    "log_path",
    "production_log_path",
    "prompt_dump",
    "trace_dump",
    "email_body",
}
_OPERATORS = {"eq", "gte", "lte", "contains", "not_contains"}


@dataclass(frozen=True)
class BadcaseCheck:
    metric: str
    operator: str
    value: str | int | float | bool


@dataclass(frozen=True)
class BadcaseCase:
    case_id: str
    task: str
    agent_id: str
    executor_type: str
    severity: str
    tags: tuple[str, ...]
    fixture_ids: tuple[str, ...]
    checks: tuple[BadcaseCheck, ...]


@dataclass(frozen=True)
class BadcaseManifest:
    manifest_version: int
    manifest_id: str
    description: str
    cases: tuple[BadcaseCase, ...]


def load_badcase_manifest(path: Path) -> BadcaseManifest:
    """Load one explicitly named, reviewed JSON manifest.

    This intentionally has no directory scanning or production-log import
    behavior. The caller must provide the manifest path directly.
    """

    try:
        size = path.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            raise ValueError(f"Badcase manifest exceeds {MAX_MANIFEST_BYTES} bytes: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Badcase manifest must be valid JSON: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Cannot read badcase manifest: {path}") from exc
    return validate_badcase_manifest(payload)


def validate_badcase_manifest(payload: Any) -> BadcaseManifest:
    """Validate privacy-reviewed manifest data and return immutable records."""

    if not isinstance(payload, dict):
        raise ValueError("Badcase manifest root must be an object")  # noqa: TRY004 - manifest validation API uses ValueError.
    _reject_keys(payload, _ALLOWED_ROOT_KEYS, "manifest")
    if payload.get("manifest_version") != BADCASE_MANIFEST_VERSION:
        raise ValueError(
            f"Unsupported badcase manifest_version: {payload.get('manifest_version')!r}"
        )
    manifest_id = _required_identifier(payload, "manifest_id", "manifest")
    description = payload.get("description", "")
    if not isinstance(description, str):
        raise ValueError("manifest.description must be a string")  # noqa: TRY004
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("manifest.cases must be an array")  # noqa: TRY004

    cases: list[BadcaseCase] = []
    seen: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        where = f"cases[{index}]"
        if not isinstance(raw_case, dict):
            raise ValueError(f"{where} must be an object")  # noqa: TRY004
        _reject_keys(raw_case, _ALLOWED_CASE_KEYS, where)
        case_id = _required_identifier(raw_case, "case_id", where)
        if case_id in seen:
            raise ValueError(f"Duplicate badcase case_id: {case_id}")
        seen.add(case_id)
        task = raw_case.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError(f"{where}.task must be a non-empty, manually sanitized string")
        if raw_case.get("privacy_reviewed") is not True:
            raise ValueError(f"{where}.privacy_reviewed must be true")
        agent_id = _required_identifier(raw_case, "agent_id", where)
        executor_type = _required_identifier(raw_case, "executor_type", where)
        severity = raw_case.get("severity")
        if severity not in {"low", "medium", "high", "critical"}:
            raise ValueError(f"{where}.severity must be low, medium, high, or critical")
        tags = _string_ids(raw_case.get("tags", []), f"{where}.tags")
        fixture_ids = _string_ids(raw_case.get("fixture_ids", []), f"{where}.fixture_ids")
        raw_checks = raw_case.get("checks")
        if not isinstance(raw_checks, list) or not raw_checks:
            raise ValueError(f"{where}.checks must be a non-empty array")
        checks = tuple(_parse_check(check, f"{where}.checks[{i}]") for i, check in enumerate(raw_checks))
        cases.append(
            BadcaseCase(
                case_id=case_id,
                task=task.strip(),
                agent_id=agent_id,
                executor_type=executor_type,
                severity=severity,
                tags=tags,
                fixture_ids=fixture_ids,
                checks=checks,
            )
        )
    return BadcaseManifest(
        manifest_version=BADCASE_MANIFEST_VERSION,
        manifest_id=manifest_id,
        description=description,
        cases=tuple(cases),
    )


def select_badcases(
    manifest: BadcaseManifest,
    *,
    case_ids: set[str] | None = None,
    tags: set[str] | None = None,
    agent_id: str | None = None,
    executor_type: str | None = None,
    limit: int | None = None,
) -> tuple[BadcaseCase, ...]:
    """Select cases deterministically in lexical case_id order.

    Tag matching is any-of. An explicit case ID that is absent is an error so
    typos cannot silently produce an incomplete regression selection.
    """

    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a non-negative integer")
    by_id = {case.case_id: case for case in manifest.cases}
    if case_ids is not None:
        missing = case_ids - by_id.keys()
        if missing:
            raise ValueError(f"Unknown badcase case_id(s): {', '.join(sorted(missing))}")
    selected = [
        case
        for case in manifest.cases
        if (case_ids is None or case.case_id in case_ids)
        and (tags is None or bool(tags.intersection(case.tags)))
        and (agent_id is None or case.agent_id == agent_id)
        and (executor_type is None or case.executor_type == executor_type)
    ]
    selected.sort(key=lambda case: case.case_id)
    if limit is not None:
        selected = selected[:limit]
    return tuple(selected)


def _parse_check(raw: Any, where: str) -> BadcaseCheck:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")  # noqa: TRY004
    _reject_keys(raw, _ALLOWED_CHECK_KEYS, where)
    metric = raw.get("metric")
    operator = raw.get("operator")
    value = raw.get("value")
    if not isinstance(metric, str) or not _METRIC.fullmatch(metric):
        raise ValueError(f"{where}.metric is invalid")
    if operator not in _OPERATORS:
        raise ValueError(f"{where}.operator must be one of {sorted(_OPERATORS)}")
    if isinstance(value, (dict, list)) or value is None or not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{where}.value must be a non-null scalar")
    return BadcaseCheck(metric=metric, operator=operator, value=value)


def _required_identifier(source: dict[str, Any], key: str, where: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{where}.{key} must be a valid identifier")
    return value


def _string_ids(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in value
    ):
        raise ValueError(f"{where} must be an array of valid identifiers")
    if len(value) != len(set(value)):
        raise ValueError(f"{where} must not contain duplicates")
    return tuple(value)


def _reject_keys(source: dict[str, Any], allowed: set[str], where: str) -> None:
    forbidden = _FORBIDDEN_KEYS.intersection(source)
    if forbidden:
        raise ValueError(f"{where} contains prohibited raw-data field(s): {', '.join(sorted(forbidden))}")
    extra = source.keys() - allowed
    if extra:
        raise ValueError(f"{where} contains unsupported field(s): {', '.join(sorted(extra))}")
