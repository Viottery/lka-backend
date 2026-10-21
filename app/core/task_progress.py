"""Bounded, evidence-linked working notes and soft review signals.

Accepted milestones are model declarations, never semantic verification. This
module neither authorizes operations nor decides when a task must terminate.
"""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ShortText = Annotated[str, Field(strict=True, max_length=240)]
EvidenceId = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
EvidenceDigest = Annotated[str, Field(strict=True, min_length=64, max_length=64)]
PurposeKey = Literal["gap", "question", "expected_observable"]

PURPOSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {name: {"type": "string", "maxLength": 240}
                   for name in ("gap", "question", "expected_observable")},
    "required": ["gap", "question", "expected_observable"],
}
WORKING_STATE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "remaining_requirements": {"type": "array", "maxItems": 32,
                                   "description": "Replace only when requirements change; omit unchanged requirements.",
                                   "items": {"type": "string", "maxLength": 240}},
        "focus_gap": {"type": "string", "maxLength": 240,
                      "description": "Omit unchanged gaps; use an empty string to clear a resolved gap."},
        "findings": {"type": "array", "maxItems": 32,
                     "description": "Only new evidence-linked notes; the server retains earlier findings.", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "enum": ["finding", "ruled_out", "change", "validation"]},
                "summary": {"type": "string", "maxLength": 240},
                "evidence_ids": {"type": "array", "maxItems": 8,
                                 "items": {"type": "string", "maxLength": 200}},
            }, "required": ["kind", "summary", "evidence_ids"],
        }},
    },
}
PROGRESS_POLICY = (
    "Return one next operation, with brief optional progress deltas rather than a new full plan. "
    "Omit unchanged working_state fields; earlier requirements and findings remain on the server. "
    "Clear a resolved focus_gap with an empty string and completed remaining_requirements with []. "
    "Send only newly established evidence-linked findings, not copies or rewordings of old notes. "
    "In JSON put working_state and purpose beside operation; an _agent_progress wrapper is also accepted. "
    "For native functions put both inside optional _agent_progress, never inside tool_input. "
    "Strict functions encode omitted notes as _agent_progress=null; unused note fields can be null. "
    "working_state has remaining_requirements (strings), focus_gap (string), and findings "
    "(objects with kind=finding|ruled_out|change|validation, summary, evidence_ids). "
    "Before a tool operation, purpose.gap, question and expected_observable need only a short clause each. Link findings to "
    "actual result.invocation_id values. A failed diagnostic or empty search can rule out "
    "a hypothesis; read_only is a safety property, not a progress category. When review_required, "
    "reconcile known findings and the concrete remaining gap, and choose a discriminating next "
    "operation, a justified alternative, or report an actual blocker. Rephrasing notes is not "
    "new evidence. Progress review is advisory: resolve it within normal decisions, not a separate "
    "field-only retry. It never postpones an otherwise valid operation, requires an edit, or "
    "forces an automatic final answer. Tool schema, scope and safety gates still apply. "
    "When session_progress_index provides a relevant local checkpoint, load its missing evidence "
    "through the supplied progress/observation handles before reacquiring the same source. "
    "Carry unfinished requirements into continuation; do not reconstruct already completed work "
    "from a short final answer alone. A failed run may still have successful operations. "
    "A versioned write response containing the updated record is evidence of that write; "
    "do not add a second verification solely for reassurance unless independent read-back is "
    "requested, the response is incomplete, or a specific conflict/freshness requirement exists. "
    "Every new tool call must resolve a concrete gap. A status read already included in a "
    "result, an unchanged source, or a differently worded note is not a new gap."
)


def native_progress_schema(*, strict: bool) -> dict:
    """Keep only the new metadata compatible with strict function schemas."""
    working = deepcopy(WORKING_STATE_SCHEMA)
    purpose = deepcopy(PURPOSE_SCHEMA)
    if strict:
        working["required"] = list(working["properties"])
        working["properties"] = {
            key: {"anyOf": [value, {"type": "null"}]}
            for key, value in working["properties"].items()
        }
    notes = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "working_state": {"anyOf": [working, {"type": "null"}]} if strict else working,
            "purpose": {"anyOf": [purpose, {"type": "null"}]} if strict else purpose,
        },
    }
    if strict:
        notes["required"] = list(notes["properties"])
        return {"anyOf": [notes, {"type": "null"}]}
    return notes


def _short(value: Any, limit: int = 240) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _specific(value: str) -> bool:
    # A structural guard only; this cannot prove the question is useful.
    return len(value) >= 4 and value.lower() not in {
        "continue", "inspect", "research", "继续检查", "继续研究", "进一步检查",
    }


def _purpose(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {key: _short(value.get(key)) for key in ("gap", "question", "expected_observable")}


def _digest(observation: dict) -> str:
    result = observation["result"]
    output = result.get("output")
    if isinstance(output, dict):
        output = {key: value for key, value in output.items()
                  if key not in {"_result_cache", "_delivery_view"}}
        if ("stdout" in output or "stderr" in output) and all(
            isinstance(output.get(key, ""), str) for key in ("stdout", "stderr")
        ):
            # Generic terminal envelopes can differ while carrying exactly the
            # same diagnostic evidence (for example sed versus grep). Preserve
            # unrelated result fields, including domain times and versions.
            output = {key: value for key, value in output.items() if key not in {
                "command", "cwd", "workspace_root", "read_only", "mode",
                "session_id", "next_offset", "running",
            }}
            if isinstance(output.get("status"), str) and output["status"] in {"exited", "running", "timed_out"}:
                output.pop("status")
            combined = output.get("stdout", "") + output.get("stderr", "")
            if output.get("output") == combined:
                output.pop("output")
    # IDs and rewritten purposes/summaries do not change underlying evidence.
    payload = {"tool_name": observation.get("tool_name"), "status": result.get("status"),
               "output": output, "error": result.get("error")}
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invocation_id: EvidenceId
    digest: EvidenceDigest
    tool_call: int = Field(ge=0)


class ProgressFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["finding", "ruled_out", "change", "validation"]
    summary: ShortText
    evidence_ids: list[EvidenceId] = Field(max_length=8)


class TaskProgressState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    remaining_requirements: list[ShortText] = Field(default_factory=list, max_length=32)
    focus_gap: str = Field(default="", max_length=240)
    findings: list[ProgressFinding] = Field(default_factory=list, max_length=32)
    last_purpose: dict[PurposeKey, ShortText] = Field(default_factory=dict, max_length=3)
    evidence: list[EvidenceRecord] = Field(default_factory=list, max_length=128)
    consumed_digests: list[EvidenceDigest] = Field(default_factory=list, max_length=256)
    tool_calls: int = Field(default=0, ge=0)
    calls_since_milestone: int = Field(default=0, ge=0)
    last_milestone_call: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    review_required: bool = False
    review_reasons: list[Literal["no_accepted_milestone", "periodic_review", "duplicate_operation"]] = Field(
        default_factory=list, max_length=3,
    )
    review_count: int = Field(default=0, ge=0)
    review_deferrals: int = Field(default=0, ge=0, le=1)

    def _review(self, reason: str) -> None:
        self.review_required = True
        if reason not in self.review_reasons:
            self.review_reasons = [*self.review_reasons, reason][-3:]

    def observe(self, observation: dict, purpose: dict | None = None) -> None:
        """Register only a current-run executed result supplied by the executor.

        Historical context must not be registered here. Tool failures count and
        may provide useful negative evidence; feedback without an invocation does not.
        """
        result = observation.get("result")
        if observation.get("historical_only") or not isinstance(result, dict):
            return
        identifier = result.get("invocation_id")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
            return
        if any(item.invocation_id == identifier for item in self.evidence):
            return
        self.tool_calls += 1
        self.calls_since_milestone += 1
        self.last_purpose = _purpose(purpose)
        self.evidence = [*self.evidence, EvidenceRecord(
            invocation_id=identifier, digest=_digest(observation), tool_call=self.tool_calls,
        )][-128:]
        if self.calls_since_milestone >= 3:
            self._review("no_accepted_milestone")
        if self.tool_calls % 8 == 0:
            self._review("periodic_review")

    def duplicate(self) -> None:
        self.duplicate_count += 1
        self._review("duplicate_operation")

    def defer_review_once(self) -> bool:
        if self.review_deferrals:
            return False
        self.review_deferrals = 1
        return True

    def apply_decision(self, decision: dict, observations: list) -> dict:
        """Accept bounded notes backed by the executor's registered evidence."""
        diagnostics: dict[str, Any] = {"accepted_findings": 0, "rejected_findings": 0,
                                       "milestone_accepted": False, "review_responded": False}
        working = decision.get("working_state")
        working = working if isinstance(working, dict) else {}
        requirements = working.get("remaining_requirements")
        if isinstance(requirements, list):
            self.remaining_requirements = list(dict.fromkeys(
                text for item in requirements[:32] if (text := _short(item))
            ))
        gap = _short(working.get("focus_gap"))
        if isinstance(working.get("focus_gap"), str):
            self.focus_gap = gap
        purpose = _purpose(decision.get("purpose"))
        if purpose:
            self.last_purpose = purpose
        if self.review_required and _specific(gap) and all(
            _specific(purpose.get(key, "")) for key in ("gap", "question", "expected_observable")
        ):
            self.review_count += 1
            self.review_required = False
            self.review_reasons = []
            diagnostics["review_responded"] = True
        ledger = {item.invocation_id: item for item in self.evidence}
        available = set()
        for obs in observations:
            if not isinstance(obs, dict) or obs.get("historical_only"):
                continue
            cache = obs.get("_cache")
            if isinstance(cache, dict) and cache.get("historical_only"):
                continue
            result = obs.get("result")
            raw_id = result.get("invocation_id") if isinstance(result, dict) else None
            identifier = obs.get("_observation_id", raw_id)
            # A wide structural preview may omit result.invocation_id. Its
            # server-side observation identity still needs current-run raw
            # registration; a conflicting raw identity invalidates the binding.
            if (isinstance(identifier, str) and identifier in ledger
                    and raw_id in (None, identifier)):
                available.add(identifier)
        proposals = working.get("findings")
        for raw in proposals[:32] if isinstance(proposals, list) else []:
            raw = raw if isinstance(raw, dict) else {}
            kind, summary, refs = raw.get("kind"), _short(raw.get("summary")), raw.get("evidence_ids")
            valid_refs = isinstance(refs, list) and 1 <= len(refs) <= 8 and all(
                isinstance(ref, str) and ref in available and ref in ledger for ref in refs
            )
            if not isinstance(kind, str) or kind not in {"finding", "ruled_out", "change", "validation"} or not summary or not valid_refs:
                diagnostics["rejected_findings"] += 1
                continue
            records = [ledger[ref] for ref in refs]
            fresh = [item for item in records if item.digest not in self.consumed_digests
                     and item.tool_call > self.last_milestone_call]
            if not _specific(gap) or not fresh or len(self.consumed_digests) + len(fresh) > 256:
                diagnostics["rejected_findings"] += 1
                continue
            self.findings = [*self.findings, ProgressFinding(
                kind=kind, summary=summary, evidence_ids=list(dict.fromkeys(refs)),
            )][-32:]
            self.consumed_digests = list(dict.fromkeys([
                *self.consumed_digests, *(item.digest for item in records),
            ]))[:256]
            self.last_milestone_call = max(item.tool_call for item in fresh)
            self.calls_since_milestone = self.tool_calls - self.last_milestone_call
            self.review_deferrals = 0
            diagnostics["accepted_findings"] += 1
            diagnostics["milestone_accepted"] = True
        diagnostics.update(review_required=self.review_required,
                           calls_since_milestone=self.calls_since_milestone,
                           scope="model_declared_not_semantic_verification")
        return diagnostics

    def context(self, remaining_steps: int | None = None, remaining_seconds: float | None = None) -> dict:
        return {
            "schema_version": 1, "scope": "model_declared_not_semantic_verification",
            "remaining_requirements": self.remaining_requirements, "focus_gap": self.focus_gap,
            "findings": [item.model_dump(mode="json") for item in self.findings],
            "last_purpose": self.last_purpose, "tool_calls": self.tool_calls,
            "calls_since_milestone": self.calls_since_milestone,
            "duplicate_count": self.duplicate_count, "review_required": self.review_required,
            "review_reasons": self.review_reasons, "review_count": self.review_count,
            "review_deferrals": self.review_deferrals,
            "remaining_steps": remaining_steps, "remaining_seconds": remaining_seconds,
        }
