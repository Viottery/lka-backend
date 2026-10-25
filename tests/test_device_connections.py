"""Direct node grants never rely on a browser knowing the node credential."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.api.account_auth import AccountAuthMiddleware
from app.api.device_connection_auth import DeviceConnectionAuthMiddleware, ordinary_session_allowed
from app.api.routes.device_connections import router
from app.core.sessions import SessionService
from app.domains.device_connections import DeviceConnectionConfig, DeviceConnectionService
from app.domains.devices import DeviceConfig, DeviceService


@pytest.fixture
def connections(tmp_path):
    clock = [1000.0]
    hub_factory = lambda: sqlite3.connect(tmp_path / "hub.sqlite3")
    def node_factory():
        conn = sqlite3.connect(tmp_path / "node.sqlite3")
        conn.row_factory = sqlite3.Row
        return conn
    devices = DeviceService(hub_factory, DeviceConfig(enabled=True))
    device = devices.enroll({"device_id": "pc", "name": "PC", "platform": "linux",
                            "environment_id": "linux", "workspace_ids": ["project"],
                            "scopes": ["heartbeat", "connections.poll"],
                            "advertised_url": "https://pc.test.ts.net"})
    hub = DeviceConnectionService(hub_factory, DeviceConnectionConfig(enabled=True),
                                  device_service=devices, clock=lambda: clock[0])
    cfg = DeviceConnectionConfig(enabled=True, node_id="pc", workspace_roots={"project": str(tmp_path)},
                                 allowed_origins=["https://pc.test.ts.net"], session_ttl_seconds=300)
    node = DeviceConnectionService(node_factory, cfg, clock=lambda: clock[0])
    return SimpleNamespace(hub=hub, node=node, devices=devices, device=device, clock=clock,
                           hub_factory=hub_factory, node_factory=node_factory, root=tmp_path)


def install(fixture):
    grant = fixture.hub.issue_grant("admin", "pc")
    page = fixture.hub.pending_grants(fixture.devices.authenticate(fixture.device["token"]))
    fixture.node.install_grants(page["items"])
    return grant, page


def test_opaque_delivery_persistence_offline_and_replay(connections):
    f = connections
    grant, page = install(f)
    assert f.device["token"] not in json.dumps(grant)
    assert grant["grant"] not in json.dumps(page)
    with f.node_factory() as conn:
        assert grant["grant"] not in str(conn.execute("SELECT * FROM device_connection_grants").fetchall())
    session = f.node.redeem(grant["grant"])
    f.node.install_grants(page["items"])
    with pytest.raises(PermissionError):
        f.node.redeem(grant["grant"])
    # Independent SQLite state survives process reconstruction and a hub outage.
    persisted = DeviceConnectionService(f.node_factory, f.node.config, clock=lambda: f.clock[0])
    f.devices.authenticate = lambda *_: (_ for _ in ()).throw(ConnectionError("hub offline"))
    principal = persisted.authenticate(session["token"])
    assert principal["node_id"] == "pc"
    assert persisted.check_csrf(principal, session["csrf_token"])
    f.clock[0] += 121
    assert persisted.authenticate(session["token"])
    f.clock[0] += 180
    assert persisted.authenticate(session["token"]) is None


def test_cross_node_tampering_expiry_and_local_revocation(connections):
    f = connections
    grant, page = install(f)
    other = DeviceConnectionService(lambda: sqlite3.connect(f.root / "other.sqlite3"),
                                    f.node.config.model_copy(update={"node_id": "other"}),
                                    clock=lambda: f.clock[0])
    with pytest.raises(PermissionError, match="audience"):
        other.install_grants(page["items"])
    for token in (grant["grant"] + "x", f.device["token"], "x" * 48):
        with pytest.raises(PermissionError):
            f.node.redeem(token)
    f.clock[0] += 121
    with pytest.raises(PermissionError):
        f.node.redeem(grant["grant"])
    with pytest.raises(ValueError, match="expired"):
        f.node.install_grants(page["items"])
    grant, _ = install(f)
    session = f.node.redeem(grant["grant"])
    f.node.revoke(session["session_id"])
    assert f.node.authenticate(session["token"]) is None


def test_registry_approvals_and_device_credential_fences(connections):
    f = connections
    with pytest.raises(PermissionError):
        f.hub.issue_grant("admin", "pc", ["unapproved"])
    grant = f.hub.issue_grant("admin", "pc")
    principal = f.devices.authenticate(f.device["token"])
    f.devices.rotate("pc", 1)
    with pytest.raises(PermissionError):
        f.hub.pending_grants(principal)
    with f.hub_factory() as conn:
        assert grant["grant"] not in str(conn.execute("SELECT * FROM device_connection_grants").fetchall())
    page = f.hub.pending_grants(f.devices.authenticate(f.devices.rotate("pc", 2)["token"]))
    assert not page["items"]
    bad = {"grant_id": "f" * 32, "token_hash": "e" * 64, "node_id": "pc", "user_id": "admin",
           "workspace_ids": ["unmapped"], "expires_at": f.clock[0] + 100}
    with pytest.raises(PermissionError, match="mapped"):
        f.node.install_grants([bad])


def test_connection_poll_requires_its_explicit_scope(connections):
    f = connections
    other = f.devices.enroll({"device_id": "unapproved-poller", "name": "PC", "platform": "linux",
                              "environment_id": "linux", "scopes": ["heartbeat"]})
    with pytest.raises(PermissionError):
        f.hub.pending_grants(f.devices.authenticate(other["token"]))


def test_direct_session_policy_is_persisted_and_cannot_widen(connections):
    f = connections
    grant, _ = install(f)
    token = f.node.redeem(grant["grant"])["token"]
    principal = f.node.authenticate(token)
    assert f.node.session_policy("ordinary") is None
    policy = f.node.bind_session_policy("ordinary", principal)
    assert policy == {"allow_default_workspace": False, "allowed_paths": [str(f.root)],
                      "safety_review_mode": "manual"}
    f.node.bind_session_policy("ordinary", {**principal, "workspace_ids": []})
    assert f.node.bind_session_policy("ordinary", principal)["allowed_paths"] == []
    restarted = DeviceConnectionService(f.node_factory, f.node.config)
    assert restarted.session_policy("ordinary")["allowed_paths"] == []


def node_app(f):
    with f.node_factory() as conn:
        conn.executescript("""
            CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY,title TEXT,status TEXT,kind TEXT,
                creation_source TEXT,creator_session_id TEXT,metadata TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE managed_session_links(manager_session_id TEXT,task_session_id TEXT);
        """)
        for identity, kind, source, creator, metadata in (
            ("ordinary", "ordinary", "user", None, {}),
            ("manager", "manager", "user", None, {}),
            ("assistant", "ordinary", "assistant", "manager", {}),
            ("linked", "ordinary", "user", None, {}),
            ("cloud", "ordinary", "user", None, {"remote_execution": {"hub_id": "hub"}}),
            ("child", "ordinary", "user", "ordinary", {}),
            ("outside", "ordinary", "user", None,
             {"workspace": {"backend_path": "/unapproved", "path": "/unapproved", "platform": "linux"}}),
        ):
            conn.execute("INSERT INTO agent_sessions VALUES(?,?,?,?,?,?,?,?,?)",
                         (identity, identity, "active", kind, source, creator, json.dumps(metadata),
                          "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
        conn.execute("INSERT INTO managed_session_links VALUES('manager','linked')")
    app = FastAPI()
    app.state.device_connection_service = f.node
    def get_run(identity):
        policy = {"allow_default_workspace": False, "safety_review_mode": "manual",
                  "allowed_paths": ["/" if identity == "wide" else str(f.root)]}
        return SimpleNamespace(session_id="manager" if identity == "manager" else "ordinary",
                               parent_run_id="manager" if identity == "descendant" else None,
                               metadata={} if identity == "legacy" else (
                                   {"remote_execution": {}} if identity == "cloudrun"
                                   else {"session_policy": policy}))
    app.state.runtime = SimpleNamespace(session_service=SessionService(f.node_factory),
                                        settings=SimpleNamespace(parsed_workspace_roots=lambda: [f.root]),
                                        agent_run_manager=SimpleNamespace(get_run=get_run,
                                            get_safety_review=lambda identity: SimpleNamespace(run_id=identity)))
    app.add_middleware(AccountAuthMiddleware)
    app.add_middleware(DeviceConnectionAuthMiddleware)
    app.include_router(router)

    @app.get("/sessions")
    def sessions():
        return {"sessions": app.state.runtime.session_service.list_sessions().model_dump()["sessions"]}

    @app.get("/sessions/{identity}")
    def session(identity):
        return {"session_id": identity}

    @app.post("/sessions/{identity}/workspace-id")
    def workspace_id(identity):
        return {"session_id": identity}

    @app.post("/agent/turn")
    async def turn(request: Request):
        return await request.json()

    @app.get("/agent/runs/{identity}")
    def run(identity):
        return {"run_id": identity}

    @app.post("/agent/runs/{identity}/resume")
    def resume(identity):
        return {"run_id": identity}

    @app.get("/agent/safety-reviews/{identity}")
    def review(identity):
        return {"review_id": identity}

    @app.post("/agent/safety-reviews/{identity}/decision")
    def decision(identity):
        return {"review_id": identity}

    @app.get("/local/workspaces/{identity}/entries")
    def entries(identity):
        return {"workspace_id": identity}

    @app.get("/managers")
    def managers():
        return {"secret": True}

    return app


def test_browser_cookie_origin_csrf_and_offline_ordinary_scope(connections):
    f = connections
    app = node_app(f)
    grant, _ = install(f)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app, client=("100.80.1.2", 5000)),
                               base_url="https://pc.test.ts.net") as client:
            assert (await client.get("/sessions")).status_code == 401
            assert (await client.post("/connections/redeem", json={"grant": grant["grant"]})).status_code == 403
            login = await client.post("/connections/redeem", json={"grant": grant["grant"]},
                                      headers={"Origin": "https://pc.test.ts.net"})
            assert login.status_code == 200
            assert "token" not in login.json()
            cookie = login.headers["set-cookie"].lower()
            assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
            f.hub = None  # All following requests are node-only operations.
            assert (await client.get("/sessions/ordinary")).status_code == 200
            sessions = (await client.get("/sessions")).json()["sessions"]
            assert [item["session_id"] for item in sessions] == ["ordinary"]
            for identity in ("manager", "assistant", "linked", "cloud", "child", "outside"):
                assert (await client.get("/sessions/" + identity)).status_code == 403
            assert (await client.get("/agent/runs/manager")).status_code == 403
            assert (await client.get("/managers")).status_code == 403
            assert (await client.get("/local/workspaces/project/entries")).status_code == 200
            assert (await client.get("/local/workspaces/unapproved/entries")).status_code == 403
            payload = {"session_id": "ordinary", "user_input": "hello", "safety_review_mode": "skip"}
            assert (await client.post("/agent/turn", json=payload)).status_code == 403
            csrf = login.json()["csrf_token"]
            assert (await client.post("/agent/turn", json=payload, headers={
                "Origin": "https://evil.test", "X-CSRF-Token": csrf})).status_code == 403
            headers = {"Origin": "https://pc.test.ts.net", "X-CSRF-Token": csrf}
            response = await client.post("/agent/turn", json=payload, headers=headers)
            assert response.status_code == 200
            assert response.json()["safety_review_mode"] == "manual"
            assert f.node.session_policy("ordinary")["allowed_paths"] == [str(f.root)]
            assert (await client.post("/connections/logout", headers=headers)).status_code == 204
            assert (await client.get("/sessions/ordinary")).status_code == 401

    asyncio.run(asyncio.wait_for(exercise(), timeout=8))


def test_native_transport_and_loopback_do_not_bypass_hub_boundary(connections):
    f = connections
    app = node_app(f)
    grant, _ = install(f)
    session = f.node.redeem(grant["grant"])

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app, client=("127.0.0.1", 5000)),
                               base_url="https://pc.test.ts.net") as client:
            assert (await client.get("/sessions/ordinary")).status_code == 200
            assert (await client.get("/sessions/manager")).status_code == 403
            assert (await client.get("/sessions/cloud")).status_code == 403
            headers = {"X-LKA-Connection-Token": session["token"]}
            assert (await client.get("/sessions/ordinary", headers=headers)).status_code == 403
            headers["X-LKA-Client"] = "native"
            assert (await client.get("/sessions/ordinary", headers=headers)).status_code == 200
            headers["Origin"] = "https://pc.test.ts.net"
            assert (await client.get("/sessions/ordinary", headers=headers)).status_code == 403
            assert (await client.get("/sessions/ordinary", headers={"Origin": "https://evil.test"})).status_code == 401
        async with AsyncClient(transport=ASGITransport(app=app, client=("100.80.1.2", 5000)),
                               base_url="http://pc.test.ts.net") as client:
            assert (await client.get("/sessions/ordinary", headers={
                "X-LKA-Connection-Token": session["token"], "X-LKA-Client": "native"})).status_code == 403

    asyncio.run(asyncio.wait_for(exercise(), timeout=8))
    assert ordinary_session_allowed(app.state.runtime, "ordinary")
    assert not ordinary_session_allowed(app.state.runtime, "linked")


def test_reviews_and_resumed_runs_keep_frozen_scope(connections):
    f = connections
    app = node_app(f)
    grant, _ = install(f)
    session = f.node.redeem(grant["grant"])
    headers = {"X-LKA-Connection-Token": session["token"], "X-LKA-Client": "native"}

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app, client=("100.80.1.2", 5000)),
                               base_url="https://pc.test.ts.net", headers=headers) as client:
            assert (await client.get("/agent/safety-reviews")).status_code == 403
            assert (await client.get("/agent/safety-reviews/ordinary")).status_code == 200
            assert (await client.post("/agent/safety-reviews/ordinary/decision", json={})).status_code == 200
            assert (await client.post("/agent/runs/ordinary/resume", json={})).status_code == 200
            for identity in ("manager", "descendant", "cloudrun"):
                assert (await client.get("/agent/safety-reviews/" + identity)).status_code == 403
                assert (await client.post(f"/agent/safety-reviews/{identity}/decision", json={})).status_code == 403
            for identity in ("legacy", "wide"):
                assert (await client.post(f"/agent/runs/{identity}/resume", json={})).status_code == 403
                assert (await client.post(f"/agent/safety-reviews/{identity}/decision", json={})).status_code == 403

    asyncio.run(asyncio.wait_for(exercise(), timeout=8))


def test_product_node_gzip_filters_json_and_preserves_stream_revocation(connections, monkeypatch):
    """Exercise the product middleware order, real session routes and SQLite."""
    from starlette.responses import StreamingResponse

    from app.api.main import create_app
    from app.core.config import get_settings

    f = connections
    monkeypatch.setenv("LKA_DATA_DIR", str(f.root / "runtime"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(f.root / "missing.toml"))
    get_settings.cache_clear()
    try:
        app = create_app()
        app.state.device_connection_service = f.node
        store = app.state.runtime.session_service
        ordinary = store.create_session(title="Visible", metadata={"padding": "ordinary " * 1000}).session
        manager = store.create_session(title="Manager secret").session
        with store._conn_factory() as conn:
            conn.execute("UPDATE agent_sessions SET kind='manager' WHERE session_id=?", (manager.session_id,))
        hidden = [manager.session_id]
        hidden.append(store.create_session(title="Assistant secret", creation_source="assistant",
                                            creator_session_id=manager.session_id).session.session_id)
        hidden.append(store.create_session(title="Cloud secret", metadata={"remote_execution": {"hub_id": "hub"}}).session.session_id)
        hidden.append(store.create_session(title="Outside secret", metadata={"workspace": {
            "path": "/unapproved", "backend_path": "/unapproved", "platform": "linux"}}).session.session_id)
        grant, _ = install(f)
        connection = f.node.redeem(grant["grant"])
        first = "data: " + "synthetic " * 1000 + "\n\n"
        produced = []

        @app.get(f"/sessions/{ordinary.session_id}/compression-stream")
        async def stream():
            async def events():
                produced.append("first")
                yield first
                f.node.revoke(connection["session_id"])
                produced.append("revoked")
                yield "data: must not be delivered\n\n"
                produced.append("must not continue")
            return StreamingResponse(events(), media_type="text/event-stream")

        async def exercise():
            headers = {"Accept-Encoding": "gzip", "X-LKA-Client": "native",
                       "X-LKA-Connection-Token": connection["token"]}
            async with AsyncClient(transport=ASGITransport(app=app, client=("100.80.1.2", 5000)),
                                   base_url="https://pc.test.ts.net", headers=headers) as client:
                listed = await client.get("/sessions")
                assert listed.status_code == 200
                assert listed.headers["content-encoding"] == "gzip"
                assert [item["session_id"] for item in listed.json()["sessions"]] == [ordinary.session_id]
                assert all(identity not in listed.text for identity in hidden)
                for identity in hidden:
                    assert (await client.get(f"/sessions/{identity}")).status_code == 403
                created = await client.post("/sessions", json={"title": "Large creation",
                    "metadata": {"padding": "synthetic " * 1000}, "initial_message": "synthetic " * 2000})
                assert created.status_code == 200
                assert created.headers["content-encoding"] == "gzip"
                created_id = created.json()["session"]["session_id"]
                assert created.json()["messages"][0]["content"] == "synthetic " * 2000
                assert f.node.session_policy(created_id)["safety_review_mode"] == "manual"
                response = await client.get(f"/sessions/{ordinary.session_id}/compression-stream")
                assert response.status_code == 200
                assert "content-encoding" not in response.headers
                assert response.text == first
                assert produced == ["first", "revoked"]
                assert (await client.get("/sessions")).status_code == 401

        asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    finally:
        get_settings.cache_clear()
