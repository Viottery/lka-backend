"""State receipts and combined reads preserve freshness, privacy and write guards."""
from datetime import UTC, datetime

import pytest

from app.core.agent_runs import AgentRunRecord, AgentRunStatus
from app.domains.management import ManagementConflictError
from tests.test_management_tasks import (
    counts,
    new_target,
)
from tests.test_management_tasks import (
    manager_context as manager_context_fixture,
)
from tests.test_management_tasks import (
    runtime as runtime_fixture,
)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def manager_context(runtime):
    return manager_context_fixture.__wrapped__(runtime)


def test_session_discovery_selects_latest_root_and_only_safe_fields(runtime, manager_context):
    target = new_target(runtime, status="paused")
    runtime.agent_run_manager._update_run(target.run_id, status=AgentRunStatus.PAUSED,
        metadata_patch={"paused_from_status": "waiting_confirmation", "raw_prompt": "PRIVATE_INPUT"})
    runtime.agent_run_store.save_run(AgentRunRecord(
        run_id="newer-child", session_id=target.session_id, trace_id="child-trace",
        parent_run_id=target.run_id, status=AgentRunStatus.RUNNING, user_input="PRIVATE_CHILD_INPUT",
        created_at="2099-01-01T00:00:00+00:00",
    ).model_dump(mode="json"), updated_at=datetime.now(UTC).isoformat())
    empty = runtime.create_session(title="Empty ordinary").session.session_id
    results = runtime.managed_tasks.list_sessions(manager_context)
    by_id = {row["session_id"]: row for row in results["sessions"]}
    assert by_id[target.session_id]["latest_run"]["run_id"] == target.run_id
    assert by_id[target.session_id]["latest_run"]["paused_from_status"] == "waiting_confirmation"
    assert by_id[empty]["latest_run"] is None
    assert "PRIVATE" not in str(results) and "raw_prompt" not in str(results) and "user_input" not in str(results)


def test_replacement_receipt_names_new_queued_and_old_cancelled_and_stays_frozen(runtime, manager_context):
    service = runtime.managed_tasks
    old = new_target(runtime, status="paused")
    receipt = service.send_instruction(manager_context, command_id="state-case", session_id=old.session_id,
                                       expected_run_id=old.run_id, purpose="Read current evidence")
    assert receipt["status"] == "accepted"
    assert receipt["snapshot_kind"] == "command_commit"
    assert receipt["snapshot_at"]
    assert receipt["run_snapshot"]["run_id"] == receipt["run_id"]
    assert receipt["run_snapshot"]["status"] == "queued"
    assert receipt["replaced_run_snapshot"]["run_id"] == old.run_id
    assert receipt["replaced_run_snapshot"]["status"] == "cancelled"
    runtime.agent_run_manager._update_run(receipt["run_id"], status=AgentRunStatus.COMPLETED,
                                         result_snapshot={"answer": "Done"})
    replay = service.send_instruction(manager_context, command_id="state-case", session_id=old.session_id,
                                      expected_run_id=old.run_id, purpose="Read current evidence")
    assert replay == receipt
    current = service.read_session(manager_context, session_id=old.session_id)
    assert current["latest_run"]["status"] == "completed"
    assert current["latest_run"]["has_result"] is True


def test_read_session_keeps_messages_and_run_in_one_database_snapshot(runtime, manager_context, monkeypatch):
    service = runtime.managed_tasks
    target = new_target(runtime, status="running")
    runtime.append_session_message(session_id=target.session_id, role="user", content="Question")
    original = service._latest_run
    observed = []

    def complete_after_reading_state(conn, sid):
        assert conn.in_transaction
        state = original(conn, sid)
        if not observed:
            observed.append(True)
            runtime.agent_run_manager._update_run(target.run_id, status=AgentRunStatus.COMPLETED)
            runtime.append_session_message(session_id=sid, role="agent", content="Completed concurrently")
        return state

    monkeypatch.setattr(service, "_latest_run", complete_after_reading_state)
    page = service.read_session(manager_context, session_id=target.session_id)
    assert page["latest_run"]["status"] == "running"
    assert [m["content"] for m in page["messages"]] == ["Question"]
    fresh = service.read_session(manager_context, session_id=target.session_id)
    assert fresh["latest_run"]["status"] == "completed"
    assert fresh["messages"][-1]["content"] == "Completed concurrently"


def test_read_snapshot_cannot_authorize_replacement_after_target_enters_waiting(runtime, manager_context):
    service = runtime.managed_tasks
    target = new_target(runtime, status="paused")
    snapshot = service.read_session(manager_context, session_id=target.session_id)
    assert snapshot["latest_run"]["status"] == "paused"
    runtime.agent_run_manager._update_run(target.run_id, status=AgentRunStatus.WAITING_CONFIRMATION)
    before = counts(runtime)
    with pytest.raises(ManagementConflictError, match="Pause the target"):
        service.send_instruction(manager_context, command_id="stale-snapshot", session_id=target.session_id,
                                 expected_run_id=snapshot["latest_run"]["run_id"], purpose="New purpose")
    assert counts(runtime) == before


def test_pause_request_receipt_does_not_claim_pause_acknowledgement(runtime, manager_context):
    target = new_target(runtime, status="running")
    receipt = runtime.managed_tasks.control_run(manager_context, command_id="request-pause",
                                                run_id=target.run_id, action="pause")
    assert receipt["run_snapshot"]["status"] == "running"
    assert receipt["run_snapshot"]["pause_requested"] is True


def test_control_receipt_observes_completion_between_pause_request_and_commit(runtime, manager_context, monkeypatch):
    service = runtime.managed_tasks
    target = new_target(runtime, status="running")
    interrupt = service.interrupt

    def complete_before_receipt(run_id):
        stale = interrupt(run_id)
        assert stale.status.value == "running"
        runtime.agent_run_manager._update_run(run_id, status=AgentRunStatus.COMPLETED)
        return stale

    monkeypatch.setattr(service, "interrupt", complete_before_receipt)
    receipt = service.control_run(manager_context, command_id="pause-race", run_id=target.run_id, action="pause")
    assert receipt["run_snapshot"]["status"] == "completed"
    assert receipt["run_snapshot"]["pause_requested"] is False
