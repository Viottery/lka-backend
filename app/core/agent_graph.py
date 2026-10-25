"""LangGraph-owned ReAct state machine using the project's Agent components."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from contextlib import nullcontext
from contextvars import copy_context
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.core.agent_checkpoints import SqliteCheckpointRuntime
from app.core.agent_dispatch import AgentDispatchFenced
from app.core.agent_runs import AgentRunCancelled, AgentRunRecord, AgentRunStatus
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_turn import (
    AgentTurnDecisionEvent,
    AgentTurnLLMEvent,
    AgentTurnProgressEvent,
    AgentTurnResult,
    AgentTurnToolEvent,
    AgentTurnVerificationWarning,
    AgentTurnWorkingSet,
    _fork_subtasks_function_arguments_example,
    _fork_subtasks_shape_example,
    _stable_id,
    _turn_inference_snapshot,
    _turn_input_content,
    _turn_llm_answer_client_name,
    _turn_llm_answer_model,
    _turn_llm_client_name,
    _turn_llm_model,
    _turn_llm_reasoning_level,
    _turn_llm_response_mode,
    _turn_run_id,
    _turn_run_manager,
    _turn_safety_review_mode,
)
from app.core.child_tool_audit import build_child_tool_audit
from app.core.llm import LLMResponseMode
from app.core.runtime_context import current_time_payload
from app.core.safety import SafetyReviewMode, SafetyReviewRecord, SafetyReviewStatus
from app.core.tools import ToolContext, ToolResult
from app.storage.sqlite_retry import is_sqlite_busy
from app.storage.sqlite_write_queue import SqliteWriteQueueError


def _is_temporary_sqlite_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, SqliteWriteQueueError) or is_sqlite_busy(current):
            return True
        current = current.__cause__
    return False


class AgentGraphState(TypedDict, total=False):
    run_id: str
    graph_thread_id: str
    checkpoint_schema_version: int
    request: dict[str, Any]
    phase: str
    status: Literal[
        "queued",
        "running",
        "waiting_confirmation",
        "waiting_user",
        "completed",
        "failed",
        "cancelled",
    ]
    run_snapshot: dict[str, Any]
    working_set: dict[str, Any]
    runtime_artifact_ref: dict[str, Any]
    result_artifact_ref: dict[str, Any]
    result_summary: dict[str, Any]
    error_summary: dict[str, str]


class AgentTurnWaitingForConfirmation(RuntimeError):
    """A non-stream graph turn stopped at a durable manual safety interrupt."""

    def __init__(self, *, run_id: str, review_id: str | None) -> None:
        super().__init__(f"Agent run is waiting for confirmation: {run_id}")
        self.run_id = run_id
        self.review_id = review_id


class AgentTurnWaitingForUser(RuntimeError):
    """A graph turn stopped at a durable, non-safety user-question interrupt."""

    def __init__(self, *, run_id: str, question_id: str | None) -> None:
        super().__init__(f"Agent run is waiting for a user response: {run_id}")
        self.run_id = run_id
        self.question_id = question_id


class AgentTurnPaused(RuntimeError):
    """Execution stopped after persisting a resumable graph boundary."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Agent run paused: {run_id}")
        self.run_id = run_id


class AgentGraphRunner:
    """LangGraph owns control flow, checkpoints and resume; project code owns semantics."""

    orchestrator_name = "langgraph"

    def __init__(
        self,
        turn_loop: Any,
        *,
        checkpointer: BaseCheckpointSaver | None = None,
        checkpoint_runtime: SqliteCheckpointRuntime | None = None,
        artifact_store: SqliteAgentRunStore | None = None,
        interrupt_after: list[str] | None = None,
    ) -> None:
        self.turn_loop, self.checkpoint_runtime = turn_loop, checkpoint_runtime
        self.artifact_store, self.interrupt_after = artifact_store, interrupt_after
        self._lock = threading.RLock()
        self._execution_locks_guard = threading.Lock()
        self._execution_locks: dict[str, threading.Lock] = {}
        self._results: dict[str, AgentTurnResult] = {}
        self._errors: dict[str, Exception] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        builder = StateGraph(AgentGraphState)
        for name, node in (
            ("initialize_run", self._initialize_run),
            ("prepare_context", self._prepare_context),
            ("route_package", self._route_package),
            ("expand_package", self._expand_package),
            ("decide_next_operation", self._decide),
            ("validate_operation", self._validate),
            ("fork_wait_interrupt", self._fork_wait_interrupt),
            ("child_user_wait_interrupt", self._child_user_wait_interrupt),
            ("user_question_interrupt", self._user_question_interrupt),
            ("safety_gate", self._safety),
            ("manual_review_interrupt", self._manual_review_interrupt),
            ("execute_tool", self._execute),
            ("build_observation", self._observe),
            ("answer", self._answer),
            ("verify_answer", self._verify),
            ("finalize_run", self._finalize),
        ):
            builder.add_node(name, node)
        builder.add_edge(START, "initialize_run")
        builder.add_edge("initialize_run", "prepare_context")
        builder.add_edge("prepare_context", "route_package")
        builder.add_conditional_edges(
            "route_package", self._after_route,
            {"expand": "expand_package", "answer": "answer", "decide": "decide_next_operation"}
        )
        builder.add_edge("expand_package", "decide_next_operation")
        builder.add_edge("decide_next_operation", "validate_operation")
        builder.add_conditional_edges(
            "validate_operation",
            self._after_validate,
            {
                "decide": "decide_next_operation",
                "paused_fork": "validate_operation",
                "fork_wait": "fork_wait_interrupt",
                "child_user_wait": "child_user_wait_interrupt",
                "user_question": "user_question_interrupt",
                "expand": "expand_package",
                "safety": "safety_gate",
                "answer": "answer",
            },
        )
        builder.add_edge("fork_wait_interrupt", "validate_operation")
        builder.add_edge("child_user_wait_interrupt", "validate_operation")
        builder.add_edge("user_question_interrupt", "decide_next_operation")
        builder.add_conditional_edges(
            "safety_gate",
            self._after_safety,
            {
                "interrupt": "manual_review_interrupt",
                "execute": "execute_tool",
                "observe": "build_observation",
            },
        )
        builder.add_edge("manual_review_interrupt", "safety_gate")
        builder.add_edge("execute_tool", "build_observation")
        builder.add_edge("build_observation", "decide_next_operation")
        builder.add_edge("answer", "verify_answer")
        builder.add_edge("verify_answer", "finalize_run")
        builder.add_edge("finalize_run", END)
        self._builder = builder
        self.checkpointer = checkpointer or InMemorySaver()
        self.graph = self._compile(self.checkpointer)

    def close(self) -> None:
        if self.checkpoint_runtime is not None:
            self.checkpoint_runtime.close()
        elif callable(close := getattr(self.checkpointer, "close", None)):
            close()

    def create_run_for_turn(self, **kwargs: Any) -> AgentRunRecord:
        metadata = {**(kwargs.pop("initial_metadata", None) or {}), "orchestrator": "langgraph"}
        return self.turn_loop.create_run_for_turn(**kwargs, initial_metadata=metadata)

    def run(self, **kwargs: Any) -> AgentTurnResult:
        run_id = self._resolve_run_id(**kwargs)
        with self._execution_lease(run_id):
            existing = self._run(run_id)
            if existing.status.value == "paused":
                raise AgentTurnPaused(run_id)
            if existing.status.value == "completed":
                recovered = self._completed_result_from_checkpoint(run_id)
                if recovered is not None:
                    return recovered
            if existing.status.value == "waiting_confirmation":
                raise AgentTurnWaitingForConfirmation(
                    run_id=run_id,
                    review_id=existing.metadata.get("confirmation_id"),
                )
            if existing.status.value == "waiting_user":
                question = existing.metadata.get("pending_user_question")
                raise AgentTurnWaitingForUser(
                    run_id=run_id,
                    question_id=question.get("question_id") if isinstance(question, dict) else None,
                )
            if existing.status.value == "cancelled":
                raise AgentRunCancelled(existing.error or "Agent run cancelled.")
            if existing.status.value != "queued":
                raise RuntimeError(f"Cannot start Agent run {run_id} from {existing.status.value}.")
            if existing.parent_run_id is not None:
                self.turn_loop.run_manager.mark_child_running(run_id)
            self._invoke_sync(run_id=run_id, **kwargs)
            return self._take_result(run_id)

    async def run_async(self, **kwargs: Any) -> AgentTurnResult:
        return await self._run_in_worker(self.run, **kwargs)

    def resume(self, run_id: str) -> AgentTurnResult:
        with self._execution_lease(run_id):
            current = self._run(run_id)
            ancestor_id = current.parent_run_id
            while ancestor_id is not None:
                ancestor = self._run(ancestor_id)
                if ancestor.status.value in {"paused", "cancelled"} or ancestor.metadata.get("pause_requested"):
                    raise RuntimeError("Resume the task tree root before resuming a child.")
                ancestor_id = ancestor.parent_run_id
            was_paused = current.status.value == "paused"
            if was_paused:
                snapshot = self.get_state(run_id)
                if not snapshot.values or not snapshot.next:
                    raise RuntimeError("Paused Agent run has no resumable checkpoint.")
                current = self.turn_loop.run_manager.resume_paused(run_id)
            if current.status.value == "completed":
                recovered = self._completed_result_from_checkpoint(run_id)
                if recovered is not None:
                    return recovered
                return self._take_result(run_id)
            if current.status.value == "waiting_confirmation":
                raise AgentTurnWaitingForConfirmation(
                    run_id=run_id,
                    review_id=current.metadata.get("confirmation_id"),
                )
            if current.status.value == "waiting_user":
                question = current.metadata.get("pending_user_question")
                raise AgentTurnWaitingForUser(
                    run_id=run_id,
                    question_id=question.get("question_id") if isinstance(question, dict) else None,
                )
            if current.status.value in {"failed", "cancelled", "timed_out"}:
                raise RuntimeError(
                    f"Cannot resume terminal Agent run {run_id} ({current.status.value})."
                )
            if current.status.value != "running" and not (was_paused and current.status.value == "queued"):
                raise RuntimeError(f"Cannot resume Agent run {run_id} from {current.status.value}.")
            boundary_resume = was_paused or bool(current.metadata.get("checkpoint_boundary_resume"))
            if current.metadata.get("checkpoint_boundary_resume"):
                self.turn_loop.run_manager._update_run(run_id, status=current.status,
                                                      metadata_patch={"checkpoint_boundary_resume": False})
            self._resume_sync(run_id, boundary_resume=boundary_resume)
            return self._take_result(run_id)

    def _execution_lease(self, run_id: str) -> threading.Lock:
        """Return the process-wide per-run graph execution lease.

        Scheduler child execution and HTTP resume both enter this runner.  A
        shared lease serializes checkpoint mutation and side effects even when
        the callers originate in separate task registries.
        """
        with self._execution_locks_guard:
            return self._execution_locks.setdefault(run_id, threading.Lock())

    async def resume_async(self, run_id: str) -> AgentTurnResult:
        return await self._run_in_worker(self.resume, run_id)

    def interrupt(self, run_id: str) -> AgentRunRecord:
        """Request a cooperative pause, acknowledging idle checkpoint owners immediately."""
        run = self._run(run_id)
        if run.parent_run_id is not None:
            raise ValueError("Interrupt the root run, not a scheduler-owned child.")
        self.turn_loop.run_manager.request_pause(run_id)
        # Queued children have no side effects/checkpoint to drain. Waiting children
        # already relinquished execution. Running graph children acknowledge themselves.
        for child in reversed(self.turn_loop.run_manager.child_tree(run_id)):
            if ((child.status.value == "queued" and not self.turn_loop.run_manager.is_child_execution_active(child.run_id)) or
                    (child.status.value in {"waiting_confirmation", "waiting_user"}
                     and child.metadata.get("executor_kind", "react") == "react")):
                child_lease = self._execution_lease(child.run_id)
                if child_lease.acquire(blocking=False):
                    try:
                        self.turn_loop.run_manager.acknowledge_pause(child.run_id)
                    finally:
                        child_lease.release()
            elif child.status.value == "running":
                child_lease = self._execution_lease(child.run_id)
                if child_lease.acquire(blocking=False):
                    try:
                        snapshot = self.get_state(child.run_id)
                        if snapshot.values and snapshot.next:
                            self.turn_loop.run_manager.acknowledge_pause(child.run_id)
                    finally:
                        child_lease.release()
        lease = self._execution_lease(run_id)
        if lease.acquire(blocking=False):
            try:
                snapshot = self.get_state(run_id)
                if snapshot.values and snapshot.next:
                    self.turn_loop.run_manager.acknowledge_pause(run_id)
            finally:
                lease.release()
        return self._run(run_id)

    @staticmethod
    async def _run_in_worker(call: Any, *args: Any, **kwargs: Any) -> AgentTurnResult:
        """Run synchronous graph work without tying it to an event loop executor.

        Graph nodes intentionally use synchronous project services and may create
        short-lived event loops for provider streaming.  A dedicated daemon
        worker mirrors the legacy turn runner and keeps SSE task completion
        independent from ``asyncio``'s default-executor shutdown lifecycle.
        """

        result_queue: queue.Queue[tuple[bool, AgentTurnResult | BaseException]] = queue.Queue(
            maxsize=1
        )

        def target() -> None:
            try:
                result_queue.put((True, call(*args, **kwargs)))
            except BaseException as exc:  # noqa: BLE001 - propagate graph boundary failures.
                result_queue.put((False, exc))

        context = copy_context()
        worker = threading.Thread(
            target=lambda: context.run(target),
            name="lka-agent-graph",
            daemon=True,
        )
        worker.start()
        while True:
            try:
                ok, value = result_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            if ok:
                return value  # type: ignore[return-value]
            raise value

    def _resume_sync(self, run_id: str, *, boundary_resume: bool = False) -> None:
        graph = self.graph
        if self.checkpoint_runtime is not None:
            with self.checkpoint_runtime.sync_saver() as saver:
                graph = self._compile(saver)
                snapshot = graph.get_state(self._config(run_id))
                self._requests[run_id] = dict(snapshot.values["request"])
                self._restore(snapshot.values)
                self._invoke_graph_sync(graph, self._resume_input(snapshot, run_id, boundary_resume), run_id)
                return
        snapshot = graph.get_state(self._config(run_id))
        self._requests[run_id] = dict(snapshot.values["request"])
        self._restore(snapshot.values)
        self._invoke_graph_sync(graph, self._resume_input(snapshot, run_id, boundary_resume), run_id)

    @staticmethod
    def _resume_input(snapshot: Any, run_id: str, boundary_resume: bool) -> Any:
        # A cooperative boundary has no dynamic interrupt to consume. Existing
        # safety/user interrupts continue using their original Command protocol.
        if boundary_resume and not any(task.interrupts for task in snapshot.tasks):
            return None
        return Command(resume={"run_id": run_id})

    async def _invoke(
        self,
        *,
        run_id: str,
        session_id: str | None,
        user_input: str,
        attachment_ids: list[str] | None = None,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_answer_client_name: str | None = None,
        llm_answer_model: str | None = None,
        llm_reasoning_level: str | None = None,
        llm_reasoning_source: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        safety_review_mode: SafetyReviewMode | None = None,
        existing_run_id: str | None = None,
    ) -> None:
        state: AgentGraphState = {
            "run_id": run_id,
            "graph_thread_id": run_id,
            "checkpoint_schema_version": 1,
            "phase": "created",
            "status": "queued",
            "request": {
                "session_id": session_id,
                "user_input": user_input,
                "attachment_ids": attachment_ids or [],
                "llm_client_name": llm_client_name,
                "llm_model": llm_model,
                "llm_answer_client_name": llm_answer_client_name,
                "llm_answer_model": llm_answer_model,
                "llm_reasoning_level": llm_reasoning_level,
                "llm_reasoning_source": llm_reasoning_source,
                "llm_response_mode": llm_response_mode.value,
                "safety_review_mode": safety_review_mode.value if safety_review_mode else None,
            },
        }
        # Compatibility hook used by recovery tests and internal callers.  The
        # public async entrypoint already dispatches to a worker thread.
        self._invoke_graph_input(state, run_id)

    def _invoke_sync(
        self,
        *,
        run_id: str,
        session_id: str | None,
        user_input: str,
        attachment_ids: list[str] | None = None,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_answer_client_name: str | None = None,
        llm_answer_model: str | None = None,
        llm_reasoning_level: str | None = None,
        llm_reasoning_source: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        safety_review_mode: SafetyReviewMode | None = None,
        existing_run_id: str | None = None,
    ) -> None:
        state: AgentGraphState = {
            "run_id": run_id,
            "graph_thread_id": run_id,
            "checkpoint_schema_version": 1,
            "phase": "created",
            "status": "queued",
            "request": {
                "session_id": session_id,
                "user_input": user_input,
                "attachment_ids": attachment_ids or [],
                "llm_client_name": llm_client_name,
                "llm_model": llm_model,
                "llm_answer_client_name": llm_answer_client_name,
                "llm_answer_model": llm_answer_model,
                "llm_reasoning_level": llm_reasoning_level,
                "llm_reasoning_source": llm_reasoning_source,
                "llm_response_mode": llm_response_mode.value,
                "safety_review_mode": safety_review_mode.value if safety_review_mode else None,
            },
        }
        self._invoke_graph_input(state, run_id)

    def _invoke_graph_input(self, state: AgentGraphState, run_id: str) -> None:
        self._requests[run_id] = dict(state["request"])
        if self.checkpoint_runtime is not None:
            with self.checkpoint_runtime.sync_saver() as saver:
                self._invoke_graph_sync(self._compile(saver), state, run_id)
        else:
            self._invoke_graph_sync(self.graph, state, run_id)

    async def _resume(self, run_id: str) -> None:
        await self._resume_in_loop(run_id)

    async def _resume_in_loop(self, run_id: str) -> None:
        graph = self.graph
        if self.checkpoint_runtime is not None:
            async with self.checkpoint_runtime.async_saver() as saver:
                graph = self._compile(saver)
                snapshot = await graph.aget_state(self._config(run_id))
                self._restore(snapshot.values)
                await self._ainvoke_graph(graph, Command(resume={"run_id": run_id}), run_id)
                return
        snapshot = await graph.aget_state(self._config(run_id))
        self._restore(snapshot.values)
        await self._ainvoke_graph(graph, Command(resume={"run_id": run_id}), run_id)

    async def _ainvoke(self, state: AgentGraphState, run_id: str) -> None:
        await self._ainvoke_in_loop(state, run_id)

    async def _ainvoke_in_loop(self, state: AgentGraphState, run_id: str) -> None:
        if self.checkpoint_runtime is not None:
            async with self.checkpoint_runtime.async_saver() as saver:
                await self._ainvoke_graph(self._compile(saver), state, run_id)
        else:
            await self._ainvoke_graph(self.graph, state, run_id)

    async def _ainvoke_graph(self, graph: Any, value: Any, run_id: str) -> None:
        self._run(run_id)
        request = self._request_for_run(run_id)
        tokens = (
            _turn_llm_client_name.set(request.get("llm_client_name")),
            _turn_llm_model.set(request.get("llm_model")),
            _turn_llm_answer_client_name.set(request.get("llm_answer_client_name")),
            _turn_llm_answer_model.set(request.get("llm_answer_model")),
            _turn_llm_response_mode.set(
                LLMResponseMode(str(request.get("llm_response_mode", LLMResponseMode.TEXT.value)))
            ),
            _turn_run_manager.set(self.turn_loop.run_manager),
            _turn_run_id.set(run_id),
            _turn_safety_review_mode.set(
                SafetyReviewMode(request["safety_review_mode"])
                if request.get("safety_review_mode")
                else None
            ),
        )
        reasoning_token = _turn_llm_reasoning_level.set(request.get("llm_reasoning_level"))
        inference_token = None
        content_token = _turn_input_content.set((self._run(run_id).session_id, tuple(request.get("attachment_ids") or ())))
        try:
            run = self._run(run_id)
            inference_token = _turn_inference_snapshot.set(
                self.turn_loop._inference_snapshot_for_run(run)
            )
            counter = self.turn_loop._selected_session_counter()
            scope = (
                self.turn_loop.session_service.use_context_token_counter(counter)
                if counter is not None else nullcontext()
            )
            with scope:
                await graph.ainvoke(value, config=self._config(run_id))
        except Exception as exc:  # noqa: BLE001 - graph boundary persists every failure.
            self._fail(run_id, exc)
        finally:
            if inference_token is not None:
                _turn_inference_snapshot.reset(inference_token)
            _turn_input_content.reset(content_token)
            _turn_safety_review_mode.reset(tokens[7])
            _turn_run_id.reset(tokens[6])
            _turn_run_manager.reset(tokens[5])
            _turn_llm_response_mode.reset(tokens[4])
            _turn_llm_answer_model.reset(tokens[3])
            _turn_llm_answer_client_name.reset(tokens[2])
            _turn_llm_model.reset(tokens[1])
            _turn_llm_client_name.reset(tokens[0])
            _turn_llm_reasoning_level.reset(reasoning_token)

    def _invoke_graph_sync(self, graph: Any, value: Any, run_id: str) -> None:
        self._run(run_id)
        request = self._request_for_run(run_id)
        tokens = (
            _turn_llm_client_name.set(request.get("llm_client_name")),
            _turn_llm_model.set(request.get("llm_model")),
            _turn_llm_answer_client_name.set(request.get("llm_answer_client_name")),
            _turn_llm_answer_model.set(request.get("llm_answer_model")),
            _turn_llm_response_mode.set(
                LLMResponseMode(str(request.get("llm_response_mode", LLMResponseMode.TEXT.value)))
            ),
            _turn_run_manager.set(self.turn_loop.run_manager),
            _turn_run_id.set(run_id),
            _turn_safety_review_mode.set(
                SafetyReviewMode(request["safety_review_mode"])
                if request.get("safety_review_mode")
                else None
            ),
        )
        reasoning_token = _turn_llm_reasoning_level.set(request.get("llm_reasoning_level"))
        inference_token = None
        content_token = _turn_input_content.set((self._run(run_id).session_id, tuple(request.get("attachment_ids") or ())))
        try:
            run = self._run(run_id)
            inference_token = _turn_inference_snapshot.set(
                self.turn_loop._inference_snapshot_for_run(run)
            )
            counter = self.turn_loop._selected_session_counter()
            scope = (
                self.turn_loop.session_service.use_context_token_counter(counter)
                if counter is not None else nullcontext()
            )
            with scope:
                stream = graph.stream(value, config=self._config(run_id), stream_mode="values")
                try:
                    for _state in stream:
                        if self._run(run_id).metadata.get("pause_requested"):
                            break
                finally:
                    # Closing drains LangGraph's pending checkpoint writes before
                    # publishing paused. Never interrupt inside a side-effect node.
                    stream.close()
                self.turn_loop.run_manager.acknowledge_pause(run_id)
        except Exception as exc:  # noqa: BLE001 - graph boundary persists every failure.
            self._fail(run_id, exc)
        finally:
            if inference_token is not None:
                _turn_inference_snapshot.reset(inference_token)
            _turn_input_content.reset(content_token)
            _turn_safety_review_mode.reset(tokens[7])
            _turn_run_id.reset(tokens[6])
            _turn_run_manager.reset(tokens[5])
            _turn_llm_response_mode.reset(tokens[4])
            _turn_llm_answer_model.reset(tokens[3])
            _turn_llm_answer_client_name.reset(tokens[2])
            _turn_llm_model.reset(tokens[1])
            _turn_llm_client_name.reset(tokens[0])
            _turn_llm_reasoning_level.reset(reasoning_token)

    def _initialize_run(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        run = self._run(s["run_id"])
        if run.status.value == "queued":
            self.turn_loop.run_manager.mark_running(run.run_id)
        if not any(
            event.type == "run_started"
            for event in self.turn_loop.run_manager.list_events(run.run_id)
        ):
            self.turn_loop.run_manager.append_event(
                run.run_id,
                "run_started",
                "Agent run started.",
                stage="run",
                payload={
                    "session_id": run.session_id,
                    "trace_id": run.trace_id,
                    **self.turn_loop._inference_call_metadata(),
                },
            )
        return {
            "phase": "initialized",
            "status": "running",
            "run_snapshot": self._snapshot(self._run(run.run_id)),
        }

    def _prepare_context(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        run = self._run(s["run_id"])
        request = self._request_for_run(run.run_id)
        turn_user_input = request.get("user_input")
        if not isinstance(turn_user_input, str) or not turn_user_input.strip():
            turn_user_input = run.user_input
        session = self.turn_loop.session_service.ensure_session(
            session_id=run.session_id,
            title=turn_user_input[:60] or "Agent Session",
            metadata={"entrypoint": "agent.turn"},
        )
        turn_user_input, _input_message = self.turn_loop._persist_turn_input(
            session_id=session.session_id, run_id=run.run_id, trace_id=run.trace_id,
            user_input=turn_user_input, message_id=f"agent_graph_user_{run.run_id}",
        )
        window = self.turn_loop.session_service.get_prompt_context_window(
            session_id=session.session_id, token_budget=self.turn_loop.session_context_token_budget
        ).model_dump(mode="json")
        window["current_time"] = current_time_payload()
        if session.workspace is not None:
            window["workspace"] = session.workspace.model_dump(mode="json")
        if self.turn_loop.instruction_files is not None and self.turn_loop._session_run_policy().use_local_context:
            workspace_path = (
                self.turn_loop._workspace_for_run(session, run.run_id)
            )
            window["agent_instructions"] = self.turn_loop.instruction_files.for_workspace(
                workspace_path
            )
        recalled = self.turn_loop._memory_context_for_turn(
                session.session_id,
                self.turn_loop._workspace_for_run(session, run.run_id),
                turn_user_input,
        )
        if recalled is not None:
            window["recalled_memories"] = recalled
        cached = self.turn_loop._cached_tool_observations_from_session(
            session_id=session.session_id,
            query=turn_user_input,
            context=self._context(
                AgentTurnWorkingSet(
                    run_id=run.run_id,
                    session_id=session.session_id,
                    trace_id=run.trace_id,
                    user_input=turn_user_input,
                )
            ),
        )
        if cached:
            window["cached_tool_observations"] = cached
        self.turn_loop._assess_configured_fast_path(
            run_id=run.run_id,
            session_id=session.session_id,
            user_input=turn_user_input,
        )
        data = {
            "context_window": window,
            "package_catalog": self.turn_loop._package_catalog(
                self.turn_loop._tool_view_for_run(run.run_id)
            ),
            "expanded_tools": [],
            "observations": list(cached) + self._replacement_handoff(run),
            "successful": [],
            "tool_events": [],
            "llm_events": [],
            "decision_events": [],
            "progress_events": [],
            "warnings": [],
        }
        ref = self._save_data(run.run_id, data)
        ws = AgentTurnWorkingSet(
            run_id=run.run_id,
            session_id=session.session_id,
            trace_id=run.trace_id,
            user_input=turn_user_input,
            context_artifact_ref=ref,
        )
        return {
            "phase": "context_prepared",
            "working_set": ws.model_dump(mode="json"),
            "runtime_artifact_ref": ref,
        }

    def _route_package(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        llm = self._models(data["llm_events"], AgentTurnLLMEvent)
        decisions = self._models(data["decision_events"], AgentTurnDecisionEvent)
        run = self._run(ws.run_id)
        tool_view = self.turn_loop._tool_view_for_run(ws.run_id)
        snapshot = run.metadata.get("context_snapshot")
        # A single-package shortcut is safe only for a child whose persisted
        # server-owned ToolView and ContextSnapshot are bound to this exact run.
        # The package catalog is re-derived from the live registry and that
        # ToolView, so stale/forged catalogs and unregistered packages cannot
        # broaden the child's capabilities.
        child_context_is_bound = bool(
            run.parent_run_id
            and tool_view is not None
            and tool_view.child_run_id == ws.run_id
            and isinstance(snapshot, dict)
            and snapshot.get("child_run_id") == ws.run_id
            and snapshot.get("snapshot_id") == tool_view.snapshot_id
        )
        authorized_catalog = (
            self.turn_loop._package_catalog(tool_view) if child_context_is_bound else []
        )
        if child_context_is_bound and len(authorized_catalog) == 1:
            selected = str(authorized_catalog[0]["name"])
            route = {
                "selected_package": selected,
                "reason": "Only one registered package is authorized by this child snapshot.",
            }
            self.turn_loop._record_route_decision(
                decisions, source="local", route=route, raw_output=None
            )
        elif (
            getattr(self.turn_loop, "unified_entry_enabled", False)
            and self.turn_loop.llm_client is not None
        ):
            route = {"selected_package": None, "entry_mode": "unified",
                     "reason": "The first ReAct operation selects capabilities on demand."}
        else:
            route = self.turn_loop._route(
                user_input=ws.user_input,
                package_catalog=data["package_catalog"],
                context_window=data["context_window"],
                llm_events=llm,
                decision_events=decisions,
            )
        data["llm_events"] = self._dump(llm)
        data["decision_events"] = self._dump(decisions)
        selected = route.get("selected_package")
        progress = self._models(data["progress_events"], AgentTurnProgressEvent)
        self.turn_loop._append_progress(
            progress,
            type=("entry_ready" if route.get("entry_mode") == "unified" else
                  "package_selected" if isinstance(selected, str) else "no_package"),
            stage="route",
            package_name=selected if isinstance(selected, str) else None,
            status="completed",
            message=f"Selected `{selected}` package."
            if isinstance(selected, str)
            else "Capabilities will be selected by the first operation."
            if route.get("entry_mode") == "unified"
            else "No tool package selected; answering from context if possible.",
            metadata={"reason": route.get("reason")},
        )
        data["progress_events"] = self._dump(progress)
        ws.route = route
        ws.initial_package = selected if isinstance(selected, str) else None
        return self._save(s, ws, data, "entry_ready" if route.get("entry_mode") == "unified" else "routed")

    def _expand_package(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        requested_package = ws.pending_decision.get("package_name")
        package = requested_package or ws.initial_package
        tool_view = self.turn_loop._tool_view_for_run(ws.run_id)
        # Legacy turns expand the route-selected package before entering the
        # decision loop.  That bootstrap is reflected by expanded_tools, not by
        # a decision observation.  Only an expansion explicitly requested by a
        # later decision belongs in observations, where it can inform the next
        # decision just like the legacy dynamic-expansion path.
        is_initial_expansion = requested_package is None
        if not isinstance(package, str) or not self.turn_loop._package_exists(
            package, tool_view=tool_view
        ):
            if not is_initial_expansion:
                data["observations"].append(
                    {
                        "action": "expand_package",
                        "package_name": package,
                        "status": "rejected",
                        "error": "Tool package is not registered.",
                    }
                )
        elif package in ws.expanded_packages:
            if not is_initial_expansion:
                data["observations"].append(
                    {
                        "action": "expand_package",
                        "package_name": package,
                        "status": "completed",
                        "message": "Tool package was already expanded.",
                    }
                )
        else:
            tools = self.turn_loop._tool_payloads_for_package(package, tool_view=tool_view)
            data["expanded_tools"].extend(tools)
            ws.expanded_packages.append(package)
            if ws.initial_package is None:
                ws.initial_package = package
            names = [str(t["name"]) for t in tools if isinstance(t.get("name"), str)]
            if not is_initial_expansion:
                data["observations"].append(
                    {
                        "action": "expand_package",
                        "package_name": package,
                        "status": "completed",
                        "expanded_tools": names,
                    }
                )
            progress = self._models(data["progress_events"], AgentTurnProgressEvent)
            self.turn_loop._append_progress(
                progress,
                type="package_expanded",
                stage="decision",
                package_name=package,
                status="completed",
                message=f"Expanded `{package}` package with {len(names)} tools.",
                metadata={"expanded_tools": names},
            )
            data["progress_events"] = self._dump(progress)
        ws.pending_decision = {}
        return self._save(s, ws, data, "package_expanded")

    def _decide(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        deadline_reached = self.turn_loop._decision_deadline_reached()
        if ws.step_index >= self.turn_loop.max_decision_steps or deadline_reached:
            ws.terminal_reason = "Execution deadline approaching." if deadline_reached else "Step limit reached after loading evidence."
            progress = self._models(data["progress_events"], AgentTurnProgressEvent)
            self.turn_loop._record_incomplete(progress, "execution_deadline" if deadline_reached else "decision_budget_exhausted")
            data["progress_events"] = self._dump(progress)
            # A pending re-plan rejects final_answer by design. Sending one at
            # the decision limit would loop decide -> reject forever because
            # this branch cannot advance the step counter. Route unresolved
            # plans to the bounded failure answer/finalization path instead.
            ws.pending_decision = {
                "action": (
                    "step_limit_exhausted"
                    if self.turn_loop._multi_agent_replan_pending(ws.run_id)
                    else "final_answer"
                )
            }
            return self._save(s, ws, data, "step_limit")
        llm = self._models(data["llm_events"], AgentTurnLLMEvent)
        d = self.turn_loop._decide_next_action(
            user_input=ws.user_input,
            route=ws.route,
            context_window=data["context_window"],
            package_catalog=data["package_catalog"],
            expanded_package_names=ws.expanded_packages,
            expanded_tools=data["expanded_tools"],
            observations=data["observations"],
            llm_events=llm,
            task_completion=ws.completion_state.context(),
            task_working_state=ws.task_working_state.context(
                remaining_steps=self.turn_loop.max_decision_steps - ws.step_index,
                remaining_seconds=self.turn_loop._remaining_execution_seconds(),
            ),
        )
        progress = self._models(data["progress_events"], AgentTurnProgressEvent)
        self.turn_loop._update_task_progress(ws.task_working_state, d or {}, data["observations"], progress)
        data["progress_events"] = self._dump(progress)
        for observation in reversed(data["observations"]):
            if (
                observation.get("action") == "fork_subtasks_schema_feedback"
                and observation.get("status") == "retry_once"
            ):
                observation["status"] = "consumed"
                break
        data["llm_events"] = self._dump(llm)
        ws.step_index += 1
        ws.pending_decision = d or {"action": "invalid_empty_decision"}
        events = self._models(data["decision_events"], AgentTurnDecisionEvent)
        x = ws.pending_decision
        self.turn_loop._record_decision(
            events,
            source="llm" if d else "local",
            action=str(x.get("action") or ""),
            selected_package=x.get("package_name")
            if isinstance(x.get("package_name"), str)
            else None,
            tool_name=x.get("tool_name"),
            tool_input=x.get("tool_input") if isinstance(x.get("tool_input"), dict) else {},
            answer=x.get("answer") if isinstance(x.get("answer"), str) else None,
            reason=x.get("reason") if isinstance(x.get("reason"), str) else None,
            assistant_message=x.get("assistant_message")
            if isinstance(x.get("assistant_message"), str)
            else None,
            operation=x.get("operation") if isinstance(x.get("operation"), dict) else {},
            raw_output=x.get("_raw_output") if isinstance(x.get("_raw_output"), str) else None,
            step_index=ws.step_index,
        )
        data["decision_events"] = self._dump(events)
        assistant_message = x.get("assistant_message")
        if isinstance(assistant_message, str) and assistant_message:
            progress = self._models(data["progress_events"], AgentTurnProgressEvent)
            self.turn_loop._append_progress(
                progress,
                type="assistant_message",
                stage="decision",
                status="completed",
                tool_name=x.get("tool_name") if isinstance(x.get("tool_name"), str) else None,
                package_name=(
                    x.get("package_name") if isinstance(x.get("package_name"), str) else None
                ),
                message=self.turn_loop._short_text(assistant_message),
                metadata={"action": x.get("action"), "step_index": ws.step_index},
            )
            data["progress_events"] = self._dump(progress)
        return self._save(s, ws, data, "decision_made")

    def _validate(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        x = ws.pending_decision
        action = str(x.get("action") or "")
        if action in {"invalid_structured_decision", "invalid_plain_text_decision", "invalid_empty_decision", "malformed_tool_call", "invalid_final_answer"}:
            progress = self._models(data["progress_events"], AgentTurnProgressEvent)
            self.turn_loop._record_incomplete(progress, "decision_protocol_invalid")
            data["progress_events"] = self._dump(progress)
        if action in {"fork_subtasks", "fork_subtasks_invalid"}:
            self.turn_loop._upgrade_fast_path_for_multi_agent(ws.run_id)
            if action == "fork_subtasks_invalid" and not data.get("fork_format_repair_used"):
                data["fork_format_repair_used"] = True
                operation = x.get("operation")
                operation_id = (
                    str(operation.get("operation_id") or "") if isinstance(operation, dict) else ""
                )
                error = str(x.get("reason") or "Invalid fork schema.")
                data["observations"].append(
                    {
                        "action": "fork_subtasks_schema_feedback",
                        "status": "retry_once",
                        "validation_errors": error,
                        "required_shape_example": _fork_subtasks_shape_example(),
                        "function_arguments_example": _fork_subtasks_function_arguments_example(),
                        "instruction": (
                            "Submit one corrected structured fork_subtasks operation. "
                            "Preserve the intended tasks and do not change authorization or policy."
                        ),
                    }
                )
                self.turn_loop.run_manager.append_event(
                    ws.run_id,
                    "fork_subtasks_schema_retry_requested",
                    "Planner fork shape was invalid; one bounded repair was requested.",
                    stage="planner",
                    payload={"operation_id": operation_id, "validation_errors": error},
                )
                progress = self._models(data["progress_events"], AgentTurnProgressEvent)
                self.turn_loop._append_progress(
                    progress,
                    type="fork_subtasks_schema_retry_requested",
                    stage="planner",
                    status="pending",
                    message="Planner fork shape was invalid; one repair attempt is available.",
                    metadata={"operation_id": operation_id},
                )
                data["progress_events"] = self._dump(progress)
                return self._save(s, ws, data, "fork_subtasks_rejected")
            outcome = self.turn_loop._handle_fork_subtasks_decision(
                run_id=ws.run_id,
                user_input=ws.user_input,
                operation=x.get("operation"),
                parse_error=(x.get("reason") if action == "fork_subtasks_invalid" else None),
            )
            operation_id = outcome.get("operation_id")
            if isinstance(operation_id, str) and operation_id:
                data["observations"] = [
                    item
                    for item in data["observations"]
                    if not (
                        isinstance(item, dict)
                        and item.get("action") == "fork_subtasks"
                        and item.get("operation_id") == operation_id
                    )
                ]
            data["observations"].append({"action": "fork_subtasks", **outcome})
            progress = self._models(data["progress_events"], AgentTurnProgressEvent)
            self.turn_loop._append_progress(
                progress,
                type=(
                    "fork_subtasks_validated"
                    if outcome.get("status") == "validated"
                    else "fork_subtasks_rejected"
                ),
                stage="planner",
                status=str(outcome.get("status") or "rejected"),
                message=str(outcome.get("message") or "Planner fork operation processed."),
                metadata=self.turn_loop._planner_progress_summary(outcome),
            )
            data["progress_events"] = self._dump(progress)
            if outcome.get("execution_status") == "paused":
                return self._save(s, ws, data, "fork_paused")
            if outcome.get("execution_status") == "waiting_user":
                return self._save(s, ws, data, "fork_waiting_user")
            if outcome.get("execution_status") == "waiting_confirmation":
                return self._save(s, ws, data, "fork_waiting_confirmation", "waiting_confirmation")
            return self._save(
                s,
                ws,
                data,
                "fork_subtasks_recorded"
                if outcome.get("status") == "validated"
                else "fork_subtasks_rejected",
            )
        if action in {"plan_patch", "plan_patch_invalid"}:
            outcome = self.turn_loop._handle_plan_patch_decision(
                run_id=ws.run_id,
                operation=x.get("operation"),
                parse_error=x.get("reason") if action == "plan_patch_invalid" else None,
            )
            data["observations"].append({"action": "plan_patch", **outcome})
            progress = self._models(data["progress_events"], AgentTurnProgressEvent)
            self.turn_loop._append_progress(
                progress,
                type="multi_agent_plan_patched"
                if outcome.get("status") in {"applied", "waiting_user", "answer_ready", "aborted"}
                else "multi_agent_plan_patch_rejected",
                stage="planner",
                status=str(outcome.get("status") or "rejected"),
                message=str(outcome.get("message") or "Planner patch processed."),
                metadata=self.turn_loop._planner_progress_summary(outcome),
            )
            data["progress_events"] = self._dump(progress)
            if outcome.get("status") == "waiting_user":
                return self._save(s, ws, data, "plan_waiting_user", "waiting_user")
            if outcome.get("status") == "answer_ready":
                command_id = str(outcome.get("command_id") or "")
                accepted = self.turn_loop.resume_user_question_plan(ws.run_id, command_id)
                if accepted is None:
                    raise RuntimeError("User continuation journal record is missing.")
                question_id, answer = accepted
                self._append_user_answer_observation(
                    data,
                    question_id=question_id,
                    command_id=command_id,
                    answer=answer,
                )
                return self._save(s, ws, data, "plan_patch_answered")
            if outcome.get("execution_status") == "paused":
                return self._save(s, ws, data, "fork_paused")
            if outcome.get("execution_status") == "waiting_confirmation":
                return self._save(s, ws, data, "fork_waiting_confirmation", "waiting_confirmation")
            return self._save(
                s,
                ws,
                data,
                "fork_subtasks_recorded"
                if outcome.get("status") == "applied"
                else "fork_subtasks_rejected",
            )
        if action == "expand_package":
            return self._save(s, ws, data, "operation_expand")
        if action == "final_answer":
            if self.turn_loop._multi_agent_replan_pending(ws.run_id):
                data["observations"].append(
                    {
                        "action": "final_answer",
                        "status": "rejected",
                        "replan_required": True,
                        "error": (
                            "The persisted child plan has unresolved failures. Use a valid "
                            "plan_patch to retry, degrade, ask the user, or abort before answering."
                        ),
                    }
                )
                progress = self._models(data["progress_events"], AgentTurnProgressEvent)
                self.turn_loop._append_progress(
                    progress,
                    type="multi_agent_final_answer_rejected",
                    stage="planner",
                    status="rejected",
                    message="Unresolved child failures require a structured PlanPatch.",
                )
                data["progress_events"] = self._dump(progress)
                ws.pending_decision = {}
                return self._save(s, ws, data, "operation_rejected")
            operation = x.get("operation")
            if ws.completion_state.requirements or (
                isinstance(operation, dict) and operation.get("answer_checks") is not None
            ):
                progress = self._models(data["progress_events"], AgentTurnProgressEvent)
                ready = self.turn_loop._check_task_completion(
                    state=ws.completion_state, decision=x, observations=data["observations"],
                    progress_events=progress, can_continue=ws.step_index < self.turn_loop.max_decision_steps,
                )
                data["progress_events"] = self._dump(progress)
                if not ready:
                    ws.pending_decision = {}
                    return self._save(s, ws, data, "operation_rejected")
            return self._save(s, ws, data, "operation_answer")
        if action in {"request_confirmation", "no_op"}:
            ws.terminal_answer = self.turn_loop._control_stop_answer(x)
            return self._save(s, ws, data, "operation_answer")
        if action != "call_tool":
            ws.terminal_reason = (
                x.get("reason")
                if isinstance(x.get("reason"), str)
                else "No valid next operation was available."
            )
            return self._save(s, ws, data, "operation_answer")
        name = str(x.get("tool_name") or "")
        tool_input = x.get("tool_input") if isinstance(x.get("tool_input"), dict) else {}
        allowed = {
            str(t.get("name")) for t in data["expanded_tools"] if isinstance(t.get("name"), str)
        }
        if name not in allowed or x.get("_native_function_unknown"):
            feedback = self.turn_loop._tool_selection_feedback(
                name, expanded_tools=data["expanded_tools"],
                tool_view=self.turn_loop._tool_view_for_run(ws.run_id),
                observations=data["observations"],
                native_functions=x.get("_native_functions") if x.get("_native_function_unknown") else None,
            )
            data["observations"].append(feedback)
            progress = self._models(data["progress_events"], AgentTurnProgressEvent)
            self.turn_loop._append_progress(
                progress, type="tool_rejected", stage="decision", tool_name=name,
                status="rejected", message=feedback["error"], metadata=feedback,
            )
            data["progress_events"] = self._dump(progress)
            if not feedback["retry_allowed"]:
                ws.terminal_reason = "Repeated invalid tool selection; report the actual blocker, not a global permission outage."
                return self._save(s, ws, data, "operation_answer")
            return self._save(s, ws, data, "operation_rejected")
        fp = self.turn_loop._tool_call_fingerprint(tool_name=name, tool_input=tool_input)
        repeat = (
            bool(x.get("operation", {}).get("repeat_successful_call"))
            if isinstance(x.get("operation"), dict)
            else False
        )
        progress = self._models(data["progress_events"], AgentTurnProgressEvent)
        if self.turn_loop._rejected_tool_repeat_feedback(
            ws.task_working_state, name, tool_input, data["observations"], progress,
        ):
            data["progress_events"] = self._dump(progress)
            ws.pending_decision = {}
            return self._save(s, ws, data, "operation_rejected")
        if fp in data["successful"] and not repeat:
            progress = self._models(data["progress_events"], AgentTurnProgressEvent)
            self.turn_loop._duplicate_tool_feedback(
                ws.task_working_state, name, tool_input, data["observations"], progress,
            )
            data["progress_events"] = self._dump(progress)
            ws.pending_decision = {}
            return self._save(s, ws, data, "operation_rejected")
        ws.pending_invocation_id = _stable_id(
            "tool_invocation", ws.run_id, str(ws.step_index), name, fp
        )
        ws.pending_tool_name = name
        ws.pending_tool_input = dict(tool_input)
        return self._save(s, ws, data, "operation_valid")

    def _fork_wait_interrupt(self, s: AgentGraphState) -> AgentGraphState:
        """Checkpoint the parent coordinator while a child review is pending."""

        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        run = self._run(ws.run_id)
        interrupt(
            {
                "run_id": ws.run_id,
                "confirmation_id": run.metadata.get("confirmation_id"),
                "child_run_ids": data.get("observations", [])[-1].get("waiting_child_run_ids", []),
            }
        )
        current = self._run(ws.run_id)
        if current.status.value == "waiting_confirmation":
            self.turn_loop.run_manager.resume_running(ws.run_id)
        return self._save(s, ws, data, "fork_resumed")

    def _child_user_wait_interrupt(self, s: AgentGraphState) -> AgentGraphState:
        """Park the parent graph while the child's own user question is pending."""
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        last = data.get("observations", [])[-1] if data.get("observations") else {}
        child_run_ids = (
            tuple(
                str(value)
                for value in last.get("waiting_child_run_ids", [])
                if isinstance(value, str)
            )
            if isinstance(last, dict)
            else ()
        )
        payload = {
            "run_id": ws.run_id,
            "child_run_ids": list(child_run_ids),
            "kind": "child_user_question",
        }
        active_statuses = {"queued", "running", "waiting_confirmation", "waiting_user"}
        while True:
            interrupt(payload)
            active = [
                child_id
                for child_id in child_run_ids
                if (child := self.turn_loop.run_manager.get_run(child_id)) is not None
                and child.status.value in active_statuses
            ]
            if not active:
                break
            payload["active_child_run_ids"] = active
        return self._save(s, ws, data, "fork_resumed")

    def _user_question_interrupt(self, s: AgentGraphState) -> AgentGraphState:
        """Pause graph execution until a journaled user answer is committed."""
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        run = self._run(ws.run_id)
        question = run.metadata.get("pending_user_question")
        question_id = question.get("question_id") if isinstance(question, dict) else None
        interrupt(
            {
                "run_id": ws.run_id,
                "question_id": question_id,
                "question": question.get("question") if isinstance(question, dict) else None,
            }
        )
        current = self._run(ws.run_id)
        command_id = current.metadata.get("pending_user_answer_command_id")
        accepted = (
            self.turn_loop.resume_user_question_plan(ws.run_id, command_id)
            if isinstance(command_id, str) and command_id
            else None
        )
        if accepted is None:
            raise RuntimeError("User-question interrupt resumed without a journaled answer.")
        accepted_question_id, answer = accepted
        self._append_user_answer_observation(
            data,
            question_id=accepted_question_id,
            command_id=command_id,
            answer=answer,
        )
        progress = self._models(data["progress_events"], AgentTurnProgressEvent)
        self.turn_loop._append_progress(
            progress,
            type="multi_agent_user_answer_received",
            stage="planner",
            status="completed",
            message="A user continuation was added to the Planner observations.",
            metadata={"question_id": accepted_question_id, "command_id": command_id},
        )
        data["progress_events"] = self._dump(progress)
        return self._save(s, ws, data, "plan_patch_answered")

    @staticmethod
    def _append_user_answer_observation(
        data: dict[str, Any], *, question_id: str, command_id: str, answer: str
    ) -> None:
        if not question_id or not command_id or not answer:
            raise RuntimeError("Journaled user continuation is incomplete.")
        observations = data.get("observations", [])
        if any(
            isinstance(item, dict)
            and item.get("action") == "user_answer"
            and item.get("command_id") == command_id
            for item in observations
        ):
            return
        observations.append(
            {
                "action": "user_answer",
                "question_id": question_id,
                "command_id": command_id,
                "answer": answer,
            }
        )
        data["observations"] = observations

    def _safety(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        name = ws.pending_tool_name
        tool_input = dict(ws.pending_tool_input)
        if not name:
            raise RuntimeError("Validated graph tool operation is missing tool_name.")
        context = self._context(ws)
        progress = self._models(data["progress_events"], AgentTurnProgressEvent)
        llm = self._models(data["llm_events"], AgentTurnLLMEvent)
        rejected, review = self.turn_loop.prepare_graph_safety_review(
            invocation_id=str(ws.pending_invocation_id),
            tool_name=name,
            tool_input=tool_input,
            context=context,
            progress_events=progress,
            llm_events=llm,
        )
        data["progress_events"] = self._dump(progress)
        data["llm_events"] = self._dump(llm)
        if review is not None and review.status == SafetyReviewStatus.PENDING:
            ws.pending_review_id = review.review_id
            return self._save(s, ws, data, "waiting_confirmation", "waiting_confirmation")
        if rejected is not None:
            data["pending_tool_result"] = rejected.model_dump(mode="json")
            events = self._models(data["tool_events"], AgentTurnToolEvent)
            events.append(
                AgentTurnToolEvent(
                    tool_name=name,
                    selected_at=datetime.now(UTC).isoformat(),
                    completed_at=datetime.now(UTC).isoformat(),
                    input=tool_input,
                    result=rejected.model_dump(mode="json"),
                    feedback=self.turn_loop._local_tool_feedback(tool_name=name, result=rejected),
                )
            )
            data["tool_events"] = self._dump(events)
            return self._save(s, ws, data, "safety_rejected")
        data["approved_review"] = review.model_dump(mode="json") if review else None
        return self._save(s, ws, data, "safety_approved")

    def _manual_review_interrupt(self, s: AgentGraphState) -> AgentGraphState:
        """Pause only after the review and its initial progress are checkpointed."""

        self.turn_loop._raise_if_cancel_requested()
        ws = self._ws(s)
        review_id = ws.pending_review_id
        if not review_id:
            raise RuntimeError("Manual review interrupt is missing review_id.")
        interrupt(
            {
                "review_id": review_id,
                "tool_name": ws.pending_tool_name,
                "run_id": s["run_id"],
            }
        )
        return {"phase": "review_resumed"}

    def _execute(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        name = ws.pending_tool_name
        if not name:
            raise RuntimeError("Validated graph tool operation is missing tool_name.")
        events = self._models(data["tool_events"], AgentTurnToolEvent)
        progress = self._models(data["progress_events"], AgentTurnProgressEvent)
        review = (
            SafetyReviewRecord.model_validate(data["approved_review"])
            if data.get("approved_review")
            else None
        )
        result = self.turn_loop.execute_graph_tool(
            invocation_id=str(ws.pending_invocation_id),
            tool_name=name,
            tool_input=dict(ws.pending_tool_input),
            context=self._context(ws),
            approved_review=review,
            tool_events=events,
            progress_events=progress,
        )
        data["tool_events"] = self._dump(events)
        data["progress_events"] = self._dump(progress)
        data["pending_tool_result"] = result.model_dump(mode="json")
        return self._save(s, ws, data, "tool_executed")

    def _observe(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        x = ws.pending_decision
        name = ws.pending_tool_name
        if not name:
            raise RuntimeError("Validated graph tool operation is missing tool_name.")
        inp = dict(ws.pending_tool_input)
        result = ToolResult.model_validate(data["pending_tool_result"])
        llm = self._models(data["llm_events"], AgentTurnLLMEvent)
        feedback = self.turn_loop._check_tool_result(
            user_input=ws.user_input,
            tool_package=self.turn_loop._package_for_tool(name),
            decision=x,
            tool_result=result,
            llm_events=llm,
        )
        data["llm_events"] = self._dump(llm)
        if data["tool_events"]:
            data["tool_events"][-1]["feedback"] = feedback
        data["observations"].append(
            self.turn_loop._observation_for_decision_prompt(
                tool_name=name,
                tool_input=inp,
                tool_result=result,
                feedback=feedback,
                run_id=ws.run_id,
            )
        )
        ws.task_working_state.observe({"tool_name": name, "input": inp,
                                       "result": result.model_dump(mode="json")}, x.get("purpose"))
        if result.status == "completed":
            if self.turn_loop._completed_tool_call_changes_state(
                tool_name=name,
                tool_input=inp,
            ):
                data["successful"] = []
            data["successful"].append(
                self.turn_loop._tool_call_fingerprint(tool_name=name, tool_input=inp)
            )
        ws.used_packages = self.turn_loop._packages_from_tool_events(
            self._models(data["tool_events"], AgentTurnToolEvent)
        )
        ws.active_package = ws.used_packages[-1] if ws.used_packages else None
        ws.pending_invocation_id = None
        ws.pending_tool_name = None
        ws.pending_tool_input = {}
        ws.pending_review_id = None
        ws.pending_decision = {}
        return self._save(s, ws, data, "observation_ready")

    def _answer(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        llm = self._models(data["llm_events"], AgentTurnLLMEvent)
        final_decision = {
            "action": "final_answer",
            "reason": ws.terminal_reason or ws.pending_decision.get("reason"),
            "_task_completion": ws.completion_state.context(),
            "_task_working_state": ws.task_working_state.context(),
        }
        if ws.pending_decision.get("action") == "final_answer":
            operation = ws.pending_decision.get("operation")
            if isinstance(operation, dict) and "answer_checks" in operation:
                # Carry only optional handoff notes, not duplicated reasons or
                # decision-stage prose. The writer normalizes the bounded notes.
                final_decision["operation"] = {
                    "type": "final_answer", "answer_checks": operation["answer_checks"],
                }
        answer = (
            self.turn_loop._unresolved_multi_agent_answer()
            if self.turn_loop._multi_agent_replan_pending(ws.run_id)
            else ws.terminal_answer if ws.pending_decision.get("action") in {"request_confirmation", "no_op"}
            else (
                self.turn_loop._answer_from_context_with_llm(
                    user_input=ws.user_input,
                    route=ws.route,
                    context_window=data["context_window"],
                    llm_events=llm,
                )
                if ws.initial_package is None and not data["observations"] and not ws.completion_state.requirements
                else self.turn_loop._answer_with_llm(
                    user_input=ws.user_input,
                    route=ws.route,
                    context_window=data["context_window"],
                    observations=data["observations"],
                    final_decision=final_decision,
                    llm_events=llm,
                )
            )
        )
        generation_failed = not answer or not answer.strip()
        progress = self._models(data["progress_events"], AgentTurnProgressEvent)
        if generation_failed:
            answer = "我还没有获得足够的有效证据来完成这个请求。请重试或提供更多上下文。"
            decisions = self._models(data["decision_events"], AgentTurnDecisionEvent)
            self.turn_loop._record_decision(
                decisions,
                source="local",
                action="answer_generation_failed",
                reason="No valid generated answer was available.",
                operation={"type": "answer_generation_failed"},
            )
            data["decision_events"] = self._dump(decisions)
            self.turn_loop._append_progress(
                progress,
                type="answer_generation_failed",
                stage="answer",
                status="failed",
                message="No valid generated answer was available.",
                metadata={"missing_requirements": ["answer_generation_failed"]},
            )
        data["llm_events"] = self._dump(llm)
        ws.terminal_answer = answer
        missing = [item.requirement_id for item in ws.completion_state.requirements if item.status != "supported"]
        if ws.completion_state.stop_reason == "requirement_capacity_exhausted":
            missing.append("task_completion_capacity_exhausted")
        if generation_failed:
            missing.insert(0, "answer_generation_failed")
        self.turn_loop._append_progress(
            progress,
            type="final_answer",
            stage="answer",
            status="failed" if generation_failed else "partial" if missing or ws.completion_state.stop_reason or self.turn_loop._task_outcome(progress)["task_outcome"] == "incomplete" else "completed",
            message=self.turn_loop._short_text(answer),
            persist=self._run(ws.run_id).metadata.get("defer_answer_publication") is not True,
            metadata={
                "answer": answer,
                **({"missing_requirements": missing, "completion_stop_reason": ws.completion_state.stop_reason}
                   if missing or ws.completion_state.stop_reason else {}),
            },
        )
        data["progress_events"] = self._dump(progress)
        return self._save(s, ws, data, "answered")

    def _verify(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        warnings = self.turn_loop._verify_final_answer(
            answer=ws.terminal_answer or "",
            tool_events=self._models(data["tool_events"], AgentTurnToolEvent),
        )
        self.turn_loop._complete_configured_fast_path(
            run_id=ws.run_id,
            answer=ws.terminal_answer or "",
        )
        data["warnings"] = self._dump(warnings)
        progress = self._models(data["progress_events"], AgentTurnProgressEvent)
        for warning in warnings:
            self.turn_loop._append_progress(
                progress,
                type="verification_warning",
                stage="verify",
                status=warning.severity,
                message=warning.message,
                metadata=warning.model_dump(mode="json"),
            )
        data["progress_events"] = self._dump(progress)
        return self._save(s, ws, data, "verified")

    def _finalize(self, s: AgentGraphState) -> AgentGraphState:
        self.turn_loop._raise_if_cancel_requested()
        ws, data = self._data(s)
        answer = ws.terminal_answer or ""
        self.turn_loop._finalize_multi_agent_plan(ws.run_id)
        result = AgentTurnResult(
            run_id=ws.run_id,
            session_id=ws.session_id,
            trace_id=ws.trace_id,
            answer=answer,
            **self.turn_loop._task_outcome(self._models(data["progress_events"], AgentTurnProgressEvent)),
            selected_package=ws.initial_package,
            initial_package=ws.initial_package,
            expanded_packages=ws.expanded_packages,
            used_packages=ws.used_packages,
            active_package=ws.active_package,
            package_catalog=data["package_catalog"],
            session_context_window=data["context_window"],
            expanded_tools=data["expanded_tools"],
            decision_events=self._models(data["decision_events"], AgentTurnDecisionEvent),
            tool_events=self._models(data["tool_events"], AgentTurnToolEvent),
            progress_events=self._models(data["progress_events"], AgentTurnProgressEvent),
            verification_warnings=self._models(data["warnings"], AgentTurnVerificationWarning),
            llm_events=self._models(data["llm_events"], AgentTurnLLMEvent),
        )
        result = result.model_copy(update=self.turn_loop._answer_publication(ws.run_id))
        log = self.turn_loop._write_log(result=result, user_input=ws.user_input)
        result = result.model_copy(update={"log_path": str(log)})
        # Persist the recoverable result before competing with cancellation.  The
        # durable completion claim makes cancel/complete mutually exclusive while
        # retaining replay-safe finalization after a process crash.
        ref = self._store_result(result)
        completed_snapshot = {
            "session_id": result.session_id,
            "trace_id": result.trace_id,
            "answer": result.answer,
            "task_outcome": result.task_outcome,
            "stop_reason": result.stop_reason,
            "user_visible": result.user_visible,
            "notification": result.notification,
            "selected_package": result.selected_package,
            "initial_package": result.initial_package,
            "expanded_packages": result.expanded_packages,
            "used_packages": result.used_packages,
            "active_package": result.active_package,
            "result_artifact_ref": ref,
        }
        message_payload = {
            "run_id": ws.run_id, "trace_id": ws.trace_id,
            "task_outcome": result.task_outcome, "stop_reason": result.stop_reason,
            "initial_package": ws.initial_package, "expanded_packages": ws.expanded_packages,
            "used_packages": ws.used_packages, "active_package": ws.active_package,
            "log_path": str(log), "tool_events": data.get("tool_events", []),
            "memory_project_id": (
                data["context_window"].get("recalled_memories", {}).get("project_id")
                if isinstance(data["context_window"].get("recalled_memories"), dict) else None
            ),
            "workspace_backend_path": (
                data["context_window"].get("workspace", {}).get("backend_path")
                if isinstance(data["context_window"].get("workspace"), dict)
                else self.turn_loop._workspace_for_run(
                    self.turn_loop.session_service.ensure_session(session_id=ws.session_id), ws.run_id
                )
            ),
        }
        self.turn_loop._raise_if_cancel_requested()
        handler = getattr(self.turn_loop, "finalization_handler", None)
        plan = self._run(ws.run_id).metadata.get("multi_agent_plan")
        plan_failed = isinstance(plan, dict) and plan.get("status") == "failed"
        if handler is not None and not plan_failed and not self.turn_loop._multi_agent_replan_pending(ws.run_id):
            handled = handler(
                ws.run_id, result, completed_snapshot,
                user_input=ws.user_input, input_role=self.turn_loop._turn_input_role(ws.run_id),
                message_payload=message_payload, message_id=f"agent_graph_agent_{ws.run_id}",
                context_effect_id=f"agent_graph_context_exchange_{ws.run_id}",
                token_budget=self.turn_loop.session_context_token_budget,
                background_enqueue=self.turn_loop.background_compaction_callback,
            )
            if handled:
                manager = self.turn_loop.run_manager
                persisted = manager.durable_store.load_run(ws.run_id) if manager.durable_store is not None else None
                if persisted is None or persisted.get("status") != "completed":
                    raise RuntimeError("Finalization handler must commit the completed run before returning True")
                manager.refresh_after_transaction(persisted)
                with self._lock:
                    self._results[ws.run_id] = result
                return {
                    "phase": "finalized", "status": "completed", "result_artifact_ref": ref,
                    "result_summary": {"run_id": ws.run_id, "answer_chars": len(answer),
                                       "tool_event_count": len(result.tool_events), "log_path": result.log_path},
                    "run_snapshot": self._snapshot(self._run(ws.run_id)),
                }
        if self._run(ws.run_id).metadata.get("defer_answer_publication") is True:
            raise RuntimeError("Deferred answer publication requires a finalization handler to commit the result")
        if not self.turn_loop.run_manager.claim_completion(ws.run_id):
            self.turn_loop._raise_if_cancel_requested()
            raise RuntimeError(f"Agent run cannot claim completion: {ws.run_id}")
        final_run = self._run(ws.run_id)
        final_plan = final_run.metadata.get("multi_agent_plan")
        if (
            isinstance(final_plan, dict) and final_plan.get("status") == "failed"
        ) or self.turn_loop._multi_agent_replan_pending(ws.run_id):
            reason = "Multi-Agent plan ended with failed, blocked, or unverified child steps."
            try:
                self.turn_loop.run_manager.flush_events()
            except Exception as exc:
                self.turn_loop.run_manager.fail_completion_claim(
                    ws.run_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    log_path=str(log),
                )
                raise
            self.turn_loop.run_manager.append_event(
                ws.run_id,
                "run_failed",
                reason,
                stage="run",
                payload={"error_type": "MultiAgentPlanFailed"},
            )
            self.turn_loop.run_manager.fail_completion_claim(
                ws.run_id,
                error_type="MultiAgentPlanFailed",
                error=reason,
                log_path=str(log),
            )
            with self._lock:
                self._results[ws.run_id] = result
            return {
                "phase": "finalized",
                "status": "failed",
                "result_artifact_ref": ref,
                "result_summary": {
                    "run_id": ws.run_id,
                    "answer_chars": len(answer),
                    "log_path": str(log),
                },
            }

        updated = self.turn_loop.session_service.record_context_exchange(
            session_id=ws.session_id,
            user_input=ws.user_input,
            input_role=self.turn_loop._turn_input_role(ws.run_id),
            agent_answer=answer,
            trace_id=ws.trace_id,
            effect_id=f"agent_graph_context_exchange_{ws.run_id}",
            token_budget=self.turn_loop.session_context_token_budget,
            context_summarizer=lambda summary, messages, recent, budget: (
                self.turn_loop._summarize_context_window(
                    summary=summary,
                    messages_to_summarize=messages,
                    retained_recent_messages=recent,
                    token_budget=budget,
                    llm_events=self._models(data["llm_events"], AgentTurnLLMEvent),
                )
            ),
            background_enqueue=self.turn_loop.background_compaction_callback,
        )
        self.turn_loop.session_service.append_message(
            session_id=ws.session_id,
            role="agent",
            content=answer,
            payload={
                **message_payload,
                "context_window": {
                    "token_budget": updated.token_budget,
                    "token_estimate": updated.token_estimate,
                    "token_count_method": self.turn_loop.session_service.context_token_count_method,
                    "recent_message_count": len(updated.recent_messages),
                },
            },
            message_id=f"agent_graph_agent_{ws.run_id}",
            persisted_message_callback=self.turn_loop.memory_answer_callback,
        )
        try:
            # A completed run promises replayable token events.  Flush every
            # asynchronous delta before emitting the durable terminal event.
            self.turn_loop.run_manager.flush_events()
        except Exception as exc:
            self.turn_loop.run_manager.fail_completion_claim(
                ws.run_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        # Do not depend on ContextVar lookup during a worker-thread graph node.
        # The durable manager remains the project-owned lifecycle authority.
        if not any(
            event.type == "run_completed"
            for event in self.turn_loop.run_manager.list_events(ws.run_id)
        ):
            self.turn_loop.run_manager.append_event(
                ws.run_id,
                "run_completed",
                "Agent run completed.",
                stage="run",
                payload={
                    "answer_length": len(result.answer),
                    "initial_package": result.initial_package,
                    "expanded_packages": result.expanded_packages,
                    "used_packages": result.used_packages,
                    "active_package": result.active_package,
                    "tool_event_count": len(result.tool_events),
                    "llm_event_count": len(result.llm_events),
                    "log_path": result.log_path,
                },
            )
        run = self._run(ws.run_id)
        if run.parent_run_id is not None:
            audit_events = [
                event for event in self.turn_loop.run_manager.list_events(ws.run_id)
                if event.type == "child_tool_audit"
            ]
            if not audit_events:
                registry = getattr(self.turn_loop.tool_executor, "registry", None)
                self.turn_loop.run_manager.append_event(
                    ws.run_id,
                    "child_tool_audit",
                    "Child tool side-effect audit recorded.",
                    stage="subtask",
                    payload={"audit": build_child_tool_audit(result.tool_events, registry)},
                    parent_run_id=run.parent_run_id,
                    child_run_id=ws.run_id,
                    plan_id=run.plan_id,
                    step_id=run.step_id,
                    attempt=run.attempt,
                )
            self.turn_loop.run_manager.complete_child_run(
                ws.run_id,
                result_snapshot=completed_snapshot,
                log_path=result.log_path,
            )
        else:
            self.turn_loop.run_manager.complete_run(
                ws.run_id,
                result_snapshot=completed_snapshot,
                log_path=result.log_path,
            )
        with self._lock:
            self._results[ws.run_id] = result
        return {
            "phase": "finalized",
            "status": "completed",
            "result_artifact_ref": ref,
            "result_summary": {
                "run_id": ws.run_id,
                "answer_chars": len(answer),
                "tool_event_count": len(result.tool_events),
                "log_path": result.log_path,
            },
            "run_snapshot": self._snapshot(self._run(ws.run_id)),
        }

    def _after_route(self, s: AgentGraphState) -> str:
        if s.get("phase") == "entry_ready":
            return "decide"
        return "expand" if self._ws(s).initial_package else "answer"

    @staticmethod
    def _async_node(node: Any) -> Any:
        async def run(state: AgentGraphState) -> AgentGraphState:
            return await asyncio.to_thread(node, state)

        return run

    def _after_validate(self, s: AgentGraphState) -> str:
        return {
            "fork_paused": "paused_fork",
            "fork_subtasks_recorded": "decide",
            "fork_subtasks_rejected": "decide",
            "multi_agent_plan_patched": "decide",
            "fork_waiting_confirmation": "fork_wait",
            "fork_waiting_user": "child_user_wait",
            "plan_waiting_user": "user_question",
            "plan_patch_answered": "decide",
            "operation_expand": "expand",
            "operation_valid": "safety",
            "operation_answer": "answer",
        }.get(s["phase"], "decide")

    def _after_safety(self, s: AgentGraphState) -> str:
        return {
            "waiting_confirmation": "interrupt",
            "safety_rejected": "observe",
        }.get(s["phase"], "execute")

    def _ws(self, s: AgentGraphState) -> AgentTurnWorkingSet:
        return AgentTurnWorkingSet.model_validate(s["working_set"])

    def _data(self, s: AgentGraphState) -> tuple[AgentTurnWorkingSet, dict[str, Any]]:
        return self._ws(s), self._load_data(s["runtime_artifact_ref"])

    def _save(
        self,
        s: AgentGraphState,
        ws: AgentTurnWorkingSet,
        data: dict[str, Any],
        phase: str,
        status: str = "running",
    ) -> AgentGraphState:
        cache = getattr(self.turn_loop, "session_progress_cache", None)
        if cache is not None:
            cache.record(context=self._context(ws), tool_events=data.get("tool_events", []),
                         working_state=ws.task_working_state.context())
        return {
            "working_set": ws.model_dump(mode="json"),
            "runtime_artifact_ref": self._save_data(s["run_id"], data),
            "phase": phase,
            "status": status,
            "run_snapshot": self._snapshot(self._run(s["run_id"])),
        }

    def _save_data(self, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
        if self.artifact_store is None:
            return {"payload": data}
        content_hash = sha256(
            json.dumps(
                data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
        ).hexdigest()
        return self.artifact_store.put_artifact(
            artifact_id=f"agent_graph_runtime_{run_id}_{content_hash[:16]}",
            run_id=run_id,
            kind="agent_graph_runtime",
            payload=data,
            summary="Private Agent graph runtime data.",
            created_at=datetime.now(UTC).isoformat(),
        )

    def _replacement_handoff(self, run: AgentRunRecord) -> list[dict[str, Any]]:
        """Copy only a verified same-session predecessor tree into a run-scoped cache.

        Tool arguments/results are source data, not instructions or rollback authority.
        Keep full trace local and expose a bounded preview plus ordinary cache reads.
        """
        previous_id = run.metadata.get("replaces_run_id")
        if not isinstance(previous_id, str):
            return []
        manager = self.turn_loop.run_manager
        previous = manager.get_run(previous_id)
        if (previous is None or previous.session_id != run.session_id
                or previous.metadata.get("superseded_by_run_id") != run.run_id):
            raise RuntimeError("Replacement predecessor ownership is invalid.")
        from app.core.tool_result_gate import preview

        operations, traces = [], []
        predecessors = [previous]
        while isinstance(predecessors[-1].metadata.get("replaces_run_id"), str):
            current = predecessors[-1]
            older = manager.get_run(current.metadata["replaces_run_id"])
            if (older is None or older.session_id != run.session_id
                    or older.metadata.get("superseded_by_run_id") != current.run_id
                    or older.run_id in {item.run_id for item in predecessors}):
                raise RuntimeError("Replacement lineage ownership is invalid.")
            predecessors.append(older)
        items = [item for root in reversed(predecessors)
                 for item in [root, *manager.child_tree(root.run_id)]]
        for item in items:
            tool_events = []
            snapshot = self.get_state(item.run_id)
            if snapshot.values.get("runtime_artifact_ref"):
                tool_events = self._load_data(snapshot.values["runtime_artifact_ref"]).get("tool_events", [])
            elif item.result_snapshot:
                tool_events = item.result_snapshot.get("tool_events", [])
            events = manager.list_events(item.run_id)
            traces.append({"run_id": item.run_id, "parent_run_id": item.parent_run_id,
                           "status": item.status.value, "events": [event.model_dump(mode="json") for event in events]})
            by_invocation = {event.get("result", {}).get("invocation_id"): event for event in tool_events}
            invocations = (self.artifact_store.list_tool_invocations(item.run_id)
                           if self.artifact_store is not None else [])
            for invocation in invocations:
                saved = by_invocation.pop(invocation["invocation_id"], {})
                input_value = saved.get("input")
                if input_value is None:
                    working_set = snapshot.values.get("working_set", {})
                    if working_set.get("pending_invocation_id") == invocation["invocation_id"]:
                        input_value = working_set.get("pending_tool_input")
                operations.append({"run_id": item.run_id, **invocation, "input": input_value,
                    "effect_state": "reported" if invocation["status"] == "completed" else "unknown"})
            for saved in by_invocation.values():
                operations.append({"run_id": item.run_id, "tool_name": saved.get("tool_name"),
                    "input": saved.get("input"), "result": saved.get("result"), "effect_state": "reported"})
            if not invocations and not tool_events:
                # Workflow executors need not use the graph invocation ledger.
                # Preserve their registered-tool trace without fabricating inputs.
                for event in events:
                    if event.type != "tool_started" or not event.payload.get("tool_name"):
                        continue
                    metadata = event.payload.get("metadata") or {}
                    operations.append({"run_id": item.run_id, "tool_name": event.payload["tool_name"],
                        "input": metadata.get("input", event.payload.get("input")),
                        "trace_sequence": event.sequence, "effect_state": "unknown", "result": None})
        payload = {"output": {"operations": operations, "traces": traces,
                              "historical_dag": previous.metadata.get("multi_agent_plan")}}
        artifact_id = f"replacement_handoff_{run.run_id}"
        if self.artifact_store is not None:
            self.artifact_store.put_artifact(artifact_id=artifact_id, run_id=run.run_id,
                kind="tool_result", payload=payload, summary="Prior task operations; no rollback performed.",
                created_at=datetime.now(UTC).isoformat())
        indexed = []
        for index, item in enumerate(operations):
            tool = self.turn_loop.tool_executor.registry.get_tool_or_none(item["tool_name"])
            read_only = tool.spec.read_only if tool is not None else None
            indexed.append((item["effect_state"] != "unknown" and read_only is True, index,
                {"operation_index": index, "run_id": item["run_id"], "tool_name": item["tool_name"],
                 "read_only": read_only, "effect_state": item["effect_state"],
                 "result_status": (item.get("result") or {}).get("status"), "input": preview(item.get("input"))}))
        summaries = [summary for _, _, summary in sorted(indexed, key=lambda entry: entry[:2])[:8]]
        return [{"action": "replacement_handoff", "status": "available", "replaces_run_id": previous_id,
            "message": "The old task tree is retired, not rolled back. Inspect prior tool operations before modifying the same objects; do not repeat successful writes. Unknown effects require checking current state. If the list preview is incomplete, expand observation and read the cached /output/operations before assuming there are no other changes. This record grants no extra permission; rollback requires user authorization.",
            "operation_count": len(operations), "operations": summaries,
            "operations_preview_complete": len(operations) <= 8,
            "_result_cache": {"artifact_id": artifact_id, "read_tool": "observation.read",
                              "path": "/output/operations"} if self.artifact_store is not None else None}]

    def _load_data(self, ref: dict[str, Any]) -> dict[str, Any]:
        if "payload" in ref:
            return dict(ref["payload"])
        if self.artifact_store is None:
            raise RuntimeError("Graph runtime artifact store is unavailable.")
        value = self.artifact_store.load_artifact(str(ref["artifact_id"]))
        if value is None:
            raise RuntimeError("Graph runtime artifact is missing.")
        return value

    def _prune_runtime_snapshots(self, run_id: str) -> None:
        """Once a run is terminal, keep only the runtime snapshot its last checkpoint uses."""
        if self.artifact_store is None or not hasattr(self.artifact_store, "prune_graph_runtime_artifacts"):
            return
        try:
            ref = self.get_state(run_id).values.get("runtime_artifact_ref")
            keep = ref.get("artifact_id") if isinstance(ref, dict) else None
            if keep:
                self.artifact_store.prune_graph_runtime_artifacts(run_id, keep_artifact_id=keep)
        except Exception:  # noqa: BLE001 - storage hygiene must never fail a finished turn.
            return

    @staticmethod
    def _models(values: list[dict[str, Any]], cls: Any) -> list[Any]:
        return [cls.model_validate(v) for v in values]

    @staticmethod
    def _dump(values: list[Any]) -> list[dict[str, Any]]:
        return [v.model_dump(mode="json") for v in values]

    def _run(self, run_id: str) -> AgentRunRecord:
        run = self.turn_loop.run_manager.get_run(run_id)
        if run is None:
            raise KeyError(f"Agent run not found: {run_id}")
        return run

    def _request_for_run(self, run_id: str) -> dict[str, Any]:
        request = self._requests.get(run_id)
        if request is None:
            raise RuntimeError(f"Agent graph request is unavailable: {run_id}")
        return request

    def _context(self, ws: AgentTurnWorkingSet) -> ToolContext:
        session = self.turn_loop.session_service.ensure_session(
            session_id=ws.session_id, title="Agent Session", metadata={"entrypoint": "agent.turn"}
        )
        inference_client, inference_model, *_ = self.turn_loop._inference_selection()
        return ToolContext(
            session_id=ws.session_id,
            trace_id=ws.trace_id,
            context_id=ws.trace_id,
            workspace_root=self.turn_loop._workspace_for_run(session, ws.run_id),
            tool_view=self.turn_loop._tool_view_for_run(ws.run_id),
            run_id=ws.run_id,
            llm_client_name=inference_client,
            llm_model=inference_model,
            llm_reasoning_level=self.turn_loop._reasoning_selection()[0],
            llm_reasoning_source=self.turn_loop._reasoning_selection()[1],
            safety_review_mode=self.turn_loop._safety_review_preference().value,
        )

    def _fail(self, run_id: str, exc: Exception) -> None:
        manager = self.turn_loop.run_manager
        if isinstance(exc, AgentDispatchFenced):
            # A lease owner may have been replaced while this graph was blocked
            # in I/O. Fence this execution only; the current owner or a later
            # lease recovery owns run status and checkpoint continuation.
            with self._lock:
                self._errors[run_id] = exc
            return
        persisted = None
        if manager.durable_store is not None:
            for attempt in range(3):
                try:
                    persisted = manager.durable_store.load_run(run_id)
                    break
                except Exception as read_exc:
                    if not _is_temporary_sqlite_error(read_exc) or attempt == 2:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        if persisted is not None and persisted.get("status") == "completed":
            # Business publication may commit before the independent graph
            # checkpoint. A checkpoint failure cannot undo that terminal result.
            manager.refresh_after_transaction(persisted)
            recovered = self._completed_result_from_checkpoint(run_id)
            with self._lock:
                if recovered is not None:
                    self._results[run_id] = recovered
                else:
                    self._errors[run_id] = exc
            return
        for attempt in range(3):
            try:
                if isinstance(exc, AgentRunCancelled):
                    manager.cancel_run(run_id, reason=str(exc))
                else:
                    if attempt == 0:
                        # This emits the single run_failed event and attempts the
                        # terminal record write.
                        self.turn_loop._mark_current_run_failed(type(exc).__name__, str(exc), exc=exc)
                    else:
                        # Retry only the status write; do not duplicate the event.
                        current = manager.get_run(run_id)
                        if current is not None and current.parent_run_id is not None:
                            manager.fail_child_run(run_id, error_type=type(exc).__name__, error=str(exc))
                        else:
                            manager.fail_run(run_id, error_type=type(exc).__name__, error=str(exc))
                break
            except Exception as write_exc:
                if not _is_temporary_sqlite_error(write_exc):
                    raise
                # Queue admission and SQLITE_BUSY failures are transient.
                # Preserve terminal state if a prior attempt committed before
                # its caller observed the error.
                latest = None
                if manager.durable_store is not None:
                    for read_attempt in range(3):
                        try:
                            latest = manager.durable_store.load_run(run_id)
                            break
                        except Exception as read_exc:
                            if not _is_temporary_sqlite_error(read_exc) or read_attempt == 2:
                                raise
                            time.sleep(0.05 * (read_attempt + 1))
                status = latest.get("status") if latest is not None else None
                if status in {"completed", "failed", "cancelled", "timed_out", "paused",
                              "waiting_user", "waiting_confirmation"}:
                    if latest is not None:
                        manager.refresh_after_transaction(latest)
                    break
                if attempt == 2:
                    if isinstance(exc, AgentRunCancelled):
                        current = manager.get_run(run_id)
                        if current is not None and current.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING}:
                            manager._update_run(
                                run_id,
                                status=AgentRunStatus.CANCELLED,
                                cancelled_at=datetime.now(UTC).isoformat(),
                                error_type="cancelled",
                                error=str(exc),
                            )
                            break
                    raise
                time.sleep(0.05 * (attempt + 1))
        with self._lock:
            self._errors[run_id] = exc

    def _take_result(self, run_id: str) -> AgentTurnResult:
        self._prune_runtime_snapshots(run_id)
        with self._lock:
            error = self._errors.pop(run_id, None)
            result = self._results.pop(run_id, None)
        if error:
            raise error
        if result:
            return result
        run = self._run(run_id)
        if run.status.value == "paused":
            raise AgentTurnPaused(run_id)
        if run.status.value == "completed":
            recovered = self._completed_result_from_checkpoint(run_id)
            if recovered is not None:
                return recovered
            raise RuntimeError(f"LangGraph completed run is missing its result artifact: {run_id}")
        if run.status.value == "waiting_confirmation":
            raise AgentTurnWaitingForConfirmation(
                run_id=run_id, review_id=run.metadata.get("confirmation_id")
            )
        if run.status.value == "waiting_user":
            question = run.metadata.get("pending_user_question")
            raise AgentTurnWaitingForUser(
                run_id=run_id,
                question_id=question.get("question_id") if isinstance(question, dict) else None,
            )
        if run.status.value in {"failed", "cancelled"}:
            raise RuntimeError(
                f"LangGraph run ended without a result: {run_id} ({run.status.value})"
            )
        raise RuntimeError(
            f"LangGraph run produced no in-memory result: {run_id} ({run.status.value})"
        )

    def _completed_result_from_checkpoint(self, run_id: str) -> AgentTurnResult | None:
        """Reload a terminal result after the process-local result cache was lost."""

        run = self._run(run_id)
        snapshot = run.result_snapshot or {}
        ref = snapshot.get("result_artifact_ref")
        if not isinstance(ref, dict):
            state = self.get_state(run_id)
            ref = state.values.get("result_artifact_ref")
        if not isinstance(ref, dict):
            return None
        if isinstance(ref.get("payload"), dict):
            return AgentTurnResult.model_validate(ref["payload"])
        if self.artifact_store is None or not isinstance(ref.get("artifact_id"), str):
            return None
        payload = self.artifact_store.load_artifact(ref["artifact_id"])
        return AgentTurnResult.model_validate(payload) if payload is not None else None

    def get_state(self, run_id: str) -> Any:
        if self.checkpoint_runtime is None:
            return self.graph.get_state(self._config(run_id))
        with self.checkpoint_runtime.sync_saver() as saver:
            return self._compile(saver).get_state(self._config(run_id))

    def get_state_history(self, run_id: str) -> list[Any]:
        if self.checkpoint_runtime is None:
            return list(self.graph.get_state_history(self._config(run_id)))
        with self.checkpoint_runtime.sync_saver() as saver:
            return list(self._compile(saver).get_state_history(self._config(run_id)))

    def _restore(self, values: dict[str, Any]) -> None:
        if "run_snapshot" not in values:
            return  # A pause before initialize_run still has an input checkpoint.
        self.turn_loop.run_manager.restore_run(
            AgentRunRecord.model_validate(values["run_snapshot"])
        )

    @staticmethod
    def _snapshot(run: AgentRunRecord) -> dict[str, Any]:
        return run.model_dump(mode="json")

    def _store_result(self, result: AgentTurnResult) -> dict[str, Any]:
        if self.artifact_store is None:
            return {"payload": result.model_dump(mode="json")}
        return self.artifact_store.put_artifact(
            artifact_id=f"agent_turn_result_{result.run_id}",
            run_id=result.run_id,
            kind="agent_turn_result",
            payload=result.model_dump(mode="json"),
            summary=f"Agent turn result: {len(result.tool_events)} tool events.",
            created_at=datetime.now(UTC).isoformat(),
        )

    def _compile(self, checkpointer: BaseCheckpointSaver) -> Any:
        return self._builder.compile(
            checkpointer=checkpointer, interrupt_after=self.interrupt_after
        )

    def _resolve_run_id(
        self,
        *,
        session_id: str | None,
        user_input: str,
        existing_run_id: str | None = None,
        llm_answer_client_name: str | None = None,
        llm_answer_model: str | None = None,
        llm_reasoning_level: str | None = None,
        llm_reasoning_source: str | None = None,
        **_: Any,
    ) -> str:
        return (
            existing_run_id
            or self.create_run_for_turn(session_id=session_id, user_input=user_input,
                                        llm_answer_client_name=llm_answer_client_name,
                                        llm_answer_model=llm_answer_model,
                                        llm_reasoning_level=llm_reasoning_level,
                                        llm_reasoning_source=llm_reasoning_source).run_id
        )

    @staticmethod
    def _config(run_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": run_id}}
