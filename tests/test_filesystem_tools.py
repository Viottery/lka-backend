from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.tools import ToolContext


def _runtime(tmp_path: Path, monkeypatch) -> LocalKnowledgeAgentRuntime:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(workspace))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    return LocalKnowledgeAgentRuntime(get_settings())


def _active_child_run_id(runtime: LocalKnowledgeAgentRuntime) -> str:
    manager = runtime.agent_run_manager
    parent = manager.create_run(session_id="parent_session", user_input="parent")
    manager.mark_running(parent.run_id)
    child = manager.create_child_run(
        parent_run_id=parent.run_id, plan_id="scope_plan", step_id="scope_step",
        attempt=1, user_input="child",
    )
    manager.mark_child_running(child.run_id)
    return child.run_id


def test_filesystem_read_file_returns_bounded_slice_and_sha(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    target = workspace / "notes.md"
    target.write_text("line 1\nline 2\nline 3\nline 4\n", encoding="utf-8")
    context = ToolContext(session_id="session_filesystem")

    result = runtime.tool_executor.execute(
        invocation_id="fs_read_001",
        tool_name="filesystem.read_file",
        tool_input={"path": str(target), "start_line": 2, "max_lines": 2},
        context=context,
    )

    assert result.status == "completed"
    assert result.output["content"] == "line 2\nline 3\n"
    assert result.output["start_line"] == 2
    assert result.output["end_line"] == 3
    assert result.output["total_lines"] == 4
    assert result.output["returned_lines"] == 2
    assert result.output["truncated"] is True
    assert len(result.output["sha256"]) == 64
    assert runtime.tool_executor.validate_output(
        tool_name="filesystem.read_file",
        result=result,
    ) == []


def test_filesystem_resolves_relative_paths_from_workspace_root(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    target = workspace / "docs" / "notes.md"
    target.parent.mkdir()
    target.write_text("Status: red\n", encoding="utf-8")

    relative = runtime.tool_executor.execute(
        invocation_id="fs_read_relative",
        tool_name="filesystem.read_file",
        tool_input={"path": "docs/notes.md"},
        context=ToolContext(session_id="session_filesystem"),
    )

    assert relative.status == "completed"
    assert relative.output["resolved_path"] == target.as_posix()
    assert relative.output["content"] == "Status: red\n"

    variable = runtime.tool_executor.execute(
        invocation_id="fs_read_workspace_variable",
        tool_name="filesystem.read_file",
        tool_input={"path": "$workspace_root/docs/notes.md"},
        context=ToolContext(session_id="session_filesystem"),
    )

    assert variable.status == "completed"
    assert variable.output["resolved_path"] == target.as_posix()


def test_filesystem_read_rejects_paths_outside_workspace(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    result = runtime.tool_executor.execute(
        invocation_id="fs_read_outside",
        tool_name="filesystem.read_file",
        tool_input={"path": str(outside)},
        context=ToolContext(session_id="session_filesystem"),
    )

    assert result.status == "rejected"
    assert result.output["safety_review_required"] is True
    assert result.execution_started is False


def test_filesystem_tool_executor_rejects_paths_outside_narrow_child_scope(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    narrow = workspace / "allowed"
    narrow.mkdir()
    allowed = narrow / "ok.txt"
    denied = workspace / "outside-narrow.txt"
    allowed.write_text("allowed", encoding="utf-8")
    denied.write_text("denied", encoding="utf-8")
    view = ToolView(
        snapshot_id="child_fs_snapshot", child_run_id=_active_child_run_id(runtime),
        allowed_packages=("filesystem",),
        allowed_tools=("filesystem.read_file", "filesystem.edit_file"),
        allowed_paths=(str(narrow),),
        side_effect_level=SideEffectLevel.EXTERNAL,
    )
    context = ToolContext(session_id="child_fs", workspace_root=str(workspace), tool_view=view)

    allowed_result = runtime.tool_executor.execute(
        invocation_id="child-fs-allowed", tool_name="filesystem.read_file",
        tool_input={"path": "allowed/ok.txt"}, context=context,
    )
    assert allowed_result.status == "completed", allowed_result.error
    denied_result = runtime.tool_executor.execute(
        invocation_id="child-fs-denied", tool_name="filesystem.read_file",
        tool_input={"path": str(denied)}, context=context,
    )
    assert denied_result.status == "rejected"
    assert "outside the child workspace scope" in (denied_result.error or "")


def test_filesystem_edit_file_requires_matching_sha_and_unique_old_text(
    tmp_path,
    monkeypatch,
):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    target = workspace / "plan.md"
    target.write_text("todo alpha\ntodo beta\ntodo alpha\n", encoding="utf-8")
    context = ToolContext(
        session_id="session_filesystem",
        safety_review_approved=True,
        safety_review_id="test_review_filesystem_edit",
    )

    read = runtime.tool_executor.execute(
        invocation_id="fs_read_before_edit",
        tool_name="filesystem.read_file",
        tool_input={"path": str(target)},
        context=context,
    )
    sha = read.output["sha256"]

    stale = runtime.tool_executor.execute(
        invocation_id="fs_edit_stale",
        tool_name="filesystem.edit_file",
        tool_input={
            "path": str(target),
            "expected_sha256": "0" * 64,
            "edits": [{"old_text": "todo beta", "new_text": "done beta"}],
        },
        context=context,
    )
    assert stale.status == "failed"
    assert "expected_sha256 does not match" in (stale.error or "")
    assert target.read_text(encoding="utf-8") == "todo alpha\ntodo beta\ntodo alpha\n"

    ambiguous = runtime.tool_executor.execute(
        invocation_id="fs_edit_ambiguous",
        tool_name="filesystem.edit_file",
        tool_input={
            "path": str(target),
            "expected_sha256": sha,
            "edits": [{"old_text": "todo alpha", "new_text": "done alpha"}],
        },
        context=context,
    )
    assert ambiguous.status == "failed"
    assert "matched 2 times; expected 1" in (ambiguous.error or "")
    assert target.read_text(encoding="utf-8") == "todo alpha\ntodo beta\ntodo alpha\n"


def test_filesystem_edit_file_applies_atomic_targeted_replacements(
    tmp_path,
    monkeypatch,
):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    target = workspace / "plan.md"
    target.write_text("todo alpha\ntodo beta\ntodo alpha\n", encoding="utf-8")
    context = ToolContext(
        session_id="session_filesystem",
        safety_review_approved=True,
        safety_review_id="test_review_filesystem_edit_success",
    )
    read = runtime.tool_executor.execute(
        invocation_id="fs_read_before_success",
        tool_name="filesystem.read_file",
        tool_input={"path": str(target)},
        context=context,
    )

    result = runtime.tool_executor.execute(
        invocation_id="fs_edit_success",
        tool_name="filesystem.edit_file",
        tool_input={
            "path": str(target),
            "expected_sha256": read.output["sha256"],
            "edits": [
                {
                    "old_text": "todo alpha",
                    "new_text": "done alpha",
                    "expected_occurrences": 2,
                },
                {"old_text": "todo beta", "new_text": "done beta"},
            ],
        },
        context=context,
    )

    assert result.status == "completed"
    assert result.output["changed"] is True
    assert result.output["edit_count"] == 2
    assert len(result.output["sha256_after"]) == 64
    assert result.output["sha256_after"] != result.output["sha256_before"]
    assert target.read_text(encoding="utf-8") == "done alpha\ndone beta\ndone alpha\n"
    assert runtime.tool_executor.validate_output(
        tool_name="filesystem.edit_file",
        result=result,
    ) == []


def test_filesystem_package_is_agent_visible(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)

    package_names = {package.name for package in runtime.tool_registry.list_packages()}
    filesystem_tool_names = {
        tool.name for tool in runtime.tool_registry.list_tools(package="filesystem")
    }

    assert "filesystem" in package_names
    assert filesystem_tool_names == {
        "filesystem.read_file",
        "filesystem.edit_file",
    }
