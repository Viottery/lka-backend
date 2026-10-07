from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.tools import ToolContext, ToolInvocation
from app.tool_packages.bash import BashAccessPolicy, BashRunTool
from app.tool_packages.filesystem import EditFileTool, FileAccessPolicy, ReadFileTool


def test_filesystem_review_request_is_canonical_and_does_not_read_file(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")
    tool = ReadFileTool(FileAccessPolicy.from_workspace_roots([workspace]))
    context = ToolContext(session_id="review")

    assert tool.workspace_access_request({"path": str(outside)}, context) == [str(outside.resolve())]
    assert tool.workspace_access_request({"path": "inside.txt"}, context) == []


def test_filesystem_requires_exact_invocation_approval(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    tool = ReadFileTool(FileAccessPolicy.from_workspace_roots([workspace]))
    invocation = ToolInvocation(invocation_id="read", tool=tool.spec, session_id="review", context_id="review",
                                input={"path": str(outside)})
    context = ToolContext(session_id="review")

    denied = tool.invoke(invocation=invocation, context=context)
    assert denied.status == "failed"
    assert "not approved for this invocation" in (denied.error or "")

    context.approved_paths = [str(outside.resolve())]
    context.safety_review_approved = True
    approved = tool.invoke(invocation=invocation, context=context)
    assert approved.status == "completed"
    assert approved.output["content"] == "secret"


def test_symlink_retarget_invalidates_review_request(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    link = workspace / "link.txt"
    link.symlink_to(first)
    tool = ReadFileTool(FileAccessPolicy.from_workspace_roots([workspace]))
    context = ToolContext(session_id="review", approved_paths=[str(first.resolve())], safety_review_approved=True)
    invocation = ToolInvocation(invocation_id="read", tool=tool.spec, session_id="review", context_id="review",
                                input={"path": str(link)})

    link.unlink()
    link.symlink_to(second)
    result = tool.invoke(invocation=invocation, context=context)
    assert result.status == "failed"
    assert "not approved for this invocation" in (result.error or "")


def test_filesystem_edit_requires_exact_grant_and_edits_approved_target(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    original = "before\n"
    outside.write_text(original, encoding="utf-8")
    tool = EditFileTool(FileAccessPolicy.from_workspace_roots([workspace]))
    invocation = ToolInvocation(
        invocation_id="edit", tool=tool.spec, session_id="review", context_id="review",
        input={"path": str(outside), "expected_sha256": hashlib.sha256(original.encode()).hexdigest(),
               "edits": [{"old_text": "before", "new_text": "after"}]},
    )
    denied = tool.invoke(invocation=invocation, context=ToolContext(session_id="review"))
    assert denied.status == "failed"
    assert outside.read_text(encoding="utf-8") == original

    context = ToolContext(session_id="review", safety_review_approved=True,
                          approved_paths=[str((tmp_path / "different.txt").resolve())])
    mismatch = tool.invoke(invocation=invocation, context=context)
    assert mismatch.status == "failed"
    assert outside.read_text(encoding="utf-8") == original

    context.approved_paths = [str(outside.resolve())]
    result = tool.invoke(invocation=invocation, context=context)
    assert result.status == "completed"
    assert outside.read_text(encoding="utf-8") == "after\n"


def test_bash_review_request_is_cwd_only_and_requires_exact_grant(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    tool = BashRunTool(policy=BashAccessPolicy.from_workspace_roots([workspace]), session_manager=None)
    context = ToolContext(session_id="review")
    assert tool.workspace_access_request({"cwd": str(outside), "command": "pwd"}, context) == [str(outside.resolve())]

    calls = []
    monkeypatch.setattr(tool, "_run_sync", lambda **kwargs: calls.append(kwargs) or {"cwd": str(kwargs["cwd"])})
    invocation = ToolInvocation(invocation_id="run", tool=tool.spec, session_id="review", context_id="review",
                                input={"cwd": str(outside), "command": "pwd"})
    denied = tool.invoke(invocation=invocation, context=context)
    assert denied.status == "failed"
    assert calls == []

    context.approved_paths = [str(outside.resolve())]
    context.safety_review_approved = True
    approved = tool.invoke(invocation=invocation, context=context)
    assert approved.status == "completed"
    assert calls and calls[-1]["cwd"] == outside.resolve()


def test_child_scope_remains_ceiling_for_external_review(tmp_path: Path):
    from app.core.context_driver import ToolView
    from app.core.multi_agent import SideEffectLevel

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")
    view = ToolView(snapshot_id="snap", child_run_id="child", allowed_paths=(str(workspace),),
                    allowed_packages=("filesystem",), allowed_tools=("filesystem.read_file",),
                    side_effect_level=SideEffectLevel.EXTERNAL)
    tool = ReadFileTool(FileAccessPolicy.from_workspace_roots([workspace]))
    context = ToolContext(session_id="child", tool_view=view)

    assert tool.workspace_access_request({"path": str(outside)}, context) == []
    context.approved_paths = [str(outside.resolve())]
    context.safety_review_approved = True
    result = tool.invoke(invocation=ToolInvocation(invocation_id="read", tool=tool.spec, session_id="child", context_id="child",
                                                    input={"path": str(outside)}), context=context)
    assert result.status == "failed"
    assert "child ContextSnapshot workspace scope" in (result.error or "")
