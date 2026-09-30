from __future__ import annotations

from app.core.agent_graph import AgentGraphRunner
from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_turn import (
    AgentTurnLoop,
    AgentTurnProgressEvent,
    AgentTurnResult,
    AgentTurnWorkingSet,
    _turn_run_id,
    _turn_run_manager,
)
from app.core.config import Settings
from app.core.context_driver import ToolView
from app.core.multi_agent import Plan, PlanStatus, PlanStep, PlanStepStatus, SideEffectLevel
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.tools import ToolContext


def test_unresolved_child_failure_rejects_final_answer_until_step_limit() -> None:
    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="parent", user_input="complete child task")
    manager.mark_running(run.run_id)
    manager._update_run(
        run.run_id,
        status=AgentRunStatus.RUNNING,
        metadata_patch={"multi_agent_replan_required": True},
    )
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.run_manager = manager
    loop.max_decision_steps = 2
    loop._package_catalog = lambda _: []
    loop._decide_next_action = lambda **_: {"action": "final_answer", "reason": "done"}
    loop._record_decision = lambda *args, **kwargs: None
    progress = []
    loop._append_progress = lambda *args, **kwargs: progress.append(kwargs)
    token_manager = _turn_run_manager.set(manager)
    token_id = _turn_run_id.set(run.run_id)
    try:
        answer = loop._run_llm_decision_loop(
            user_input="complete child task", route={}, context_window={},
            context=ToolContext(session_id="parent"), tool_events=[], llm_events=[],
            decision_events=[], progress_events=[], expanded_tools=[],
            selected_package="filesystem",
        )
    finally:
        _turn_run_id.reset(token_id)
        _turn_run_manager.reset(token_manager)
    assert answer == loop._unresolved_multi_agent_answer()
    assert [item["type"] for item in progress] == [
        "multi_agent_final_answer_rejected", "multi_agent_final_answer_rejected"
    ]


def test_failed_plan_cannot_complete_parent_run() -> None:
    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="parent", user_input="complete child task")
    manager.mark_running(run.run_id)
    manager._update_run(
        run.run_id,
        status=AgentRunStatus.RUNNING,
        metadata_patch={"multi_agent_plan": {"status": "failed"}},
    )
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.run_manager = manager
    result = AgentTurnResult(
        run_id=run.run_id, session_id=run.session_id, trace_id=run.trace_id,
        answer="An incomplete fallback answer.", log_path="/tmp/test-run-log.md",
    )
    token_manager = _turn_run_manager.set(manager)
    token_id = _turn_run_id.set(run.run_id)
    try:
        loop._complete_current_run(result)
    finally:
        _turn_run_id.reset(token_id)
        _turn_run_manager.reset(token_manager)
    updated = manager.get_run(run.run_id)
    assert updated.status == AgentRunStatus.FAILED
    assert updated.error_type == "MultiAgentPlanFailed"
    assert updated.log_path == result.log_path
    assert "run_completed" not in [event.type for event in manager.list_events(run.run_id)]


def test_langgraph_rejects_final_answer_while_replan_is_pending() -> None:
    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="parent", user_input="complete child task")
    manager.mark_running(run.run_id)
    manager._update_run(
        run.run_id, status=AgentRunStatus.RUNNING,
        metadata_patch={"multi_agent_replan_required": True},
    )
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.run_manager = manager
    loop._raise_if_cancel_requested = lambda: None
    loop._append_progress = lambda events, **kwargs: events.append(
        AgentTurnProgressEvent(
            event_index=len(events) + 1, created_at="2026-09-29T00:00:00+00:00",
            **kwargs,
        )
    )
    graph = AgentGraphRunner.__new__(AgentGraphRunner)
    graph.turn_loop = loop
    ws = AgentTurnWorkingSet(
        run_id=run.run_id, session_id=run.session_id,
        trace_id=run.trace_id, user_input=run.user_input,
        pending_decision={"action": "final_answer"},
    )
    data = {"observations": [], "progress_events": []}
    graph._data = lambda _: (ws, data)
    graph._save = lambda _s, _ws, _data, phase: {"phase": phase}

    state = graph._validate({})

    assert state["phase"] == "operation_rejected"
    assert data["observations"][-1]["replan_required"] is True
    assert data["progress_events"][-1]["type"] == "multi_agent_final_answer_rejected"


def test_child_relative_path_uses_first_configured_workspace_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "alpha.txt").write_text("Numbers: 17 and 19.\n", encoding="utf-8")
    runtime = LocalKnowledgeAgentRuntime(Settings(
        LKA_DATA_DIR=tmp_path / "data",
        LKA_LOCAL_CONFIG=tmp_path / "missing.toml",
        LKA_WORKSPACE_ROOTS=str(workspace),
    ))
    manager = runtime.agent_run_manager
    parent = manager.create_run(session_id="parent", user_input="read")
    child = manager.create_child_run(
        parent_run_id=parent.run_id, plan_id="plan", step_id="read_alpha",
        attempt=1, user_input="read alpha.txt",
    )
    manager.mark_child_running(child.run_id)
    view = ToolView(
        snapshot_id="snapshot", child_run_id=child.run_id,
        allowed_packages=("filesystem",), allowed_tools=("filesystem.read_file",),
        allowed_paths=(str(workspace),), side_effect_level=SideEffectLevel.READ,
    )
    assert runtime.agent_turn_loop.default_workspace_root == str(workspace)
    graph_context = AgentGraphRunner(runtime.agent_turn_loop)._context(AgentTurnWorkingSet(
        run_id=child.run_id, session_id=child.session_id,
        trace_id=child.trace_id, user_input="read alpha.txt",
    ))
    assert graph_context.workspace_root == str(workspace)
    result = runtime.tool_executor.execute(
        invocation_id="read-alpha", tool_name="filesystem.read_file",
        tool_input={"path": "alpha.txt"},
        context=ToolContext(
            session_id=child.session_id,
            workspace_root=runtime.agent_turn_loop.default_workspace_root,
            tool_view=view,
        ),
    )
    assert result.status == "completed", result.error


def test_langgraph_failed_plan_does_not_emit_completed_parent(tmp_path) -> None:
    runtime = LocalKnowledgeAgentRuntime(Settings(
        LKA_DATA_DIR=tmp_path / "data",
        LKA_LOCAL_CONFIG=tmp_path / "missing.toml",
    ))
    manager = runtime.agent_run_manager
    run = manager.create_run(session_id="parent", user_input="complete child task")
    manager.mark_running(run.run_id)
    loop = runtime.agent_turn_loop
    loop._finalize_multi_agent_plan = lambda run_id: manager._update_run(
        run_id, status=AgentRunStatus.RUNNING,
        metadata_patch={"multi_agent_plan": {"status": "failed"}},
    )
    graph = AgentGraphRunner(loop)
    ws = AgentTurnWorkingSet(
        run_id=run.run_id, session_id=run.session_id,
        trace_id=run.trace_id, user_input=run.user_input,
        terminal_answer="Child plan did not complete.",
    )
    data = {
        "package_catalog": [], "context_window": {}, "expanded_tools": [],
        "decision_events": [], "tool_events": [], "progress_events": [],
        "warnings": [], "llm_events": [],
    }
    graph._data = lambda _: (ws, data)
    graph._finalize({})

    updated = manager.get_run(run.run_id)
    assert updated.status == AgentRunStatus.FAILED
    assert updated.error_type == "MultiAgentPlanFailed"
    assert "run_completed" not in [event.type for event in manager.list_events(run.run_id)]
    assert any(event.type == "run_failed" for event in manager.list_events(run.run_id))


def test_answered_user_question_keeps_failed_child_pending_for_patch() -> None:
    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="parent", user_input="complete child task")
    manager.mark_running(run.run_id)
    plan = Plan(
        correlation_id=run.trace_id, plan_id="plan", parent_run_id=run.run_id,
        session_id=run.session_id, objective=run.user_input,
        status=PlanStatus.WAITING_USER,
        steps=(
            PlanStep(
                correlation_id=run.trace_id, step_id="root_coordinator",
                objective=run.user_input, output_contract="Answer", status=PlanStepStatus.RUNNING,
            ),
            PlanStep(
                correlation_id=run.trace_id, step_id="failed_child",
                objective="Read evidence", output_contract="Evidence", status=PlanStepStatus.FAILED,
            ),
        ),
    )
    manager._update_run(
        run.run_id, status=AgentRunStatus.WAITING_USER,
        metadata_patch={
            "multi_agent_plan": plan.model_dump(mode="json"),
            "multi_agent_replan_required": True,
            "pending_user_question": {"question_id": "question", "patch_id": "patch"},
            "pending_user_answer_command_id": "answer-command",
        },
    )
    manager.get_user_continuation = lambda _run_id, _command_id: "Use an alternative source."
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.run_manager = manager

    assert loop.resume_user_question_plan(run.run_id, "answer-command") == (
        "question", "Use an alternative source."
    )
    updated = manager.get_run(run.run_id)
    assert updated.metadata["multi_agent_replan_required"] is True
    assert updated.metadata["multi_agent_plan"]["status"] == "replanning"
