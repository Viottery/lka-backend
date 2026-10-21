"""Reserved session workspaces are created on the first write that lands in them."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.main import create_app
from app.api.routes.sessions import materialize_session_workspace, set_session_workspace
from app.api.schemas import SessionWorkspaceUpdateRequest
from app.core.config import get_settings
from app.core.tools import ToolContext


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("LKA_WORKSPACE_ROOTS", raising=False)
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _reserve(app, tmp_path, name="conv_reserved"):
    runtime = app.state.runtime
    session_id = runtime.create_session(title="reserve").session.session_id
    target = tmp_path / "Workspaces" / name
    workspace = set_session_workspace(
        session_id,
        SessionWorkspaceUpdateRequest(path=target.as_posix(), platform="linux", reserve=True),
        SimpleNamespace(app=app),
    ).workspace
    return runtime, session_id, target, workspace


def _write(runtime, session_id, root, tool_name, tool_input, invocation_id="write"):
    # Direct executor calls (no run_id) model an already approved safety review.
    context = ToolContext(session_id=session_id, workspace_root=root, safety_review_approved=True)
    return runtime.tool_executor.execute(
        invocation_id=invocation_id, tool_name=tool_name, tool_input=tool_input, context=context,
    )


def test_reserved_workspace_has_no_folder_and_no_project(app, tmp_path):
    runtime, session_id, target, workspace = _reserve(app, tmp_path)
    assert workspace.state == "reserved" and not target.exists()
    session = runtime.session_service.get_session_or_none(session_id=session_id)
    assert session.workspace.state == "reserved" and session.project_id is None
    assert runtime.project_service.list()["projects"] == []


def test_reading_a_reserved_workspace_creates_nothing(app, tmp_path):
    runtime, session_id, target, workspace = _reserve(app, tmp_path)
    context = ToolContext(session_id=session_id, workspace_root=workspace.backend_path)
    listing = runtime.tool_executor.execute(
        invocation_id="ls", tool_name="filesystem.list_directory", tool_input={}, context=context,
    )
    assert listing.status == "completed"
    assert listing.output["entries"] == [] and listing.output["workspace_state"] == "reserved"
    pwd = runtime.tool_executor.execute(
        invocation_id="pwd", tool_name="bash.run", tool_input={"command": "pwd"}, context=context,
    )
    assert pwd.status == "failed" and "not been created yet" in pwd.error
    assert not target.exists()
    assert runtime.session_service.get_session_or_none(session_id=session_id).workspace.state == "reserved"


def test_first_write_into_default_workspace_creates_folder_and_project(app, tmp_path):
    runtime, session_id, target, workspace = _reserve(app, tmp_path)
    result = _write(runtime, session_id, workspace.backend_path, "filesystem.create_file",
                    {"path": "reports/summary.md", "content": "# Summary\n"})
    assert result.status == "completed", result.error
    assert (target / "reports" / "summary.md").read_text(encoding="utf-8") == "# Summary\n"
    session = runtime.session_service.get_session_or_none(session_id=session_id)
    assert session.workspace.state == "created" and session.project_id
    assert [p["project_id"] for p in runtime.project_service.list()["projects"]] == [session.project_id]


def test_editing_an_existing_file_elsewhere_keeps_default_workspace_reserved(app, tmp_path):
    runtime, session_id, target, workspace = _reserve(app, tmp_path)
    existing = tmp_path / "elsewhere" / "notes.txt"
    existing.parent.mkdir()
    existing.write_text("alpha\n", encoding="utf-8")
    context = ToolContext(session_id=session_id, workspace_root=workspace.backend_path,
                          safety_review_approved=True, approved_paths=[str(existing)])
    read = runtime.tool_executor.execute(
        invocation_id="read", tool_name="filesystem.read_file", tool_input={"path": str(existing)}, context=context,
    )
    assert read.status == "completed", read.error
    edit = runtime.tool_executor.execute(
        invocation_id="edit", tool_name="filesystem.edit_file",
        tool_input={"path": str(existing), "expected_sha256": read.output["sha256"],
                    "edits": [{"old_text": "alpha", "new_text": "beta"}]},
        context=context,
    )
    assert edit.status == "completed", edit.error
    assert existing.read_text(encoding="utf-8") == "beta\n"
    assert not target.exists()
    assert runtime.session_service.get_session_or_none(session_id=session_id).workspace.state == "reserved"


def test_write_command_in_default_cwd_creates_workspace_first(app, tmp_path):
    runtime, session_id, target, workspace = _reserve(app, tmp_path)
    result = _write(runtime, session_id, workspace.backend_path, "bash.run",
                    {"command": "echo hello > out.txt"})
    assert result.status == "completed", result.error
    assert (target / "out.txt").read_text(encoding="utf-8").strip() == "hello"
    assert runtime.session_service.get_session_or_none(session_id=session_id).workspace.state == "created"


def test_explicit_materialize_route_is_idempotent(app, tmp_path):
    runtime, session_id, target, _workspace = _reserve(app, tmp_path)
    request = SimpleNamespace(app=app)
    first = materialize_session_workspace(session_id, request)
    second = materialize_session_workspace(session_id, request)
    assert first.workspace.state == second.workspace.state == "created"
    assert target.is_dir() and first.project_id == second.project_id


def test_without_reserve_a_missing_directory_is_still_rejected(app, tmp_path):
    runtime = app.state.runtime
    session_id = runtime.create_session(title="strict").session.session_id
    with pytest.raises(ValueError, match="does not exist"):
        runtime.set_session_workspace(session_id=session_id, path=(tmp_path / "missing").as_posix(), platform="linux")


def test_existing_directory_binds_as_created(app, tmp_path):
    runtime = app.state.runtime
    session_id = runtime.create_session(title="existing").session.session_id
    folder = tmp_path / "chosen"
    folder.mkdir()
    workspace = runtime.set_session_workspace(session_id=session_id, path=folder.as_posix(), platform="linux", reserve=True)
    assert workspace.state == "created"
    assert runtime.session_service.get_session_or_none(session_id=session_id).project_id


def test_file_panel_reports_reserved_workspace(app, tmp_path):
    from fastapi import HTTPException

    from app.api.routes.session_files import _get_workspace

    _runtime, session_id, _target, _workspace = _reserve(app, tmp_path)
    with pytest.raises(HTTPException) as caught:
        _get_workspace(SimpleNamespace(app=app), session_id)
    assert caught.value.status_code == 409 and caught.value.detail == "session_workspace_reserved"
