from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.main import create_app
from app.api.routes.sessions import delete_session
from app.core.config import get_settings


def runtime(tmp_path, monkeypatch):
    config = tmp_path / "local.toml"
    config.write_text("[memory]\nenabled = false\nbackground_enabled = false\n")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    return create_app().state.runtime


def test_empty_cleanup_soft_deletes_and_can_restore(tmp_path, monkeypatch):
    rt = runtime(tmp_path, monkeypatch)
    session = rt.session_service.ensure_session(session_id="blank", title="新会话")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=rt)))
    delete_session(session.session_id, request, only_if_empty=True,
                   expected_updated_at=session.updated_at)
    with rt._conn() as conn:
        assert conn.execute("SELECT status FROM agent_sessions WHERE session_id='blank'").fetchone()[0] == "deleted"
    assert rt.restore_session(session_id="blank")
    assert rt.session_service.get_session_or_none(session_id="blank") is not None


@pytest.mark.parametrize("change", ["message", "rename", "run", "missing_revision"])
def test_empty_cleanup_rejects_new_work_in_same_transaction(tmp_path, monkeypatch, change):
    rt = runtime(tmp_path, monkeypatch)
    session = rt.session_service.ensure_session(session_id="blank", title="新会话")
    revision = session.updated_at
    if change == "message":
        rt.session_service.append_message(session_id="blank", role="user", content="Keep my message.")
        # Even a fresh revision must not allow deleting a nonempty session.
        revision = rt.session_service.get_session_or_none(session_id="blank").updated_at
    elif change == "rename":
        rt.rename_session(session_id="blank", title="Keep my named session")
    elif change == "run":
        rt.agent_run_manager.create_run(session_id="blank", user_input="Queued work")
    elif change == "missing_revision":
        revision = None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=rt)))
    with pytest.raises(HTTPException) as exc:
        delete_session("blank", request, only_if_empty=True, expected_updated_at=revision)
    assert exc.value.status_code == 409
    assert rt.session_service.get_session_or_none(session_id="blank") is not None
