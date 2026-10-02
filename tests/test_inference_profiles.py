from __future__ import annotations

from app.core.agent_graph import AgentGraphRunner
from app.core.agent_runs import AgentRunRecord, AgentRunStatus
from app.core.agent_turn import AgentTurnLoop
from app.core.llm import LLMResponse, LLMResponseMode
from app.core.multi_agent import ContextSnapshot, RuntimeBudget, ScopeGrant


class _RunManager:
    def __init__(self, run: AgentRunRecord) -> None:
        self.run = run

    def get_run(self, run_id: str) -> AgentRunRecord | None:
        return self.run if run_id == self.run.run_id else None


class _LLM:
    def __init__(self) -> None:
        self.request: dict[str, object] | None = None

    def supports_reasoning_effort(self, *, client_name: str) -> bool:
        return client_name == "profile_client"

    def complete_text(self, **kwargs: object) -> LLMResponse:
        self.request = kwargs
        return LLMResponse(
            provider="test",
            status="completed",
            content="ok",
            prompt_summary="test",
            client_name=str(kwargs.get("client_name")),
            model=str(kwargs.get("model")),
        )


class _Graph:
    def __init__(self, turn_loop: AgentTurnLoop) -> None:
        self.turn_loop = turn_loop

    def invoke(self, _value: object, *, config: object) -> None:
        self.turn_loop._complete_text_once(
            system_prompt="system",
            user_prompt="user",
            prompt_summary="test",
            max_output_tokens=32,
            stage="decision",
            response_mode=LLMResponseMode.TEXT,
        )


def test_graph_runner_uses_frozen_child_profile_in_actual_llm_request() -> None:
    run_id = "child_run"
    snapshot = ContextSnapshot(
        correlation_id="trace",
        snapshot_id="snapshot",
        parent_run_id="parent_run",
        child_run_id=run_id,
        session_id="session",
        plan_id="plan",
        step_id="step",
        inference_profile_id="careful",
        inference_client_name="profile_client",
        inference_model="model-v2",
        inference_reasoning_effort=None,
        inference_selection_source="server_profile",
        objective="do work",
        output_contract="report",
        effective_scope=ScopeGrant(),
        budget=RuntimeBudget(max_tokens=100, max_tool_calls=2),
        policy_version="p1",
        workspace_version="w1",
        permission_version="a1",
    )
    run = AgentRunRecord(
        run_id=run_id,
        session_id="session",
        trace_id="trace",
        parent_run_id="parent_run",
        status=AgentRunStatus.RUNNING,
        user_input="do work",
        created_at="2026-01-01T00:00:00+00:00",
        metadata={"context_snapshot": snapshot.model_dump(mode="json")},
    )
    llm = _LLM()
    turn_loop = AgentTurnLoop.__new__(AgentTurnLoop)
    turn_loop.run_manager = _RunManager(run)
    turn_loop.llm_client = llm
    runner = AgentGraphRunner.__new__(AgentGraphRunner)
    runner.turn_loop = turn_loop
    runner._requests = {run_id: {"llm_response_mode": "text"}}

    runner._invoke_graph_sync(_Graph(turn_loop), {}, run_id)

    assert llm.request is not None
    assert llm.request["client_name"] == "profile_client"
    assert llm.request["model"] == "model-v2"
    assert llm.request["reasoning_effort"] is None
    assert llm.request["response_mode"] == LLMResponseMode.TEXT
    assert llm.request["metadata"] == {
        "stage": "decision",
        "inference_selection_source": "server_profile",
        "inference_profile_id": "careful",
    }
