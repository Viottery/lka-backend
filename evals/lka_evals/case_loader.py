"""Load benchmark suite definitions.

Suite files are JSON-compatible YAML. Keeping the first version JSON-compatible
avoids adding a YAML dependency while preserving a future-friendly `.yaml`
extension.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_suite(path: Path) -> dict[str, Any]:
    """Load a benchmark suite from a JSON-compatible YAML file."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Suite file must be JSON-compatible YAML in this first version: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Suite root must be an object: {path}")
    payload.setdefault("suite_id", path.stem)
    payload.setdefault("cases", [])
    if not isinstance(payload["cases"], list):
        raise ValueError(f"Suite cases must be a list: {path}")
    return payload


def select_suite_cases(
    cases: list[dict[str, Any]], *, case_ids: set[str] | None = None,
    exclude_case_ids: set[str] | None = None, tags: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Preserve suite order, reject typos, and never call zero tests a pass."""
    available: set[str] = set()
    available_tags: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or not case["case_id"]:
            raise ValueError("Suite cases require nonempty case_id strings")
        if case["case_id"] in available:
            raise ValueError(f"Duplicate suite case_id: {case['case_id']}")
        available.add(case["case_id"])
        labels = case.get("tags", [])
        if not isinstance(labels, list) or any(not isinstance(t, str) or not t for t in labels):
            raise ValueError(f"Invalid tags for {case['case_id']}")
        available_tags.update(labels)
    for name, selected, known in (("case", case_ids, available),
                                   ("excluded case", exclude_case_ids, available),
                                   ("tag", tags, available_tags)):
        if selected is not None and (missing := selected - known):
            raise ValueError(f"Unknown {name}: {', '.join(sorted(missing))}")
    selected = [case for case in cases
                if (case_ids is None or case["case_id"] in case_ids)
                and (exclude_case_ids is None or case["case_id"] not in exclude_case_ids)
                and (tags is None or bool(tags.intersection(case.get("tags", []))))]
    if not selected:
        raise ValueError("Empty evaluation selection; no tests executed and no pass established")
    return selected
