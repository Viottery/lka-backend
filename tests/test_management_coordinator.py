"""Offline acceptance of durable management wake and fenced publication."""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from app.core.agent_runs import AgentRunStatus
from app.core.agent_turn import AgentTurnProgressEvent, AgentTurnResult
from app.core.llm_workloads import WorkBudgetExceeded, current_workload
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.session_admission import SessionBusyError
from app.core.tools import ToolContext
from app.domains.management import ManagementConflictError
from app.experts.management import ManagementExecutionFenced
from tests.test_management_api import runtime as runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def enable(runtime):
    config = runtime.local_app_config.management
    config.autonomous_enabled = True
    config.min_wake_interval_seconds = 0
    runtime.local_app_config.agent.checkpoint_backend = "sqlite"
    return config


def timer(runtime, *, active=True, objective="Check progress"):
    enable(runtime)
    sid = runtime.management_service.create()["session"]["session_id"]
    if active:
        runtime.management_service.update(sid, expected_revision=1, status="active")
    goal = runtime.management_goals.create_goal(sid, objective=objective, next_wake_at=datetime.now(UTC).isoformat())
    return sid, goal


def bind(runtime):
    assert runtime.management_coordinator.run_one()
    with closing(runtime._conn()) as conn:
        row = conn.execute("SELECT run_id FROM manager_executions ORDER BY rowid DESC LIMIT 1").fetchone()
    return runtime.agent_run_manager.get_run(row[0])


def finalize(runtime, run):
    result = AgentTurnResult(run_id=run.run_id, session_id=run.session_id, trace_id=run.trace_id, answer="Checked.",
                             progress_events=[AgentTurnProgressEvent(
                                 event_index=1, created_at=datetime.now(UTC).isoformat(), type="final_answer",
                                 stage="answer", status="completed", message="Checked.", metadata={"answer": "Checked."},
                             )])
    return runtime.management_coordinator.finalize(
        run.run_id, result, {"answer": result.answer}, user_input=run.user_input, input_role="tool",
        message_payload={"run_id": run.run_id}, message_id="test_answer_" + run.run_id,
        context_effect_id="test_context_" + run.run_id, token_budget=1000, background_enqueue=None,
    )


def test_capture_while_paused_and_explicit_autonomy_opt_in(runtime):
    sid, _ = timer(runtime, active=False)
    assert runtime.management_coordinator.tick() == {"captured": 1, "scheduled": 0}
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] is None
    runtime.management_service.update(sid, expected_revision=1, status="active")
    runtime.local_app_config.management.autonomous_enabled = False
    assert runtime.management_coordinator.tick()["scheduled"] == 0
    runtime.local_app_config.management.autonomous_enabled = True
    assert runtime.management_coordinator.tick()["scheduled"] == 1
    assert runtime.management_coordinator.tick()["scheduled"] == 0
    run = bind(runtime)
    assert run.status == AgentRunStatus.QUEUED
    assert run.metadata["input_envelope"]["origin"] == "background"
    assert runtime.management_coordinator.tick()["scheduled"] == 0


def test_foreground_wins_admission_without_losing_inbox(runtime):
    sid, _ = timer(runtime)
    assert runtime.management_coordinator.tick()["scheduled"] == 1
    foreground = runtime.create_agent_run(session_id=sid, user_input="User question", resumable=True)
    assert runtime.management_coordinator.run_one()
    assert runtime.management_goals.list_executions(sid)["executions"] == []
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] is None
    runtime.agent_run_manager.mark_cancelled(foreground.run_id)
    assert runtime.management_coordinator.tick()["scheduled"] == 1
    run = bind(runtime)
    with pytest.raises(SessionBusyError):
        runtime.create_agent_run(session_id=sid, user_input="Cannot open a second writer", resumable=True)
    assert run.session_id == sid


def test_concurrent_ticks_bind_one_root_and_small_queue_payload(runtime):
    sid, _ = timer(runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.management_coordinator.tick(), range(2)))
    assert sum(r["scheduled"] for r in results) == 1
    run = bind(runtime)
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM manager_executions").fetchone()[0] == 1
        payloads = [r[0] for r in conn.execute("SELECT payload_json FROM background_jobs")]
    assert all(len(p.encode()) < 4096 and "Check progress" not in p for p in payloads)
    assert runtime.management_goals.list_executions(sid)["executions"][0]["run_id"] == run.run_id


def test_bound_before_restart_executes_original_and_publishes_once(runtime):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    original = bind(runtime)
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        client = ScriptedClient([operation("final_answer", reason="Report timer check"), "Checked after restart."])
        reopened.agent_turn_loop.llm_client = client
        assert reopened.agent_dispatch.run_one()
        run = reopened.agent_run_manager.get_run(original.run_id)
        assert run.status.value == "completed", (run.error_type, run.error)
        events = reopened.management_goals.list_events(sid)["events"]
        execution = reopened.management_goals.list_executions(sid)["executions"][0]
        assert execution["status"] == "completed"
        assert events[0]["consumed_execution_id"] == execution["execution_id"]
        deliveries = reopened.management_goals.list_deliveries(sid)["deliveries"]
        assert len(deliveries) == 1 and deliveries[0]["run_id"] == original.run_id
        types = [e.type for e in reopened.agent_run_manager.list_events(original.run_id)]
        assert types.count("final_answer") == 1
        assert types.index("final_answer") < types.index("run_completed")
        messages = reopened.session_service.get_session(session_id=sid).messages
        assert [m.role for m in messages] == ["tool", "agent"]
        assert messages[-1].message_id == deliveries[0]["message_id"]
        before = len(client.calls)
        assert reopened.resume_agent_run(run.run_id).answer == "Checked after restart."
        assert len(client.calls) == before
        assert reopened.management_coordinator.tick()["scheduled"] == 0
    finally:
        reopened.stop()


@pytest.mark.parametrize("change", ["pause", "goal_revision", "cancel", "lease"])
def test_stale_execution_cannot_publish_or_consume(runtime, monkeypatch, change):
    sid, goal = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    if change == "pause":
        runtime.management_service.update(sid, expected_revision=2, status="paused")
    elif change == "goal_revision":
        runtime.management_goals.update_goal(sid, goal["goal_id"], expected_revision=1, objective="New instruction")
    elif change == "cancel":
        runtime.agent_run_manager.request_cancel(run.run_id, reason="User cancellation")
    else:
        def lose(*args, **kwargs):
            raise ManagementExecutionFenced("Lost dispatch ownership")
        monkeypatch.setattr(runtime.agent_dispatch, "check_run", lose)
    with pytest.raises(ManagementExecutionFenced):
        finalize(runtime, run)
    assert runtime.management_goals.list_deliveries(sid)["deliveries"] == []
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] is None
    assert runtime.session_service.get_session(session_id=sid).messages == []


def test_goal_proposal_and_answer_rollback_together_then_commit_once(runtime, monkeypatch):
    sid, goal = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    receipt = runtime.managed_tasks.set_goal(context, command_id="goal-done", goal_id=goal["goal_id"],
                                             expected_revision=1, status="completed")
    assert receipt["status"] == "proposed"
    assert runtime.management_goals.get_goal(sid, goal["goal_id"])["status"] == "active"
    original_complete = runtime.agent_run_manager.complete_run_in_transaction
    def fail(*args, **kwargs):
        raise OSError("Injected final commit failure")
    monkeypatch.setattr(runtime.agent_run_manager, "complete_run_in_transaction", fail)
    with pytest.raises(OSError):
        finalize(runtime, run)
    assert runtime.management_goals.get_goal(sid, goal["goal_id"])["status"] == "active"
    assert runtime.management_goals.list_deliveries(sid)["deliveries"] == []
    assert runtime.session_service.get_session(session_id=sid).messages == []
    monkeypatch.setattr(runtime.agent_run_manager, "complete_run_in_transaction", original_complete)
    assert finalize(runtime, run)
    assert finalize(runtime, run)
    assert runtime.management_goals.get_goal(sid, goal["goal_id"])["status"] == "completed"
    assert len(runtime.management_goals.list_deliveries(sid)["deliveries"]) == 1
    assert len(runtime.session_service.get_session(session_id=sid).messages) == 1
    assert runtime.agent_run_manager.get_run(run.run_id).status.value == "completed"


def test_real_harness_stages_goal_tool_and_publishes(runtime):
    sid, goal = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.set_goal", tool_input={
            "command_id": "done", "goal_id": goal["goal_id"], "expected_revision": 1, "status": "completed"}),
        operation("final_answer", reason="One-shot check is complete"), "The check is complete.",
    ])
    runtime.agent_turn_loop.llm_client = client
    assert runtime.agent_dispatch.run_one()
    current = runtime.agent_run_manager.get_run(run.run_id)
    assert current.status.value == "completed", (current.error_type, current.error)
    assert runtime.management_goals.get_goal(sid, goal["goal_id"])["status"] == "completed"
    assert len(runtime.management_goals.list_deliveries(sid)["deliveries"]) == 1


def test_interactive_goal_command_idempotency_and_long_objective_paging(runtime):
    sid, _ = timer(runtime)
    run = runtime.create_agent_run(session_id=sid, user_input="Remember a goal", resumable=True)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    receipt = runtime.managed_tasks.set_goal(context, command_id="remember", objective="A" * 5000)
    assert receipt["status"] == "applied"
    assert runtime.managed_tasks.set_goal(context, command_id="remember", objective="A" * 5000) == receipt
    preview = runtime.managed_tasks.list_goals(context)["goals"][-1]
    assert len(preview["objective"]) == 500 and preview["next_char"] == 500
    rest = runtime.managed_tasks.read_goal(context, goal_id=preview["goal_id"], start_char=500)
    assert len(rest["objective"]) == 4000 and rest["next_char"] == 4500


def test_spawned_tasks_inherit_persistent_quota_and_causal_chain(runtime):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    receipt = runtime.managed_tasks.start_task(context, command_id="child-task", purpose="Follow up")
    task = runtime.agent_run_manager.get_run(receipt["run_id"])
    assert task.metadata["work_budget_scopes"] == run.metadata["work_budget_scopes"]
    assert task.metadata["manager_cause_paths"] == run.metadata["manager_cause_paths"]
    with runtime._management_workload_for_run(task.run_id):
        scope = current_workload()
        assert scope.pool == "background_agent"
        assert {q.scope_id for q in scope.quotas} == {s["scope_id"] for s in run.metadata["work_budget_scopes"]}
    finalize(runtime, run)
    runtime.agent_run_manager.mark_cancelled(task.run_id)
    runtime.local_app_config.management.max_causal_rounds = 1
    assert runtime.management_coordinator.tick()["scheduled"] == 0
    assert runtime.management_goals.list_events(sid)["events"][-1]["consumed_execution_id"] is None


def test_daily_and_failure_limits_persist_across_reopen(runtime):
    config = enable(runtime)
    config.failure_limit = 2
    _sid, _ = timer(runtime)
    for _ in range(2):
        assert runtime.management_coordinator.tick()["scheduled"] == 1
        run = bind(runtime)
        runtime.agent_run_manager.fail_run(run.run_id, error_type="provider_failure", error="Unavailable")
    assert runtime.management_coordinator.tick()["scheduled"] == 0
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        assert reopened.management_coordinator.tick()["scheduled"] == 0
        config.failure_limit = 3
        config.max_daily_rounds = 2
        assert reopened.management_coordinator.tick()["scheduled"] == 0
    finally:
        reopened.stop()


def test_new_timer_generation_does_not_lose_event_during_active_round(runtime):
    sid, goal = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    finalize(runtime, run)
    runtime.management_goals.update_goal(sid, goal["goal_id"], expected_revision=1,
                                         next_wake_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    assert runtime.management_coordinator.tick() == {"captured": 1, "scheduled": 1}
    assert bind(runtime).run_id != run.run_id
    events = runtime.management_goals.list_events(sid)["events"]
    assert len(events) == 2 and events[1]["consumed_execution_id"] is None


def test_autonomous_timer_proposal_inherits_causal_depth(runtime):
    sid, goal = timer(runtime)
    runtime.local_app_config.management.max_causal_rounds = 1
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    runtime.managed_tasks.set_goal(context, command_id="again", goal_id=goal["goal_id"], expected_revision=1,
                                    next_wake_at=datetime.now(UTC).isoformat())
    finalize(runtime, run)
    assert runtime.management_coordinator.tick() == {"captured": 1, "scheduled": 0}
    # An explicit user edit starts a new causal root while preserving quota use.
    runtime.management_goals.update_goal(sid, goal["goal_id"], expected_revision=2, next_wake_at=datetime.now(UTC).isoformat())
    assert runtime.management_coordinator.tick() == {"captured": 1, "scheduled": 1}


def test_causal_limited_prefix_does_not_starve_new_timer(runtime):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    tasks = [runtime.managed_tasks.start_task(context, command_id=f"task-{n}", purpose="Check") for n in range(16)]
    finalize(runtime, run)
    for receipt in tasks:
        runtime.agent_run_manager.mark_cancelled(receipt["run_id"])
    runtime.local_app_config.management.max_causal_rounds = 1
    assert runtime.management_coordinator.tick()["scheduled"] == 0
    new_goal = runtime.management_goals.create_goal(sid, objective="Fresh explicit goal", next_wake_at=datetime.now(UTC).isoformat())
    assert runtime.management_coordinator.tick()["scheduled"] == 1
    next_run = bind(runtime)
    execution = runtime.management_goals.list_executions(sid)["executions"][-1]
    assert execution["run_id"] == next_run.run_id
    assert new_goal["goal_id"] in execution["goal_revisions"]
    assert len(execution["event_ids"]) == 1


def test_paused_replacement_preserves_budget_and_resume_cannot_bypass(runtime):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    caller = bind(runtime)
    runtime.agent_run_manager.mark_running(caller.run_id)
    context = ToolContext(session_id=sid, run_id=caller.run_id, safety_review_approved=True)
    task_sid = runtime.session_service.create_session().session.session_id
    old = runtime.create_agent_run(session_id=task_sid, user_input="Original", resumable=True)
    runtime.agent_run_manager._update_run(old.run_id, status=AgentRunStatus.PAUSED)
    with pytest.raises(ManagementConflictError, match="matching existing budgets"):
        runtime.managed_tasks.control_run(context, command_id="resume-unsafe", run_id=old.run_id, action="resume")
    receipt = runtime.managed_tasks.send_instruction(context, command_id="replace-safe", session_id=task_sid,
                                                    expected_run_id=old.run_id, purpose="Continue with attached scope")
    new = runtime.agent_run_manager.get_run(receipt["run_id"])
    assert new.metadata["work_budget_scopes"] == caller.metadata["work_budget_scopes"]
    assert new.metadata["manager_cause_paths"] == caller.metadata["manager_cause_paths"]
    runtime.agent_run_manager._update_run(new.run_id, status=AgentRunStatus.PAUSED)
    replaced = runtime.replace_agent_run(new.run_id, user_input="User replacement", command_id="user-replace", request_fingerprint="fp")
    assert replaced.metadata["work_budget_scopes"] == new.metadata["work_budget_scopes"]


def test_quota_usage_survives_reopen_and_is_shared_with_independent_task(runtime):
    enable(runtime).execution_max_calls = 1
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    receipt = runtime.managed_tasks.start_task(context, command_id="bounded-task", purpose="Follow up")
    async def spend():
        with runtime._management_workload_for_run(run.run_id):
            async with runtime.llm_workloads.admit(input_tokens=10, output_tokens=1) as ticket:
                ticket.dispatched = True
                ticket.usage = {"input_tokens": 10, "output_tokens": 1}
    asyncio.run(spend())
    execution_scope = next(s["scope_id"] for s in run.metadata["work_budget_scopes"] if s["scope_id"].startswith("manager_execution:"))
    assert runtime.llm_workloads.quota_usage(execution_scope)["total_calls"] == 1
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        async def denied():
            with reopened._management_workload_for_run(receipt["run_id"]), pytest.raises(WorkBudgetExceeded):
                async with reopened.llm_workloads.admit(input_tokens=1, output_tokens=1):
                    raise AssertionError("must not dispatch")
        asyncio.run(denied())
        assert reopened.llm_workloads.quota_usage(execution_scope)["total_calls"] == 1
    finally:
        reopened.stop()


def test_wake_lease_is_checked_after_waiting_for_write_transaction(runtime, monkeypatch):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    job = runtime.background_job_store.claim("expiring", .02, kinds=("manager_wake",))
    factory = runtime.management_coordinator._conn
    class SlowTransaction:
        def __init__(self):
            self.conn = factory()
        def execute(self, statement, *args):
            result = self.conn.execute(statement, *args)
            if statement == "BEGIN IMMEDIATE":
                time.sleep(.04)
            return result
        def __getattr__(self, key):
            return getattr(self.conn, key)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.conn.__exit__(*args)
    monkeypatch.setattr(runtime.management_coordinator, "_conn", SlowTransaction)
    with pytest.raises(ManagementExecutionFenced, match="lease_lost"):
        runtime.management_coordinator._bind(job)
    assert runtime.management_goals.list_executions(sid)["executions"] == []


def test_binding_failure_does_not_leave_cached_run_or_partial_execution(runtime, monkeypatch):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    cached = set(runtime.agent_run_manager._runs)
    def fail(*args, **kwargs):
        raise OSError("Injected dispatch enqueue failure")
    monkeypatch.setattr(runtime.agent_dispatch, "enqueue", fail)
    assert runtime.management_coordinator.run_one()
    assert runtime.management_goals.list_executions(sid)["executions"] == []
    assert set(runtime.agent_run_manager._runs) == cached
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] is None


@pytest.mark.parametrize("error_type", ["work_budget_exhausted", "execution_uncertain"])
def test_exhausted_or_uncertain_work_does_not_get_fresh_round_automatically(runtime, error_type):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.fail_run(run.run_id, error_type=error_type, error="Needs explicit attention")
    assert runtime.management_coordinator.tick()["scheduled"] == 0
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] is None


def test_harness_control_fence_does_not_emit_formal_final_answer(runtime):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Prepare a report"), "This must not be published.",
    ])
    finalize_handler = runtime.agent_turn_loop.finalization_handler
    def pause_before_commit(*args, **kwargs):
        runtime.management_service.update(sid, expected_revision=2, status="paused")
        return finalize_handler(*args, **kwargs)
    runtime.agent_turn_loop.finalization_handler = pause_before_commit
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(run.run_id).status.value == "cancelled"
    types = [e.type for e in runtime.agent_run_manager.list_events(run.run_id)]
    assert "final_answer" not in types and "run_completed" not in types
    assert runtime.management_goals.list_deliveries(sid)["deliveries"] == []
    assert all(m.role != "agent" for m in runtime.session_service.get_session(session_id=sid).messages)


def test_task_terminal_wakes_manager_to_read_result_without_user_poll(runtime):
    enable(runtime)
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    foreground = runtime.create_agent_run(session_id=sid, user_input="Start a task and report when done", resumable=True)
    runtime.agent_run_manager.mark_running(foreground.run_id)
    receipt = runtime.managed_tasks.start_task(
        ToolContext(session_id=sid, run_id=foreground.run_id, safety_review_approved=True),
        command_id="terminal-report", purpose="Calculate the synthetic result",
    )
    runtime.agent_run_manager.complete_run(foreground.run_id)
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Synthetic task is complete"), "SYNTHETIC_RESULT_713",
    ])
    assert runtime.agent_dispatch.run_one()
    assert runtime.agent_run_manager.get_run(receipt["run_id"]).status.value == "completed"
    assert runtime.management_coordinator.tick() == {"captured": 1, "scheduled": 1}
    manager = bind(runtime)
    client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.read_session", tool_input={"session_id": receipt["session_id"]}),
        operation("final_answer", reason="The task result was read"), "Task result: SYNTHETIC_RESULT_713",
    ])
    runtime.agent_turn_loop.llm_client = client
    assert runtime.agent_dispatch.run_one()
    current = runtime.agent_run_manager.get_run(manager.run_id)
    assert current.status.value == "completed", (current.error_type, current.error)
    assert "SYNTHETIC_RESULT_713" in client.calls[-1]["user_prompt"]
    delivery = runtime.management_goals.list_deliveries(sid)["deliveries"][0]
    assert delivery["run_id"] == manager.run_id
    assert runtime.management_coordinator.tick() == {"captured": 0, "scheduled": 0}


def test_wake_lease_expiring_during_binding_rolls_back_dispatch(runtime, monkeypatch):
    sid, _ = timer(runtime)
    runtime.management_coordinator.tick()
    job = runtime.background_job_store.claim("expiring-at-commit", .02, kinds=("manager_wake",))
    enqueue = runtime.agent_dispatch.enqueue
    def enqueue_then_delay(*args, **kwargs):
        result = enqueue(*args, **kwargs)
        time.sleep(.04)
        return result
    monkeypatch.setattr(runtime.agent_dispatch, "enqueue", enqueue_then_delay)
    with pytest.raises(ManagementExecutionFenced, match="lease_lost"):
        runtime.management_coordinator._bind(job)
    assert runtime.management_goals.list_executions(sid)["executions"] == []
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch'").fetchone()[0] == 0


def test_unpublished_proposal_cannot_look_like_current_execution_proposal(runtime):
    sid, goal = timer(runtime)
    runtime.management_coordinator.tick()
    first = bind(runtime)
    runtime.agent_run_manager.mark_running(first.run_id)
    context = ToolContext(session_id=sid, run_id=first.run_id, safety_review_approved=True)
    args = {"goal_id": goal["goal_id"], "expected_revision": 1, "status": "completed"}
    original = runtime.managed_tasks.set_goal(context, command_id="proposal", **args)
    assert original["status"] == "proposed"
    runtime.agent_run_manager.fail_run(first.run_id, error_type="provider_failure", error="Interrupted")
    # A user may open the next root before the scanner reconciles the old
    # execution row. Its persisted run status must already fence stale receipts.
    foreground = runtime.create_agent_run(session_id=sid, user_input="Check old proposal", resumable=True)
    runtime.agent_run_manager.mark_running(foreground.run_id)
    with pytest.raises(ManagementConflictError, match="did not publish"):
        runtime.managed_tasks.set_goal(ToolContext(session_id=sid, run_id=foreground.run_id,
                                                   safety_review_approved=True), command_id="proposal", **args)
    runtime.agent_run_manager.mark_cancelled(foreground.run_id)
    assert runtime.management_coordinator.tick()["scheduled"] == 1
    second = bind(runtime)
    runtime.agent_run_manager.mark_running(second.run_id)
    new_context = ToolContext(session_id=sid, run_id=second.run_id, safety_review_approved=True)
    with pytest.raises(ManagementConflictError, match="did not publish"):
        runtime.managed_tasks.set_goal(new_context, command_id="proposal", **args)
    assert runtime.management_goals.get_goal(sid, goal["goal_id"])["status"] == "active"
    runtime.managed_tasks.set_goal(new_context, command_id="reconsidered-proposal", **args)
    finalize(runtime, second)
    assert runtime.management_goals.get_goal(sid, goal["goal_id"])["status"] == "completed"
