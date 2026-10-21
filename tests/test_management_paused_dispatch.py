"""Paused management is discoverably read-only and never pretends to accept work."""
import pytest

from app.core.tools import ToolInvocation
from app.tool_packages.management import management_tools
from tests.test_management_tasks import counts
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_management_tasks import runtime as runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def test_paused_manager_policy_exposes_reads_and_explains_blocked_dispatch(runtime):
    sid = runtime.management_service.create()["session"]["session_id"]
    policy = runtime._session_run_policy(sid)
    assert "当前管理状态为 paused" in policy.role_instructions
    assert "management.read_session" in policy.allowed_tools
    assert "management.list_goals" in policy.allowed_tools
    assert "management.read_history" in policy.allowed_tools
    for name in ("management.start_task", "management.delegate_work", "management.create_matter",
                 "management.update_matter", "management.send_instruction", "management.set_goal"):
        assert name not in policy.allowed_tools
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Management is paused; no task accepted"),
        "管理已暂停，任务尚未受理。请先在跟进面板启用管理。",
    ])
    before = counts(runtime)
    answer = runtime.run_agent_turn(session_id=sid, user_input="Create a project in D drive")
    assert "尚未受理" in answer.answer
    assert counts(runtime)["managed_session_links"] == before["managed_session_links"]
    assert counts(runtime)["management_commands"] == before["management_commands"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    assert "management.start_task" in runtime._session_run_policy(sid).allowed_tools


def test_controller_paused_after_policy_freeze_returns_nonretryable_rejection(runtime):
    context = context_fixture.__wrapped__(runtime)
    frozen = runtime.agent_run_manager.get_run(context.run_id).metadata["session_policy"]
    assert "management.start_task" in frozen["allowed_tools"]
    runtime.management_service.update(context.session_id, expected_revision=2, status="paused")
    tool = next(t for t in management_tools(runtime.managed_tasks) if t.spec.name == "management.start_task")
    before = counts(runtime)
    result = tool.invoke(invocation=ToolInvocation(
        invocation_id="paused-call", tool=tool.spec, session_id=context.session_id, context_id=context.trace_id,
        input={"command_id": "first", "purpose": "Build a synthetic project"},
    ), context=context)
    assert result.status == "rejected"
    assert result.output["task_accepted"] is False
    assert result.output["management_status"] == "paused"
    assert result.output["failure"]["code"] == "management_paused"
    assert result.output["failure"]["retryable"] is False
    assert counts(runtime) == before


@pytest.mark.parametrize("name", ["management.read_task_result", "management.inspect_run"])
def test_deleted_task_read_is_nonretryable_source_rejection_not_approval_failure(runtime, name):
    context = context_fixture.__wrapped__(runtime)
    task = runtime.managed_tasks.start_task(context, command_id="deleted-task", purpose="Synthetic task")
    runtime.agent_run_manager.mark_running(task["run_id"])
    runtime.agent_run_manager.complete_run(task["run_id"])
    runtime.delete_session(session_id=task["session_id"])
    tool = runtime.tool_registry.get_tool(name)
    result = tool.invoke(invocation=ToolInvocation(
        invocation_id="stale-source", tool=tool.spec, session_id=context.session_id, context_id=context.trace_id,
        input={"run_id": task["run_id"]},
    ), context=context)
    assert result.status == "rejected"
    assert result.output["failure"]["category"] == "source_lifecycle"
    assert result.output["failure"]["code"] == "task_source_unavailable"
    assert result.output["failure"]["retryable"] is False
    assert "content" not in result.output
