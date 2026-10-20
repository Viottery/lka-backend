"""Management workspaces define a default cwd while preserving tool authority."""
import pytest

from app.core.agent_runs import AgentRunStatus
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewStatus
from app.core.tools import ToolContext
from app.domains.management import ManagementConflictError
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_management_tasks import runtime as runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def manager_context(runtime):
    return context_fixture.__wrapped__(runtime)


def _script(runtime, tool, inputs, answer="Finished."):
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name=tool.split(".", 1)[0]),
        operation("tool_call", tool_name=tool, tool_input=inputs),
        operation("final_answer", reason="The requested local operation is complete."), answer,
    ])


def test_start_task_workspace_exposes_bash_and_allows_reviewed_cross_workspace_read(runtime, manager_context, tmp_path):
    workspace = tmp_path / "task-workspace"
    workspace.mkdir()
    outside = tmp_path / "sibling" / "evidence.txt"
    outside.parent.mkdir()
    outside.write_text("cross-workspace evidence", encoding="utf-8")

    receipt = runtime.managed_tasks.start_task(
        manager_context, command_id="workspace-read", purpose="Read the supplied evidence",
        workspace_path=str(workspace),
    )
    run = runtime.agent_run_manager.get_run(receipt["run_id"])
    view = runtime.agent_turn_loop._tool_view_for_run(run.run_id)
    assert view.full_workspace_authority is True
    assert view.allowed_paths in (None, ())
    assert view.allows_tool(tool_name="bash.run", package="bash", read_only=False)

    _script(runtime, "filesystem.read_file", {"path": str(outside)})
    assert runtime.agent_dispatch.run_one()
    current = runtime.agent_run_manager.get_run(run.run_id)
    assert current.status == AgentRunStatus.COMPLETED
    events = runtime.agent_run_manager.list_safety_reviews(run.run_id)
    [review] = [item for item in events if item.tool_name == "filesystem.read_file"]
    assert review.workspace_access == [str(outside.resolve())]
    assert review.status.value == "approved"
    tool_events = [event for event in runtime.agent_run_manager.list_events(run.run_id)
                   if event.type == "tool_completed"]
    assert any("cross-workspace evidence" in str(event.payload) for event in tool_events)


@pytest.mark.parametrize("decision", [SafetyReviewDecision.APPROVE, SafetyReviewDecision.REJECT])
def test_task_workspace_external_bash_effect_requires_manual_decision_and_never_replays(
    runtime, manager_context, tmp_path, decision,
):
    workspace = tmp_path / "task-workspace"
    external = tmp_path / "other-workspace"
    workspace.mkdir()
    external.mkdir()
    target = external / "effect.txt"
    receipt = runtime.managed_tasks.start_task(
        manager_context, command_id=f"bash-{decision.value}", purpose="Run the requested local command",
        workspace_path=str(workspace),
    )
    run = runtime.agent_run_manager.get_run(receipt["run_id"])
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.SKIP
    _script(runtime, "bash.run", {"command": "touch effect.txt", "cwd": str(external)})

    assert runtime.agent_dispatch.run_one()
    waiting = runtime.agent_run_manager.get_run(run.run_id)
    assert waiting.status == AgentRunStatus.WAITING_CONFIRMATION
    [review] = runtime.agent_run_manager.list_safety_reviews(run.run_id)
    assert review.mode == SafetyReviewMode.MANUAL
    assert review.status == SafetyReviewStatus.PENDING
    assert review.workspace_access == [str(external.resolve())]
    assert not target.exists()

    decided, changed = runtime.agent_run_manager.decide_safety_review_with_transition(
        review_id=review.review_id, decision=decision, decided_by="user",
        reason="Explicit decision for this synthetic command",
    )
    assert changed and decided.review_id == review.review_id
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="The user's decision was respected."), "Decision respected.",
    ])
    runtime.resume_agent_run(run.run_id)
    assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED
    assert target.exists() is (decision == SafetyReviewDecision.APPROVE)
    # Completed graph recovery does not replay the approved command.
    runtime.resume_agent_run(run.run_id)
    assert target.exists() is (decision == SafetyReviewDecision.APPROVE)
    assert len(runtime.agent_run_manager.list_safety_reviews(run.run_id)) == 1


def test_delegate_workspace_changes_default_cwd_without_widening_tools_or_manager_access(
    runtime, manager_context, tmp_path,
):
    from tests.test_management_delegate_integration import bind_all, queued

    first_workspace = tmp_path / "delegate-default"
    second_workspace = tmp_path / "delegate-selected"
    first_workspace.mkdir()
    second_workspace.mkdir()
    allowed = ["filesystem.read_file"]
    first = runtime.management_delegates.delegate_work(
        manager_context, command_id="delegate-default", objective="Read one bounded file",
        workspace_path=str(first_workspace), allowed_tools=allowed,
    )
    second = runtime.management_delegates.delegate_work(
        manager_context, command_id="delegate-selected", objective="Read one bounded file elsewhere",
        workspace_path=str(second_workspace), allowed_tools=allowed,
    )
    bind_all(runtime)

    cases = [(first, first_workspace, queued(runtime, first["session_id"])),
             (second, second_workspace, queued(runtime, second["session_id"]))]
    tool_sets = []
    for receipt, expected_workspace, run in cases:
        session = runtime.get_session(session_id=receipt["session_id"]).session
        assert session.workspace.backend_path == str(expected_workspace)
        view = runtime.agent_turn_loop._tool_view_for_run(run.run_id)
        assert view.full_workspace_authority is True
        assert view.allowed_paths in (None, ())
        assert view.allows_tool(tool_name="filesystem.read_file", package="filesystem", read_only=True)
        assert not view.allows_tool(tool_name="filesystem.edit_file", package="filesystem", read_only=False)
        assert not view.allows_tool(tool_name="bash.run", package="bash", read_only=False)

        runtime.agent_run_manager.mark_running(run.run_id)
        context = ToolContext(session_id=run.session_id, run_id=run.run_id, tool_view=view)
        with pytest.raises(ManagementConflictError):
            runtime.managed_tasks.read_session(context, session_id=manager_context.session_id)
        denied = runtime.tool_executor.execute(
            invocation_id="ungranted-edit-" + receipt["session_id"],
            tool_name="filesystem.edit_file",
            tool_input={"path": str(expected_workspace / "not-created.txt"),
                        "expected_sha256": "0" * 64,
                        "edits": [{"old_text": "old", "new_text": "new"}]},
            context=context,
        )
        assert denied.status == "rejected" and denied.execution_started is False
        tool_sets.append(set(run.metadata["session_policy"]["allowed_tools"]))

    first_tools, second_tools = tool_sets
    assert first_tools == second_tools
    assert "filesystem.read_file" in first_tools
    assert "filesystem.edit_file" not in first_tools and "bash.run" not in first_tools
