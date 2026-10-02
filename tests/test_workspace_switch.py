from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.main import create_app
from app.api.routes.sessions import get_session, set_session_workspace
from app.api.schemas import SessionWorkspaceUpdateRequest
from app.core.config import get_settings
from app.core.tools import ToolContext


def test_windows_session_workspace_switch_changes_tool_root_and_persists(tmp_path, monkeypatch):
    mount = tmp_path / "mount"
    first = mount / "c" / "Users" / "test" / "first"
    second = mount / "c" / "Users" / "test" / "second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "marker.txt").write_text("first", encoding="utf-8")
    (second / "marker.txt").write_text("second", encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("LKA_WSL_WINDOWS_MOUNT_ROOT", str(mount))
    monkeypatch.delenv("LKA_WORKSPACE_ROOTS", raising=False)
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    session_id = app.state.runtime.create_session(title="workspace switch test").session.session_id

    def bind(path: str):
        return set_session_workspace(
            session_id,
            SessionWorkspaceUpdateRequest(path=path, platform="windows"),
            request,
        ).workspace

    original = bind("C:/Users/test/first")
    assert original.backend_path == first.as_posix()
    switched = bind("C:/Users/test/second")
    assert switched.backend_path == second.as_posix()
    assert get_session(session_id, request).session.workspace == switched

    context = ToolContext(session_id=session_id, workspace_root=switched.backend_path)
    pwd = app.state.runtime.tool_executor.execute(
        invocation_id="switched_pwd",
        tool_name="bash.run",
        tool_input={"command": "pwd"},
        context=context,
    )
    assert pwd.status == "completed"
    assert pwd.output["stdout"].strip() == second.as_posix()
    content = app.state.runtime.tool_executor.execute(
        invocation_id="switched_read",
        tool_name="filesystem.read_file",
        tool_input={"path": "marker.txt"},
        context=context,
    )
    assert content.status == "completed"
    assert content.output["content"] == "second"

    with pytest.raises(HTTPException) as error:
        bind("C:/Users/test/missing")
    assert error.value.status_code == 422
    assert get_session(session_id, request).session.workspace == switched

    get_settings.cache_clear()
    restarted = create_app()
    assert get_session(session_id, SimpleNamespace(app=restarted)).session.workspace == switched
