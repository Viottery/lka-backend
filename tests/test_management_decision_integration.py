"""A waiting independent line wakes its supervisor without transferring approval."""
import pytest

from app.core.tools import ToolContext
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_coordinator import bind, enable, finalize


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def test_waiting_task_wakes_manager_and_leaves_original_decision_pending(runtime):
    enable(runtime)
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    foreground = runtime.create_agent_run(session_id=sid, user_input="Manage this work", resumable=True)
    runtime.agent_run_manager.mark_running(foreground.run_id)
    context = ToolContext(session_id=sid, run_id=foreground.run_id, safety_review_approved=True)
    accepted = runtime.managed_tasks.start_task(context, command_id="decision-task", purpose="Ask for the key tradeoff")
    task_id = accepted["run_id"]
    runtime.agent_run_manager.mark_running(task_id)
    runtime.agent_run_manager.mark_waiting_user(task_id, question_id="choose-approach", question="Prefer speed or coverage?")
    runtime.agent_run_manager.complete_run(foreground.run_id)
    assert runtime.management_coordinator.tick() == {"captured": 1, "scheduled": 1}
    manager_run = bind(runtime)
    runtime.agent_run_manager.mark_running(manager_run.run_id)
    manager_context = ToolContext(session_id=sid, run_id=manager_run.run_id)
    snapshot = runtime.managed_tasks.inspect_run(manager_context, run_id=task_id)
    assert snapshot["pending_user_question"]["question"] == "Prefer speed or coverage?"
    assert snapshot["user_control"]["run_id"] == task_id
    assert snapshot["user_control"]["continue_path"] == f"/agent/runs/{task_id}/continue"
    assert finalize(runtime, manager_run)
    assert runtime.agent_run_manager.get_run(task_id).status.value == "waiting_user"
    assert runtime.management_coordinator.tick() == {"captured": 0, "scheduled": 0}
    # The manager slot is free even though the independent decision is pending.
    next_turn = runtime.create_agent_run(session_id=sid, user_input="Handle another independent line", resumable=True)
    assert next_turn.session_id == sid
