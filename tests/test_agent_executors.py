import asyncio

import pytest

from app.core.agent_executors import (
    AgentDefinition,
    AgentExecutorRegistry,
    MockWorkflowExecutor,
    MockWorkflowScenario,
)
from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.context_driver import AgentView, AuditView, ContextViews, PlannerView, ToolView
from app.core.multi_agent import (
    ContextSnapshot,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
)


def _child_context(manager: InMemoryAgentRunManager):
    parent = manager.create_run(session_id="session-parent", user_input="Run workflow.")
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan-1",
        step_id="workflow-step",
        attempt=1,
        user_input="Execute the workflow assignment.",
        agent_id="mock_workflow",
        agent_version="1",
    )
    scope = ScopeGrant(side_effect_level=SideEffectLevel.NONE)
    snapshot = ContextSnapshot(
        correlation_id="trace-child",
        snapshot_id="snapshot-1",
        parent_run_id=parent.run_id,
        child_run_id=child.run_id,
        session_id=child.session_id,
        plan_id="plan-1",
        step_id="workflow-step",
        agent_id="mock_workflow",
        agent_version="1",
        objective="Summarize the assigned fixture.",
        output_contract="Return a concise summary.",
        effective_scope=scope,
        budget=RuntimeBudget(max_tool_calls=1),
        policy_version="policy-1",
        workspace_version="workspace-1",
        permission_version="permissions-1",
    )
    views = ContextViews(
        agent=AgentView(
            objective=snapshot.objective,
            output_contract=snapshot.output_contract,
            mode="working",
        ),
        tool=ToolView(
            snapshot_id=snapshot.snapshot_id,
            child_run_id=child.run_id,
            side_effect_level=SideEffectLevel.NONE,
        ),
        planner=PlannerView(plan_id=snapshot.plan_id, step_id=snapshot.step_id),
        audit=AuditView(
            snapshot_id=snapshot.snapshot_id,
            child_run_id=child.run_id,
            session_id=child.session_id,
            policy_version=snapshot.policy_version,
            workspace_version=snapshot.workspace_version,
            permission_version=snapshot.permission_version,
        ),
    )
    return child, snapshot, views


class _NoopExecutor:
    async def execute(self, **kwargs):
        raise AssertionError(f"Unexpected execution: {kwargs}")

    async def resume(self, child_run_id):
        raise AssertionError(f"Unexpected resume: {child_run_id}")

    def from_terminal(self, child, snapshot):
        raise AssertionError(f"Unexpected terminal conversion: {child}, {snapshot}")


def test_registry_resolves_enabled_agent_and_rejects_unknown_or_wrong_version():
    registry = AgentExecutorRegistry()
    definition = AgentDefinition(
        agent_id="mock_workflow",
        version="1",
        executor_kind="workflow",
    )
    executor = _NoopExecutor()
    registry.register(definition, executor)

    resolved_definition, resolved_executor = registry.resolve("mock_workflow", "1")
    assert resolved_definition is definition
    assert resolved_executor is executor
    assert registry.enabled_ids == ("mock_workflow",)
    with pytest.raises(ValueError, match="unavailable: absent"):
        registry.resolve("absent")
    with pytest.raises(ValueError, match="version is unavailable"):
        registry.resolve("mock_workflow", "2")


def test_registry_keeps_versions_available_and_pins_default_explicitly():
    registry = AgentExecutorRegistry()
    version_one = AgentDefinition(
        agent_id="mock_workflow", version="1", executor_kind="workflow"
    )
    version_two = AgentDefinition(
        agent_id="mock_workflow", version="2", executor_kind="workflow"
    )
    executor_one = _NoopExecutor()
    executor_two = _NoopExecutor()
    registry.register(version_one, executor_one)
    registry.register(version_two, executor_two)

    assert registry.resolve("mock_workflow")[0] is version_one
    assert registry.resolve("mock_workflow", "1") == (version_one, executor_one)
    assert registry.resolve("mock_workflow", "2") == (version_two, executor_two)

    registry.set_current("mock_workflow", "2")
    assert registry.resolve("mock_workflow")[0] is version_two
    # A persisted version remains exact after the default for new steps changes.
    assert registry.resolve("mock_workflow", "1") == (version_one, executor_one)
    with pytest.raises(ValueError, match="version is unavailable"):
        registry.resolve("mock_workflow", "3")


def test_registry_disabled_version_fails_closed_without_fallback():
    registry = AgentExecutorRegistry()
    enabled = AgentDefinition(
        agent_id="versioned_agent", version="1", executor_kind="workflow"
    )
    disabled = AgentDefinition(
        agent_id="versioned_agent",
        version="2",
        executor_kind="workflow",
        enabled=False,
    )
    registry.register(enabled, _NoopExecutor())
    registry.register(disabled, _NoopExecutor())

    with pytest.raises(ValueError, match="Agent is unavailable: versioned_agent"):
        registry.resolve("versioned_agent", "2")
    with pytest.raises(ValueError, match="cannot be current"):
        registry.register(
            AgentDefinition(
                agent_id="another_agent",
                version="1",
                executor_kind="workflow",
                enabled=False,
            ),
            _NoopExecutor(),
            make_current=True,
        )
    assert registry.resolve("versioned_agent")[0] is enabled


def test_registry_rejects_disabled_definition():
    registry = AgentExecutorRegistry()
    registry.register(
        AgentDefinition(
            agent_id="disabled-agent",
            version="1",
            executor_kind="workflow",
            enabled=False,
        ),
        _NoopExecutor(),
    )

    assert registry.enabled_ids == ()
    with pytest.raises(ValueError, match="unavailable: disabled-agent"):
        registry.resolve("disabled-agent")


def test_mock_workflow_binds_context_and_emits_deterministic_node_events():
    manager = InMemoryAgentRunManager()
    child, snapshot, views = _child_context(manager)
    executor = MockWorkflowExecutor(manager)

    result = asyncio.run(
        executor.execute(child_run_id=child.run_id, snapshot=snapshot, views=views)
    )

    current = manager.get_run(child.run_id)
    assert current.status == AgentRunStatus.COMPLETED
    assert current.metadata["context_snapshot"]["snapshot_id"] == snapshot.snapshot_id
    assert current.metadata["context_views"]["tool"]["child_run_id"] == child.run_id
    events = manager.list_events(child.run_id)
    node_events = [event for event in events if event.type == "expert.workflow.node_completed"]
    assert [event.payload["node"] for event in node_events] == ["prepare", "work", "verify"]
    assert all(event.payload["agent_id"] == "mock_workflow" for event in node_events)
    assert all(event.payload["schema_version"] == 1 for event in node_events)
    assert result.status.value == "completed"
    assert result.summary == "Mock workflow completed: Summarize the assigned fixture."
    assert result.snapshot_id == snapshot.snapshot_id


def test_mock_workflow_rejects_mismatched_context_before_running_child():
    manager = InMemoryAgentRunManager()
    child, snapshot, views = _child_context(manager)
    mismatched_snapshot = snapshot.model_copy(update={"session_id": "other-session"})

    with pytest.raises(ValueError, match="context does not match"):
        asyncio.run(
            MockWorkflowExecutor(manager).execute(
                child_run_id=child.run_id,
                snapshot=mismatched_snapshot,
                views=views,
            )
        )

    assert manager.get_run(child.run_id).status == AgentRunStatus.QUEUED
    assert not any(
        event.type == "expert.workflow.node_completed"
        for event in manager.list_events(child.run_id)
    )


def test_mock_workflow_terminal_replay_returns_result_without_duplicate_events():
    manager = InMemoryAgentRunManager()
    child, snapshot, views = _child_context(manager)
    executor = MockWorkflowExecutor(manager)
    first_result = asyncio.run(
        executor.execute(child_run_id=child.run_id, snapshot=snapshot, views=views)
    )
    event_count = len(manager.list_events(child.run_id))

    replay_result = asyncio.run(
        executor.execute(child_run_id=child.run_id, snapshot=snapshot, views=views)
    )

    assert replay_result.result_id == first_result.result_id
    assert replay_result.child_run_id == first_result.child_run_id
    assert replay_result.snapshot_id == first_result.snapshot_id
    assert replay_result.status == first_result.status
    assert replay_result.summary == first_result.summary
    assert len(manager.list_events(child.run_id)) == event_count
    assert manager.get_run(child.run_id).status == AgentRunStatus.COMPLETED


@pytest.mark.parametrize(
    "scenario, run_status, result_status, event_type",
    [
        (MockWorkflowScenario.PARTIAL, AgentRunStatus.COMPLETED, "partial", "expert.workflow.node_completed"),
        (MockWorkflowScenario.FAILURE, AgentRunStatus.FAILED, "failed", "expert.workflow.node_failed"),
        (
            MockWorkflowScenario.WAITING_CONFIRMATION,
            AgentRunStatus.WAITING_CONFIRMATION,
            "blocked",
            "expert.workflow.approval_waiting",
        ),
        (MockWorkflowScenario.CANCEL, AgentRunStatus.CANCELLED, "cancelled", "expert.workflow.cancel_requested"),
        (MockWorkflowScenario.TIMEOUT, AgentRunStatus.TIMED_OUT, "timed_out", "expert.workflow.node_timed_out"),
    ],
)
def test_mock_workflow_server_selected_outcomes_have_consistent_result_and_events(
    scenario, run_status, result_status, event_type
):
    manager = InMemoryAgentRunManager()
    child, snapshot, views = _child_context(manager)

    result = asyncio.run(
        MockWorkflowExecutor(manager, scenario=scenario).execute(
            child_run_id=child.run_id,
            snapshot=snapshot,
            views=views,
        )
    )

    current = manager.get_run(child.run_id)
    events = manager.list_events(child.run_id)
    assert current.status == run_status
    assert result.status.value == result_status
    assert any(event.type == event_type for event in events)
    assert all(
        event.child_run_id == child.run_id
        for event in events
        if event.stage == "expert_workflow"
    )
    if scenario == MockWorkflowScenario.PARTIAL:
        assert result.missing_requirements
        assert result.warnings
    if scenario == MockWorkflowScenario.FAILURE:
        assert result.failure is not None
        assert result.failure.code == "mock_workflow_failure"
    if scenario == MockWorkflowScenario.WAITING_CONFIRMATION:
        assert result.failure is not None
        assert result.failure.code == "waiting_confirmation"
    if scenario == MockWorkflowScenario.TIMEOUT:
        assert result.failure is not None
        assert result.failure.code == "timeout"


def test_mock_workflow_node_events_are_durable_across_run_manager_reopen(tmp_path):
    store_path = tmp_path / "mock-workflow.sqlite3"
    manager = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store_path))
    child, snapshot, views = _child_context(manager)
    asyncio.run(MockWorkflowExecutor(manager).execute(child_run_id=child.run_id, snapshot=snapshot, views=views))
    manager.close()

    restored = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store_path))
    restored.get_run(child.run_id)
    restored_events = restored.list_events(child.run_id)
    assert [
        event.payload["node"]
        for event in restored_events
        if event.type == "expert.workflow.node_completed"
    ] == ["prepare", "work", "verify"]
    assert restored.get_run(child.run_id).status == AgentRunStatus.COMPLETED
    restored.close()


def test_mock_workflow_scenario_cannot_be_supplied_as_arbitrary_string():
    with pytest.raises(TypeError, match="trusted server code"):
        MockWorkflowExecutor(InMemoryAgentRunManager(), scenario="failure")
