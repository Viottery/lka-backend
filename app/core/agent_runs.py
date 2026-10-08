"""In-memory Agent Run state and event tracking."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha1
from typing import Any

from pydantic import BaseModel, Field

from app.core.agent_storage import SqliteAgentRunStore, validate_child_plan_ownership
from app.core.safety import (
    InMemorySafetyReviewStore,
    SafetyReviewDecision,
    SafetyReviewMode,
    SafetyReviewQueueConflict,
    SafetyReviewRecord,
    SafetyReviewRequest,
    SafetyReviewStatus,
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
    PAUSED = "paused"
    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class AgentRunRecord(BaseModel):
    run_id: str
    session_id: str
    trace_id: str
    parent_run_id: str | None = None
    child_run_ids: tuple[str, ...] = ()
    plan_id: str | None = None
    step_id: str | None = None
    attempt: int | None = None
    status: AgentRunStatus
    user_input: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    cancelled_at: str | None = None
    paused_at: str | None = None
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
    parent_run_id: str | None = None
    child_run_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    attempt: int | None = None


class InMemoryAgentRunManager:
    """Thread-safe hot cache with optional durable SQLite backing for Agent runs."""

    def __init__(self, *, durable_store: SqliteAgentRunStore | None = None) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, AgentRunRecord] = {}
        self._events: dict[str, list[AgentRunEvent]] = {}
        self._cancel_requests: dict[str, str | None] = {}
        self.safety_reviews = InMemorySafetyReviewStore()
        self._user_continuations: dict[str, tuple[str, str, str]] = {}
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

    def create_child_run(
        self, *, parent_run_id: str, plan_id: str, step_id: str, attempt: int, user_input: str,
        agent_id: str = "general_agent", agent_version: str | None = None,
        executor_kind: str = "react",
    ) -> AgentRunRecord:
        """Create one durable child attempt and link it to its parent atomically."""

        if attempt < 1:
            raise ValueError("Child run attempt must be positive.")
        with self._lock:
            parent = self.get_run(parent_run_id)
            if parent is None:
                raise KeyError(f"Parent Agent run not found: {parent_run_id}")
            if parent.status == AgentRunStatus.PAUSED or parent.metadata.get("pause_requested"):
                raise ValueError("Cannot create a child while its parent is pausing or paused.")
            if parent.status in {
                AgentRunStatus.WAITING_USER,
                AgentRunStatus.WAITING_CONFIRMATION,
            }:
                raise ValueError("Cannot create a child while the parent is waiting for a user response or confirmation.")
            if parent.status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED, AgentRunStatus.TIMED_OUT}:
                raise ValueError("Cannot create a child for a terminal parent run.")
            validate_child_plan_ownership(parent.model_dump(mode="json"), plan_id)
            for sibling_id in parent.child_run_ids:
                sibling = self.get_run(sibling_id)
                if sibling and sibling.plan_id == plan_id and sibling.step_id == step_id and sibling.attempt == attempt:
                    raise ValueError("Child run attempt already exists.")
            now = _now_iso()
            child = AgentRunRecord(
                run_id=_stable_id("agent_run", parent.trace_id, step_id, str(attempt), now),
                session_id=_stable_id("child_session", parent.session_id, step_id, str(attempt), now),
                trace_id=parent.trace_id,
                parent_run_id=parent_run_id,
                plan_id=plan_id,
                step_id=step_id,
                attempt=attempt,
                status=AgentRunStatus.QUEUED,
                user_input=user_input,
                created_at=now,
                metadata={
                    "multi_agent": True,
                    "agent_id": agent_id,
                    "agent_version": agent_version,
                    "executor_kind": executor_kind,
                },
            )
            parent = parent.model_copy(
                update={
                    # Keep outer ownership on a coordinator run. Its children
                    # belong to the nested Plan persisted in metadata.
                    "plan_id": parent.plan_id or plan_id,
                    "child_run_ids": parent.child_run_ids + (child.run_id,),
                }
            )
            refs = self._child_event_refs(child)
            events = [
                self._new_event(parent_run_id, "subtask_created", "Subtask created.", "subtask", refs),
                self._new_event(child.run_id, "subtask_created", "Subtask created.", "subtask", refs),
                self._new_event(child.run_id, "subtask_queued", "Subtask queued.", "subtask", refs),
            ]
            events[2] = events[2].model_copy(
                update={"sequence": 2, "event_id": f"{child.run_id}_event_000002"}
            )
            if self.durable_store is not None:
                self.flush_events()
                self.durable_store.save_child_creation(
                    parent=parent.model_dump(mode="json"), child=child.model_dump(mode="json"),
                    events=[event.model_dump(mode="json") for event in events], updated_at=now,
                )
            self._runs[child.run_id] = child
            self._runs[parent_run_id] = parent
            self._events[child.run_id] = events[1:]
            self._events[parent_run_id].append(events[0])
            return child

    def attach_child_context(self, run_id: str, *, snapshot: dict[str, Any], views: dict[str, Any]) -> AgentRunRecord:
        """Bind the immutable snapshot and derived views before child execution."""
        child = self._require_child(run_id)
        if child.status != AgentRunStatus.QUEUED:
            raise ValueError("Child context can only be attached before execution.")
        if snapshot.get("child_run_id") != child.run_id or snapshot.get("parent_run_id") != child.parent_run_id:
            raise ValueError("ContextSnapshot run references do not match the child run.")
        if snapshot.get("session_id") != child.session_id:
            raise ValueError("ContextSnapshot session does not match the isolated child session.")
        if snapshot.get("plan_id") != child.plan_id or snapshot.get("step_id") != child.step_id:
            raise ValueError("ContextSnapshot plan references do not match the child run.")
        if snapshot.get("agent_id", "general_agent") != child.metadata.get("agent_id", "general_agent"):
            raise ValueError("ContextSnapshot Agent does not match the Child Run.")
        if snapshot.get("agent_version") != child.metadata.get("agent_version"):
            raise ValueError("ContextSnapshot Agent version does not match the Child Run.")
        tool_view = views.get("tool")
        if not isinstance(tool_view, dict) or tool_view.get("snapshot_id") != snapshot.get("snapshot_id"):
            raise ValueError("ToolView must reference the attached ContextSnapshot.")
        updated = self._update_run(
            run_id,
            status=AgentRunStatus.QUEUED,
            metadata_patch={"context_snapshot": snapshot, "context_views": views},
        )
        self.append_event(
            run_id,
            "context_snapshot_attached",
            "Child context snapshot attached.",
            stage="context",
            payload={"snapshot_id": snapshot.get("snapshot_id")},
            **self._child_event_refs(child),
        )
        return updated

    def record_multi_agent_plan(
        self,
        run_id: str,
        *,
        event_type: str,
        payload: dict[str, Any],
        plan: dict[str, Any] | None = None,
        reserve_operation_id: bool = True,
    ) -> AgentRunEvent:
        """Atomically persist the plan/event and executable fork ID reservation.

        A malformed proposal has no executable effect, so its ID must remain
        available for a corrected proposal. Policy-rejected and validated
        operations retain the existing idempotency reservation.
        """
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            metadata = dict(current.metadata)
            if plan is not None:
                metadata["multi_agent_plan"] = plan
            operations = dict(metadata.get("fork_operations", {}))
            operation_id = str(payload.get("operation_id") or "")
            if reserve_operation_id and operation_id and operation_id not in operations:
                operations[operation_id] = payload
                metadata["fork_operations"] = operations
            updated = current.model_copy(update={"metadata": metadata})
            event = self._control_event(
                run_id,
                event_type,
                "Planner fork operation recorded.",
                stage="planner",
                payload=payload,
                pending=[],
            )
            self._commit_control_updates([current], [updated], [event])
            return event

    def record_multi_agent_aggregation(
        self,
        run_id: str,
        *,
        plan: dict[str, Any],
        aggregate: dict[str, Any],
        verification: dict[str, Any],
        replan_required: bool,
    ) -> AgentRunEvent:
        """Persist internal aggregate state and a compact, non-payload event."""
        fingerprint = sha1(json.dumps(
            {"aggregate": aggregate, "verification": verification, "replan_required": replan_required},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            metadata = dict(current.metadata)
            if metadata.get("multi_agent_aggregate_fingerprint") == fingerprint:
                prior = next(
                    (event for event in reversed(self._events.get(run_id, [])) if event.type == "multi_agent_aggregated"),
                    None,
                )
                if prior is not None:
                    return prior
            metadata.update({
                "multi_agent_plan": plan,
                "multi_agent_aggregate": aggregate,
                "multi_agent_verification": verification,
                "multi_agent_replan_required": replan_required,
                "multi_agent_aggregate_fingerprint": fingerprint,
            })
            updated = current.model_copy(update={"metadata": metadata})
            self._runs[run_id] = updated
            self._persist_run(updated)
        return self.append_event(
            run_id,
            "multi_agent_aggregated",
            "Multi-Agent results aggregated and verified.",
            stage="subtask",
            payload={
                "plan_id": plan.get("plan_id"),
                "aggregation_status": aggregate.get("status"),
                "verification_status": verification.get("status"),
                "replan_required": replan_required,
                "task_result_count": len(aggregate.get("task_results", ())),
                "conflict_count": len(aggregate.get("conflicts", ())),
            },
            plan_id=plan.get("plan_id"),
            parent_run_id=run_id,
        )

    def _new_event(self, run_id: str, type: str, message: str, stage: str, refs: dict[str, Any]) -> AgentRunEvent:
        sequence = len(self._events.setdefault(run_id, [])) + 1
        return AgentRunEvent(
            event_id=f"{run_id}_event_{sequence:06d}", run_id=run_id, sequence=sequence,
            type=type, stage=stage, message=message, payload={}, created_at=_now_iso(), **refs,
        )

    def child_tree(self, parent_run_id: str) -> list[AgentRunRecord]:
        """Return the durable descendant tree in parent-before-child order."""

        parent = self.get_run(parent_run_id)
        if parent is None:
            raise KeyError(f"Agent run not found: {parent_run_id}")
        result: list[AgentRunRecord] = []
        for child_id in parent.child_run_ids:
            child = self.get_run(child_id)
            if child is not None:
                result.append(child)
                result.extend(self.child_tree(child_id))
        return result

    def mark_child_running(self, run_id: str) -> AgentRunRecord:
        child = self._require_child(run_id)
        if child.status != AgentRunStatus.QUEUED:
            return child
        updated = self.mark_running(run_id)
        self.append_event(run_id, "subtask_started", "Subtask started.", stage="subtask", **self._child_event_refs(child))
        return updated

    def complete_child_run(self, run_id: str, *, result_snapshot: dict[str, Any] | None = None, log_path: str | None = None) -> AgentRunRecord:
        child = self._require_child(run_id)
        if child.status != AgentRunStatus.RUNNING:
            return child
        updated = self.complete_run(run_id, result_snapshot=result_snapshot, log_path=log_path)
        self.append_event(run_id, "subtask_completed", "Subtask completed.", stage="subtask", **self._child_event_refs(child))
        return updated

    def fail_child_run(self, run_id: str, *, error_type: str, error: str) -> AgentRunRecord:
        child = self._require_child(run_id)
        if child.status not in {
            AgentRunStatus.QUEUED, AgentRunStatus.RUNNING,
            AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER,
        }:
            return child
        updated = self.fail_run(run_id, error_type=error_type, error=error)
        self.append_event(run_id, "subtask_failed", "Subtask failed.", stage="subtask", **self._child_event_refs(child))
        return updated

    def timeout_child_run(self, run_id: str, *, error: str) -> AgentRunRecord:
        with self._lock:
            child = self._require_child(run_id)
            current = self._runs[run_id]
            if current.status not in {
                AgentRunStatus.QUEUED,
                AgentRunStatus.RUNNING,
                AgentRunStatus.WAITING_CONFIRMATION,
                AgentRunStatus.WAITING_USER,
            } or current.metadata.get("completion_claimed") is True:
                return current
            now = _now_iso()
            metadata = dict(current.metadata)
            events: list[AgentRunEvent] = []
            if run_id not in self._cancel_requests:
                metadata.update({
                    "cancel_requested": True,
                    "cancel_reason": "Child run timed out.",
                    "cancel_requested_at": now,
                })
                events.append(self._control_event(
                    run_id, "run_cancel_requested", "Child run timed out.",
                    stage="control", payload={"reason": "Child run timed out."}, pending=events,
                ))
            updated = current.model_copy(update={
                "status": AgentRunStatus.TIMED_OUT,
                "metadata": metadata,
                "failed_at": now,
                "error_type": "timeout",
                "error": error,
            })
            events.append(self._control_event(
                run_id, "subtask_failed", "Subtask timed out.",
                stage="subtask", refs=self._child_event_refs(child), pending=events,
            ))
            self._commit_control_updates([current], [updated], events)
            return updated

    def _require_child(self, run_id: str) -> AgentRunRecord:
        child = self.get_run(run_id)
        if child is None or child.parent_run_id is None or child.plan_id is None or child.step_id is None:
            raise ValueError("Run is not a multi-Agent child run.")
        return child

    @staticmethod
    def _child_event_refs(child: AgentRunRecord) -> dict[str, Any]:
        return {"parent_run_id": child.parent_run_id, "child_run_id": child.run_id, "plan_id": child.plan_id, "step_id": child.step_id, "attempt": child.attempt}

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

    def mark_waiting_user(
        self,
        run_id: str,
        *,
        question_id: str | None = None,
        question: str,
        patch_id: str | None = None,
    ) -> AgentRunRecord:
        """Persist a user-facing question independently of safety approval."""

        clean_question = question.strip()
        if not clean_question:
            raise ValueError("User question must not be empty.")
        if len(clean_question) > 4000:
            raise ValueError("User question is too long.")
        question_id = question_id or _stable_id("user_question", run_id, patch_id, clean_question)
        if not question_id.strip():
            raise ValueError("question_id must not be empty.")
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                current = self.get_run(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            pending = {
                "question_id": question_id,
                "patch_id": patch_id,
                "question": clean_question,
                "asked_at": _now_iso(),
            }
            if current.status == AgentRunStatus.WAITING_USER:
                existing = current.metadata.get("pending_user_question")
                if isinstance(existing, dict) and all(
                    existing.get(key) == pending.get(key)
                    for key in ("question_id", "patch_id", "question")
                ):
                    return current
                raise ValueError("Agent run is already waiting for a different user question.")
            if current.status != AgentRunStatus.RUNNING:
                raise ValueError("Only a running Agent run can ask the user a question.")
            if current.metadata.get("cancel_requested") or current.metadata.get("completion_claimed"):
                raise ValueError("Agent run cannot wait for a user response in its current state.")
            metadata = dict(current.metadata)
            metadata["pending_user_question"] = pending
            metadata.pop("pending_user_answer_command_id", None)
            updated = current.model_copy(
                update={
                    "status": AgentRunStatus.WAITING_USER,
                    "waiting_since": pending["asked_at"],
                    "metadata": metadata,
                }
            )
            event_payload = {
                "question_id": question_id,
                "patch_id": patch_id,
                "question": clean_question,
            }
            if self.durable_store is None:
                self._runs[run_id] = updated
                self.append_event(
                    run_id,
                    "multi_agent_user_question",
                    clean_question,
                    stage="user_input",
                    payload=event_payload,
                )
                return updated
            event = AgentRunEvent(
                event_id="pending_multi_agent_user_question",
                run_id=run_id,
                sequence=0,
                type="multi_agent_user_question",
                stage="user_input",
                message=clean_question,
                payload=event_payload,
                created_at=pending["asked_at"],
                parent_run_id=current.parent_run_id,
                child_run_id=run_id if current.parent_run_id else None,
                plan_id=current.plan_id,
                step_id=current.step_id,
                attempt=current.attempt,
            )
            self.flush_events()
            record_payload, _, _ = self.durable_store.save_waiting_user_question(
                record=updated.model_dump(mode="json"),
                event=event.model_dump(mode="json"),
                updated_at=pending["asked_at"],
            )
            restored = AgentRunRecord.model_validate(record_payload)
            self._runs[run_id] = restored
            self._events[run_id] = self._load_events(run_id)
            return restored

    def continue_user_question(
        self,
        *,
        run_id: str,
        command_id: str,
        answer: str,
    ) -> tuple[AgentRunRecord, str, bool]:
        """Atomically accept one answer; return (run, question_id, replayed)."""

        clean_answer = answer.strip()
        if not command_id.strip():
            raise ValueError("command_id must not be empty.")
        if not clean_answer:
            raise ValueError("User answer must not be empty.")
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                current = self.get_run(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            now = _now_iso()
            event = AgentRunEvent(
                event_id="pending_multi_agent_user_answer",
                run_id=run_id,
                sequence=0,
                type="multi_agent_user_answer_received",
                stage="user_input",
                message="User continuation received.",
                payload={"command_id": command_id},
                created_at=now,
                parent_run_id=current.parent_run_id,
                child_run_id=run_id if current.parent_run_id else None,
                plan_id=current.plan_id,
                step_id=current.step_id,
                attempt=current.attempt,
            )
            if self.durable_store is not None:
                self.flush_events()
                record_payload, _, replayed, question_id = self.durable_store.continue_user_question(
                    run_id=run_id,
                    command_id=command_id,
                    answer=clean_answer,
                    event=event.model_dump(mode="json"),
                    updated_at=now,
                )
                updated = AgentRunRecord.model_validate(record_payload)
                self._runs[run_id] = updated
                self._events[run_id] = self._load_events(run_id)
                return updated, question_id, replayed

            existing = self._user_continuations.get(command_id)
            if existing is not None:
                existing_run_id, question_id, existing_answer = existing
                if existing_run_id != run_id or existing_answer != clean_answer:
                    raise ValueError("command_id was already used with a different continuation.")
                return current, question_id, True
            question = current.metadata.get("pending_user_question")
            if current.status != AgentRunStatus.WAITING_USER or not isinstance(question, dict):
                raise ValueError("Agent run is not waiting for a user response.")
            if current.metadata.get("cancel_requested") or current.metadata.get("completion_claimed"):
                raise ValueError("Agent run cannot be continued in its current state.")
            question_id = question.get("question_id")
            if not isinstance(question_id, str) or not question_id:
                raise ValueError("Agent run has no valid pending question.")
            metadata = dict(current.metadata)
            metadata["pending_user_answer_command_id"] = command_id
            updated = current.model_copy(
                update={"status": AgentRunStatus.RUNNING, "waiting_since": None, "metadata": metadata}
            )
            self._runs[run_id] = updated
            self._user_continuations[command_id] = (run_id, question_id, clean_answer)
            self.append_event(
                run_id,
                "multi_agent_user_answer_received",
                "User continuation received.",
                stage="user_input",
                payload={"command_id": command_id, "question_id": question_id},
            )
            return updated, question_id, False

    def get_user_continuation(self, run_id: str, command_id: str) -> str | None:
        """Return a journaled answer to trusted runtime code only."""

        if self.durable_store is not None:
            return self.durable_store.load_user_continuation_answer(
                run_id=run_id, command_id=command_id
            )
        value = self._user_continuations.get(command_id)
        return value[2] if value is not None and value[0] == run_id else None

    def resume_running(self, run_id: str) -> AgentRunRecord:
        with self._lock:
            current = self.get_run(run_id)
            if current is not None and current.status == AgentRunStatus.WAITING_USER:
                raise ValueError("WAITING_USER runs require a user answer or child completion to resume.")
            return self._update_run(run_id, status=AgentRunStatus.RUNNING)

    def mark_waiting_for_child_user(
        self,
        parent_run_id: str,
        child_run_ids: Iterable[str],
    ) -> AgentRunRecord:
        """Park a parent on waiting descendants without inventing a parent question."""

        clean_ids = tuple(dict.fromkeys(item.strip() for item in child_run_ids if item.strip()))
        if not clean_ids:
            raise ValueError("At least one waiting child run ID is required.")
        with self._lock:
            parent = self._runs.get(parent_run_id)
            if parent is None:
                parent = self.get_run(parent_run_id)
            if parent is None:
                raise KeyError(f"Agent run not found: {parent_run_id}")
            if parent.status not in {AgentRunStatus.RUNNING, AgentRunStatus.WAITING_USER}:
                raise ValueError("Parent Agent run cannot wait on children in its current state.")
            if (
                parent.metadata.get("pending_user_question")
                and not parent.metadata.get("pending_user_answer_command_id")
            ):
                raise ValueError("A parent with its own pending question cannot wait on child questions.")
            for child_run_id in clean_ids:
                child = self.get_run(child_run_id)
                if (
                    child is None
                    or child.parent_run_id != parent_run_id
                    or child.status not in {
                        AgentRunStatus.WAITING_USER,
                        AgentRunStatus.WAITING_CONFIRMATION,
                    }
                ):
                    raise ValueError("Only a waiting child owned by this parent can be referenced.")
            now = _now_iso()
            metadata = dict(parent.metadata)
            previous_ids = metadata.get("waiting_child_user_run_ids", [])
            previous_ids = previous_ids if isinstance(previous_ids, list) else []
            combined_ids = list(dict.fromkeys([*previous_ids, *clean_ids]))
            if (
                parent.status == AgentRunStatus.WAITING_USER
                and previous_ids == combined_ids
                and not parent.metadata.get("pending_user_question")
                and not parent.metadata.get("pending_user_answer_command_id")
            ):
                return parent
            metadata["waiting_child_user_run_ids"] = combined_ids
            metadata.pop("pending_user_question", None)
            metadata.pop("pending_user_answer_command_id", None)
            updated = parent.model_copy(
                update={
                    "status": AgentRunStatus.WAITING_USER,
                    "waiting_since": now,
                    "metadata": metadata,
                }
            )
            event = AgentRunEvent(
                event_id="pending_multi_agent_children_waiting_user",
                run_id=parent_run_id,
                sequence=0,
                type="multi_agent_children_waiting_user",
                stage="scheduler",
                message="Parent is waiting for child user input.",
                payload={"child_run_ids": combined_ids},
                created_at=now,
                plan_id=parent.plan_id,
            )
            if self.durable_store is None:
                self._runs[parent_run_id] = updated
                self.append_event(
                    parent_run_id,
                    event.type,
                    event.message,
                    stage=event.stage,
                    payload=event.payload,
                )
                return updated
            self.flush_events()
            record_payload, _persisted_event = self.durable_store.save_waiting_for_child_user(
                parent_record=updated.model_dump(mode="json"),
                child_run_ids=list(clean_ids),
                event=event.model_dump(mode="json"),
                updated_at=now,
            )
            restored = AgentRunRecord.model_validate(record_payload)
            self._runs[parent_run_id] = restored
            self._events[parent_run_id] = self._load_events(parent_run_id)
            return restored

    def resume_after_child_user(self, parent_run_id: str) -> AgentRunRecord:
        """Resume a parent only after all children it waited on are terminal."""

        with self._lock:
            parent = self._runs.get(parent_run_id)
            if parent is None:
                parent = self.get_run(parent_run_id)
            if parent is None:
                raise KeyError(f"Agent run not found: {parent_run_id}")
            now = _now_iso()
            event = AgentRunEvent(
                event_id="pending_multi_agent_children_user_resumed",
                run_id=parent_run_id,
                sequence=0,
                type="multi_agent_children_user_resumed",
                stage="scheduler",
                message="Parent resumed after child user input completed.",
                payload={},
                created_at=now,
                plan_id=parent.plan_id,
            )
            if self.durable_store is not None:
                self.flush_events()
                record_payload, _persisted_event, _resumed = self.durable_store.resume_after_child_user(
                    parent_run_id=parent_run_id,
                    event=event.model_dump(mode="json"),
                    updated_at=now,
                )
                restored = AgentRunRecord.model_validate(record_payload)
                self._runs[parent_run_id] = restored
                self._events[parent_run_id] = self._load_events(parent_run_id)
                return restored

            child_ids = parent.metadata.get("waiting_child_user_run_ids")
            if (
                parent.status != AgentRunStatus.WAITING_USER
                or not isinstance(child_ids, list)
                or not child_ids
                or parent.metadata.get("pending_user_question")
                or parent.metadata.get("pending_user_answer_command_id")
                or parent.metadata.get("cancel_requested") is True
                or parent.metadata.get("completion_claimed") is True
            ):
                return parent
            for child_run_id in child_ids:
                child = self.get_run(child_run_id)
                if (
                    child is None
                    or child.parent_run_id != parent_run_id
                    or child.status not in {
                        AgentRunStatus.COMPLETED,
                        AgentRunStatus.FAILED,
                        AgentRunStatus.CANCELLED,
                        AgentRunStatus.TIMED_OUT,
                    }
                ):
                    return parent
            metadata = dict(parent.metadata)
            resumed_children = metadata.pop("waiting_child_user_run_ids")
            updated = parent.model_copy(
                update={
                    "status": AgentRunStatus.RUNNING,
                    "waiting_since": None,
                    "metadata": metadata,
                }
            )
            self._runs[parent_run_id] = updated
            self.append_event(
                parent_run_id,
                event.type,
                event.message,
                stage=event.stage,
                payload={"child_run_ids": resumed_children},
            )
            return updated

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

    def list_pending_safety_reviews(self) -> list[SafetyReviewRecord]:
        if self.durable_store is None:
            records = self.safety_reviews.list_pending()
            return [
                review for review in records
                if (run := self.get_run(review.run_id)) is not None
                and run.status == AgentRunStatus.WAITING_CONFIRMATION
            ]
        payloads = self.durable_store.list_pending_reviews()
        records = [SafetyReviewRecord.model_validate(item) for item in payloads]
        for record in records:
            self.get_run(record.run_id)
            self.safety_reviews.restore(record)
        return records

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
            f"Safety review {review.status.value}: {review.decision_reason or review.reason}",
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
                if (
                    self.durable_store is not None
                    and current.status in {
                        SafetyReviewStatus.APPROVED,
                        SafetyReviewStatus.REJECTED,
                    }
                ):
                    self.flush_events()
                    _, _, _, persisted_event = self.durable_store.decide_pending_review(
                        review_id=review_id,
                        review=current.model_dump(mode="json"),
                        event=self._safety_review_decision_event(current).model_dump(mode="json"),
                        updated_at=_now_iso(),
                    )
                    if persisted_event is not None:
                        self._cache_persisted_event(
                            AgentRunEvent.model_validate(persisted_event)
                        )
                    run = self.get_run(current.run_id)
                    if run is not None and run.status == AgentRunStatus.WAITING_CONFIRMATION:
                        self.resume_running(current.run_id)
                return current, False
            status = (
                SafetyReviewStatus.APPROVED
                if decision == SafetyReviewDecision.APPROVE
                else SafetyReviewStatus.REJECTED
            )
            decided = current.model_copy(update={
                "status": status,
                "decided_by": decided_by,
                "decision_reason": reason,
                "decided_at": _now_iso(),
            })
            decision_event = self._safety_review_decision_event(decided)
            persisted_event: dict[str, Any] | None = None
            if self.durable_store is not None:
                self.flush_events()
                persisted, transitioned, conflict, persisted_event = self.durable_store.decide_pending_review(
                    review_id=review_id,
                    review=decided.model_dump(mode="json"),
                    event=decision_event.model_dump(mode="json"),
                    updated_at=_now_iso(),
                )
                if conflict:
                    if conflict == "inactive":
                        raise SafetyReviewQueueConflict(
                            "Safety review belongs to an inactive Agent run."
                        )
                    _, _, head = conflict.partition(":")
                    raise SafetyReviewQueueConflict(
                        "Only the first pending safety review can be decided.",
                        head_review_id=head or None,
                    )
                decided = SafetyReviewRecord.model_validate(persisted)
                if not transitioned:
                    self.safety_reviews.restore(decided)
                    if persisted_event is not None:
                        self._cache_persisted_event(AgentRunEvent.model_validate(persisted_event))
                    # Older persisted decisions may have been committed just
                    # before a crash left the run paused. Reconcile that
                    # recoverable state when the decision is replayed.
                    if decided.status in {
                        SafetyReviewStatus.APPROVED,
                        SafetyReviewStatus.REJECTED,
                    }:
                        run = self.get_run(decided.run_id)
                        if run is not None and run.status == AgentRunStatus.WAITING_CONFIRMATION:
                            self.resume_running(decided.run_id)
                    return decided, False
            else:
                pending = self.list_pending_safety_reviews()
                if not pending or pending[0].review_id != review_id:
                    raise SafetyReviewQueueConflict(
                        "Only the first pending safety review can be decided.",
                        head_review_id=pending[0].review_id if pending else None,
                    )
                run = self.get_run(current.run_id)
                if run is None or run.status in {
                    AgentRunStatus.COMPLETED, AgentRunStatus.FAILED,
                    AgentRunStatus.CANCELLED, AgentRunStatus.TIMED_OUT,
                }:
                    raise SafetyReviewQueueConflict(
                        "Safety review belongs to an inactive Agent run."
                    )
                self.safety_reviews.decide(
                    review_id=review_id, decision=decision,
                    decided_by=decided_by, reason=reason, decided_at=decided.decided_at or _now_iso(),
                )
            self.safety_reviews.restore(decided)
            self._persist_review(decided)
            if persisted_event is not None:
                self._cache_persisted_event(AgentRunEvent.model_validate(persisted_event))
            else:
                self.append_event(
                    decided.run_id,
                    "safety_review_decided",
                    f"Safety review {decided.status.value}.",
                    stage="safety_review",
                    payload={"review": decided.model_dump(mode="json")},
                )
            if decided.status in {
                SafetyReviewStatus.APPROVED,
                SafetyReviewStatus.REJECTED,
            }:
                self.resume_running(decided.run_id)
            return decided, True

    def _cache_persisted_event(self, event: AgentRunEvent) -> None:
        """Mirror an event already committed by a multi-row SQLite transaction."""
        with self._lock:
            if self.durable_store is not None:
                # Reload the ordered durable stream so subsequent events use a
                # sequence after any event allocated by another manager.
                self._events[event.run_id] = self._load_events(event.run_id)
                return
            events = self._events.setdefault(event.run_id, [])
            review_id = event.payload.get("review", {}).get("review_id")
            if any(
                item.event_id == event.event_id
                or (
                    review_id
                    and item.type == "safety_review_decided"
                    and item.payload.get("review", {}).get("review_id") == review_id
                )
                for item in events
            ):
                return
            events.append(event)
            events.sort(key=lambda item: item.sequence)

    @staticmethod
    def _safety_review_decision_event(review: SafetyReviewRecord) -> AgentRunEvent:
        return AgentRunEvent(
            event_id="pending_safety_review_decision",
            run_id=review.run_id,
            sequence=0,
            type="safety_review_decided",
            stage="safety_review",
            message=f"Safety review {review.status.value}: {review.decision_reason or review.reason}",
            payload={"review": review.model_dump(mode="json")},
            created_at=_now_iso(),
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
                AgentRunStatus.TIMED_OUT,
            }:
                return current
            # Completion and cancellation must have one durable winner.  A graph
            # finalizer writes the result artifact first, then claims completion
            # under this same lock before it may write user-visible side effects.
            # Once that claim exists, cancellation deliberately loses rather than
            # producing a cancelled run with a completed answer/event stream.
            if current.metadata.get("completion_claimed") is True:
                return current
            previous: list[AgentRunRecord] = []
            updated: list[AgentRunRecord] = []
            events: list[AgentRunEvent] = []

            def stage(current_run: AgentRunRecord, current_reason: str | None) -> None:
                if current_run.status in {
                    AgentRunStatus.COMPLETED, AgentRunStatus.FAILED,
                    AgentRunStatus.CANCELLED, AgentRunStatus.TIMED_OUT,
                } or current_run.metadata.get("completion_claimed") is True:
                    return
                for child_id in current_run.child_run_ids:
                    child = self.get_run(child_id)
                    if child is not None:
                        stage(child, f"Parent run cancelled: {current_reason or 'cancelled'}")
                now = _now_iso()
                metadata = dict(current_run.metadata)
                if current_run.run_id not in self._cancel_requests:
                    metadata.update({
                        "cancel_requested": True,
                        "cancel_reason": current_reason,
                        "cancel_requested_at": now,
                    })
                    events.append(self._control_event(
                        current_run.run_id, "run_cancel_requested",
                        current_reason or "Cancellation requested.",
                        stage="control", payload={"reason": current_reason}, pending=events,
                    ))
                if not any(event.type == "run_cancelled" for event in self._events[current_run.run_id]):
                    events.append(self._control_event(
                        current_run.run_id, "run_cancelled",
                        current_reason or "Agent run cancelled.",
                        stage="run", payload={"reason": current_reason}, pending=events,
                    ))
                changes: dict[str, Any] = {
                    "status": AgentRunStatus.CANCELLED,
                    "metadata": metadata,
                    "cancelled_at": now,
                    "error_type": "cancelled",
                }
                if current_reason is not None:
                    changes["error"] = current_reason
                previous.append(current_run)
                updated.append(current_run.model_copy(update=changes))
                if current_run.parent_run_id is not None:
                    events.append(self._control_event(
                        current_run.run_id, "subtask_cancelled", "Subtask cancelled.",
                        stage="subtask", refs=self._child_event_refs(current_run), pending=events,
                    ))

            stage(current, reason)
            self._commit_control_updates(previous, updated, events)
            return self._runs[run_id]

    def claim_completion(self, run_id: str) -> bool:
        """Atomically reserve completion if cancellation has not already won.

        The reservation is durable so replaying ``finalize_run`` after a crash is
        safe: it may continue the idempotent finalization effects, while a later
        cancel request cannot turn the same run into ``cancelled``.
        """

        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            if current.status in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
                AgentRunStatus.TIMED_OUT,
            }:
                return current.status == AgentRunStatus.COMPLETED
            if run_id in self._cancel_requests or current.metadata.get("cancel_requested") is True:
                return False
            if current.metadata.get("completion_claimed") is True:
                return True
            metadata = dict(current.metadata)
            metadata.update(
                {
                    "completion_claimed": True,
                    "completion_claimed_at": _now_iso(),
                }
            )
            updated = current.model_copy(update={"metadata": metadata})
            self._persist_run(updated)
            self._runs[run_id] = updated
            return True

    def fail_completion_claim(
        self, run_id: str, *, error_type: str, error: str, log_path: str | None = None
    ) -> AgentRunRecord:
        """Release a completion reservation only by durably failing the run."""

        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            if current.status in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
                AgentRunStatus.TIMED_OUT,
            }:
                return current
            metadata = dict(current.metadata)
            metadata.pop("completion_claimed", None)
            metadata.pop("completion_claimed_at", None)
            updated = current.model_copy(
                update={
                    "status": AgentRunStatus.FAILED,
                    "failed_at": _now_iso(),
                    "error_type": error_type,
                    "error": error,
                    "log_path": log_path or current.log_path,
                    "metadata": metadata,
                }
            )
            self._persist_run(updated)
            self._runs[run_id] = updated
            return updated

    def complete_run(
        self,
        run_id: str,
        result_snapshot: dict[str, Any] | None = None,
        log_path: str | None = None,
    ) -> AgentRunRecord:
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            if current.status in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
                AgentRunStatus.TIMED_OUT,
            }:
                return current
            if run_id in self._cancel_requests or current.metadata.get("cancel_requested") is True:
                return current
            active_children = [
                child.run_id for child in self.child_tree(run_id)
                if child.status not in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED, AgentRunStatus.TIMED_OUT}
            ]
            if active_children:
                raise ValueError("Cannot complete a parent run with active child runs.")
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
        parent_run_id: str | None = None,
        child_run_id: str | None = None,
        plan_id: str | None = None,
        step_id: str | None = None,
        attempt: int | None = None,
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
                parent_run_id=parent_run_id,
                child_run_id=child_run_id,
                plan_id=plan_id,
                step_id=step_id,
                attempt=attempt,
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

    def request_pause(self, run_id: str) -> AgentRunRecord:
        """Persist intent; only the checkpoint owner may acknowledge a safe pause."""
        with self._lock:
            current = self.get_run(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            if current.status == AgentRunStatus.PAUSED or current.metadata.get("pause_requested"):
                return current
            if any(child.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING, AgentRunStatus.PAUSED,
                                    AgentRunStatus.WAITING_USER, AgentRunStatus.WAITING_CONFIRMATION}
                   for child in self.child_tree(run_id)):
                raise ValueError("Interrupt requires child tasks to finish or be cancelled first.")
            if current.status not in {
                AgentRunStatus.QUEUED, AgentRunStatus.RUNNING,
                AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER,
            } or current.metadata.get("completion_claimed") or current.metadata.get("cancel_requested"):
                raise ValueError("Agent run can no longer be interrupted.")
            updated = current.model_copy(update={"metadata": {
                **current.metadata, "pause_requested": True, "pause_requested_at": _now_iso(),
            }})
            event = self._control_event(run_id, "run_pause_requested", "Pause requested; waiting for a checkpoint.",
                                        stage="control", pending=[])
            self._commit_control_updates([current], [updated], [event])
            return updated

    def acknowledge_pause(self, run_id: str) -> AgentRunRecord:
        """Called with the graph lease held, after checkpoint persistence drained."""
        with self._lock:
            current = self.get_run(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            if current.status == AgentRunStatus.PAUSED:
                return current
            if (not current.metadata.get("pause_requested") or current.metadata.get("completion_claimed")
                    or current.status not in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING,
                                              AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER}):
                return current
            now = _now_iso()
            updated = current.model_copy(update={"status": AgentRunStatus.PAUSED, "paused_at": now,
                "metadata": {**current.metadata, "paused_from_status": current.status.value}})
            event = self._control_event(run_id, "run_paused", "Agent run paused at a saved checkpoint.",
                                        stage="control", pending=[])
            self._commit_control_updates([current], [updated], [event])
            return updated

    def resume_paused(self, run_id: str) -> AgentRunRecord:
        """Restore the exact pre-pause state, without bypassing questions/reviews."""
        with self._lock:
            current = self.get_run(run_id)
            if current is None or current.status != AgentRunStatus.PAUSED:
                raise ValueError("Agent run is not paused.")
            status = AgentRunStatus(current.metadata["paused_from_status"])
            updated = current.model_copy(update={"status": status, "paused_at": None,
                "metadata": {**current.metadata, "pause_requested": False, "paused_from_status": None}})
            event = self._control_event(run_id, "run_resumed", "Agent run resumed from its checkpoint.",
                                        stage="control", pending=[])
            self._commit_control_updates([current], [updated], [event])
            return updated

    def replace_paused(self, run_id: str, *, user_input: str, command_id: str,
                       request_fingerprint: str) -> AgentRunRecord:
        """Atomically abandon a paused root and queue its idempotent replacement."""
        with self._lock:
            current = self.get_run(run_id)
            if current is None:
                raise KeyError(f"Agent run not found: {run_id}")
            prior = current.metadata.get("replacement_command_id")
            if prior is not None:
                if prior != command_id or current.metadata.get("replacement_fingerprint") != request_fingerprint:
                    raise ValueError("Agent run was replaced by a different command.")
                replacement = self.get_run(current.metadata["superseded_by_run_id"])
                if replacement is None:
                    raise RuntimeError("Replacement run is unavailable.")
                return replacement
            if current.status != AgentRunStatus.PAUSED or current.parent_run_id is not None:
                raise ValueError("Only a checkpoint-paused root run can be replaced.")
            if any(child.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING, AgentRunStatus.PAUSED,
                                    AgentRunStatus.WAITING_USER, AgentRunStatus.WAITING_CONFIRMATION}
                   for child in self.child_tree(run_id)):
                raise ValueError("Active child tasks prevent replacing this run.")
            now = _now_iso()
            trace_id = _stable_id("agent_turn", current.session_id, command_id, now)
            new_id = _stable_id("agent_run", trace_id, now)
            replacement = AgentRunRecord(run_id=new_id, session_id=current.session_id, trace_id=trace_id,
                status=AgentRunStatus.QUEUED, user_input=user_input, created_at=now,
                metadata={"entrypoint": "agent.turn", "replaces_run_id": run_id,
                          **({"orchestrator": "langgraph"} if current.metadata.get("orchestrator") == "langgraph" else {})})
            updated = current.model_copy(update={"status": AgentRunStatus.CANCELLED, "cancelled_at": now,
                "error_type": "cancelled", "error": "Replaced by a new user message.", "metadata": {
                    **current.metadata, "cancel_requested": True, "cancel_requested_at": now,
                    "cancel_reason": "superseded", "pause_requested": False,
                    "replacement_command_id": command_id, "replacement_fingerprint": request_fingerprint,
                    "superseded_by_run_id": new_id,
                }})
            events = [self._control_event(run_id, "run_cancelled", "Replaced by a new user message.",
                stage="control", pending=[], payload={"superseded_by_run_id": new_id}),
                self._control_event(new_id, "run_replacement_created", "Replacement run queued.",
                stage="control", pending=[], payload={"replaces_run_id": run_id})]
            self._commit_control_updates([current], [updated], events, created=[replacement])
            return replacement

    def request_cancel(self, run_id: str, reason: str | None = None) -> None:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(f"Agent run not found: {run_id}")
            current = self._runs[run_id]
            if current.status in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
                AgentRunStatus.TIMED_OUT,
            } or current.metadata.get("completion_claimed") is True or run_id in self._cancel_requests:
                return
            metadata = dict(current.metadata)
            metadata.update(
                {
                    "cancel_requested": True,
                    "cancel_reason": reason,
                    "cancel_requested_at": _now_iso(),
                }
            )
            updated = current.model_copy(update={"metadata": metadata})
            event = self._control_event(
                run_id, "run_cancel_requested", reason or "Cancellation requested.",
                stage="control", payload={"reason": reason}, pending=[],
            )
            self._commit_control_updates([current], [updated], [event])

    def _control_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        stage: str,
        pending: list[AgentRunEvent],
        payload: dict[str, Any] | None = None,
        refs: dict[str, Any] | None = None,
    ) -> AgentRunEvent:
        sequence = len(self._events.get(run_id, [])) + 1 + sum(
            event.run_id == run_id for event in pending
        )
        return AgentRunEvent(
            event_id=f"{run_id}_event_{sequence:06d}",
            run_id=run_id,
            sequence=sequence,
            type=event_type,
            stage=stage,
            message=message,
            payload=payload or {},
            created_at=_now_iso(),
            **(refs or {}),
        )

    def _commit_control_updates(
        self,
        previous: list[AgentRunRecord],
        updated: list[AgentRunRecord],
        events: list[AgentRunEvent],
        *,
        created: list[AgentRunRecord] | None = None,
    ) -> None:
        if not updated:
            return
        if self.durable_store is not None:
            self.flush_events()
            self.durable_store.save_run_control_batch(
                previous=[record.model_dump(mode="json") for record in previous],
                updated=[record.model_dump(mode="json") for record in updated],
                events=[event.model_dump(mode="json") for event in events],
                updated_at=_now_iso(),
                created=[record.model_dump(mode="json") for record in created or []],
            )
        for record in created or []:
            self._runs[record.run_id] = record
            self._events[record.run_id] = []
        for record in updated:
            self._runs[record.run_id] = record
            if record.metadata.get("cancel_requested") is True:
                self._cancel_requests[record.run_id] = record.metadata.get("cancel_reason")
        for event in events:
            self._events[event.run_id].append(event)

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
                AgentRunStatus.TIMED_OUT,
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
