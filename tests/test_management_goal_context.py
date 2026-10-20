"""Background manager turns carry only causally related goal references."""

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from app.core.tools import ToolContext
from app.experts.management import ManagementExecutionFenced
from tests.test_management_api import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def manager_with_completed_task(runtime):
    config = runtime.local_app_config.management
    config.autonomous_enabled = True
    config.min_wake_interval_seconds = 0
    runtime.local_app_config.agent.checkpoint_backend = "sqlite"
    session_id = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(session_id, expected_revision=1, status="active")
    foreground = runtime.create_agent_run(
        session_id=session_id, user_input="Start the requested task", resumable=True,
    )
    runtime.agent_run_manager.mark_running(foreground.run_id)
    receipt = runtime.managed_tasks.start_task(
        ToolContext(session_id=session_id, run_id=foreground.run_id, safety_review_approved=True),
        command_id="goal-context-task", purpose="Complete the requested task",
    )
    runtime.agent_run_manager.complete_run(foreground.run_id)
    runtime.agent_run_manager.complete_run(receipt["run_id"])
    return session_id, receipt


def bind_wake(runtime, session_id):
    assert runtime.management_coordinator.tick()["scheduled"] == 1
    assert runtime.management_coordinator.run_one()
    with closing(runtime._conn()) as conn:
        row = conn.execute(
            "SELECT run_id FROM manager_executions WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    assert row is not None
    return runtime.agent_run_manager.get_run(row["run_id"])


def related_ids(run):
    purpose = run.metadata["input_envelope"]["purpose"]
    payload = json.loads(purpose[purpose.index("{"):])
    return purpose, {goal["goal_id"] for goal in payload["goals"]}


def test_generic_task_terminal_does_not_inject_unrelated_active_goals(runtime):
    session_id, _ = manager_with_completed_task(runtime)
    unrelated = runtime.management_goals.create_goal(
        session_id, objective="UNRELATED_DAILY_EMAIL_OBJECTIVE_" + "x" * 900,
    )

    run = bind_wake(runtime, session_id)
    purpose, goal_ids = related_ids(run)
    goal_scope_ids = {scope["scope_id"] for scope in run.metadata["work_budget_scopes"]}
    assert goal_ids == set()
    assert "manager_goal:" + unrelated["goal_id"] not in goal_scope_ids
    assert all(not scope_id.startswith("manager_goal:") for scope_id in goal_scope_ids)
    assert "UNRELATED_DAILY_EMAIL_OBJECTIVE" not in purpose
    execution = runtime.management_goals.list_executions(session_id)["executions"][-1]
    assert execution["goal_revisions"] == {}


def test_task_wait_and_schedule_events_include_only_their_goal_revisions(runtime):
    session_id, receipt = manager_with_completed_task(runtime)
    waiting = runtime.management_goals.create_goal(
        session_id, objective="WAITING_GOAL_OBJECTIVE_" + "w" * 800,
        wait_run_id=receipt["run_id"],
    )
    scheduled = runtime.management_goals.create_goal(
        session_id, objective="SCHEDULE_GOAL_OBJECTIVE_" + "s" * 800,
    )
    unrelated = runtime.management_goals.create_goal(
        session_id, objective="UNRELATED_GOAL_OBJECTIVE_" + "u" * 800,
    )
    now = datetime.now(UTC)
    historical = now - timedelta(days=1)
    runtime.management_schedules.configure(
        session_id, "goal-context-schedule", now=historical.isoformat(),
        goal_id=scheduled["goal_id"], timezone="UTC", kind="one_shot",
        run_at=(historical + timedelta(minutes=1)).isoformat(),
    )

    run = bind_wake(runtime, session_id)
    purpose, goal_ids = related_ids(run)
    assert goal_ids == {waiting["goal_id"], scheduled["goal_id"]}
    assert unrelated["goal_id"] not in goal_ids
    assert "WAITING_GOAL_OBJECTIVE" not in purpose
    assert "SCHEDULE_GOAL_OBJECTIVE" not in purpose
    assert "UNRELATED_GOAL_OBJECTIVE" not in purpose
    execution = runtime.management_goals.list_executions(session_id)["executions"][-1]
    assert execution["goal_revisions"] == {
        waiting["goal_id"]: waiting["revision"],
        scheduled["goal_id"]: scheduled["revision"],
    }
    assert {scope["scope_id"] for scope in run.metadata["work_budget_scopes"]
            if scope["scope_id"].startswith("manager_goal:")} == {
        "manager_goal:" + waiting["goal_id"], "manager_goal:" + scheduled["goal_id"],
    }

    runtime.management_goals.update_goal(
        session_id, unrelated["goal_id"], expected_revision=unrelated["revision"],
        objective="Changed unrelated goal",
    )
    runtime.management_coordinator.check_run(run.run_id)
    runtime.management_goals.update_goal(
        session_id, waiting["goal_id"], expected_revision=waiting["revision"],
        objective="Changed related goal",
    )
    with pytest.raises(ManagementExecutionFenced, match="manager_goal_changed"):
        runtime.management_coordinator.check_run(run.run_id)


def test_payload_goal_reference_is_scoped_and_revision_fenced(runtime):
    session_id = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(session_id, expected_revision=1, status="active")
    related = runtime.management_goals.create_goal(session_id, objective="Referenced goal")
    unrelated = runtime.management_goals.create_goal(session_id, objective="Another active goal")

    with closing(runtime._conn()) as conn:
        conn.execute("BEGIN")
        goals, revisions = runtime.management_coordinator._related_goals(conn, session_id, [{
            "goal_id": None,
            "goal_revision": None,
            "kind": "task_terminal",
            "payload": {"goal_id": related["goal_id"], "goal_revision": related["revision"]},
        }])
    assert goals == [{"goal_id": related["goal_id"], "revision": related["revision"]}]
    assert revisions == {related["goal_id"]: related["revision"]}
    assert unrelated["goal_id"] not in revisions


def test_stale_payload_goal_reference_is_obsoleted_before_binding(runtime):
    runtime.local_app_config.management.autonomous_enabled = True
    session_id = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(session_id, expected_revision=1, status="active")
    goal = runtime.management_goals.create_goal(session_id, objective="Original referenced goal")
    runtime.management_goals.update_goal(
        session_id, goal["goal_id"], expected_revision=goal["revision"],
        objective="Revised referenced goal",
    )
    with closing(runtime._conn()) as conn, conn:
        runtime.management_goals._event(
            conn, session_id, "stale-payload-goal", "timer",
            {"goal_id": goal["goal_id"], "goal_revision": goal["revision"]},
            datetime.now(UTC).isoformat(),
        )
    with closing(runtime._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        assert runtime.management_coordinator._pending(conn, session_id) == []
    event = runtime.management_goals.list_events(session_id)["events"][0]
    assert event["consumed_execution_id"] == "obsolete"
