from __future__ import annotations

from app.core.agent_runs import AgentRunRecord, AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_turn import AgentTurnLoop
from app.core.multi_agent import (
    ForkCallerKind,
    ForkPolicy,
    ForkSubtasksOperation,
    ForkSubtaskSpec,
    ScopeGrant,
    SideEffectLevel,
)


def test_fork_caller_role_comes_from_snapshot_and_missing_role_fails_closed() -> None:
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.run_manager = None
    base = {
        "run_id": "child",
        "session_id": "session",
        "trace_id": "trace",
        "parent_run_id": "parent",
        "status": AgentRunStatus.RUNNING,
        "user_input": "do task",
        "created_at": "2026-09-29T00:00:00+00:00",
    }
    coordinator = AgentRunRecord(
        **base, metadata={"context_snapshot": {"agent_kind": "coordinator"}}
    )
    leaf = AgentRunRecord(
        **base, metadata={"context_snapshot": {"agent_kind": "leaf"}}
    )
    legacy = AgentRunRecord(**base)

    assert loop._fork_caller_kind_for_run(coordinator) == ForkCallerKind.COORDINATOR
    assert loop._fork_caller_kind_for_run(leaf) == ForkCallerKind.LEAF
    assert loop._fork_caller_kind_for_run(legacy) == ForkCallerKind.LEAF


def _loop_and_run(
    durable_store: SqliteAgentRunStore | None = None,
) -> tuple[AgentTurnLoop, InMemoryAgentRunManager, str]:
    manager = InMemoryAgentRunManager(durable_store=durable_store)
    run = manager.create_run(session_id="session_planner", user_input="compare options")
    manager.mark_running(run.run_id)
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.run_manager = manager
    loop.fork_policy = ForkPolicy(
        max_depth=1,
        max_children=4,
        max_fork_size=3,
        allowed_scope=ScopeGrant(
            allowed_packages=("knowledge",),
            allowed_tools=("knowledge.search",),
            side_effect_level=SideEffectLevel.READ,
        ),
    )
    return loop, manager, run.run_id


def _fork(operation_id: str, subtasks: tuple[ForkSubtaskSpec, ...]) -> dict:
    return ForkSubtasksOperation(
        correlation_id="fork_trace",
        operation_id=operation_id,
        parent_step_id="root_coordinator",
        subtasks=subtasks,
    ).model_dump(mode="json")


def test_planner_fork_validates_and_persists_replayable_dag(tmp_path) -> None:
    store = SqliteAgentRunStore(tmp_path / "agent_runs.sqlite3")
    loop, manager, run_id = _loop_and_run(store)
    operation = _fork(
        "fork_valid",
        (
            ForkSubtaskSpec(
                step_id="option_a",
                objective="Research option A.",
                output_contract="A short finding.",
                requested_scope={
                    "allowed_packages": ["knowledge"],
                    "allowed_tools": ["knowledge.search"],
                    "side_effect_level": "read",
                },
            ),
            ForkSubtaskSpec(
                step_id="option_b",
                objective="Research option B.",
                output_contract="A short finding.",
                depends_on=("option_a",),
                requested_scope={
                    "allowed_packages": ["knowledge"],
                    "allowed_tools": ["knowledge.search"],
                    "side_effect_level": "read",
                },
            ),
        ),
    )

    result = loop._handle_fork_subtasks_decision(
        run_id=run_id, user_input="compare options", operation=operation
    )

    run = manager.get_run(run_id)
    events = manager.list_events(run_id)
    assert result["status"] == "validated"
    assert [step["step_id"] for step in run.metadata["multi_agent_plan"]["steps"]] == [
        "root_coordinator",
        "option_a",
        "option_b",
    ]
    assert events[-1].type == "fork_subtasks_validated"
    assert events[-1].payload["operation_id"] == "fork_valid"
    assert manager.child_tree(run_id) == []
    restored = InMemoryAgentRunManager(durable_store=store)
    replayed_run = restored.get_run(run_id)
    assert replayed_run is not None
    assert replayed_run.metadata["multi_agent_plan"]["plan_id"] == result["plan_id"]
    assert restored.list_events(run_id)[-1].type == "fork_subtasks_validated"


def test_schema_rejection_keeps_operation_id_available_for_durable_correction(tmp_path) -> None:
    store = SqliteAgentRunStore(tmp_path / "agent_runs.sqlite3")
    loop, manager, run_id = _loop_and_run(store)
    malformed = {"type": "fork_subtasks", "operation_id": "fork_correctable"}

    rejected = loop._handle_fork_subtasks_decision(
        run_id=run_id,
        user_input="compare options",
        operation=malformed,
        parse_error="subtasks: required field missing",
    )

    assert rejected["status"] == "rejected"
    assert manager.list_events(run_id)[-1].type == "fork_subtasks_rejected"
    assert manager.get_run(run_id).metadata.get("fork_operations", {}).get("fork_correctable") is None

    restored = InMemoryAgentRunManager(durable_store=store)
    loop.run_manager = restored
    corrected = _fork(
        "fork_correctable",
        (ForkSubtaskSpec(
            step_id="option_a",
            objective="Research option A.",
            output_contract="A concise supported finding.",
        ),),
    )
    accepted = loop._handle_fork_subtasks_decision(
        run_id=run_id, user_input="compare options", operation=corrected,
    )

    assert accepted["status"] == "validated"
    assert restored.get_run(run_id).metadata["fork_operations"]["fork_correctable"]["status"] == "validated"

    loop._handle_fork_subtasks_decision(
        run_id=run_id,
        user_input="compare options",
        operation=malformed,
        parse_error="subtasks: required field missing",
    )
    assert restored.get_run(run_id).metadata["fork_operations"]["fork_correctable"]["status"] == "validated"


def test_omitted_requested_scope_keeps_server_computed_child_grant() -> None:
    loop, manager, run_id = _loop_and_run()
    operation = _fork(
        "fork_server_scope",
        (ForkSubtaskSpec(
            step_id="option_a",
            objective="Research option A.",
            output_contract="A concise supported finding.",
        ),),
    )

    accepted = loop._handle_fork_subtasks_decision(
        run_id=run_id, user_input="compare options", operation=operation,
    )

    assert accepted["status"] == "validated"
    validated = next(
        step for step in manager.get_run(run_id).metadata["multi_agent_plan"]["steps"]
        if step["step_id"] == "option_a"
    )
    assert validated["allowed_tools"] == ["knowledge.search"]
    assert validated["effective_scope"]["side_effect_level"] == "read"


def test_only_structured_fork_operation_can_enter_fork_action() -> None:
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    structured = loop._normalize_decision_output(
        {
            "operation": {
                "type": "fork_subtasks",
                "operation_id": "fork_structured",
                "parent_step_id": "root_coordinator",
                "subtasks": [
                    {
                        "step_id": "research",
                        "objective": "Research the question.",
                        "output_contract": "A short finding.",
                    }
                ],
            }
        },
        raw_output="{}",
    )
    natural_language = loop._normalize_decision_output(
        {"assistant_message": "I should fork this into subtasks."},
        raw_output="I should fork this into subtasks.",
    )
    assert structured["action"] == "fork_subtasks"
    assert natural_language.get("action") != "fork_subtasks"


def test_planner_rejects_cyclic_fork_without_creating_child_runs() -> None:
    loop, manager, run_id = _loop_and_run()
    operation = _fork(
        "fork_cycle",
        (
            ForkSubtaskSpec(
                step_id="step_a",
                objective="A.",
                output_contract="A.",
                depends_on=("step_b",),
            ),
            ForkSubtaskSpec(
                step_id="step_b",
                objective="B.",
                output_contract="B.",
                depends_on=("step_a",),
            ),
        ),
    )

    result = loop._handle_fork_subtasks_decision(
        run_id=run_id, user_input="compare options", operation=operation
    )

    assert result["status"] == "rejected"
    assert "cyclic" in result["message"]
    assert manager.get_run(run_id).metadata.get("multi_agent_plan") is None
    assert manager.child_tree(run_id) == []
    assert manager.list_events(run_id)[-1].type == "fork_subtasks_rejected"
    assert manager.get_run(run_id).metadata["fork_operations"]["fork_cycle"]["status"] == "rejected"


def test_fork_step_cannot_depend_on_active_coordinator() -> None:
    loop, manager, run_id = _loop_and_run()
    operation = _fork(
        "fork_parent_dependency",
        (
            ForkSubtaskSpec(
                step_id="research",
                objective="Research.",
                output_contract="A finding.",
                depends_on=("root_coordinator",),
            ),
        ),
    )

    result = loop._handle_fork_subtasks_decision(
        run_id=run_id, user_input="research", operation=operation
    )

    assert result["status"] == "rejected"
    assert "still-running coordinator" in result["message"]
    assert manager.child_tree(run_id) == []
