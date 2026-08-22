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
