"""Offline harness and publication coverage for queued work and watch delivery."""
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from app.core.agent_runs import AgentRunStatus
from app.core.safety import SafetyReviewMode
from app.core.tools import ToolContext, ToolInvocation
from app.domains.management import ManagementConflictError
from app.domains.watch import WatchInput, WatchPatch
from app.experts.management import ManagementExecutionFenced
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_coordinator import bind, finalize, timer
from tests.test_management_policies_integration import active_manager
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_management_watch_delivery import publish
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def watch_state(runtime, sid):
    watch = runtime.watch_service.create(WatchInput(title="Synthetic source", goal="Observe changes",
        timezone="UTC", daily_time="00:00", scope={"source_ids": ["synthetic-source"]}))
    return (runtime._conn, runtime.management_service, runtime.management_goals, sid, runtime.watch_service,
            watch["watch_id"], runtime.management_watch_subscriptions, runtime.management_delivery)


def test_harness_accepts_read_only_queue_then_dispatches_original_task(runtime):
    sid = active_manager(runtime)
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.queue_work", tool_input={
            "command_id": "queue-analysis", "purpose": "Compare evidence without changing files", "priority": "high"}),
        operation("final_answer", reason="Task accepted for queue"), "Work accepted.",
    ])
    result = runtime.run_agent_turn(session_id=sid, user_input="后台比较资料，不建立待办。", safety_review_mode=SafetyReviewMode.SKIP)
    event = next(e for e in result.tool_events if e.tool_name == "management.queue_work")
    assert event.result["status"] == "completed", event.model_dump()
    accepted = event.result["output"]
    task = runtime.agent_run_manager.get_run(accepted["run_id"])
    assert accepted["queue_state"] == "pending" and task.status.value == "queued"
    assert runtime.agent_run_manager.list_safety_reviews(result.run_id)
    policy = task.metadata["session_policy"]
    assert policy["allowed_tools"] and not policy["allow_fork"]
    assert all(runtime.tool_registry.get_tool(name).spec.read_only is True for name in policy["allowed_tools"])
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch'").fetchone()[0] == 0
    assert runtime.matter_service.list_matters(visibility="all").total_count == 0
    runtime.management_coordinator.tick()
    runtime.agent_turn_loop.llm_client = ScriptedClient([operation("final_answer", reason="Evidence compared"), "Bounded research result."])
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(task.run_id).status.value == "completed"
    assert runtime.get_session(session_id=task.session_id).messages[-1].content == "Bounded research result."
    runtime.management_queue.tick()
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT state FROM manager_work_queue WHERE run_id=?", (task.run_id,)).fetchone()[0] == "finished"


def test_pending_queue_and_its_child_cannot_bypass_guard_or_replace_policy(runtime):
    context = context_fixture.__wrapped__(runtime)
    receipt = runtime.management_queue.queue_work(context, command_id="guard", purpose="Read sources only")
    run_id = receipt["run_id"]
    with pytest.raises(ManagementConflictError, match="not been released"):
        runtime.management_coordinator.check_run(run_id)
    # Exercise ancestor checks even for a forged child record; a queued read-only
    # root cannot actually fork because its server-owned policy disables that.
    child = runtime.agent_run_manager.create_child_run(parent_run_id=run_id, plan_id="p", step_id="s", attempt=1, user_input="Child")
    with pytest.raises(ManagementConflictError, match="not been released"):
        runtime.management_coordinator.check_run(child.run_id)
    runtime.agent_run_manager._update_run(run_id, status=AgentRunStatus.PAUSED)
    before = runtime.agent_run_store.load_run(run_id)
    with pytest.raises(ValueError, match="cancelled and requeued"):
        runtime.replace_agent_run(run_id, user_input="Change to write", command_id="replace", request_fingerprint="f")
    assert runtime.agent_run_store.load_run(run_id) == before
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM agent_runs WHERE session_id=? AND run_id!=?",
                            (before["session_id"], run_id)).fetchone()[0] == 0


@pytest.mark.parametrize("snooze", [False, True])
def test_muted_capture_survives_and_delivers_once_after_release(runtime, snooze):
    sid = active_manager(runtime)
    state = watch_state(runtime, sid)
    runtime.management_watch_subscriptions.configure(sid, "watch", state[5], condition="changes")
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    runtime.management_delivery.configure(sid, 0, muted=not snooze, snooze_until=future if snooze else None)
    briefing = publish(state)
    assert runtime.management_coordinator.tick() == {"captured": 1, "scheduled": 0}
    event = runtime.management_goals.list_events(sid)["events"][0]
    assert event["consumed_execution_id"] is None
    assert "private body" not in json.dumps(event)
    publish(state, 1, changes=False)
    assert runtime.management_coordinator.tick() == {"captured": 0, "scheduled": 0}
    runtime.management_delivery.configure(sid, 1)
    assert runtime.management_coordinator.tick() == {"captured": 0, "scheduled": 1}
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id)
    tool = runtime.tool_registry.get_tool("management.read_notification")
    output = tool.invoke(context=context, invocation=ToolInvocation(invocation_id="read", tool=tool.spec,
        session_id=sid, context_id="ctx", input={"briefing_id": briefing["briefing_id"], "max_chars": 100})).output
    assert len(output["content"]) == 100 and output["next_char"] == 100
    assert finalize(runtime, run)
    assert len(runtime.management_goals.list_deliveries(sid)["deliveries"]) == 1
    assert runtime.management_coordinator.tick() == {"captured": 0, "scheduled": 0}


@pytest.mark.parametrize("change", ["delivery", "watch_scope"])
def test_changed_delivery_or_watch_policy_fences_publication(runtime, change):
    sid = active_manager(runtime)
    state = watch_state(runtime, sid)
    runtime.management_watch_subscriptions.configure(sid, "watch", state[5])
    publish(state)
    assert runtime.management_coordinator.tick()["scheduled"] == 1
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    if change == "delivery":
        runtime.management_delivery.configure(sid, 0, muted=True)
    else:
        runtime.watch_service.update(state[5], WatchPatch(scope={"source_ids": ["new-source"]}))
    with pytest.raises(ManagementExecutionFenced):
        finalize(runtime, run)
    assert not runtime.management_goals.list_deliveries(sid)["deliveries"]
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] is None


def test_background_policy_tools_keep_write_gate_and_reject_autonomous_changes(runtime):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=False)
    tool = runtime.tool_registry.get_tool("management.configure_delivery")
    invocation = ToolInvocation(invocation_id="policy", tool=tool.spec, session_id=sid, context_id="ctx",
                                input={"expected_revision": 0, "muted": True})
    with pytest.raises(ValueError, match="safety review"):
        tool.invoke(invocation=invocation, context=context)
    context.safety_review_approved = True
    with pytest.raises(ValueError, match="foreground"):
        tool.invoke(invocation=invocation, context=context)
    assert runtime.management_delivery.get(sid) is None
