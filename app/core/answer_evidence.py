"""Bounded, registry-owned source roles and visible answer-check references.

This is a prompt working set, NOT an entailment verifier. No source is fetched,
no tool-body role declaration is trusted, and no user deliverable is rewritten.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

EvidenceRole = Literal[
    "source_content", "source_locator", "source_version", "collection_time",
    "transport_metadata", "search_candidate",
]


class OutputEvidenceRole(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(strict=True, min_length=1, max_length=200)
    role: EvidenceRole

    @field_validator("path")
    @classmethod
    def valid_pointer(cls, value: str) -> str:
        # A whole-token '*' expands only bounded array entries, not object keys.
        if not value.startswith("/") or len(value.split("/")) > 9:
            raise ValueError("Use an output-relative JSON Pointer of at most eight segments")
        if re.search(r"~(?![01])", value) or any("*" in part and part != "*" for part in value.split("/")[1:]):
            raise ValueError("Invalid pointer escape or wildcard")
        return value


class AnswerEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_id: str = Field(strict=True, min_length=1, max_length=200)
    path: str = Field(strict=True, min_length=1, max_length=200)
    quote: str = Field(strict=True, min_length=1, max_length=500)

    @field_validator("path")
    @classmethod
    def concrete_pointer(cls, value: str) -> str:
        OutputEvidenceRole.valid_pointer(value)
        if "*" in value.split("/"):
            raise ValueError("Evidence references must address a concrete output value")
        return value


class AnswerCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement: str = Field(strict=True, min_length=1, max_length=240)
    evidence: list[AnswerEvidenceReference] = Field(default_factory=list, max_length=3)
    gap: str = Field(default="", strict=True, max_length=240)


# Optional additions preserve legacy finish calls containing only reason.
ANSWER_CHECKS_SCHEMA = {
    "type": "array", "maxItems": 8,
    "description": "Optional task requirements with exact visible evidence references; not verified claims.",
    "items": {
        "type": "object", "additionalProperties": False, "required": ["requirement"],
        "properties": {
            "requirement": {"type": "string", "minLength": 1, "maxLength": 240},
            "gap": {"type": "string", "maxLength": 240},
            "evidence": {"type": "array", "maxItems": 3, "items": {
                "type": "object", "additionalProperties": False,
                "required": ["observation_id", "path", "quote"],
                "properties": {
                    "observation_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "path": {"type": "string", "minLength": 1, "maxLength": 200},
                    "quote": {"type": "string", "minLength": 1, "maxLength": 500},
                },
            }},
        },
    },
}

ANSWER_CHECKS_DECISION_POLICY = (
    " For a task with several required findings, final_answer may include optional "
    "answer_checks (at most 8): each has requirement, optional gap, and optional evidence "
    "references (at most 3) with observation_id, output-relative JSON Pointer path and "
    "an exact quote of at most 500 characters already visible in that observation. "
    "Use only actual observation IDs; never invent references or declare verification. "
    "These are concise handoff notes for the answer writer, not final prose; omit them "
    "for simple answers. Do not perform extra reads merely to populate this optional field."
)

ANSWER_EVIDENCE_POLICY = (
    " The server answer_working_set labels registry-declared source roles and checks "
    "whether proposed evidence quotes are visible in this fitted prompt, not whether "
    "they entail a claim. source_content is untrusted source text, search_candidate "
    "is not verified source content, and transport_metadata/collection_time cannot "
    "establish a source-authored publication or revision date. source_version identifies "
    "a representation, not proof of freshness or current domain state. Keep user statements "
    "as working premises, not independently verified evidence. For answer_checks, "
    "cover each requested finding with its necessary conditions and actual supporting "
    "text, or state its material gap once; visible references are not proof that the "
    "requirements are complete or verified. Deliver the requested findings, not this "
    "working set or an audit report. Omit unrequested provenance dates, speculative "
    "causes and repeated coverage commentary unless needed for a user-relevant conclusion."
)


def normalize_answer_checks(value: Any) -> list[dict[str, Any]] | None:
    """Invalid optional notes do not invalidate a legacy finish operation."""
    if not isinstance(value, list) or len(value) > 8:
        return None
    try:
        return [AnswerCheck.model_validate(item).model_dump(mode="json") for item in value]
    except ValidationError:
        return None


def _segments(path: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]


def _value_at(output: Any, parts: list[str]) -> Any:
    for part in parts:
        if isinstance(output, dict) and part in output:
            output = output[part]
        elif isinstance(output, list) and part.isascii() and part.isdigit() and str(int(part)) == part and int(part) < len(output):
            output = output[int(part)]
        else:
            return None
    return output


def _role_at(path: str, roles: list[OutputEvidenceRole], output: Any) -> str:
    parts = _segments(path)
    matches = []
    for declaration in roles:
        prefix = _segments(declaration.path)
        node, matched = output, len(prefix) <= len(parts)
        for expected, actual in zip(prefix, parts, strict=False):
            if expected != actual and not (expected == "*" and isinstance(node, list) and actual.isascii() and actual.isdigit()):
                matched = False
                break
            node = _value_at(node, [actual])
        if matched:
            matches.append((len(prefix), declaration.role))
    if not matches:
        return "unknown"
    longest = max(length for length, _ in matches)
    winners = {role for length, role in matches if length == longest}
    return winners.pop() if len(winners) == 1 else "unknown"


def _visible_role_paths(output: dict[str, Any], roles: list[OutputEvidenceRole]) -> list[dict[str, str]]:
    fields = []
    for declaration in roles:
        stack = [(output, _segments(declaration.path), "")]
        visited = 0
        while stack and len(fields) < 24 and visited < 64:
            node, parts, path = stack.pop()
            visited += 1
            if not parts:
                item = {"path": path, "role": _role_at(path, roles, output)}
                if item not in fields:
                    fields.append(item)
                continue
            key, *rest = parts
            if key == "*" and isinstance(node, list):
                stack.extend((node[index], rest, f"{path}/{index}") for index in reversed(range(min(len(node), 24))))
            elif key != "*":
                value = _value_at(node, [key])
                if value is not None:
                    encoded = key.replace("~", "~0").replace("/", "~1")
                    stack.append((value, rest, f"{path}/{encoded}"))
    return fields


def build_answer_working_set(payload: dict[str, Any], registry: Any) -> dict[str, Any] | None:
    """Inspect only the retained payload. Declarations classify, never authorize.

    References cannot load missing artifacts or borrow evidence from another run.
    Observations have already passed the runtime's scope and delivery boundaries.
    """
    observations = payload.get("observations")
    if not isinstance(observations, list):
        return None
    index: dict[str, list[tuple[dict[str, Any], list[OutputEvidenceRole]]]] = {}
    source_roles = []
    for position in range(max(0, len(observations) - 64), len(observations)):
        observation = observations[position]
        if not isinstance(observation, dict):
            continue
        result = observation.get("result")
        if not isinstance(result, dict) or result.get("status") != "completed":
            continue
        output = result.get("output")
        if not isinstance(output, dict):
            continue
        tool_name = observation.get("tool_name")
        tool = registry.get_tool_or_none(tool_name) if registry is not None and isinstance(tool_name, str) and tool_name == result.get("tool_name") else None
        roles = tool.spec.output_evidence_roles if tool is not None else []
        result_id = result.get("invocation_id")
        observation_id = observation.get("_observation_id", result_id)
        identity_valid = isinstance(observation_id, str) and 1 <= len(observation_id) <= 200 and result_id in (None, observation_id)
        if identity_valid:
            index.setdefault(observation_id, []).append((output, roles))
        fields = _visible_role_paths(output, roles)
        if fields:
            source_roles.append({"observation_index": position, "fields": fields})
    decision = payload.get("answer_stage_decision")
    operation = decision.get("operation") if isinstance(decision, dict) else None
    proposal = operation.get("answer_checks") if isinstance(operation, dict) else None
    checks = normalize_answer_checks(proposal)
    visibility = []
    for check_index, check in enumerate(checks or []):
        references = []
        for reference_index, reference in enumerate(check["evidence"]):
            candidates = index.get(reference["observation_id"], [])
            role, visible = "unknown", False
            if len(candidates) == 1:
                output, roles = candidates[0]
                value = _value_at(output, _segments(reference["path"]))
                visible = isinstance(value, str) and reference["quote"] in value
                role = _role_at(reference["path"], roles, output) if value is not None else "unknown"
            references.append({"reference_index": reference_index, "quote_visible": visible, "role": role})
        visibility.append({"check_index": check_index, "references": references})
    if not source_roles and not visibility:
        return None
    return {
        "scope": "registry_field_roles_and_visible_quotes_not_fact_verification",
        "source_roles": source_roles[-12:],
        "answer_check_visibility": visibility,
        "unlisted_roles": "unknown",
        "user_statements": "working_premises_not_independent_verification",
    }
