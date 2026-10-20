from pathlib import Path

from app.core.context_driver import ToolView
from app.core.tools import ToolContext, ToolInvocation
from app.tool_packages.filesystem import FileAccessPolicy, ListDirectoryTool


def invoke(tool, root, **values):
    return tool.invoke(invocation=ToolInvocation(invocation_id="directory-test", tool=tool.spec,
                                                 session_id="test", context_id="test", input=values),
                       context=ToolContext(session_id="test", workspace_root=str(root)))


def test_shallow_pages_keep_names_and_do_not_follow_symlinks(tmp_path):
    (tmp_path / "a.txt").write_text("private body")
    (tmp_path / "b").mkdir()
    (tmp_path / "c-link").symlink_to(Path("/tmp"), target_is_directory=True)
    tool = ListDirectoryTool(FileAccessPolicy.from_workspace_roots([tmp_path]))
    first = invoke(tool, tmp_path, limit=2)
    assert first.status == "completed"
    assert first.output["entries"] == [{"name": "a.txt", "kind": "file"}, {"name": "b", "kind": "directory"}]
    second = invoke(tool, tmp_path, offset=first.output["next_offset"], limit=2,
                    expected_snapshot=first.output["snapshot_id"])
    assert second.output["entries"] == [{"name": "c-link", "kind": "symlink"}]
    assert second.output["has_more"] is False
    assert "private body" not in str(first.output)
    assert tool.spec.read_only is True


def test_changed_directory_invalidates_continuation(tmp_path):
    (tmp_path / "a").touch()
    tool = ListDirectoryTool(FileAccessPolicy.from_workspace_roots([tmp_path]))
    first = invoke(tool, tmp_path)
    (tmp_path / "b").touch()
    assert invoke(tool, tmp_path, offset=1, expected_snapshot=first.output["snapshot_id"]).status == "failed"
    assert invoke(tool, tmp_path, offset=1).status == "failed"


def test_external_path_still_requires_exact_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ListDirectoryTool(FileAccessPolicy.from_workspace_roots([workspace]))
    assert invoke(tool, workspace, path=str(tmp_path)).status == "failed"


def test_child_file_grant_does_not_grant_parent_listing(tmp_path):
    source = tmp_path / "source.txt"
    source.touch()
    tool = ListDirectoryTool(FileAccessPolicy.from_workspace_roots([tmp_path]))
    view = ToolView(snapshot_id="test", child_run_id="child", allowed_paths=(str(source),),
                    allowed_packages=("filesystem",), side_effect_level="read")
    invocation = ToolInvocation(invocation_id="child-list", tool=tool.spec, session_id="test",
                                context_id="test", input={"path": "."})
    context = ToolContext(session_id="test", workspace_root=str(tmp_path), tool_view=view)
    result = tool.invoke(invocation=invocation, context=context)
    assert result.status == "failed"
    assert "child ContextSnapshot" in result.error
    granted = context.model_copy(update={"tool_view": view.model_copy(update={"allowed_paths": (str(tmp_path),)})})
    assert tool.invoke(invocation=invocation, context=granted).status == "completed"


def test_large_listing_has_explicit_partial_scope(tmp_path, monkeypatch):
    class Entry:
        def __init__(self, index):
            self.name = str(index)
        def is_symlink(self):
            return False
        def is_dir(self, **_):
            return False
        def is_file(self, **_):
            return True
    class Scan:
        def __enter__(self):
            return iter(Entry(i) for i in range(5000))
        def __exit__(self, *_):
            return False
    monkeypatch.setattr("app.tool_packages.filesystem.os.scandir", lambda _: Scan())
    tool = ListDirectoryTool(FileAccessPolicy.from_workspace_roots([tmp_path]))
    result = invoke(tool, tmp_path)
    assert len(result.output["entries"]) == 100
    assert result.output["enumerated_entries"] == 2000
    assert result.output["enumeration_complete"] is False
    assert result.output["truncated"] is True
