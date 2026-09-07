"""LangGraph-owned ReAct state machine using the project's Agent components."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.core.agent_checkpoints import SqliteCheckpointRuntime
from app.core.agent_runs import AgentRunCancelled, AgentRunRecord
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_turn import (
    AgentTurnDecisionEvent,
    AgentTurnLLMEvent,
    AgentTurnProgressEvent,
    AgentTurnResult,
    AgentTurnToolEvent,
    AgentTurnVerificationWarning,
    AgentTurnWorkingSet,
    _session_workspace_path,
    _stable_id,
    _turn_llm_client_name,
    _turn_llm_model,
    _turn_llm_response_mode,
    _turn_run_id,
    _turn_run_manager,
)
from app.core.llm import LLMResponseMode
from app.core.runtime_context import current_time_payload
from app.core.safety import SafetyReviewRecord, SafetyReviewStatus
from app.core.tools import ToolContext, ToolResult


class AgentGraphState(TypedDict, total=False):
    run_id: str
    graph_thread_id: str
    checkpoint_schema_version: int
    request: dict[str, Any]
    phase: str
    status: Literal["queued", "running", "waiting_confirmation", "completed", "failed", "cancelled"]
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
            ("safety_gate", self._safety),
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
            "route_package", self._after_route, {"expand": "expand_package", "answer": "answer"}
        )
        builder.add_edge("expand_package", "decide_next_operation")
        builder.add_edge("decide_next_operation", "validate_operation")
        builder.add_conditional_edges(
            "validate_operation",
            self._after_validate,
            {
                "decide": "decide_next_operation",
                "expand": "expand_package",
                "safety": "safety_gate",
                "answer": "answer",
            },
        )
        builder.add_conditional_edges(
            "safety_gate",
            self._after_safety,
            {"execute": "execute_tool", "observe": "build_observation"},
        )
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
        return self.turn_loop.create_run_for_turn(**kwargs)

    def run(self, **kwargs: Any) -> AgentTurnResult:
        run_id = self._resolve_run_id(**kwargs)
        self._invoke_sync(run_id=run_id, **kwargs)
        return self._take_result(run_id)

    async def run_async(self, **kwargs: Any) -> AgentTurnResult:
        return await self._run_in_worker(self.run, **kwargs)

    def resume(self, run_id: str) -> AgentTurnResult:
        if self._run(run_id).status.value == "completed":
            return self._take_result(run_id)
        self._resume_sync(run_id)
        return self._take_result(run_id)

    async def resume_async(self, run_id: str) -> AgentTurnResult:
        return await self._run_in_worker(self.resume, run_id)

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

        worker = threading.Thread(
            target=target,
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

    def _resume_sync(self, run_id: str) -> None:
        graph = self.graph
        if self.checkpoint_runtime is not None:
            with self.checkpoint_runtime.sync_saver() as saver:
                graph = self._compile(saver)
                snapshot = graph.get_state(self._config(run_id))
                self._requests[run_id] = dict(snapshot.values["request"])
                self._restore(snapshot.values)
                self._invoke_graph_sync(graph, Command(resume={"run_id": run_id}), run_id)
                return
        snapshot = graph.get_state(self._config(run_id))
        self._requests[run_id] = dict(snapshot.values["request"])
        self._restore(snapshot.values)
        self._invoke_graph_sync(graph, Command(resume={"run_id": run_id}), run_id)

    async def _invoke(
        self,
        *,
        run_id: str,
        session_id: str | None,
        user_input: str,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
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
                "llm_client_name": llm_client_name,
                "llm_model": llm_model,
                "llm_response_mode": llm_response_mode.value,
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
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
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
                "llm_client_name": llm_client_name,
                "llm_model": llm_model,
                "llm_response_mode": llm_response_mode.value,
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
            _turn_llm_response_mode.set(
                LLMResponseMode(str(request.get("llm_response_mode", LLMResponseMode.TEXT.value)))
            ),
            _turn_run_manager.set(self.turn_loop.run_manager),
            _turn_run_id.set(run_id),
        )
        try:
            await graph.ainvoke(value, config=self._config(run_id))
        except Exception as exc:  # noqa: BLE001 - graph boundary persists every failure.
            self._fail(run_id, exc)
        finally:
            _turn_run_id.reset(tokens[4])
            _turn_run_manager.reset(tokens[3])
            _turn_llm_response_mode.reset(tokens[2])
            _turn_llm_model.reset(tokens[1])
            _turn_llm_client_name.reset(tokens[0])

    def _invoke_graph_sync(self, graph: Any, value: Any, run_id: str) -> None:
        self._run(run_id)
        request = self._request_for_run(run_id)
        tokens = (
            _turn_llm_client_name.set(request.get("llm_client_name")),
            _turn_llm_model.set(request.get("llm_model")),
            _turn_llm_response_mode.set(
                LLMResponseMode(str(request.get("llm_response_mode", LLMResponseMode.TEXT.value)))
            ),
            _turn_run_manager.set(self.turn_loop.run_manager),
            _turn_run_id.set(run_id),
        )
        try:
            graph.invoke(value, config=self._config(run_id))
        except Exception as exc:  # noqa: BLE001 - graph boundary persists every failure.
            self._fail(run_id, exc)
        finally:
            _turn_run_id.reset(tokens[4])
            _turn_run_manager.reset(tokens[3])
            _turn_llm_response_mode.reset(tokens[2])
            _turn_llm_model.reset(tokens[1])
            _turn_llm_client_name.reset(tokens[0])

    def _initialize_run(self, s: AgentGraphState) -> AgentGraphState:
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
                payload={"session_id": run.session_id, "trace_id": run.trace_id},
            )
        return {
            "phase": "initialized",
            "status": "running",
            "run_snapshot": self._snapshot(self._run(run.run_id)),
        }

    def _prepare_context(self, s: AgentGraphState) -> AgentGraphState:
        run = self._run(s["run_id"])
        session = self.turn_loop.session_service.ensure_session(
            session_id=run.session_id,
            title=run.user_input[:60] or "Agent Session",
            metadata={"entrypoint": "agent.turn"},
        )
        self.turn_loop.session_service.append_message(
            session_id=session.session_id,
            role="user",
            content=run.user_input,
            payload={"trace_id": run.trace_id, "entrypoint": "agent.turn"},
            message_id=f"agent_graph_user_{run.run_id}",
        )
        window = self.turn_loop.session_service.get_context_window(
            session_id=session.session_id, token_budget=self.turn_loop.session_context_token_budget
        ).model_dump(mode="json")
        window["current_time"] = current_time_payload()
        if session.workspace is not None:
            window["workspace"] = session.workspace.model_dump(mode="json")
        cached = self.turn_loop._cached_tool_observations_from_session(
            session_id=session.session_id
        )
        if cached:
            window["cached_tool_observations"] = cached
        data = {
            "context_window": window,
            "package_catalog": [
                p.model_dump(mode="json")
                for p in self.turn_loop.tool_executor.registry.list_packages()
            ],
            "expanded_tools": [],
            "observations": list(cached),
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
            user_input=run.user_input,
            context_artifact_ref=ref,
        )
        return {
            "phase": "context_prepared",
            "working_set": ws.model_dump(mode="json"),
            "runtime_artifact_ref": ref,
        }

    def _route_package(self, s: AgentGraphState) -> AgentGraphState:
        ws, data = self._data(s)
        llm = self._models(data["llm_events"], AgentTurnLLMEvent)
        decisions = self._models(data["decision_events"], AgentTurnDecisionEvent)
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
            type="package_selected" if isinstance(selected, str) else "no_package",
            stage="route",
            package_name=selected if isinstance(selected, str) else None,
            status="completed",
            message=f"Selected `{selected}` package."
            if isinstance(selected, str)
            else "No tool package selected; answering from context if possible.",
            metadata={"reason": route.get("reason")},
        )
        data["progress_events"] = self._dump(progress)
        ws.route = route
        ws.initial_package = selected if isinstance(selected, str) else None
        return self._save(s, ws, data, "routed")

    def _expand_package(self, s: AgentGraphState) -> AgentGraphState:
        ws, data = self._data(s)
        requested_package = ws.pending_decision.get("package_name")
        package = requested_package or ws.initial_package
        # Legacy turns expand the route-selected package before entering the
        # decision loop.  That bootstrap is reflected by expanded_tools, not by
        # a decision observation.  Only an expansion explicitly requested by a
        # later decision belongs in observations, where it can inform the next
        # decision just like the legacy dynamic-expansion path.
        is_initial_expansion = requested_package is None
        if not isinstance(package, str) or not self.turn_loop._package_exists(package):
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
            tools = self.turn_loop._tool_payloads_for_package(package)
            data["expanded_tools"].extend(tools)
            ws.expanded_packages.append(package)
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
        ws, data = self._data(s)
        if ws.step_index >= self.turn_loop.max_decision_steps:
            ws.terminal_reason = "Step limit reached after loading evidence."
            ws.pending_decision = {"action": "final_answer"}
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
        )
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
            step_index=ws.step_index + 1,
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
                metadata={"action": x.get("action"), "step_index": ws.step_index + 1},
            )
            data["progress_events"] = self._dump(progress)
        return self._save(s, ws, data, "decision_made")

    def _validate(self, s: AgentGraphState) -> AgentGraphState:
        ws, data = self._data(s)
        x = ws.pending_decision
        action = str(x.get("action") or "")
        if action == "expand_package":
            return self._save(s, ws, data, "operation_expand")
        if action == "final_answer":
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
        if name not in allowed:
            data["observations"].append(
                {
                    "tool_name": name,
                    "status": "rejected",
                    "error": "Tool is not available in the expanded package.",
                }
            )
            return self._save(s, ws, data, "operation_rejected")
        fp = self.turn_loop._tool_call_fingerprint(tool_name=name, tool_input=tool_input)
        repeat = (
            bool(x.get("operation", {}).get("repeat_successful_call"))
            if isinstance(x.get("operation"), dict)
            else False
        )
        if fp in data["successful"] and not repeat:
            ws.terminal_reason = (
                "Blocked an identical tool call after a successful result in this turn."
            )
            return self._save(s, ws, data, "operation_answer")
        ws.pending_invocation_id = _stable_id(
            "tool_invocation", ws.run_id, str(ws.step_index), name, fp
        )
        ws.pending_tool_name = name
        ws.pending_tool_input = dict(tool_input)
        return self._save(s, ws, data, "operation_valid")

    def _safety(self, s: AgentGraphState) -> AgentGraphState:
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
            self._save_data(s["run_id"], data)
            interrupt({"review_id": review.review_id, "tool_name": name, "run_id": s["run_id"]})
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

    def _execute(self, s: AgentGraphState) -> AgentGraphState:
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
                tool_name=name, tool_input=inp, tool_result=result, feedback=feedback
            )
        )
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
        ws, data = self._data(s)
        llm = self._models(data["llm_events"], AgentTurnLLMEvent)
        answer = (
            self.turn_loop._answer_from_context_with_llm(
                user_input=ws.user_input,
                route=ws.route,
                context_window=data["context_window"],
                llm_events=llm,
            )
            if ws.initial_package is None
            else self.turn_loop._answer_with_llm(
                user_input=ws.user_input,
                route=ws.route,
                context_window=data["context_window"],
                observations=data["observations"],
                final_decision={
                    "action": "final_answer",
                    "reason": ws.terminal_reason or ws.pending_decision.get("reason"),
                },
                llm_events=llm,
            )
        ) or "我还没有获得足够的有效证据来完成这个请求。请重试或提供更多上下文。"
        data["llm_events"] = self._dump(llm)
        ws.terminal_answer = answer
        progress = self._models(data["progress_events"], AgentTurnProgressEvent)
        self.turn_loop._append_progress(
            progress,
            type="final_answer",
            stage="answer",
            status="completed",
            message=self.turn_loop._short_text(answer),
            metadata={"answer": answer},
        )
        data["progress_events"] = self._dump(progress)
        return self._save(s, ws, data, "answered")

    def _verify(self, s: AgentGraphState) -> AgentGraphState:
        ws, data = self._data(s)
        warnings = self.turn_loop._verify_final_answer(
            answer=ws.terminal_answer or "",
            tool_events=self._models(data["tool_events"], AgentTurnToolEvent),
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
        ws, data = self._data(s)
        answer = ws.terminal_answer or ""
        updated = self.turn_loop.session_service.record_context_exchange(
            session_id=ws.session_id,
            user_input=ws.user_input,
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
        )
        result = AgentTurnResult(
            run_id=ws.run_id,
            session_id=ws.session_id,
            trace_id=ws.trace_id,
            answer=answer,
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
        log = self.turn_loop._write_log(result=result, user_input=ws.user_input)
        result = result.model_copy(update={"log_path": str(log)})
        self.turn_loop.session_service.append_message(
            session_id=ws.session_id,
            role="agent",
            content=answer,
            payload={
                "run_id": ws.run_id,
                "trace_id": ws.trace_id,
                "initial_package": ws.initial_package,
                "expanded_packages": ws.expanded_packages,
                "used_packages": ws.used_packages,
                "active_package": ws.active_package,
                "log_path": str(log),
                "context_window": {
                    "token_budget": updated.token_budget,
                    "token_estimate": updated.token_estimate,
                    "recent_message_count": len(updated.recent_messages),
                },
            },
            message_id=f"agent_graph_agent_{ws.run_id}",
        )
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
        self.turn_loop.run_manager.complete_run(
            ws.run_id,
            result_snapshot={
                "session_id": result.session_id,
                "trace_id": result.trace_id,
                "answer": result.answer,
                "selected_package": result.selected_package,
                "initial_package": result.initial_package,
                "expanded_packages": result.expanded_packages,
                "used_packages": result.used_packages,
                "active_package": result.active_package,
            },
            log_path=result.log_path,
        )
        with self._lock:
            self._results[ws.run_id] = result
        ref = self._store_result(result)
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
        return "expand" if self._ws(s).initial_package else "answer"

    @staticmethod
    def _async_node(node: Any) -> Any:
        async def run(state: AgentGraphState) -> AgentGraphState:
            return await asyncio.to_thread(node, state)

        return run

    def _after_validate(self, s: AgentGraphState) -> str:
        return {
            "operation_expand": "expand",
            "operation_valid": "safety",
            "operation_answer": "answer",
        }.get(s["phase"], "decide")

    def _after_safety(self, s: AgentGraphState) -> str:
        return "observe" if s["phase"] == "safety_rejected" else "execute"

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

    def _load_data(self, ref: dict[str, Any]) -> dict[str, Any]:
        if "payload" in ref:
            return dict(ref["payload"])
        if self.artifact_store is None:
            raise RuntimeError("Graph runtime artifact store is unavailable.")
        value = self.artifact_store.load_artifact(str(ref["artifact_id"]))
        if value is None:
            raise RuntimeError("Graph runtime artifact is missing.")
        return value

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
        return ToolContext(
            session_id=ws.session_id,
            trace_id=ws.trace_id,
            context_id=ws.trace_id,
            workspace_root=_session_workspace_path(session),
        )

    def _fail(self, run_id: str, exc: Exception) -> None:
        if isinstance(exc, AgentRunCancelled):
            self.turn_loop._mark_current_run_cancelled(str(exc))
        else:
            self.turn_loop._mark_current_run_failed(type(exc).__name__, str(exc))
        with self._lock:
            self._errors[run_id] = exc

    def _take_result(self, run_id: str) -> AgentTurnResult:
        with self._lock:
            error = self._errors.pop(run_id, None)
            result = self._results.pop(run_id, None)
        if error:
            raise error
        if result:
            return result
        run = self._run(run_id)
        if run.status.value == "completed":
            recovered = self._completed_result_from_checkpoint(run_id)
            if recovered is not None:
                return recovered
            raise RuntimeError(
                f"LangGraph completed run is missing its result artifact: {run_id}"
            )
        if run.status.value == "waiting_confirmation":
            raise AgentTurnWaitingForConfirmation(
                run_id=run_id, review_id=run.metadata.get("confirmation_id")
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
        **_: Any,
    ) -> str:
        return (
            existing_run_id
            or self.create_run_for_turn(session_id=session_id, user_input=user_input).run_id
        )

    @staticmethod
    def _config(run_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": run_id}}
