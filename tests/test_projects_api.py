from __future__ import annotations

import asyncio
import json

import httpx

from app.api.main import create_app
from app.core.config import get_settings


def app_for(tmp_path, monkeypatch):
    root = tmp_path / "folders"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(root))
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    return create_app(), root


def request(app, method, path, *, host="127.0.0.1", **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 1)),
                                     base_url="http://test") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def test_empty_project_multiple_sessions_restart_and_rename(tmp_path, monkeypatch):
    app, root = app_for(tmp_path, monkeypatch)
    folder = root / "repo"
    folder.mkdir()
    project = request(app, "POST", "/projects", json={"name": "My project", "path": str(folder)}).json()
    pid = project["project_id"]
    assert project["session_count"] == 0
    assert request(app, "GET", f"/projects/{pid}/sessions").json()["sessions"] == []
    ids = []
    for title in ("Planning", "Coding"):
        created = request(app, "POST", "/sessions", json={"title": title, "project_id": pid})
        assert created.status_code == 200
        session = created.json()["session"]
        ids.append(session["session_id"])
        assert session["project_id"] == pid
        assert session["workspace"]["backend_path"] == str(folder)
    assert len(set(ids)) == 2
    assert request(app, "GET", f"/projects/{pid}").json()["session_count"] == 2
    renamed = request(app, "PATCH", f"/projects/{pid}", json={"name": "New name", "expected_revision": 1})
    assert renamed.status_code == 200 and renamed.json()["revision"] == 2
    assert folder.exists() and not (root / "New name").exists()
    assert request(app, "PATCH", f"/projects/{pid}", json={"name": "Stale", "expected_revision": 1}).status_code == 409
    repeat = request(app, "POST", "/projects", json={"path": str(folder / "."), "name": "Ignored"}).json()
    assert repeat["project_id"] == pid and repeat["name"] == "New name"
    restarted, _ = app_for(tmp_path, monkeypatch)
    assert request(restarted, "GET", "/projects").json()["projects"][0]["project_id"] == pid
    page = request(restarted, "GET", f"/sessions?project_id={pid}&limit=1").json()["sessions"]
    assert len(page) == 1
    assert len(request(restarted, "GET", f"/projects/{pid}/sessions").json()["sessions"]) == 2


def test_project_isolation_rebinding_deleted_sessions_and_legacy_backfill(tmp_path, monkeypatch):
    app, root = app_for(tmp_path, monkeypatch)
    folders = [root / "a", root / "b"]
    for folder in folders:
        folder.mkdir()
    session = request(app, "POST", "/sessions", json={"title": "Legacy"}).json()["session"]
    sid = session["session_id"]
    # Simulate the old schema's workspace-only association.
    with app.state.runtime._conn() as conn:
        conn.execute("UPDATE agent_sessions SET metadata=? WHERE session_id=?", (json.dumps({"workspace": {
            "path": str(folders[0]), "backend_path": str(folders[0]), "platform": "linux",
        }}), sid))
    restarted, _ = app_for(tmp_path, monkeypatch)
    a = request(restarted, "GET", "/projects").json()["projects"][0]
    assert a["session_count"] == 1
    assert request(restarted, "GET", f"/sessions?project_id={a['project_id']}").json()["sessions"][0]["project_id"] == a["project_id"]
    assert request(restarted, "PUT", f"/sessions/{sid}/workspace", json={"path": str(folders[1]), "platform": "linux"}).status_code == 200
    b = restarted.state.runtime.memory_service.resolve_project(folders[1], create=False)
    assert b != a["project_id"]
    assert request(restarted, "GET", f"/projects/{a['project_id']}").json()["session_count"] == 0
    assert request(restarted, "GET", f"/sessions?project_id={a['project_id']}").json()["sessions"] == []
    assert request(restarted, "GET", f"/projects/{b}/sessions").json()["sessions"][0]["session_id"] == sid
    assert request(restarted, "DELETE", f"/sessions/{sid}").status_code == 204
    assert request(restarted, "GET", f"/projects/{b}").json()["session_count"] == 0
    assert request(restarted, "GET", f"/projects/{b}/sessions").json()["sessions"] == []


def test_project_validation_pagination_auth_and_no_orphan_session(tmp_path, monkeypatch):
    app, root = app_for(tmp_path, monkeypatch)
    assert request(app, "POST", "/projects", json={"path": str(root / "missing")}).status_code == 422
    outside = tmp_path / "outside"
    outside.mkdir()
    assert request(app, "POST", "/projects", json={"path": str(outside)}).status_code == 422
    assert request(app, "POST", "/projects", json={"path": str(root), "name": "  "}).status_code == 422
    assert request(app, "GET", "/projects", host="192.0.2.1").status_code == 403
    assert request(app, "POST", "/sessions", json={"project_id": "missing"}).status_code == 404
    assert request(app, "GET", "/sessions").json()["sessions"] == []
    for name in ("100%", "Alpha", "Beta"):
        path = root / name
        path.mkdir()
        request(app, "POST", "/projects", json={"path": str(path), "name": name})
    page = request(app, "GET", "/projects?limit=2").json()
    assert page["next_offset"] == 2
    assert len(request(app, "GET", "/projects?limit=2&offset=2").json()["projects"]) == 1
    assert len(request(app, "GET", "/projects?q=%25").json()["projects"]) == 1
    monkeypatch.setenv("LKA_MEMORY_API_TOKEN", "projects-token")
    assert request(app, "GET", "/projects").status_code == 401
    assert request(app, "GET", "/projects", headers={"Authorization": "Bearer projects-token"}).status_code == 200
