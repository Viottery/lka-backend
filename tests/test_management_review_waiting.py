"""Managed background work retains its binding while waiting for user review."""
from contextlib import closing

import pytest
from pydantic import ValidationError

from app.core.agent_runs import AgentRunStatus
from app.core.local_config import ManagementConfig
from app.core.safety import SafetyReviewDecision, SafetyReviewMode
from app.experts.management import ManagementExecutionFenced
from tests.test_management_coordinator import bind, timer
from tests.test_management_coordinator import runtime as runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def test_management_review_mode_is_opt_in_and_validated():
    assert ManagementConfig().safety_review_mode is None
    assert ManagementConfig(safety_review_mode="llm").safety_review_mode == "llm"
    with pytest.raises(ValidationError):
        ManagementConfig(safety_review_mode="unreviewed")


def test_background_manual_review_stays_bound_and_resumes_once(runtime):
    sid, _ = timer(runtime)
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.MANUAL
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.read_events", tool_input={}),
        operation("final_answer", reason="The event inspection is complete"),
        "Events checked after approval.",
    ])
    assert runtime.management_coordinator.tick()["scheduled"] == 1
    run = bind(runtime)
    assert runtime.agent_dispatch.run_one()
    manager = runtime.agent_run_manager
    assert manager.get_run(run.run_id).status == AgentRunStatus.WAITING_CONFIRMATION
    [review] = manager.list_safety_reviews(run.run_id)
    assert review.status.value == "pending"
    runtime.management_coordinator.reconcile()
    assert runtime.management_goals.list_executions(sid)["executions"][0]["status"] == "bound"
    assert runtime.management_coordinator.tick()["scheduled"] == 0
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] is None
    manager.decide_safety_review_with_transition(
        review_id=review.review_id, decision=SafetyReviewDecision.APPROVE,
        decided_by="user", reason="Approved this exact event read.",
    )
    result = runtime.resume_agent_run(run.run_id)
    assert result.answer == "Events checked after approval."
    assert manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED
    execution = runtime.management_goals.list_executions(sid)["executions"][0]
    assert execution["status"] == "completed"
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] == execution["execution_id"]
    completed = [event for event in manager.list_events(run.run_id)
                 if event.type == "tool_completed" and event.payload.get("tool_name") == "management.read_events"]
    assert len(completed) == 1


@pytest.mark.parametrize("status", ["waiting_confirmation", "waiting_user"])
def test_waiting_run_keeps_all_controller_and_goal_fences(runtime, status):
    sid, goal = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    with closing(runtime._conn()) as conn:
        execution = conn.execute("SELECT * FROM manager_executions WHERE run_id=?", (run.run_id,)).fetchone()
        record = runtime.agent_run_manager.get_run(run.run_id).model_dump(mode="json")
        record["status"] = status
        runtime.management_coordinator._fence(conn, execution, record)
    runtime.management_goals.update_goal(sid, goal["goal_id"], expected_revision=1, objective="Changed scope")
    with closing(runtime._conn()) as conn, pytest.raises(ManagementExecutionFenced, match="manager_goal_changed"):
        runtime.management_coordinator._fence(conn, execution, record)
    runtime.management_service.update(sid, expected_revision=2, status="paused")
    with closing(runtime._conn()) as conn, pytest.raises(ManagementExecutionFenced, match="manager_controller_changed"):
        runtime.management_coordinator._fence(conn, execution, record)
