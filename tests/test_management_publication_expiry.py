"""A schedule expiring during final projection cannot publish partial effects."""
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

import app.experts.management as coordinator_module
from app.core.tools import ToolContext
from app.domains.matters import MatterCreateInput
from app.experts.management import ManagementExecutionFenced
from tests.test_management_coordinator import bind, finalize
from tests.test_management_policies_integration import active_manager
from tests.test_management_policies_integration import runtime as runtime_fixture

runtime = runtime_fixture


def test_schedule_expiry_after_message_append_rolls_back_matter_and_delivery(runtime, monkeypatch):
    sid = active_manager(runtime)
    goal = runtime.management_goals.create_goal(sid, objective="Deliver the scheduled review")
    matter = runtime.matter_service.create_matter(MatterCreateInput(title="Existing work", summary="Original evidence"))
    start = datetime.now(UTC)
    expires = start + timedelta(seconds=30)
    clock = [start + timedelta(seconds=2)]

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(coordinator_module, "datetime", ControlledDatetime)
    runtime.management_schedules.configure(
        sid, "expiring-check", now=start, goal_id=goal["goal_id"], timezone="UTC", kind="one_shot",
        run_at=(start + timedelta(seconds=1)).isoformat(), expires_at=expires.isoformat(),
    )
    assert runtime.management_coordinator.tick(now=clock[0]) == {"captured": 1, "scheduled": 1}
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    receipt = runtime.management_work.organize_work(context, "review-evidence", [{
        "op": "update", "matter_id": matter.matter_id, "expected_revision": matter.revision,
        "changes": {"summary": "Changed by scheduled review"},
    }])
    assert receipt["status"] == "proposed"
    assert runtime.matter_service.get_matter(matter_id=matter.matter_id).revision == 1
    original_append = runtime.session_service.append_message_in_transaction
    appended = []

    def append_then_expire(conn, *args, **kwargs):
        result = original_append(conn, *args, **kwargs)
        # Business effects and the answer have been projected into this same
        # uncommitted transaction; expiry must roll all of them back together.
        assert conn.execute("SELECT summary FROM matters WHERE matter_id=?", (matter.matter_id,)).fetchone()[0] == "Changed by scheduled review"
        assert conn.execute("SELECT count(*) FROM agent_session_messages WHERE session_id=?", (sid,)).fetchone()[0] == 1
        appended.append(True)
        clock[0] = expires + timedelta(seconds=1)
        return result

    monkeypatch.setattr(runtime.session_service, "append_message_in_transaction", append_then_expire)
    with pytest.raises(ManagementExecutionFenced, match="manager_schedule_expired"):
        finalize(runtime, run)
    assert appended == [True]
    current = runtime.matter_service.get_matter(matter_id=matter.matter_id)
    assert current.summary == "Original evidence" and current.revision == matter.revision
    assert runtime.management_goals.list_deliveries(sid)["deliveries"] == []
    assert runtime.session_service.get_session(session_id=sid).messages == []
    assert all(event["consumed_execution_id"] is None for event in runtime.management_goals.list_events(sid)["events"])
    assert runtime.management_goals.list_executions(sid)["executions"][0]["status"] == "bound"
    assert runtime.agent_run_manager.get_run(run.run_id).status.value == "running"
    with closing(runtime._conn()) as conn:
        stored = conn.execute("SELECT receipt FROM management_commands WHERE manager_session_id=? AND command_id=?",
                              (sid, "review-evidence")).fetchone()
        assert json.loads(stored[0])["status"] == "proposed"
