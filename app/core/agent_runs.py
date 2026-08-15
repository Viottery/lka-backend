"""In-memory Agent Run state and event tracking."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha1
from typing import Any

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    """Thread-safe local run store for the single-process MVP runtime."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, AgentRunRecord] = {}
        self._events: dict[str, list[AgentRunEvent]] = {}
        self._cancel_requests: dict[str, str | None] = {}

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
            return event

    def get_run(self, run_id: str) -> AgentRunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_events(
        self,
        run_id: str,
        after_sequence: int = 0,
    ) -> list[AgentRunEvent]:
        with self._lock:
            return [
                event
                for event in self._events.get(run_id, [])
                if event.sequence > after_sequence
            ]

    def request_cancel(self, run_id: str, reason: str | None = None) -> None:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(f"Agent run not found: {run_id}")
            self._cancel_requests[run_id] = reason
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
            return updated
