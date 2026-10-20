"""Matter-aware manager integration with real harness, offline scripted decisions."""
import json
from contextlib import closing
from datetime import UTC, datetime

import pytest

from app.core.safety import SafetyReviewMode
from app.core.tools import ToolContext
from app.domains.matters import MatterCreateInput, MatterUpdateInput
from app.experts.management import ManagementExecutionFenced
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_coordinator import bind, finalize, timer
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def setup_bound(runtime):
    runtime.local_app_config.management.internal_items_enabled = True
    sid, goal = timer(runtime, objective="Legacy objective must not win")
    matter = runtime.matter_service.create_matter(MatterCreateInput(title="Current business objective", visibility="internal"))
    run = runtime.create_agent_run(session_id=sid, user_input="Bind responsibility", resumable=True)
    runtime.agent_run_manager.mark_running(run.run_id)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    runtime.management_work.organize_work(context, "bind-work", [{"op": "bind_goal", "goal_id": goal["goal_id"],
        "matter_id": matter.matter_id, "expected_goal_revision": 1, "expected_matter_revision": 1}])
    runtime.agent_run_manager.mark_cancelled(run.run_id)
    runtime.management_coordinator.tick()
    execution_run = bind(runtime)
    runtime.agent_run_manager.mark_running(execution_run.run_id)
    return sid, goal, matter, execution_run


def test_manager_only_reads_matter_through_direct_domain_tools(runtime):
    sid = runtime.management_service.create()["session"]["session_id"]
    run = runtime.create_agent_run(session_id=sid, user_input="Read only")
    view = runtime.agent_turn_loop._tool_view_for_run(run.run_id)
    assert view.allows_tool(tool_name="matter.get", package="matter", read_only=True)
    assert not view.allows_tool(tool_name="matter.update", package="matter", read_only=False)
    assert view.allows_tool(tool_name="management.organize_work", package="management", read_only=False)
    ordinary = runtime.create_session(title="Ordinary").session.session_id
    other = runtime.create_agent_run(session_id=ordinary, user_input="No supervisor tools")
    other_view = runtime.agent_turn_loop._tool_view_for_run(other.run_id)
    assert not other_view.allows_tool(tool_name="management.organize_work", package="management", read_only=False)


def test_scripted_harness_organizes_internal_work_through_review_gate(runtime):
    runtime.local_app_config.management.internal_items_enabled = True
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.organize_work", tool_input={"command_id": "one-intent", "operations": [
            {"op": "create", "ref": "research", "fields": {"title": "Explore three approaches"},
             "management": {"purpose": "goal", "next_action": "Compare evidence"}}]}),
        operation("final_answer", reason="Work registered"), "I will manage this investigation.",
    ])
    result = runtime.run_agent_turn(session_id=sid, user_input="分析三种方案，不加到待办。", safety_review_mode=SafetyReviewMode.SKIP)
    event = next(e for e in result.tool_events if e.tool_name == "management.organize_work")
    assert event.result["status"] == "completed", event.model_dump()
    assert runtime.agent_run_manager.list_safety_reviews(result.run_id)
    assert runtime.matter_service.list_matters().total_count == 0
    items = runtime.matter_service.list_matters(visibility="internal").matters
    assert len(items) == 1 and items[0].due_at is None


def test_simple_manager_query_does_not_create_matter(runtime):
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.agent_turn_loop.llm_client = ScriptedClient([operation("final_answer", reason="Simple question"), "Three options."])
    runtime.run_agent_turn(session_id=sid, user_input="有哪些方向？")
    assert runtime.matter_service.list_matters(visibility="all").total_count == 0


def test_bound_matter_snapshot_and_final_proposal_share_delivery_transaction(runtime):
    sid, _, matter, run = setup_bound(runtime)
    assert run.metadata["manager_matter_versions"] == {matter.matter_id: matter.revision}
    assert "Current business objective" in run.user_input
    assert "Legacy objective must not win" not in run.user_input
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    receipt = runtime.management_work.organize_work(context, "finish", [{"op": "update", "matter_id": matter.matter_id,
        "expected_revision": 1, "changes": {"summary": "Evidence checked"}}])
    assert receipt["status"] == "proposed"
    assert runtime.matter_service.get_matter(matter_id=matter.matter_id).summary == matter.summary
    assert finalize(runtime, run)
    assert runtime.matter_service.get_matter(matter_id=matter.matter_id).summary == "Evidence checked"
    assert len(runtime.management_goals.list_deliveries(sid)["deliveries"]) == 1
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] == run.metadata["manager_execution_id"]
    with closing(runtime._conn()) as conn:
        stored = json.loads(conn.execute("SELECT receipt FROM management_commands WHERE command_id='finish'").fetchone()[0])
    assert stored["status"] == "applied"
    assert finalize(runtime, run)  # exactly once replay after the same committed execution
    assert runtime.matter_service.get_matter(matter_id=matter.matter_id).revision == 2


@pytest.mark.parametrize("failure", ["human_change", "commit_window"])
def test_stale_or_failed_publication_never_partially_writes(runtime, monkeypatch, failure):
    sid, _, matter, run = setup_bound(runtime)
    context = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    runtime.management_work.organize_work(context, "staged", [{"op": "update", "matter_id": matter.matter_id,
        "expected_revision": 1, "changes": {"summary": "Agent stale summary"}}])
    if failure == "human_change":
        runtime.matter_service.update_matter(matter_id=matter.matter_id,
            payload=MatterUpdateInput(expected_revision=1, summary="Human edit"))
        error = ManagementExecutionFenced
    else:
        def crash(*args, **kwargs):
            raise RuntimeError("Synthetic precommit interruption")
        monkeypatch.setattr(runtime.session_service, "append_message_in_transaction", crash)
        error = RuntimeError
    with pytest.raises(error):
        finalize(runtime, run)
    assert runtime.matter_service.get_matter(matter_id=matter.matter_id).summary == ("Human edit" if failure == "human_change" else matter.summary)
    assert not runtime.management_goals.list_deliveries(sid)["deliveries"]
    assert runtime.management_goals.list_events(sid)["events"][0]["consumed_execution_id"] is None
    with closing(runtime._conn()) as conn:
        receipt = json.loads(conn.execute("SELECT receipt FROM management_commands WHERE command_id='staged'").fetchone()[0])
    assert receipt["status"] == "proposed"


def test_goal_timer_still_usable_after_business_binding(runtime):
    sid, goal, matter, run = setup_bound(runtime)
    assert runtime.management_goals.get_goal(sid, goal["goal_id"])["objective"] == matter.title
    # Timer changes are scheduling fields; they never overwrite business content.
    runtime.agent_run_manager.mark_cancelled(run.run_id)
    updated = runtime.management_goals.update_goal(sid, goal["goal_id"], expected_revision=1,
        next_wake_at=datetime.now(UTC).isoformat())
    assert updated["matter_revision"] == 1


def test_harness_starts_ready_work_with_an_atomic_execution_binding(runtime):
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    matter = runtime.matter_service.create_matter(MatterCreateInput(title="Independent research"))
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="management"),
        operation("tool_call", tool_name="management.start_work", tool_input={"command_id": "start-ready",
            "matter_id": matter.matter_id, "expected_revision": 1, "purpose": "Compare two sources and return evidence"}),
        operation("final_answer", reason="Independent task accepted"), "Research accepted.",
    ])
    result = runtime.run_agent_turn(session_id=sid, user_input="请推进这件事。", safety_review_mode=SafetyReviewMode.SKIP)
    event = next(e for e in result.tool_events if e.tool_name == "management.start_work")
    assert event.result["status"] == "completed", event.model_dump()
    receipt = event.result["output"]
    assert receipt["action"] == "start_work" and receipt["matter_id"] == matter.matter_id
    task = runtime.agent_run_manager.get_run(receipt["run_id"])
    assert task.status.value == "queued" and task.parent_run_id is None
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT run_id FROM manager_work_assignments WHERE matter_id=?", (matter.matter_id,)).fetchone()[0] == task.run_id


def test_dispatched_task_without_autonomous_quota_still_uses_background_pool(runtime, monkeypatch):
    from app.core.llm_workloads import current_workload

    sid = runtime.create_session(title="User-delegated background task").session.session_id
    run = runtime.create_agent_run(session_id=sid, user_input="Independent task", resumable=True)
    seen = []
    def execute(**kwargs):
        seen.append(current_workload().pool)
        runtime.agent_run_manager.mark_running(kwargs["existing_run_id"])
        runtime.agent_run_manager.complete_run(kwargs["existing_run_id"])
    monkeypatch.setattr(runtime, "run_agent_turn", execute)
    runtime.agent_dispatch.enqueue(run.run_id)
    assert runtime.agent_dispatch.run_one()
    assert seen == ["background_agent"]
