"""Single-record manager writes stay small and use durable guarded publication."""
import json
from contextlib import closing

import pytest

from app.core.safety import SafetyReviewMode
from app.core.tools import ToolContext
from app.domains.management import ManagementConflictError
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_coordinator import bind, finalize, timer
from tests.test_management_history import execute
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def context(runtime):
    return context_fixture.__wrapped__(runtime)


def test_capture_retries_and_reschedule_keep_revision_and_visibility(runtime, context):
    service = runtime.management_work
    args = {"command_id": "capture", "title": "Synthetic interview", "summary": "Start 14:00; duration unknown",
            "scheduled_on": "2026-11-12"}
    receipt = service.create_matter(context, **args)
    assert service.create_matter(context, **args) == receipt
    matter_id = receipt["refs"]["matter"]
    matter = runtime.matter_service.get_matter(matter_id=matter_id)
    assert matter.visibility == "user" and matter.revision == 1
    assert matter.scheduled_start is None and matter.scheduled_end is None and matter.due_at is None
    updated = service.update_matter(context, command_id="move", matter_id=matter_id, expected_revision=1,
                                    changes={"scheduled_on": "2026-11-13"})
    assert updated["results"][0]["revision"] == 2
    with pytest.raises(ManagementConflictError, match="revision"):
        service.update_matter(context, command_id="stale", matter_id=matter_id, expected_revision=1,
                              changes={"title": "Must not win"})
    assert runtime.matter_service.get_matter(matter_id=matter_id).title == "Synthetic interview"
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM matters").fetchone()[0] == 1


def test_bad_time_pair_and_empty_edit_have_no_receipt_or_write(runtime, context):
    with pytest.raises(ValueError):
        runtime.management_work.create_matter(context, command_id="bad-time", title="Bad block",
            scheduled_start="2026-11-12T14:00:00+08:00")
    with pytest.raises(ValueError, match="at least one"):
        runtime.management_work.update_matter(context, command_id="empty", matter_id="missing",
                                               expected_revision=1, changes={})
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM matters").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM management_commands").fetchone()[0] == 0


def test_capture_does_not_bypass_controller_or_delegate_boundary(runtime, context):
    receipt = runtime.management_delegates.delegate_work(context, command_id="line", objective="Read only", allowed_tools=[])
    runtime.agent_run_manager.complete_run(context.run_id)
    runtime.management_coordinator.tick()
    run = bind(runtime)
    assert run.session_id == receipt["session_id"]
    runtime.agent_run_manager.mark_running(run.run_id)
    child = ToolContext(session_id=run.session_id, run_id=run.run_id, safety_review_approved=True)
    with pytest.raises(ManagementConflictError, match="supervisor"):
        runtime.management_work.create_matter(child, command_id="escape", title="No authority")
    policy = runtime._session_run_policy(child.session_id)
    assert "management.create_matter" not in policy.allowed_tools
    runtime.management_service.update(context.session_id, expected_revision=2, status="paused")
    with pytest.raises(ManagementConflictError):
        runtime.management_work.create_matter(child, command_id="paused", title="No authority")


def test_autonomous_capture_and_edit_are_proposals_until_publication(runtime):
    sid, _ = timer(runtime, objective="Capture synthetic work")
    runtime.management_coordinator.tick()
    run = bind(runtime)
    runtime.agent_run_manager.mark_running(run.run_id)
    ctx = ToolContext(session_id=sid, run_id=run.run_id, safety_review_approved=True)
    receipt = runtime.management_work.create_matter(ctx, command_id="background", title="Deferred")
    assert receipt["status"] == "proposed"
    assert runtime.matter_service.list_matters().total_count == 0
    assert finalize(runtime, run)
    matter_id = receipt["refs"]["matter"]
    assert runtime.matter_service.get_matter(matter_id=matter_id).title == "Deferred"
    assert finalize(runtime, run)
    assert runtime.matter_service.list_matters().total_count == 1


@pytest.mark.parametrize("graph", [False, True])
def test_simple_create_and_adjust_need_only_matter_expansion(runtime, graph):
    sid = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="matter"),
        operation("tool_call", tool_name="matter.search", tool_input={"query": "Interview"}),
        operation("tool_call", tool_name="management.create_matter", tool_input={
            "command_id": "simple", "title": "Interview", "summary": "14:00, duration unknown",
            "scheduled_on": "2026-11-12"}),
        operation("final_answer", reason="Saved"), "Saved interview.",
    ])
    result = runtime.run_agent_turn(session_id=sid, user_input="Capture the interview.", resumable=graph,
                                    safety_review_mode=SafetyReviewMode.SKIP)
    assert result.expanded_packages == ["matter"]
    assert [e.tool_name for e in result.tool_events] == ["matter.search", "management.create_matter"]
    assert all(e.result["status"] == "completed" for e in result.tool_events)
    assert runtime.agent_run_manager.list_safety_reviews(result.run_id)
    matter = runtime.matter_service.list_matters().matters[0]
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="matter"),
        operation("tool_call", tool_name="matter.get", tool_input={"matter_id": matter.matter_id}),
        operation("tool_call", tool_name="management.update_matter", tool_input={
            "command_id": "adjust", "matter_id": matter.matter_id, "expected_revision": matter.revision,
            "changes": {"scheduled_on": "2026-11-13"}}),
        operation("final_answer", reason="Updated"), "Updated date.",
    ])
    result = runtime.run_agent_turn(session_id=sid, user_input="Move to the following day.", resumable=graph,
                                    safety_review_mode=SafetyReviewMode.SKIP)
    assert result.expanded_packages == ["matter"]
    assert [e.tool_name for e in result.tool_events] == ["matter.get", "management.update_matter"]
    assert all(e.result["status"] == "completed" for e in result.tool_events)
    assert runtime.matter_service.get_matter(matter_id=matter.matter_id).scheduled_on == "2026-11-13"
    # This guards the observed prompt inflation, rather than elapsed model timing.
    assert len(json.dumps(result.expanded_tools, ensure_ascii=False)) < 18000


def test_ordinary_sessions_cannot_discover_cross_package_supervisor_writers(runtime):
    sid = runtime.create_session(metadata={"kind": "manager"}).session.session_id
    policy = runtime._session_run_policy(sid)
    assert not any(n.startswith("management.") for n in policy.allowed_tools)
    run = runtime.create_agent_run(session_id=sid, user_input="Do not gain authority")
    runtime.agent_run_manager.mark_running(run.run_id)
    result = execute(runtime, ToolContext(session_id=sid, run_id=run.run_id), "create_matter",
                     {"command_id": "forged", "title": "No grant"})
    assert result.status != "completed"
    assert runtime.matter_service.list_matters().total_count == 0


@pytest.mark.parametrize("extra", [{"unexpected": 1}, {"changes": {"unexpected": 1}}])
def test_unknown_fields_are_rejected_before_invocation(runtime, context, extra):
    inputs = {"command_id": "schema", "matter_id": "missing", "expected_revision": 1,
              "changes": {"title": "Synthetic"}, **extra}
    result = execute(runtime, context, "update_matter", inputs)
    assert result.status == "rejected" and result.execution_started is False
    assert "unexpected" in str(result.output)
