"""LangGraph nodes for one Agent tool invocation lifecycle.

The outer decision loop still owns tool selection.  This graph owns the
side-effect boundary so its phases remain explicit and independently
observable while preserving the project ToolExecutor and safety contracts.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.agent_storage import SqliteAgentRunStore
from app.core.safety import SafetyReviewRecord
from app.core.tools import ToolContext, ToolExecutor, ToolResult


class ToolLifecycleState(TypedDict, total=False):
    """Small serializable state for a single tool lifecycle.

    Full results stay in the durable artifact store and the process-local
    result cache.  They are deliberately not checkpointed as graph state.
    """

    invocation_id: str
    run_id: str | None
    tool_name: str
    tool_input: dict[str, Any]
    phase: str
    approved_review_id: str | None
    result_artifact_ref: dict[str, Any]
    execution_source: str


ReviewToolCall = Callable[
    [str, str, dict[str, Any], ToolContext], tuple[ToolResult | None, SafetyReviewRecord | None]
]
ProgressCallback = Callable[[str, str, str, dict[str, Any]], None]
CancelCheck = Callable[[], None]


class AgentToolLifecycleGraph:
    """Execute safety, tool invocation and observation as LangGraph nodes."""

    def __init__(
        self,
        *,
        tool_executor: ToolExecutor,
        review_tool_call: ReviewToolCall,
        append_progress: ProgressCallback,
        raise_if_cancel_requested: CancelCheck,
        artifact_store: SqliteAgentRunStore | None = None,
    ) -> None:
        self.tool_executor = tool_executor
        self.review_tool_call = review_tool_call
        self.append_progress = append_progress
        self.raise_if_cancel_requested = raise_if_cancel_requested
        self.artifact_store = artifact_store
        self._lock = threading.RLock()
        self._inputs: dict[str, ToolContext] = {}
        self._results: dict[str, ToolResult] = {}
        self._builder = StateGraph(ToolLifecycleState)
        self._builder.add_node("safety_gate", self._safety_gate)
        self._builder.add_node("execute_tool", self._execute_tool)
        self._builder.add_node("build_observation", self._build_observation)
        self._builder.add_edge(START, "safety_gate")
        self._builder.add_edge("safety_gate", "execute_tool")
        self._builder.add_edge("execute_tool", "build_observation")
        self._builder.add_edge("build_observation", END)
        self.graph = self._builder.compile()

    def run(
        self,
        *,
        invocation_id: str,
        run_id: str | None,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        with self._lock:
            self._inputs[invocation_id] = context
        try:
            self.graph.invoke(
                {
                    "invocation_id": invocation_id,
                    "run_id": run_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "phase": "created",
                }
            )
            with self._lock:
                result = self._results.pop(invocation_id, None)
            if result is None:
                raise RuntimeError(f"Tool lifecycle graph produced no result: {invocation_id}")
            return result
        finally:
            with self._lock:
                self._inputs.pop(invocation_id, None)

    def _safety_gate(self, state: ToolLifecycleState) -> ToolLifecycleState:
        context = self._context_for(state["invocation_id"])
        rejected_result, approved_review = self.review_tool_call(
            state["invocation_id"],
            state["tool_name"],
            state["tool_input"],
            context,
        )
        if rejected_result is not None:
            self._set_result(state["invocation_id"], rejected_result)
            return {"phase": "safety_rejected", "execution_source": "safety_gate"}
        return {
            "phase": "safety_approved",
            "approved_review_id": (
                approved_review.review_id if approved_review is not None else None
            ),
        }

    def _execute_tool(self, state: ToolLifecycleState) -> ToolLifecycleState:
        if state.get("phase") == "safety_rejected":
            return {"phase": "execution_skipped"}
        context = self._context_for(state["invocation_id"])
        approved_review_id = state.get("approved_review_id")
        if approved_review_id is not None:
            context = context.model_copy(
                update={
                    "safety_review_approved": True,
                    "safety_review_id": approved_review_id,
                }
            )
        claim = self._claim(state)
        if claim["status"] == "completed":
            result = ToolResult.model_validate(claim["result"])
            self._set_result(state["invocation_id"], result)
            self.append_progress(
                "tool_recovered",
                "completed",
                f"Recovered completed `{state['tool_name']}` invocation.",
                {"result": result.model_dump(mode="json")},
            )
            return {"phase": "executed", "execution_source": "durable_claim"}
        if claim["status"] != "claimed":
            result = ToolResult(
                invocation_id=state["invocation_id"],
                tool_name=state["tool_name"],
                status="failed",
                error=(
                    "A previous invocation may have produced a side effect but did not "
                    "record a result; refusing to replay it automatically."
                ),
                output={"invocation_claim": claim["status"]},
            )
            self._set_result(state["invocation_id"], result)
            self.append_progress(
                "tool_execution_uncertain",
                "failed",
                result.error or "Tool invocation state is uncertain.",
                {"invocation_claim": claim["status"]},
            )
            return {"phase": "execution_uncertain", "execution_source": "durable_claim"}

        self.append_progress(
            "tool_started",
            "running",
            f"Calling `{state['tool_name']}`.",
            {"input": state["tool_input"]},
        )
        self.raise_if_cancel_requested()
        result = self.tool_executor.execute(
            invocation_id=state["invocation_id"],
            tool_name=state["tool_name"],
            tool_input=state["tool_input"],
            context=context,
        )
        if self.artifact_store is not None:
            self.artifact_store.complete_tool_invocation(
                invocation_id=state["invocation_id"],
                result=result.model_dump(mode="json"),
                completed_at=_now_iso(),
            )
        self._set_result(state["invocation_id"], result)
        return {"phase": "executed", "execution_source": "tool_executor"}

    def _build_observation(self, state: ToolLifecycleState) -> ToolLifecycleState:
        result = self._result_for(state["invocation_id"])
        artifact_ref = self._store_result_artifact(state, result)
        if state.get("phase") != "execution_skipped":
            self.append_progress(
                "tool_completed",
                result.status,
                self._tool_progress_message(state["tool_name"], result),
                {"result": result.model_dump(mode="json")},
            )
        return {
            "phase": "observation_ready",
            "result_artifact_ref": artifact_ref,
        }

    def _claim(self, state: ToolLifecycleState) -> dict[str, Any]:
        if self.artifact_store is None or state.get("run_id") is None:
            return {"status": "claimed"}
        return self.artifact_store.claim_tool_invocation(
            invocation_id=state["invocation_id"],
            run_id=str(state["run_id"]),
            tool_name=state["tool_name"],
            tool_input=state["tool_input"],
            claimed_at=_now_iso(),
        )

    def _store_result_artifact(
        self, state: ToolLifecycleState, result: ToolResult
    ) -> dict[str, Any]:
        if self.artifact_store is None or state.get("run_id") is None:
            return {}
        return self.artifact_store.put_artifact(
            artifact_id=f"tool_result_{state['invocation_id']}",
            run_id=str(state["run_id"]),
            kind="tool_result",
            payload=result.model_dump(mode="json"),
            summary=f"{result.tool_name}: {result.status}",
            created_at=_now_iso(),
        )

    def _context_for(self, invocation_id: str) -> ToolContext:
        with self._lock:
            return self._inputs[invocation_id]

    def _set_result(self, invocation_id: str, result: ToolResult) -> None:
        with self._lock:
            self._results[invocation_id] = result

    def _result_for(self, invocation_id: str) -> ToolResult:
        with self._lock:
            return self._results[invocation_id]

    @staticmethod
    def _tool_progress_message(tool_name: str, result: ToolResult) -> str:
        if result.status == "completed":
            return f"`{tool_name}` completed."
        if result.error:
            return f"`{tool_name}` {result.status}: {result.error}"
        return f"`{tool_name}` {result.status}."


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
