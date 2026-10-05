"""Bounded finish control over model-declared requirements, not semantic verification."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.answer_evidence import AnswerCheck, normalize_answer_checks


class CompletionRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_id: str = Field(strict=True, min_length=1, max_length=80)
    requirement: str = Field(strict=True, min_length=1, max_length=240)
    status: Literal["pending", "supported", "blocked"]
    gap: str = Field(strict=True, max_length=240)


class TaskCompletionState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    requirements: list[CompletionRequirement] = Field(default_factory=list, max_length=32)
    recovery_attempts: int = Field(default=0, ge=0, le=2)
    last_evidence_digest: str | None = None
    stop_reason: str | None = None

    def context(self) -> dict[str, Any] | None:
        if not self.requirements:
            return None
        return {
            "schema_version": self.schema_version,
            "scope": "model_declared_requirements_not_semantic_verification",
            "requirements": [
                {"requirement_id": item.requirement_id, "requirement": item.requirement,
                 "status": item.status, "gap": item.gap}
                for item in self.requirements
            ],
            "recovery_attempts": self.recovery_attempts,
            "stop_reason": self.stop_reason,
        }


def _identity(requirement: str) -> str:
    return "req_" + sha256(requirement.strip().encode("utf-8")).hexdigest()[:16]


def _evidence_digest(observations: list[dict[str, Any]]) -> str:
    # Control feedback, call IDs, progress prose and list ordering are not new
    # evidence. Only successful tool output can reopen a recovery opportunity.
    evidence = set()
    for observation in observations:
        result = observation.get("result")
        if not isinstance(result, dict) or result.get("status") != "completed":
            continue
        evidence.add(json.dumps({"tool_name": observation.get("tool_name"),
                                 "input": observation.get("input"), "output": result.get("output")},
                                sort_keys=True, ensure_ascii=False, default=str))
    return sha256("\n".join(sorted(evidence)).encode("utf-8")).hexdigest()


def assess_finish(
    state: TaskCompletionState, *, operation: Any, observations: list[dict[str, Any]],
    can_continue: bool, stop_reason: str = "decision_budget_exhausted",
) -> bool:
    """Return whether to enter answer; retain known gaps even if later omitted.

    Absence of optional notes preserves reason-only clients. A supported status
    is the model's assertion, never a server-authored factual certification.
    """
    proposal = operation.get("answer_checks") if isinstance(operation, dict) else None
    checks = normalize_answer_checks(proposal)
    existing = {item.requirement_id: item for item in state.requirements}
    by_text = {item.requirement.strip(): item.requirement_id for item in state.requirements}
    for raw in checks or []:
        item = AnswerCheck.model_validate(raw)
        identifier = by_text.get(item.requirement.strip()) or item.requirement_id or _identity(item.requirement)
        previous = existing.get(identifier)
        if previous is not None and previous.requirement.strip() != item.requirement.strip():
            # An ID cannot silently replace a different known requirement.
            continue
        if previous is None and len(existing) >= 32:
            state.stop_reason = "requirement_capacity_exhausted"
            break
        status = item.status or ("pending" if item.gap.strip() else "supported")
        if status == "supported" and item.gap.strip():
            status = "pending"
        gap = item.gap
        if status == "blocked" and not gap.strip():
            status, gap = "pending", "A blocked requirement needs an explicit reason."
        existing[identifier] = CompletionRequirement(
            requirement_id=identifier, requirement=item.requirement, status=status, gap=gap,
        )
        by_text[item.requirement.strip()] = identifier
    state.requirements = list(existing.values())
    pending = any(item.status == "pending" for item in state.requirements)
    if not pending or state.stop_reason == "requirement_capacity_exhausted":
        if state.stop_reason != "requirement_capacity_exhausted":
            state.stop_reason = None
        return True
    if not can_continue:
        state.stop_reason = stop_reason
        return True
    digest = _evidence_digest(observations)
    if state.recovery_attempts >= 2:
        state.stop_reason = "completion_recovery_limit"
        return True
    if state.recovery_attempts and digest == state.last_evidence_digest:
        state.stop_reason = "no_new_evidence"
        return True
    state.recovery_attempts += 1
    state.last_evidence_digest = digest
    state.stop_reason = None
    return False


def completion_feedback(state: TaskCompletionState) -> dict[str, Any]:
    return {
        "action": "task_completion_feedback", "status": "needs_work", **(state.context() or {}),
        "instruction": (
            "Known pending requirements remain. Continue only the missing work within existing "
            "permissions and budget. At the next finish, update their stable IDs to supported "
            "only when the requirement is satisfied, or blocked with the actual reason. "
            "Omitting an item does not resolve it. Do not reread satisfied requirements merely to fill notes."
        ),
    }
