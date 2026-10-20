"""Frozen root workspace policy is enforced before tool effects and approvals."""
from types import SimpleNamespace

import pytest

from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolContext, ToolExecutor, ToolInvocation, ToolRegistry
from app.tool_packages.bash import (
    BashAccessPolicy,
    BashListSessionsTool,
    BashReadSessionTool,
    BashRunTool,
    BashSession,
    BashSessionManager,
)
from app.tool_packages.filesystem import FileAccessPolicy, ReadFileTool


def view(*tools, paths=(), unrestricted=False):
    return ToolView(snapshot_id="session_policy:root", allowed_tools=tools,
                    allowed_paths=tuple(map(str, paths)), full_workspace_authority=unrestricted,
                    full_data_authority=True, side_effect_level=SideEffectLevel.EXTERNAL)


def executor(tool):
    registry = ToolRegistry()
    registry.register_tool(tool)
    return ToolExecutor(registry)


def context(workspace, policy):
    return ToolContext(session_id="root", workspace_root=str(workspace), tool_view=policy,
                       safety_review_approved=True)


@pytest.mark.parametrize("target_kind", ["inside", "outside", "no_workspace", "symlink"])
def test_root_filesystem_execution_enforces_frozen_paths_despite_approval(tmp_path, target_kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    target = inside if target_kind in ("inside", "no_workspace") else outside
    if target_kind == "symlink":
        target = workspace / "escape.txt"
        target.symlink_to(outside)
    policy = view("filesystem.read_file", paths=() if target_kind == "no_workspace" else (workspace,))
    ctx = context(workspace, policy)
    ctx.approved_paths = [str(target.resolve())]
    tool = ReadFileTool(FileAccessPolicy.from_workspace_roots([workspace]))
    result = executor(tool).execute(invocation_id="read", tool_name=tool.spec.name,
                                     tool_input={"path": str(target)}, context=ctx)
    if target_kind == "inside":
        assert result.status == "completed" and result.output["content"] == "inside"
    else:
        assert result.status == "rejected" and result.execution_started is False
        # Tool-level checks must independently reject the same approved path.
        direct = tool.invoke(invocation=ToolInvocation(invocation_id="direct", tool=tool.spec,
                             session_id="root", context_id="ctx", input={"path": str(target)}), context=ctx)
        assert direct.status == "failed" and "workspace scope" in direct.error


def test_unrestricted_root_still_uses_exact_path_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("approved", encoding="utf-8")
    tool = ReadFileTool(FileAccessPolicy.from_workspace_roots([workspace]))
    ctx = context(workspace, view("filesystem.read_file", unrestricted=True))
    ctx.approved_paths = [str(outside.resolve())]
    result = executor(tool).execute(invocation_id="approved", tool_name=tool.spec.name,
                                   tool_input={"path": str(outside)}, context=ctx)
    assert result.status == "completed" and result.output["content"] == "approved"


@pytest.mark.parametrize("paths", [False, True])
def test_scoped_root_bash_cannot_escape_through_arbitrary_command_operands(tmp_path, paths):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "must-not-exist"
    tool = BashRunTool(policy=BashAccessPolicy.from_workspace_roots([workspace]), session_manager=None)
    policy = view("bash.run", paths=(workspace,) if paths else ())
    ctx = context(workspace, policy)
    ctx.approved_paths = [str(tmp_path.resolve())]
    inputs = {"command": f"touch {marker}", "cwd": str(workspace)}
    result = executor(tool).execute(invocation_id="bash", tool_name="bash.run", tool_input=inputs, context=ctx)
    assert result.status == "rejected" and not marker.exists()
    direct = tool.invoke(invocation=ToolInvocation(invocation_id="direct", tool=tool.spec,
                         session_id="root", context_id="ctx", input=inputs), context=ctx)
    assert direct.status == "rejected" and not marker.exists()


def test_separate_effective_view_reaches_tool_implementation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "note.txt"
    inside.write_text("safe", encoding="utf-8")
    tool = ReadFileTool(FileAccessPolicy.from_workspace_roots([workspace]))
    policy = view("filesystem.read_file", paths=(workspace,))
    ctx = context(workspace, None)
    result = executor(tool).execute(invocation_id="read", tool_name=tool.spec.name,
                                   tool_input={"path": str(inside)}, context=ctx, tool_view=policy)
    assert result.status == "completed"
    assert ctx.tool_view is None


def test_root_shell_session_tools_do_not_expose_other_runs(tmp_path):
    manager = BashSessionManager()
    process = SimpleNamespace(poll=lambda: 0, terminate=lambda: None)
    manager._sessions["other"] = BashSession(session_id="other", command="echo other", cwd=tmp_path,
        workspace_root=tmp_path, read_only=True, process=process, started_at=0, owner_run_id="other-run")
    manager._sessions["own"] = BashSession(session_id="own", command="echo own", cwd=tmp_path,
        workspace_root=tmp_path, read_only=True, process=process, started_at=0, owner_run_id="session_policy:root")
    list_tool, read_tool = BashListSessionsTool(manager), BashReadSessionTool(manager)
    ctx = context(tmp_path, view("bash.list_sessions", "bash.read_session", paths=(tmp_path,)))
    try:
        listed = executor(list_tool).execute(invocation_id="list", tool_name=list_tool.spec.name,
                                            tool_input={}, context=ctx)
        assert listed.status == "completed"
        assert [item["session_id"] for item in listed.output["sessions"]] == ["own"]
        rejected = executor(read_tool).execute(invocation_id="other", tool_name=read_tool.spec.name,
                                                tool_input={"session_id": "other"}, context=ctx)
        assert rejected.status == "failed" and "not found" in rejected.error
        allowed = executor(read_tool).execute(invocation_id="own", tool_name=read_tool.spec.name,
                                              tool_input={"session_id": "own"}, context=ctx)
        assert allowed.status == "completed"
        ctx.run_id = "own-runtime"
        manager._sessions["own"].owner_run_id = ctx.run_id
        runtime_owned = list_tool.invoke(invocation=ToolInvocation(invocation_id="runtime-list",
            tool=list_tool.spec, session_id="root", context_id="ctx", input={}), context=ctx)
        assert [item["session_id"] for item in runtime_owned.output["sessions"]] == ["own"]
    finally:
        manager.close()



def test_root_workspace_policy_preserves_existing_source_and_account_authority(tmp_path):
    from tests.test_tool_scope_discovery_quality import Reader

    tool = Reader(scope_source_fields=("sources",), scope_account_fields=("accounts",))
    ctx = context(tmp_path, view("records.read"))
    result = executor(tool).execute(invocation_id="data", tool_name=tool.spec.name,
        tool_input={"sources": ["source-other"], "accounts": ["account-other"]}, context=ctx)
    assert result.status == "completed" and tool.invocations == 1
