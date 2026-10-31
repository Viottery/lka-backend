"""Current management guard applicability is read without cross-call caching."""
from __future__ import annotations

import json
from contextlib import closing

import pytest

from app.domains.management import ManagementConflictError
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_tasks import manager_context as manager_context_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    generator = runtime_fixture.__wrapped__(tmp_path, monkeypatch)
    instance = next(generator)
    yield instance
    try:
        next(generator)
    except StopIteration:
        pass


def test_ordinary_guard_snapshot_uses_one_select_and_accepts_missing_run(runtime):
    session_id = runtime.session_service.create_session(title="ordinary").session.session_id
    run = runtime.create_agent_run(session_id=session_id, user_input="ordinary work", resumable=True)
    statements = []
    with closing(runtime._conn()) as conn:
        conn.set_trace_callback(statements.append)
        record, applicable = runtime.management_coordinator._run_guard_snapshot(conn, run.run_id)
        conn.set_trace_callback(None)
        runtime.management_coordinator.check_run("unknown-run", conn=conn)

    snapshot_selects = [sql for sql in statements
                        if sql.lstrip().upper().startswith("SELECT")
                        and "FROM (SELECT 1) AS PROBE" in sql.upper()]
    assert len(snapshot_selects) == 1
    assert record["run_id"] == run.run_id
    assert applicable == {"is_delegate": False, "has_delegate_owner": False, "is_queued": False}


def test_guard_rechecks_delegate_creation_and_parent_pause(runtime):
    parent = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(parent, expected_revision=1, status="active")
    child = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(child, expected_revision=1, status="active")
    run = runtime.create_agent_run(session_id=child, user_input="delegate work", resumable=True)

    # No relationship exists at the first check.
    runtime.management_coordinator.check_run(run.run_id)
    stamp = "2026-10-29T00:00:00+00:00"
    with closing(runtime._conn()) as conn, conn:
        conn.execute(
            "INSERT INTO manager_delegates(delegate_id,session_id,parent_session_id,objective,allowed_tools,"
            "workspace,kickoff_envelope,state,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'active',1,?,?)",
            ("delegate-snapshot", child, parent, "objective", "[]", None, "{}", stamp, stamp),
        )

    # The next call must observe the newly committed relationship and validate it.
    runtime.management_coordinator.check_run(run.run_id)
    runtime.management_service.update(parent, expected_revision=2, status="paused")
    with pytest.raises(ManagementConflictError, match="Delegate and parent controllers"):
        runtime.management_coordinator.check_run(run.run_id)


def test_guard_observes_delegate_revocation_on_next_call(runtime):
    parent = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(parent, expected_revision=1, status="active")
    child = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(child, expected_revision=1, status="active")
    run = runtime.create_agent_run(session_id=child, user_input="delegate work", resumable=True)
    stamp = "2026-10-29T00:00:00+00:00"
    with closing(runtime._conn()) as conn, conn:
        conn.execute(
            "INSERT INTO manager_delegates(delegate_id,session_id,parent_session_id,objective,allowed_tools,"
            "workspace,kickoff_envelope,state,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'active',1,?,?)",
            ("delegate-revoke", child, parent, "objective", "[]", None, "{}", stamp, stamp),
        )
    runtime.management_coordinator.check_run(run.run_id)
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE manager_delegates SET state='cancelled' WHERE session_id=?", (child,))

    with pytest.raises(ManagementConflictError, match="Delegate and parent controllers"):
        runtime.management_coordinator.check_run(run.run_id)


def test_released_queued_run_rechecks_workspace_source(runtime, tmp_path):
    context = manager_context_fixture.__wrapped__(runtime)
    original_workspace = tmp_path / "original-workspace"
    original_workspace.mkdir()
    receipt = runtime.management_queue.queue_work(
        context, command_id="guard-snapshot-queue", purpose="Read the assigned workspace",
        read_only_work=False, workspace_path=str(original_workspace),
    )
    assert runtime.management_queue.tick() == 1
    runtime.management_coordinator.check_run(receipt["run_id"])

    with closing(runtime._conn()) as conn, conn:
        row = conn.execute("SELECT session_id,metadata FROM agent_sessions WHERE session_id=?",
                           (receipt["session_id"],)).fetchone()
        metadata = json.loads(row["metadata"])
        metadata["workspace"]["backend_path"] = str(tmp_path / "changed-workspace")
        conn.execute("UPDATE agent_sessions SET metadata=? WHERE session_id=?",
                     (json.dumps(metadata), receipt["session_id"]))

    with pytest.raises(ManagementConflictError, match="workspace changed"):
        runtime.management_coordinator.check_run(receipt["run_id"])
