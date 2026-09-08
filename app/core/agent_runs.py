"""In-memory Agent Run state and event tracking."""

from __future__ import annotations

import queue
import threading
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha1
from typing import Any

from pydantic import BaseModel, Field

from app.core.agent_storage import SqliteAgentRunStore
from app.core.safety import (
    InMemorySafetyReviewStore,
    SafetyReviewDecision,
    SafetyReviewMode,
    SafetyReviewRecord,
    SafetyReviewRequest,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


class AgentRunCancelled(RuntimeError):
    """Raised at step boundaries when a run has been cooperatively cancelled."""


class AgentRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRunRecord(BaseModel):
    run_id: str
    session_id: str
    trace_id: str
    parent_run_id: str | None = None
    status: AgentRunStatus
    user_input: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    cancelled_at: str | None = None
    waiting_since: str | None = None
    error_type: str | None = None
    error: str | None = None
    result_snapshot: dict[str, Any] | None = None
    log_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRunEvent(BaseModel):
    event_id: str
    run_id: str
    sequence: int
    type: str
    stage: str | None = None
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class InMemoryAgentRunManager:
    """Thread-safe hot cache with optional durable SQLite backing for Agent runs."""

    def __init__(self, *, durable_store: SqliteAgentRunStore | None = None) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, AgentRunRecord] = {}
        self._events: dict[str, list[AgentRunEvent]] = {}
        self._cancel_requests: dict[str, str | None] = {}
        self.safety_reviews = InMemorySafetyReviewStore()
        self.durable_store = durable_store
        self._pending_delta_events: queue.Queue[AgentRunEvent | None] | None = None
        self._event_writer: threading.Thread | None = None
        self._event_writer_error: BaseException | None = None
        self._closed = False
        if durable_store is not None:
            self._pending_delta_events = queue.Queue()
            self._event_writer = threading.Thread(
                target=self._write_delta_events,
                name="lka-agent-event-writer",
                daemon=True,
            )
            self._event_writer.start()

    def create_run(
        self,
        *,
        session_id: str,
        user_input: str,
        trace_id: str | None = None,
        parent_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRunRecord:
        now = _now_iso()
        clean_trace_id = trace_id or _stable_id("agent_turn", session_id, user_input, now)
        run_id = _stable_id("agent_run", clean_trace_id, now)
        record = AgentRunRecord(
            run_id=run_id,
            session_id=session_id,
            trace_id=clean_trace_id,
            parent_run_id=parent_run_id,
            status=AgentRunStatus.QUEUED,
            user_input=user_input,
            created_at=now,
            metadata=metadata or {},
        )
        with self._lock:
            self._runs[run_id] = record
            self._events[run_id] = []
            self._persist_run(record)
        return record

    def restore_run(self, record: AgentRunRecord) -> AgentRunRecord:
        """Restore a checkpointed run identity into the process-local event manager."""

        with self._lock:
            existing = self._runs.get(record.run_id)
            if existing is not None:
                return existing
            self._runs[record.run_id] = record
            self._events[record.run_id] = self._load_events(record.run_id)
            self._restore_reviews(record.run_id)
            self._restore_cancel_request(record)
            self._persist_run(record)
        return record

    def mark_running(self, run_id: str) -> AgentRunRecord:
        return self._update_run(
            run_id,
            status=AgentRunStatus.RUNNING,
            started_at=_now_iso(),
        )

    def mark_waiting_confirmation(
        self,
        run_id: str,
        confirmation_id: str,
    ) -> AgentRunRecord:
        return self._update_run(
            run_id,
            status=AgentRunStatus.WAITING_CONFIRMATION,
            waiting_since=_now_iso(),
            metadata_patch={"confirmation_id": confirmation_id},
        )

    def resume_running(self, run_id: str) -> AgentRunRecord:
        return self._update_run(run_id, status=AgentRunStatus.RUNNING)

    def create_safety_review(self, request: SafetyReviewRequest) -> SafetyReviewRecord:
        with self._lock:
            existing = self.safety_reviews.get(request.review_id)
            if request.mode == SafetyReviewMode.MANUAL and (
                existing is None or existing.status.value == "pending"
            ):
                # Publish the run's paused state before the pending review
                # becomes observable to API/SSE readers.
                self.mark_waiting_confirmation(request.run_id, confirmation_id=request.review_id)
            review = self.safety_reviews.create(request)
            self._persist_review(review)
            return review

    def get_safety_review(self, review_id: str) -> SafetyReviewRecord | None:
        review = self.safety_reviews.get(review_id)
        if review is not None or self.durable_store is None:
            return review
        payload = self.durable_store.load_review(review_id)
        if payload is None:
            return None
        restored = SafetyReviewRecord.model_validate(payload)
        self.get_run(restored.run_id)
        return self.safety_reviews.restore(restored)

    def list_safety_reviews(self, run_id: str) -> list[SafetyReviewRecord]:
        self._restore_reviews(run_id)
        return self.safety_reviews.list_for_run(run_id)

    def decide_safety_review(
        self,
        *,
        review_id: str,
        decision: SafetyReviewDecision,
        decided_by: str,
        reason: str | None,
    ) -> SafetyReviewRecord:
        review = self.safety_reviews.decide(
            review_id=review_id,
            decision=decision,
            decided_by=decided_by,
            reason=reason,
            decided_at=_now_iso(),
        )
        self._persist_review(review)
        self.append_event(
            review.run_id,
            "safety_review_decided",
            f"Safety review {review.status.value}.",
            stage="safety_review",
            payload={"review": review.model_dump(mode="json")},
        )
        if review.status.value == "approved":
            self.resume_running(review.run_id)
        return review

    def decide_safety_review_with_transition(
        self,
        *,
        review_id: str,
        decision: SafetyReviewDecision,
        decided_by: str,
        reason: str | None,
    ) -> tuple[SafetyReviewRecord, bool]:
        """Decide once and report whether this call changed pending state.

        The transition flag is the control-plane idempotency key for graph
        resume: retries can return the durable decision but must not resume the
        same graph thread again.
        """
        with self._lock:
            current = self.get_safety_review(review_id)
            if current is None:
                raise KeyError(f"Safety review not found: {review_id}")
            if current.status.value != "pending":
                return current, False
            return (
                self.decide_safety_review(
                    review_id=review_id,
                    decision=decision,
                    decided_by=decided_by,
                    reason=reason,
                ),
                True,
            )

    def attach_safety_review_llm_output(
        self,
        *,
        review_id: str,
        llm_output: str | None,
    ) -> SafetyReviewRecord:
        review = self.safety_reviews.attach_llm_output(
            review_id=review_id,
            llm_output=llm_output,
        )
        self._persist_review(review)
        return review

    def mark_cancelled(
        self,
        run_id: str,
        reason: str | None = None,
    ) -> AgentRunRecord:
        return self._update_run(
            run_id,
            status=AgentRunStatus.CANCELLED,
            cancelled_at=_now_iso(),
            error_type="cancelled",
            error=reason,
        )

    def cancel_run(self, run_id: str, reason: str | None = None) -> AgentRunRecord:
        """Durably terminate a run while an in-flight provider call winds down."""

        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            if current.status in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
            }:
                return current
            self.request_cancel(run_id, reason)
            if not any(event.type == "run_cancelled" for event in self._events[run_id]):
                self.append_event(
                    run_id,
                    "run_cancelled",
                    reason or "Agent run cancelled.",
                    stage="run",
                    payload={"reason": reason},
                )
            return self.mark_cancelled(run_id, reason)

    def complete_run(
        self,
        run_id: str,
        result_snapshot: dict[str, Any] | None = None,
        log_path: str | None = None,
    ) -> AgentRunRecord:
        return self._update_run(
            run_id,
            status=AgentRunStatus.COMPLETED,
            completed_at=_now_iso(),
            result_snapshot=result_snapshot,
            log_path=log_path,
        )

    def fail_run(
        self,
        run_id: str,
        error_type: str,
        error: str,
        log_path: str | None = None,
    ) -> AgentRunRecord:
        return self._update_run(
            run_id,
            status=AgentRunStatus.FAILED,
            failed_at=_now_iso(),
            error_type=error_type,
            error=error,
            log_path=log_path,
        )

    def append_event(
        self,
        run_id: str,
        type: str,
        message: str,
        stage: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AgentRunEvent:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(f"Agent run not found: {run_id}")
            sequence = len(self._events.setdefault(run_id, [])) + 1
            event = AgentRunEvent(
                event_id=f"{run_id}_event_{sequence:06d}",
                run_id=run_id,
                sequence=sequence,
                type=type,
                stage=stage,
                message=message,
                payload=payload or {},
                created_at=_now_iso(),
            )
            self._events[run_id].append(event)
            if type == "llm_delta":
                self._queue_delta_event(event)
            else:
                self._persist_event(event)
            return event

    def get_run(self, run_id: str) -> AgentRunRecord | None:
        with self._lock:
            current = self._runs.get(run_id)
            if current is not None or self.durable_store is None:
                return current
            payload = self.durable_store.load_run(run_id)
            if payload is None:
                return None
            return self.restore_run(AgentRunRecord.model_validate(payload))

    def list_events(
        self,
        run_id: str,
        after_sequence: int = 0,
    ) -> list[AgentRunEvent]:
        with self._lock:
            return [
                event for event in self._events.get(run_id, []) if event.sequence > after_sequence
            ]

    def request_cancel(self, run_id: str, reason: str | None = None) -> None:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(f"Agent run not found: {run_id}")
            current = self._runs[run_id]
            if current.status in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
            } or run_id in self._cancel_requests:
                return
            self._cancel_requests[run_id] = reason
            metadata = dict(current.metadata)
            metadata.update(
                {
                    "cancel_requested": True,
                    "cancel_reason": reason,
                    "cancel_requested_at": _now_iso(),
                }
            )
            updated = current.model_copy(update={"metadata": metadata})
            self._runs[run_id] = updated
            self._persist_run(updated)
            self.append_event(
                run_id,
                "run_cancel_requested",
                reason or "Cancellation requested.",
                stage="control",
                payload={"reason": reason},
            )

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancel_requests

    def cancel_reason(self, run_id: str) -> str | None:
        with self._lock:
            return self._cancel_requests.get(run_id)

    def _update_run(
        self,
        run_id: str,
        *,
        status: AgentRunStatus,
        metadata_patch: dict[str, Any] | None = None,
        **updates: Any,
    ) -> AgentRunRecord:
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            if current.status in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
            }:
                return current
            payload = {key: value for key, value in updates.items() if value is not None}
            metadata = dict(current.metadata)
            if metadata_patch:
                metadata.update(metadata_patch)
                payload["metadata"] = metadata
            updated = current.model_copy(update={"status": status, **payload})
            self._runs[run_id] = updated
            self._persist_run(updated)
            return updated

    def _persist_run(self, record: AgentRunRecord) -> None:
        if self.durable_store is not None:
            self.durable_store.save_run(record.model_dump(mode="json"), updated_at=_now_iso())

    def _persist_event(self, event: AgentRunEvent) -> None:
        if self.durable_store is not None:
            self.durable_store.save_event(event.model_dump(mode="json"))

    def _queue_delta_event(self, event: AgentRunEvent) -> None:
        if self._pending_delta_events is None:
            return
        self._pending_delta_events.put(event)

    def _write_delta_events(self) -> None:
        if self._pending_delta_events is None or self.durable_store is None:
            return
        while True:
            first = self._pending_delta_events.get()
            if first is None:
                self._pending_delta_events.task_done()
                return
            batch = [first]
            while len(batch) < 64:
                try:
                    next_event = self._pending_delta_events.get_nowait()
                except queue.Empty:
                    break
                if next_event is None:
                    self._pending_delta_events.task_done()
                    self._pending_delta_events.put(None)
                    break
                batch.append(next_event)
            try:
                self.durable_store.save_events([event.model_dump(mode="json") for event in batch])
            except BaseException as exc:  # noqa: BLE001 - surfaced by flush/close callers.
                self._event_writer_error = exc
            finally:
                for _ in batch:
                    self._pending_delta_events.task_done()

    def flush_events(self) -> None:
        if self._pending_delta_events is None:
            return
        self._pending_delta_events.join()
        if self._event_writer_error is not None:
            raise RuntimeError("Agent event persistence failed.") from self._event_writer_error

    def close(self) -> None:
        """Flush pending token events before shutting down the local runtime."""

        if self._pending_delta_events is None or self._closed:
            return
        self.flush_events()
        self._closed = True
        self._pending_delta_events.put(None)
        if self._event_writer is not None:
            self._event_writer.join(timeout=2)

    def _restore_cancel_request(self, record: AgentRunRecord) -> None:
        if record.metadata.get("cancel_requested") is True:
            reason = record.metadata.get("cancel_reason")
            self._cancel_requests[record.run_id] = reason if isinstance(reason, str) else None

    def _persist_review(self, review: SafetyReviewRecord) -> None:
        if self.durable_store is not None:
            self.durable_store.save_review(review.model_dump(mode="json"), updated_at=_now_iso())

    def _load_events(self, run_id: str) -> list[AgentRunEvent]:
        if self.durable_store is None:
            return []
        events = [
            AgentRunEvent.model_validate(payload)
            for payload in self.durable_store.list_events(run_id)
        ]
        snapshots: dict[str, str] = {}
        for event in events:
            if event.type != "llm_delta":
                continue
            payload = event.payload
            if isinstance(payload.get("content_snapshot"), str):
                continue
            delta = payload.get("delta")
            if not isinstance(delta, str):
                continue
            call_id = payload.get("llm_call_id")
            key = str(call_id) if isinstance(call_id, str) else f"stage:{event.stage or ''}"
            snapshot = snapshots.get(key, "") + delta
            snapshots[key] = snapshot
            payload["content_snapshot"] = snapshot
        return events

    def _restore_reviews(self, run_id: str) -> None:
        if self.durable_store is None:
            return
        for payload in self.durable_store.list_reviews(run_id):
            self.safety_reviews.restore(SafetyReviewRecord.model_validate(payload))
