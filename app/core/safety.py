"""Safety review contracts for agent tool execution."""

from __future__ import annotations

import threading
from collections import OrderedDict
from enum import Enum
from hashlib import sha1
from typing import Any, Literal

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
    EXPIRED = "expired"


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
    # Local audit context, never reconstructed from model-visible source text.
    user_request: str = ""
    workspace_access: list[str] = Field(default_factory=list)
    workspace_root: str | None = None
    unrestricted_execution: bool = False
    tool_risk: str = "low"
    side_effects: list[str] = Field(default_factory=list)
    read_only: bool | None = None
    safety_review_policy: Literal["standard", "autonomous"] = "standard"
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


class SafetyReviewQueueConflict(ValueError):
    """Raised when a review cannot be decided in the current queue state."""

    def __init__(self, message: str, *, head_review_id: str | None = None) -> None:
        super().__init__(message)
        self.head_review_id = head_review_id


class InMemorySafetyReviewStore:
    """Thread-safe local safety review store for current in-memory Agent runs."""

    def __init__(self, *, decided_cache_limit: int | None = None) -> None:
        if decided_cache_limit is not None and (
            type(decided_cache_limit) is not int or decided_cache_limit < 1
        ):
            raise ValueError("decided_cache_limit must be a positive integer or None")
        self._condition = threading.Condition(threading.RLock())
        self._reviews: dict[str, SafetyReviewRecord] = {}
        self._run_reviews: dict[str, list[str]] = {}
        self._decided_cache_limit = decided_cache_limit
        self._decided_lru: OrderedDict[str, None] = OrderedDict()
        self._active_waiters: dict[str, int] = {}

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
            if existing is not None and not (
                existing.status == SafetyReviewStatus.PENDING
                and record.status != SafetyReviewStatus.PENDING
            ):
                return existing
            self._reviews[record.review_id] = record
            if record.review_id not in self._run_reviews.setdefault(record.run_id, []):
                self._run_reviews[record.run_id].append(record.review_id)
            self._track_decided_locked(record)
            self._condition.notify_all()
        return record

    def get(self, review_id: str) -> SafetyReviewRecord | None:
        with self._condition:
            record = self._reviews.get(review_id)
            if record is not None:
                self._touch_decided_locked(record)
            return record

    def list_for_run(self, run_id: str) -> list[SafetyReviewRecord]:
        with self._condition:
            return [
                self._reviews[review_id]
                for review_id in self._run_reviews.get(run_id, [])
                if review_id in self._reviews
            ]

    def list_pending(self) -> list[SafetyReviewRecord]:
        with self._condition:
            return sorted(
                (review for review in self._reviews.values()
                 if review.mode == SafetyReviewMode.MANUAL
                 and review.status == SafetyReviewStatus.PENDING),
                key=lambda review: (review.created_at, review.review_id),
            )

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
            self._track_decided_locked(updated)
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
            self._track_decided_locked(updated)
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
            self._active_waiters[review_id] = self._active_waiters.get(review_id, 0) + 1
        try:
            while True:
                # Decision routes acquire the run-manager lock before this store's
                # lock; check cancellation outside the store lock to avoid inversion.
                if cancel_check is not None and cancel_check():
                    with self._condition:
                        current = self._reviews.get(review_id)
                        if current is None:
                            raise KeyError(f"Safety review not found: {review_id}")
                        return current
                with self._condition:
                    current = self._reviews.get(review_id)
                    if current is None:
                        raise KeyError(f"Safety review not found: {review_id}")
                    if current.status != SafetyReviewStatus.PENDING:
                        self._touch_decided_locked(current)
                        return current
                    self._condition.wait(timeout=timeout_seconds or 0.5)
        finally:
            with self._condition:
                remaining = self._active_waiters.get(review_id, 0) - 1
                if remaining > 0:
                    self._active_waiters[review_id] = remaining
                else:
                    self._active_waiters.pop(review_id, None)
                self._evict_decided_locked()

    def trim_decided_cache(self) -> None:
        """Trim only after the durable review update has committed."""
        with self._condition:
            self._evict_decided_locked()

    def _track_decided_locked(self, record: SafetyReviewRecord) -> None:
        if self._decided_cache_limit is None:
            return
        if record.status == SafetyReviewStatus.PENDING:
            self._decided_lru.pop(record.review_id, None)
            return
        self._decided_lru.pop(record.review_id, None)
        self._decided_lru[record.review_id] = None

    def _touch_decided_locked(self, record: SafetyReviewRecord) -> None:
        if self._decided_cache_limit is None or record.status == SafetyReviewStatus.PENDING:
            return
        if record.review_id in self._decided_lru:
            self._decided_lru.move_to_end(record.review_id)

    def _evict_decided_locked(self) -> None:
        if self._decided_cache_limit is None:
            return
        while len(self._decided_lru) > self._decided_cache_limit:
            review_id = next(
                (candidate for candidate in self._decided_lru
                 if not self._active_waiters.get(candidate, 0)),
                None,
            )
            if review_id is None:
                # Active waiters pin their decisions until they have consumed them.
                return
            self._decided_lru.pop(review_id)
            record = self._reviews.pop(review_id, None)
            if record is not None:
                run_reviews = self._run_reviews.get(record.run_id)
                if run_reviews is not None:
                    try:
                        run_reviews.remove(review_id)
                    except ValueError:
                        pass
                    if not run_reviews:
                        self._run_reviews.pop(record.run_id, None)
