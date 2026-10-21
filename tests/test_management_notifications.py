"""Temporary-SQLite acceptance for separate background processing and delivery."""
import json
from contextlib import closing

import pytest

from app.core.tools import ToolContext
from app.domains.management import ManagementConflictError
from app.experts.management import ManagementExecutionFenced
from app.tool_packages.management import management_notification_tools
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_coordinator import bind, timer
from tests.test_management_coordinator import finalize as finalize_with_notification


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def finalize(runtime, run):
    return finalize_with_notification(runtime, run, notify_user=None)


def background(runtime):
    sid, goal = timer(runtime)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    return sid, goal, run, ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)


def decide(runtime, context, notify_user=False, command_id="notification"):
    return runtime.management_coordinator.notifications.decide_notification(
        context, command_id=command_id, notify_user=notify_user, reason="No new user action is needed.",
    )


@pytest.mark.parametrize("notify_user", [False, True])
def test_explicit_decision_publishes_internal_state_and_optional_delivery_once(runtime, notify_user):
    sid, goal, run, context = background(runtime)
    receipt = decide(runtime, context, notify_user)
    assert receipt["status"] == "proposed"
    assert decide(runtime, context, notify_user) == receipt
    runtime.managed_tasks.set_goal(context, command_id="done", goal_id=goal["goal_id"],
                                   expected_revision=1, status="completed")
    assert finalize(runtime, run)
    assert finalize(runtime, run)
    completed = runtime.agent_run_manager.get_run(run.run_id)
    assert completed.result_snapshot["answer"] == "Checked."
    assert completed.result_snapshot["user_visible"] is notify_user
    assert completed.result_snapshot["notification"]["decision_source"] == "explicit"
    assert runtime.management_goals.get_goal(sid, goal["goal_id"])["status"] == "completed"
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] == receipt["execution_id"]
    assert len(runtime.management_goals.list_deliveries(sid)["deliveries"]) == int(notify_user)
    assert len(runtime.session_service.get_session(session_id=sid).messages) == int(notify_user)
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT applied_at FROM manager_notification_proposals").fetchone()[0]
        context_tail = conn.execute("SELECT role,content FROM agent_session_context_messages WHERE session_id=? ORDER BY seq", (sid,)).fetchall()
    assert [(r[0], r[1]) for r in context_tail] == [("tool", run.user_input), ("agent", "Checked.")]
    events = runtime.agent_run_manager.list_events(run.run_id)
    final_events = [e for e in events if e.type == "final_answer"]
    assert len(final_events) == 1
    assert final_events[0].payload["metadata"]["answer"] == "Checked."
    assert final_events[0].payload["metadata"]["user_visible"] is notify_user
    assert next(e for e in events if e.type == "run_completed").payload["user_visible"] is notify_user


@pytest.mark.parametrize("legacy", [False, True])
def test_missing_decision_is_silent_for_new_runs_and_compatible_for_old_runs(runtime, legacy):
    sid, _, run, _ = background(runtime)
    if legacy:
        with closing(runtime._conn()) as conn, conn:
            row = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (run.run_id,)).fetchone()
            record = json.loads(row[0])
            record["metadata"].pop("manager_notification_required")
            conn.execute("UPDATE agent_runs SET record_payload=? WHERE run_id=?", (json.dumps(record), run.run_id))
    projection = runtime.management_coordinator.notifications.for_run(run.run_id)
    assert projection["user_visible"] is legacy
    assert projection["notification"]["decision_source"] == (
        "legacy_default_notify" if legacy else "missing_decision_default_silent"
    )
    assert finalize(runtime, run)
    assert len(runtime.management_goals.list_deliveries(sid)["deliveries"]) == int(legacy)


def test_decision_is_immutable_and_uses_shared_command_idempotency(runtime):
    _, _, _, context = background(runtime)
    decide(runtime, context)
    with pytest.raises(ManagementConflictError, match="different arguments"):
        decide(runtime, context, True)
    with pytest.raises(ManagementConflictError, match="one notification"):
        decide(runtime, context, command_id="second")


def test_silent_publication_rollback_keeps_every_proposal_and_event_unapplied(runtime, monkeypatch):
    sid, goal, run, context = background(runtime)
    decide(runtime, context)
    runtime.managed_tasks.set_goal(context, command_id="done", goal_id=goal["goal_id"],
                                   expected_revision=1, status="completed")
    original = runtime.agent_run_manager.complete_run_in_transaction
    def fail(*args, **kwargs):
        raise OSError("Injected atomic completion failure")
    monkeypatch.setattr(runtime.agent_run_manager, "complete_run_in_transaction", fail)
    with pytest.raises(OSError):
        finalize(runtime, run)
    assert runtime.management_goals.get_goal(sid, goal["goal_id"])["status"] == "active"
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] is None
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT applied_at FROM manager_notification_proposals").fetchone()[0] is None
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_session_context_messages'").fetchone()
        if exists:
            assert conn.execute("SELECT count(*) FROM agent_session_context_messages").fetchone()[0] == 0
    monkeypatch.setattr(runtime.agent_run_manager, "complete_run_in_transaction", original)
    assert finalize(runtime, run)
    assert runtime.management_goals.list_deliveries(sid)["deliveries"] == []


@pytest.mark.parametrize("change", ["controller", "goal", "cancel", "lease", "source"])
def test_silent_decision_preserves_existing_publication_fences(runtime, monkeypatch, change):
    sid, goal, run, context = background(runtime)
    decide(runtime, context)
    if change == "controller":
        runtime.management_service.update(sid, expected_revision=2, status="paused")
    elif change == "goal":
        runtime.management_goals.update_goal(sid, goal["goal_id"], expected_revision=1, objective="Changed")
    elif change == "cancel":
        runtime.agent_run_manager.request_cancel(run.run_id, reason="User cancellation")
    elif change == "source":
        with closing(runtime._conn()) as conn, conn:
            conn.execute("DELETE FROM manager_inbox WHERE session_id=?", (sid,))
    else:
        def lose(*args, **kwargs):
            raise ManagementExecutionFenced("Lost dispatcher lease")
        monkeypatch.setattr(runtime.agent_dispatch, "check_run", lose)
    with pytest.raises(ManagementExecutionFenced):
        finalize(runtime, run)
    assert runtime.management_goals.list_deliveries(sid)["deliveries"] == []
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT applied_at FROM manager_notification_proposals").fetchone()[0] is None


def test_foreground_cannot_decide_and_safety_review_is_required(runtime):
    sid, _ = timer(runtime)
    run = runtime.create_agent_run(session_id=sid, user_input="Ordinary user question", resumable=True)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    assert runtime.management_coordinator.notifications.for_run(run.run_id) is None
    with pytest.raises(ManagementConflictError, match="background execution"):
        decide(runtime, context)
    context.safety_review_approved = False
    with pytest.raises(ValueError, match="review gate"):
        decide(runtime, context)
    tool = management_notification_tools(runtime.management_coordinator.notifications)[0]
    assert tool.spec.read_only is False
    assert tool.spec.effect_domains == ("agent_control",)


def test_decision_requires_exact_execution_run_session_binding(runtime):
    _, _, run, context = background(runtime)
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE manager_executions SET run_id='unrelated-run' WHERE run_id=?", (run.run_id,))
    assert runtime.management_coordinator.notifications.for_run(run.run_id) is None
    with pytest.raises(ManagementExecutionFenced):
        decide(runtime, context)


def test_lease_lost_after_context_projection_rolls_back_silent_commit(runtime, monkeypatch):
    sid, goal, run, context = background(runtime)
    decide(runtime, context)
    runtime.managed_tasks.set_goal(context, command_id="done", goal_id=goal["goal_id"],
                                   expected_revision=1, status="completed")
    original = runtime.session_service.record_context_exchange_in_transaction
    def lose_after_projection(*args, **kwargs):
        updated = original(*args, **kwargs)
        def lost(*args, **kwargs):
            raise ManagementExecutionFenced("Lease expired during context projection")
        monkeypatch.setattr(runtime.agent_dispatch, "check_run", lost)
        return updated
    monkeypatch.setattr(runtime.session_service, "record_context_exchange_in_transaction", lose_after_projection)
    with pytest.raises(ManagementExecutionFenced, match="during context"):
        finalize(runtime, run)
    assert runtime.management_goals.get_goal(sid, goal["goal_id"])["status"] == "active"
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] is None
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT applied_at FROM manager_notification_proposals").fetchone()[0] is None
        assert conn.execute("SELECT status FROM manager_executions").fetchone()[0] == "bound"
