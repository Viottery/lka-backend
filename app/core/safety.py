"""Safety review contracts for agent tool execution."""

from __future__ import annotations

import threading
from enum import Enum
from hashlib import sha1
from typing import Any

from pydantic import BaseModel, Field


def stable_safety_review_id(*parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"safety_review_{digest}"


class SafetyReviewMode(str, Enum):
    SKIP = "skip"
    LLM = "llm"
    MANUAL = "manual"


class SafetyReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SafetyReviewDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class SafetyReviewRequest(BaseModel):
    review_id: str
    run_id: str
    session_id: str
    trace_id: str
    invocation_id: str
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_risk: str = "low"
    side_effects: list[str] = Field(default_factory=list)
    read_only: bool | None = None
    mode: SafetyReviewMode
    reason: str
    created_at: str


class SafetyReviewRecord(SafetyReviewRequest):
    status: SafetyReviewStatus = SafetyReviewStatus.PENDING
    decided_by: str | None = None
    decision_reason: str | None = None
    decided_at: str | None = None
    llm_output: str | None = None


class SafetyReviewRequired(RuntimeError):
    """Raised when a tool call is waiting for external confirmation."""

    def __init__(self, review: SafetyReviewRecord) -> None:
        super().__init__(f"Safety review is required: {review.review_id}")
        self.review = review


class SafetyReviewRejected(RuntimeError):
    """Raised when a safety review rejects a tool call."""

    def __init__(self, review: SafetyReviewRecord) -> None:
        super().__init__(review.decision_reason or "Safety review rejected tool call.")
        self.review = review


class InMemorySafetyReviewStore:
    """Thread-safe local safety review store for current in-memory Agent runs."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._reviews: dict[str, SafetyReviewRecord] = {}
        self._run_reviews: dict[str, list[str]] = {}

    def create(self, request: SafetyReviewRequest) -> SafetyReviewRecord:
        with self._condition:
            existing = self._reviews.get(request.review_id)
            if existing is not None:
                return existing
            record = SafetyReviewRecord(**request.model_dump(mode="python"))
            self._reviews[record.review_id] = record
            self._run_reviews.setdefault(record.run_id, []).append(record.review_id)
            self._condition.notify_all()
            return record

    def restore(self, record: SafetyReviewRecord) -> SafetyReviewRecord:
        """Rehydrate a durable review without changing its decision state."""

        with self._condition:
            existing = self._reviews.get(record.review_id)
            if existing is not None:
                return existing
            self._reviews[record.review_id] = record
            self._run_reviews.setdefault(record.run_id, []).append(record.review_id)
            self._condition.notify_all()
        return record

    def get(self, review_id: str) -> SafetyReviewRecord | None:
        with self._condition:
            return self._reviews.get(review_id)

    def list_for_run(self, run_id: str) -> list[SafetyReviewRecord]:
        with self._condition:
            return [
                self._reviews[review_id]
                for review_id in self._run_reviews.get(run_id, [])
                if review_id in self._reviews
            ]

    def attach_llm_output(
        self,
        *,
        review_id: str,
        llm_output: str | None,
    ) -> SafetyReviewRecord:
        with self._condition:
            current = self._reviews.get(review_id)
            if current is None:
                raise KeyError(f"Safety review not found: {review_id}")
            updated = current.model_copy(update={"llm_output": llm_output})
            self._reviews[review_id] = updated
            self._condition.notify_all()
            return updated

    def decide(
        self,
        *,
        review_id: str,
        decision: SafetyReviewDecision,
        decided_by: str,
        reason: str | None,
        decided_at: str,
    ) -> SafetyReviewRecord:
        with self._condition:
            current = self._reviews.get(review_id)
            if current is None:
                raise KeyError(f"Safety review not found: {review_id}")
            if current.status != SafetyReviewStatus.PENDING:
                return current
            status = (
                SafetyReviewStatus.APPROVED
                if decision == SafetyReviewDecision.APPROVE
                else SafetyReviewStatus.REJECTED
            )
            updated = current.model_copy(
                update={
                    "status": status,
                    "decided_by": decided_by,
                    "decision_reason": reason,
                    "decided_at": decided_at,
                }
            )
            self._reviews[review_id] = updated
            self._condition.notify_all()
            return updated

    def wait_for_decision(
        self,
        review_id: str,
        *,
        timeout_seconds: float | None = None,
        cancel_check: Any | None = None,
    ) -> SafetyReviewRecord:
        with self._condition:
            while True:
                current = self._reviews.get(review_id)
                if current is None:
                    raise KeyError(f"Safety review not found: {review_id}")
                if current.status != SafetyReviewStatus.PENDING:
                    return current
                if cancel_check is not None and cancel_check():
                    return current
                self._condition.wait(timeout=timeout_seconds or 0.5)
