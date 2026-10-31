"""Management retries only database admissions proven to precede Agent execution."""
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_management_api import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def active_timer(runtime):
    config = runtime.local_app_config.management
    config.autonomous_enabled = True
    config.min_wake_interval_seconds = 0
    config.failure_limit = 3
    runtime.local_app_config.agent.checkpoint_backend = "sqlite"
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    runtime.management_goals.create_goal(
        sid, objective="Check pending work", next_wake_at=datetime.now(UTC).isoformat(),
    )
    return sid


def bind(runtime, sid):
    assert runtime.management_coordinator.tick()["scheduled"] == 1
    assert runtime.management_coordinator.run_one()
    with closing(runtime._conn()) as conn:
        row = conn.execute(
            "SELECT run_id FROM manager_executions WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (sid,),
        ).fetchone()
    return runtime.agent_run_manager.get_run(row[0])


def fail_storage(run, runtime, error_type, error):
    runtime.agent_run_manager.append_event(
        run.run_id, "run_failed", "Storage admission failed before execution.", stage="run",
        payload={"error_type": error_type},
    )
    runtime.agent_run_manager.fail_run(run.run_id, error_type=error_type, error=error)
    runtime.management_coordinator.reconcile()


@pytest.mark.parametrize(("error_type", "error"), [
    ("SqliteWriteAdmissionError", "SQLite write queue wait timed out"),
    ("SqliteWriteAdmissionError", "SQLite write queue is full"),
    ("SqliteWriteQueueError", "SQLite write queue wait timed out"),
    ("MySQLPoolTimeout", "MySQL connection pool wait timed out"),
])
def test_exact_unstarted_storage_admission_gets_bounded_retry(runtime, error_type, error):
    sid = active_timer(runtime)
    run = bind(runtime, sid)
    fail_storage(run, runtime, error_type, error)

    status = runtime.management_coordinator.wake_status(sid)
    assert status["reason"] == "storage_retry_wait"
    assert status["blocked_reason"] == "storage_retry_wait"
    assert datetime.fromisoformat(status["retry_at"]) > datetime.now(UTC)
    with closing(runtime._conn()) as conn:
        assert conn.execute(
            "SELECT consumed_execution_id FROM manager_inbox WHERE session_id=?", (sid,),
        ).fetchone()[0] is None


def test_event_scheduler_uses_storage_retry_deadline(runtime):
    sid = active_timer(runtime)
    run = bind(runtime, sid)
    fail_storage(run, runtime, "SqliteWriteAdmissionError", "SQLite write queue wait timed out")
    status = runtime.management_coordinator.wake_status(sid)
    now = datetime.now(UTC)
    with closing(runtime._conn()) as conn:
        due = runtime.management_coordinator._future_management_due(conn, now)
    assert due is not None and due > now
    assert due <= datetime.fromisoformat(status["retry_at"])


@pytest.mark.parametrize(("error_type", "error"), [
    ("SqliteWriteAdmissionError", "SQLite busy after the loop started"),
    ("SqliteWriteQueueError", "nested SQLite write on another connection would deadlock"),
    ("MySQLPoolTimeout", "different timeout text"),
])
def test_unknown_database_failure_fails_closed(runtime, error_type, error):
    sid = active_timer(runtime)
    run = bind(runtime, sid)
    fail_storage(run, runtime, error_type, error)
    assert runtime.management_coordinator.wake_status(sid)["reason"] == "storage_recovery_required"


def test_started_run_or_tool_effect_is_never_replayed(runtime):
    sid = active_timer(runtime)
    run = bind(runtime, sid)
    runtime.agent_run_manager.mark_running(run.run_id)
    runtime.agent_run_store.claim_tool_invocation(
        invocation_id="late-storage-timeout", run_id=run.run_id, tool_name="test.write",
        tool_input={"value": "already attempted"}, claimed_at=datetime.now(UTC).isoformat(),
    )
    runtime.agent_run_manager.fail_run(
        run.run_id, error_type="MySQLPoolTimeout", error="MySQL connection pool wait timed out",
    )
    runtime.management_coordinator.reconcile()
    assert runtime.management_coordinator.wake_status(sid)["reason"] == "storage_recovery_required"
    assert runtime.management_coordinator.tick()["scheduled"] == 0


@pytest.mark.parametrize("error_type", ["work_budget_exhausted", "execution_uncertain"])
def test_budget_and_uncertain_guards_keep_precedence(runtime, error_type):
    sid = active_timer(runtime)
    run = bind(runtime, sid)
    runtime.agent_run_manager.fail_run(run.run_id, error_type=error_type, error="Needs explicit attention")
    runtime.management_coordinator.reconcile()
    assert runtime.management_coordinator.wake_status(sid)["reason"] == "execution_guard"


def test_ordinary_failure_limit_behavior_is_unchanged(runtime):
    sid = active_timer(runtime)
    run = bind(runtime, sid)
    runtime.agent_run_manager.fail_run(run.run_id, error_type="provider_failure", error="Unavailable")
    runtime.management_coordinator.reconcile()
    assert runtime.management_coordinator.wake_status(sid)["reason"] == "ready"


def test_controller_revision_change_explicitly_releases_storage_guard(runtime):
    sid = active_timer(runtime)
    run = bind(runtime, sid)
    runtime.agent_run_manager.mark_running(run.run_id)
    runtime.agent_run_manager.fail_run(
        run.run_id, error_type="MySQLPoolTimeout", error="MySQL connection pool wait timed out",
    )
    runtime.management_coordinator.reconcile()
    assert runtime.management_coordinator.wake_status(sid)["reason"] == "storage_recovery_required"

    with closing(runtime._conn()) as conn:
        revision = conn.execute(
            "SELECT revision FROM manager_controllers WHERE session_id=?", (sid,),
        ).fetchone()[0]
    runtime.management_service.update(sid, expected_revision=revision, status="paused")
    with closing(runtime._conn()) as conn:
        revision = conn.execute(
            "SELECT revision FROM manager_controllers WHERE session_id=?", (sid,),
        ).fetchone()[0]
    runtime.management_service.update(sid, expected_revision=revision, status="active")
    assert runtime.management_coordinator.tick()["scheduled"] == 1


def test_database_retry_backoff_is_five_ten_twenty_then_failure_limit(runtime):
    sid = active_timer(runtime)
    runtime.local_app_config.management.failure_limit = 4
    expected_delays = (5, 10, 20)
    for delay in expected_delays:
        run = bind(runtime, sid)
        fail_storage(run, runtime, "SqliteWriteAdmissionError", "SQLite write queue wait timed out")
        with closing(runtime._conn()) as conn:
            row = conn.execute(
                "SELECT updated_at FROM manager_executions WHERE run_id=?", (run.run_id,),
            ).fetchone()
            failed_at = datetime.fromisoformat(row[0])
        status = runtime.management_coordinator.wake_status(sid)
        retry_at = datetime.fromisoformat(status["retry_at"])
        assert (retry_at - failed_at).total_seconds() == pytest.approx(delay, abs=0.01)
        assert status["reason"] == "storage_retry_wait"
        with closing(runtime._conn()) as conn:
            # Move only the synthetic fixture's persisted failure time past its
            # due point so this test can exercise successive bounded attempts.
            conn.execute(
                "UPDATE manager_executions SET updated_at=? WHERE run_id=?",
                ((failed_at - timedelta(seconds=delay + 1)).isoformat(), run.run_id),
            )
            conn.commit()
        status = runtime.management_coordinator.wake_status(sid)
        assert status["reason"] == "ready"

    run = bind(runtime, sid)
    fail_storage(run, runtime, "SqliteWriteAdmissionError", "SQLite write queue wait timed out")
    assert runtime.management_coordinator.wake_status(sid)["reason"] == "failure_limit"


def test_failure_limit_three_bounds_total_safe_failed_rounds(runtime):
    sid = active_timer(runtime)
    for _ in range(3):
        run = bind(runtime, sid)
        fail_storage(run, runtime, "SqliteWriteAdmissionError", "SQLite write queue wait timed out")
        with closing(runtime._conn()) as conn:
            conn.execute(
                "UPDATE manager_executions SET updated_at=? WHERE run_id=?",
                ((datetime.now(UTC) - timedelta(minutes=2)).isoformat(), run.run_id),
            )
            conn.commit()
    assert runtime.management_coordinator.wake_status(sid)["reason"] == "failure_limit"
