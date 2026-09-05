"""LangGraph-backed Agent turn orchestrator."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.core.agent_runs import AgentRunRecord
from app.core.agent_turn import AgentTurnLoop, AgentTurnResult
from app.core.llm import LLMResponseMode


class AgentGraphState(TypedDict, total=False):
    """Serializable state for the first graph migration boundary."""

    run_id: str
    request: dict[str, Any]
    phase: str
    result_summary: dict[str, Any]


class AgentGraphRunner:
    """Run the current Agent semantics through a checkpointed LangGraph entrypoint."""

    orchestrator_name = "langgraph"

    def __init__(self, turn_loop: AgentTurnLoop) -> None:
        self.turn_loop = turn_loop
        self._result_lock = threading.RLock()
        self._results: dict[str, AgentTurnResult] = {}
        builder = StateGraph(AgentGraphState)
        builder.add_node("execute_turn", self._execute_turn)
        builder.add_edge(START, "execute_turn")
        builder.add_edge("execute_turn", END)
        self.checkpointer = InMemorySaver()
        self.graph = builder.compile(checkpointer=self.checkpointer)

    def create_run_for_turn(
        self,
        *,
        session_id: str | None,
        user_input: str,
        parent_run_id: str | None = None,
    ) -> AgentRunRecord:
        return self.turn_loop.create_run_for_turn(
            session_id=session_id,
            user_input=user_input,
            parent_run_id=parent_run_id,
        )

    def run(
        self,
        *,
        session_id: str | None,
        user_input: str,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        existing_run_id: str | None = None,
    ) -> AgentTurnResult:
        run_id = self._resolve_run_id(
            session_id=session_id,
            user_input=user_input,
            existing_run_id=existing_run_id,
        )
        asyncio.run(
            self._invoke(
                run_id=run_id,
                session_id=session_id,
                user_input=user_input,
                llm_client_name=llm_client_name,
                llm_model=llm_model,
                llm_response_mode=llm_response_mode,
            )
        )
        return self._take_result(run_id)

    async def run_async(
        self,
        *,
        session_id: str | None,
        user_input: str,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
        llm_response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        existing_run_id: str | None = None,
    ) -> AgentTurnResult:
        run_id = self._resolve_run_id(
            session_id=session_id,
            user_input=user_input,
            existing_run_id=existing_run_id,
        )
        await self._invoke(
            run_id=run_id,
            session_id=session_id,
            user_input=user_input,
            llm_client_name=llm_client_name,
            llm_model=llm_model,
            llm_response_mode=llm_response_mode,
        )
        return self._take_result(run_id)

    async def _invoke(
        self,
        *,
        run_id: str,
        session_id: str | None,
        user_input: str,
        llm_client_name: str | None,
        llm_model: str | None,
        llm_response_mode: LLMResponseMode,
    ) -> None:
        await self.graph.ainvoke(
            {
                "run_id": run_id,
                "request": {
                    "session_id": session_id,
                    "user_input": user_input,
                    "llm_client_name": llm_client_name,
                    "llm_model": llm_model,
                    "llm_response_mode": llm_response_mode.value,
                },
            },
            config=self._graph_config(run_id),
        )

    async def _execute_turn(self, state: AgentGraphState) -> AgentGraphState:
        request = state["request"]
        result = await self.turn_loop.run_async(
            session_id=request.get("session_id"),
            user_input=str(request["user_input"]),
            llm_client_name=request.get("llm_client_name"),
            llm_model=request.get("llm_model"),
            llm_response_mode=LLMResponseMode(str(request["llm_response_mode"])),
            existing_run_id=state["run_id"],
        )
        with self._result_lock:
            self._results[result.run_id] = result
        return {
            "phase": "completed",
            "result_summary": {
                "run_id": result.run_id,
                "session_id": result.session_id,
                "trace_id": result.trace_id,
                "answer_chars": len(result.answer),
                "tool_event_count": len(result.tool_events),
                "log_path": result.log_path,
            },
        }

    def _take_result(self, run_id: str) -> AgentTurnResult:
        with self._result_lock:
            result = self._results.pop(run_id, None)
        if result is None:
            raise RuntimeError(f"LangGraph run produced no Agent turn result: {run_id}")
        return result

    def _resolve_run_id(
        self,
        *,
        session_id: str | None,
        user_input: str,
        existing_run_id: str | None,
    ) -> str:
        if existing_run_id:
            return existing_run_id
        return self.create_run_for_turn(
            session_id=session_id,
            user_input=user_input,
        ).run_id

    @staticmethod
    def _graph_config(run_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": run_id}}
