from __future__ import annotations

import asyncio

import pytest

from app.core.agent_executors import AgentDefinition, AgentExecutorRegistry, MockWorkflowExecutor
from app.core.agent_runs import InMemoryAgentRunManager
from app.core.local_config import AgentInferenceProfile
from app.core.multi_agent import (
    Plan,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RuntimeBudget,
    ScopeGrant,
)
from app.core.multi_agent_scheduler import MultiAgentScheduler, SchedulerContext


def test_scheduler_runs_registered_mock_workflow_without_react() -> None:
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="expert_session", user_input="run the example")
    manager.mark_running(parent.run_id)
    plan = Plan(
        correlation_id=parent.trace_id,
        plan_id="expert_plan",
        parent_run_id=parent.run_id,
        session_id=parent.session_id,
        objective="run the example",
        steps=(
            PlanStep(
                correlation_id=parent.trace_id,
                step_id="root_coordinator",
                objective="Coordinate.",
                output_contract="Answer.",
                status=PlanStepStatus.RUNNING,
            ),
            PlanStep(
                correlation_id=parent.trace_id,
                step_id="expert_step",
                agent_id="mock_workflow",
                objective="Produce a deterministic example result.",
                output_contract="Example result.",
            ),
        ),
        status=PlanStatus.RUNNING,
    )
    manager.record_multi_agent_plan(
        parent.run_id,
        event_type="fork_subtasks_validated",
        payload={"plan_id": plan.plan_id},
        plan=plan.model_dump(mode="json"),
    )

    class NeverRunReact:
        async def execute(self, **kwargs):
            raise AssertionError("ReAct runner must not execute a workflow expert")

    general = NeverRunReact()
    registry = AgentExecutorRegistry()
    registry.register(
        AgentDefinition(agent_id="general_agent", version="1", executor_kind="react"),
        general,
    )
    registry.register(
        AgentDefinition(agent_id="mock_workflow", version="1", executor_kind="workflow"),
        MockWorkflowExecutor(manager),
    )
    scheduler = MultiAgentScheduler(
        run_manager=manager,
        child_executor=general,
        agent_registry=registry,
    )
    scope = ScopeGrant()
    outcome = asyncio.run(scheduler.execute_plan_async(
        parent.run_id,
        context=SchedulerContext(
            parent_effective_scope=scope,
            session_scope=scope,
            workspace_scope=scope,
            policy_scope=scope,
            budget=RuntimeBudget(max_wall_time_seconds=30),
        ),
    ))
    assert outcome.status == "completed"
    assert len(outcome.task_results) == 1
    assert outcome.task_results[0].summary.startswith("Mock workflow completed:")
    child = manager.child_tree(parent.run_id)[0]
    assert child.metadata["agent_id"] == "mock_workflow"
    assert child.metadata["agent_version"] == "1"
    assert any(event.type == "expert.workflow.node_completed" for event in manager.list_events(child.run_id))


def test_scheduler_rejects_frozen_inference_profile_drift_including_none_effort() -> None:
    manager = InMemoryAgentRunManager()
    registry = AgentExecutorRegistry()
    registry.register_inference_profile(
        AgentInferenceProfile(
            profile_id="careful",
            client_name="profile_client",
            model="model-v1",
            reasoning_effort="high",
        )
    )

    class NeverRun:
        async def execute(self, **kwargs):
            raise AssertionError("Profile drift must be rejected before execution")

    scheduler = MultiAgentScheduler(
        run_manager=manager,
        child_executor=NeverRun(),
        agent_registry=registry,
    )
    frozen_step = PlanStep(
        correlation_id="trace",
        step_id="step",
        objective="Check profile drift.",
        output_contract="Result.",
        inference_profile_id="careful",
        inference_client_name="profile_client",
        inference_model="model-v1",
        inference_reasoning_effort=None,
    )

    with pytest.raises(ValueError, match="changed after PlanStep was frozen"):
        scheduler._resolved_profile(frozen_step)
