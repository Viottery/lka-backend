"""Parse current Agent markdown run logs for eval assertions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


REQUIRED_LOG_SECTIONS = [
    "User Input",
    "Package Catalog",
    "Session Context Window",
    "Expanded Tools",
    "Decision Events",
    "Tool Events",
    "Progress Events",
    "Verification Warnings",
    "LLM Events",
    "Answer",
]


SECTION_RE = re.compile(r"^## (?P<name>.+?)\n\n(?P<body>.*?)(?=^## |\Z)", re.M | re.S)
FENCED_RE = re.compile(r"```(?P<kind>json|text)\n(?P<body>.*?)\n```", re.S)


def parse_agent_log(path: str | None) -> dict[str, Any]:
    """Return parsed section metadata and JSON payloads from a run log."""

    if not path:
        return {
            "path": None,
            "exists": False,
            "sections": {},
            "missing_sections": list(REQUIRED_LOG_SECTIONS),
            "json_parse_errors": [],
        }
    log_path = Path(path)
    if not log_path.exists():
        return {
            "path": str(log_path),
            "exists": False,
            "sections": {},
            "missing_sections": list(REQUIRED_LOG_SECTIONS),
            "json_parse_errors": [],
        }

    text = log_path.read_text(encoding="utf-8")
    sections: dict[str, Any] = {}
    json_parse_errors: list[str] = []
    for match in SECTION_RE.finditer(text):
        name = match.group("name").strip()
        body = match.group("body").strip()
        fenced = FENCED_RE.search(body)
        if not fenced:
            sections[name] = {"kind": "raw", "value": body}
            continue
        kind = fenced.group("kind")
        fenced_body = fenced.group("body")
        if kind == "json":
            try:
                value = json.loads(fenced_body)
            except json.JSONDecodeError as exc:
                value = None
                json_parse_errors.append(f"{name}: {exc}")
            sections[name] = {"kind": "json", "value": value}
        else:
            sections[name] = {"kind": "text", "value": fenced_body}

    missing = [section for section in REQUIRED_LOG_SECTIONS if section not in sections]
    return {
        "path": str(log_path),
        "exists": True,
        "sections": sections,
        "missing_sections": missing,
        "json_parse_errors": json_parse_errors,
    }
