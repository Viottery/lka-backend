"""Capture policies feed durable manager executions, with final policy fences."""
from datetime import UTC, datetime, timedelta

import pytest

from app.core.safety import SafetyReviewMode
from app.core.tools import ToolInvocation
from app.domains.matters import MatterCreateInput
from app.experts.management import ManagementExecutionFenced
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_coordinator import bind, enable, finalize
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def active_manager(runtime):
    enable(runtime)
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    return sid


@pytest.mark.parametrize("disable", [False, True])
def test_attention_event_binds_matter_and_checks_policy_at_commit(runtime, disable):
    sid = active_manager(runtime)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    matter = runtime.matter_service.create_matter(MatterCreateInput(title="Important overdue commitment", due_at=yesterday))
    runtime.management_attention.configure(sid, 0, True)
    assert runtime.management_coordinator.tick() == {"captured": 1, "scheduled": 1}
    run = bind(runtime)
    assert run.metadata["manager_matter_versions"] == {matter.matter_id: matter.revision}
    assert len(run.metadata["manager_cause_paths"]) == 1
    runtime.agent_run_manager.mark_running(run.run_id)
    if disable:
        runtime.management_attention.configure(sid, 1, False)
        with pytest.raises(ManagementExecutionFenced, match="manager_policy_changed"):
            finalize(runtime, run)
        assert not runtime.management_goals.list_deliveries(sid)["deliveries"]
    else:
        assert finalize(runtime, run)
        assert runtime.management_coordinator.tick()["captured"] == 0
        assert len(runtime.management_goals.list_deliveries(sid)["deliveries"]) == 1


@pytest.mark.parametrize("disable", [False, True])
def test_schedule_event_reaches_coordinator_and_stale_policy_cannot_publish(runtime, disable):
    sid = active_manager(runtime)
    goal = runtime.management_goals.create_goal(sid, objective="Produce a periodic summary")
    now = datetime.now(UTC)
    fields = {"goal_id": goal["goal_id"], "timezone": "Asia/Shanghai", "kind": "one_shot", "run_at": (now + timedelta(seconds=1)).isoformat()}
    receipt = runtime.management_schedules.configure(sid, "one-shot", **fields)
    assert runtime.management_coordinator.tick(now=now + timedelta(seconds=2)) == {"captured": 1, "scheduled": 1}
    schedule_id = receipt["schedule"]["schedule_id"]
    [scheduled_event] = [event for event in runtime.management_goals.list_events(sid)["events"]
                         if event["payload"].get("schedule_id") == schedule_id]
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    if disable:
        runtime.management_schedules.configure(sid, "disable", **fields,
            schedule_id=receipt["schedule"]["schedule_id"], expected_revision=1, enabled=False)
        with pytest.raises(ManagementExecutionFenced, match="manager_policy_changed"):
            finalize(runtime, run)
    else:
        assert finalize(runtime, run)
        # The unfinished goal may receive an independent attention sweep later.
        runtime.management_coordinator.tick(now=now + timedelta(days=90))
        [event] = [event for event in runtime.management_goals.list_events(sid)["events"]
                   if event["payload"].get("schedule_id") == schedule_id]
        [execution] = runtime.management_goals.list_executions(sid)["executions"]
        assert event["event_id"] == scheduled_event["event_id"]
        assert event["payload"] == scheduled_event["payload"]
        assert event["consumed_execution_id"] == execution["execution_id"]
        assert runtime.management_schedules.get(sid, schedule_id)["next_run_at"] is None


def test_harness_configures_weekly_policy_and_preserves_mandatory_review(runtime):
    sid = active_manager(runtime)
    goal = runtime.management_goals.create_goal(sid, objective="Weekly evidence summary")
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.configure_schedule", tool_input={
            "command_id": "weekly-user-delegation", "goal_id": goal["goal_id"], "timezone": "Asia/Shanghai",
            "kind": "weekly", "weekday": 4, "local_time": "18:00"}),
        operation("final_answer", reason="Schedule saved"), "Weekly check configured.",
    ])
    result = runtime.run_agent_turn(session_id=sid, user_input="每周五18点汇总一次。", safety_review_mode=SafetyReviewMode.SKIP)
    event = next(e for e in result.tool_events if e.tool_name == "management.configure_schedule")
    assert event.result["status"] == "completed", event.model_dump()
    assert runtime.agent_run_manager.list_safety_reviews(result.run_id)
    rows = runtime.management_schedules.list_schedules(sid)["schedules"]
    assert len(rows) == 1 and rows[0]["kind"] == "weekly" and rows[0]["weekday"] == 4


def test_policy_tool_rejects_unknown_clock_override_and_unreviewed_write(runtime):
    context = context_fixture.__wrapped__(runtime)
    tool = runtime.tool_registry.get_tool("management.configure_schedule")
    assert "command_id" in tool.spec.input_schema["required"]
    invocation = ToolInvocation(invocation_id="bad-clock", tool=tool.spec, session_id=context.session_id, context_id="context",
        input={"now": "2000-01-01T00:00:00Z"})
    with pytest.raises(ValueError, match="Unknown"):
        tool.invoke(invocation=invocation, context=context)
    invocation.input = {}
    context.safety_review_approved = False
    with pytest.raises(ValueError, match="safety review"):
        tool.invoke(invocation=invocation, context=context)
    assert runtime.management_schedules.list_schedules(context.session_id)["schedules"] == []
