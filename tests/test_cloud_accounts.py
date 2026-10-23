"""Authentication contracts run against disposable databases without network/models."""
import asyncio
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from app.api.account_auth import AccountAuthMiddleware
from app.api.routes.accounts import router
from app.domains.accounts import AccountConfig, AccountError, AccountService

PASSWORD = "correct horse password"
ORIGIN = "https://testserver"


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "auth.sqlite3"
    return AccountService(lambda: sqlite3.connect(path), AccountConfig(
        enabled=True, allowed_origins=[ORIGIN], allow_bearer_sessions=True,
    ))


@pytest.fixture
def admin(service):
    return service.create_admin("owner", PASSWORD)


def application(service):
    app = FastAPI()
    app.state.account_service = service
    app.state.device_service = None
    app.include_router(router)
    app.add_middleware(AccountAuthMiddleware)

    @app.get("/secret")
    def secret():
        return {"private": "dataset"}

    @app.post("/write")
    def write():
        return {"ok": True}

    @app.get("/node/ping")
    def node(request: Request):
        if "ping" not in request.state.device["scopes"]:
            from fastapi import HTTPException
            raise HTTPException(403, "Scope denied")
        return {"node_id": request.state.device["node_id"]}

    return app


def client(app, base_url=ORIGIN):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


async def login(client, **kwargs):
    return await client.post("/auth/login", json={"username": "owner", "password": PASSWORD},
                             headers={"Origin": ORIGIN}, **kwargs)


def test_cookie_login_csrf_logout(service, admin):
    async def scenario():
        async with client(application(service)) as web:
            assert (await web.get("/secret")).status_code == 401
            assert (await web.get("/docs")).status_code == 401
            assert (await web.get("/openapi.json")).status_code == 401
            health = await web.get("/health")
            assert health.json() == {"status": "ok", "service": "local-knowledge-agent-os"}
            result = await login(web)
            assert result.status_code == 200
            assert "token" not in result.json()
            cookie = result.headers["set-cookie"].lower()
            assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
            assert (await web.get("/secret")).status_code == 200
            assert (await web.post("/write")).status_code == 403
            csrf = result.json()["csrf_token"]
            valid = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
            assert (await web.post("/write", headers={**valid, "Origin": "https://evil"})).status_code == 403
            assert (await web.post("/write", headers=valid)).status_code == 200
            assert (await web.post("/auth/logout", headers=valid)).status_code == 204
            assert (await web.get("/secret")).status_code == 401
    asyncio.run(scenario())


def test_native_bearer_is_opt_in_and_revoked(service, admin):
    async def scenario():
        app = application(service)
        async with client(app) as web:
            response = await web.post("/auth/login", headers={"X-LKA-Client": "native"},
                                      json={"username": "owner", "password": PASSWORD})
            assert "set-cookie" not in response.headers
            token = response.json()["token"]
            headers = {"Authorization": "Bearer " + token}
            assert (await web.post("/write", headers=headers)).status_code == 200
            assert (await web.get("/secret", headers={**headers, "Origin": ORIGIN})).status_code == 401
            service.config.allow_bearer_sessions = False
            assert (await web.get("/secret", headers=headers)).status_code == 401
            service.config.allow_bearer_sessions = True
            assert (await web.post("/auth/logout", headers=headers)).status_code == 204
            assert (await web.get("/secret", headers=headers)).status_code == 401
    asyncio.run(scenario())


def test_password_change_revokes_every_session(service, admin):
    async def scenario():
        async with client(application(service)) as web:
            response = await login(web)
            second = service.login("owner", PASSWORD, "another-client")
            changed = await web.post("/auth/password", headers={"Origin": ORIGIN,
                "X-CSRF-Token": response.json()["csrf_token"]},
                json={"current_password": PASSWORD, "new_password": "replacement password"})
            assert changed.status_code == 204
            assert service.authenticate(second["token"]) is None
            assert (await web.get("/auth/me")).status_code == 401
            assert (await login(web)).status_code == 401
            assert service.login("owner", "replacement password", "new-client")["account"]["user_id"] == admin["user_id"]
    asyncio.run(scenario())


def test_session_management_account_admin_and_last_admin(service, admin):
    async def scenario():
        async with client(application(service)) as web:
            result = await login(web)
            valid = {"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]}
            assert (await web.patch(f"/auth/accounts/{admin['user_id']}", headers=valid,
                                    json={"disabled": True})).status_code == 409
            coadmin = await web.post("/auth/accounts", headers=valid,
                                    json={"username": "coadmin", "password": PASSWORD})
            assert coadmin.status_code == 201
            account_id = coadmin.json()["user_id"]
            assert coadmin.json()["is_admin"] is True
            assert len((await web.get("/auth/accounts")).json()["items"]) == 2
            other = service.login("owner", PASSWORD, "other")
            sessions = (await web.get("/auth/sessions")).json()["items"]
            assert len(sessions) == 2 and all("token" not in item for item in sessions)
            assert (await web.delete("/auth/sessions/" + other["session_id"], headers=valid)).status_code == 204
            assert service.authenticate(other["token"]) is None
            before = service.login("coadmin", PASSWORD, "coadmin")
            assert (await web.post(f"/auth/accounts/{account_id}/password", headers=valid,
                                   json={"new_password": "reset coadmin password"})).status_code == 204
            assert service.authenticate(before["token"]) is None
            assert (await web.patch(f"/auth/accounts/{account_id}", headers=valid,
                                    json={"disabled": True})).status_code == 204
            assert (await web.patch(f"/auth/accounts/{admin['user_id']}", headers=valid,
                                    json={"disabled": True})).status_code == 409
    asyncio.run(scenario())


def test_throttling_persists_and_unknown_user_generic(service, admin):
    for name in ("owner", "unknown"):
        with pytest.raises(AccountError, match="Invalid username or password") as exc:
            service.login(name, "bad password", "source")
        assert exc.value.status_code == 401
    for _ in range(service.config.login_max_attempts - 1):
        with pytest.raises(AccountError):
            service.login("owner", "bad password", "source")
    restarted = AccountService(service._conn_factory, service.config)
    with pytest.raises(AccountError) as exc:
        restarted.login("owner", PASSWORD, "source")
    assert exc.value.status_code == 429


def test_password_and_session_secrets_are_hashed(service, admin):
    result = service.login("owner", PASSWORD, "source")
    with service._db() as conn:
        stored = conn.execute("SELECT password_hash FROM auth_accounts").fetchone()[0]
        session = dict(conn.execute("SELECT * FROM auth_sessions").fetchone())
    assert stored.startswith("pbkdf2_sha256$600000$")
    assert PASSWORD not in stored and result["token"] not in session.values()
    assert result["csrf_token"] not in session.values()
    with pytest.raises(AccountError):
        service.create_admin("second", PASSWORD)
    with pytest.raises(AccountError):
        service.create_admin("bad name", PASSWORD, actor_user_id=admin["user_id"])
    with pytest.raises(AccountError):
        service.create_admin("short", "short", actor_user_id=admin["user_id"])


def test_https_origins_and_disabled_legacy(service, admin):
    async def scenario():
        async with client(application(service), "http://testserver") as web:
            assert (await web.get("/secret")).status_code == 403
        async with client(application(service)) as web:
            response = await web.post("/auth/login", json={"username": "owner", "password": PASSWORD})
            assert response.status_code == 403
        for disabled in (None, AccountService(service._conn_factory, AccountConfig())):
            async with client(application(disabled)) as web:
                assert (await web.get("/secret")).status_code == 200
                assert (await web.get("/auth/me")).status_code == 404
                assert (await web.get("/node/ping")).status_code == 401
    asyncio.run(scenario())


def test_node_credentials_never_authorize_account_apis(service, admin):
    class Devices:
        config = SimpleNamespace(enabled=True)

        def authenticate(self, token):
            return {"node_id": "n1", "scopes": ["ping"]} if token == "device-only-token" else None

    async def scenario():
        for account_service in (None, service):
            app = application(account_service)
            app.state.device_service = Devices()
            async with client(app) as web:
                assert (await web.get("/node/ping")).status_code == 401
                headers = {"Authorization": "Bearer device-only-token"}
                assert (await web.get("/node/ping", headers=headers)).json() == {"node_id": "n1"}
                if account_service:
                    assert (await web.get("/secret", headers=headers)).status_code == 401
                    account = service.login("owner", PASSWORD, "native")
                    assert (await web.get("/node/ping", headers={"Authorization": "Bearer " + account["token"]})).status_code == 401
    asyncio.run(scenario())


def test_revoked_sse_stops_before_next_chunk(service, admin):
    async def scenario():
        app = application(service)
        issued = service.login("owner", PASSWORD, "stream")

        @app.get("/stream")
        async def stream():
            async def chunks():
                yield b"data: visible\n\n"
                service.revoke_session(admin["user_id"], issued["session_id"])
                yield b"data: secret after revocation\n\n"
            return StreamingResponse(chunks(), media_type="text/event-stream")

        async with client(app) as web:
            response = await web.get("/stream", headers={"Authorization": "Bearer " + issued["token"]})
            assert response.status_code == 200
            assert response.content == b"data: visible\n\n"
    asyncio.run(scenario())


def test_expiration_and_disabled_accounts_revoke(service, admin):
    token = service.login("owner", PASSWORD, "expiry")["token"]
    with service._db(write=True) as conn:
        conn.execute("UPDATE auth_sessions SET expires_at=0")
    assert service.authenticate(token) is None
    coadmin = service.create_admin("coadmin", PASSWORD, actor_user_id=admin["user_id"])
    token = service.login("coadmin", PASSWORD, "disable")["token"]
    service.set_disabled(admin["user_id"], coadmin["user_id"], True)
    assert service.authenticate(token) is None
    with pytest.raises(AccountError, match="Invalid username or password"):
        service.login("coadmin", PASSWORD, "disable")


def test_validation_does_not_reflect_secrets(service, admin):
    async def scenario():
        async with client(application(service)) as web:
            secret = "sensitive-password-content"
            response = await web.post("/auth/login", headers={"Origin": ORIGIN},
                                      json={"username": [], "password": secret})
            assert response.status_code == 422
            assert secret not in response.text
            assert "input" not in response.text
            assert response.headers["cache-control"] == "no-store"
        async with client(application(None)) as web:
            response = await web.post("/auth/login", json={"password": "bad"})
            assert response.status_code == 404
    asyncio.run(scenario())


def test_source_throttle_cannot_be_bypassed_by_random_usernames(service):
    service.config.login_max_attempts = 1
    for index in range(5):
        with pytest.raises(AccountError) as exc:
            service.login(f"unknown{index}", "bad password", "same-source")
        assert exc.value.status_code == 401
    restarted = AccountService(service._conn_factory, service.config)
    with pytest.raises(AccountError) as exc:
        restarted.login("another-unknown", "bad password", "same-source")
    assert exc.value.status_code == 429
    with service._db(write=True) as conn:
        conn.execute("UPDATE auth_login_throttle SET window_start=0")
    with pytest.raises(AccountError) as exc:
        restarted.login("another-unknown", "bad password", "same-source")
    assert exc.value.status_code == 401


def test_sessions_survive_service_restart_and_disabled_stays_revoked(service, admin):
    issued = service.login("owner", PASSWORD, "source")
    restarted = AccountService(service._conn_factory, service.config)
    assert restarted.authenticate(issued["token"])["user_id"] == admin["user_id"]
    other = service.create_admin("other", PASSWORD, actor_user_id=admin["user_id"])
    token = service.login("other", PASSWORD, "other-source")["token"]
    restarted.set_disabled(admin["user_id"], other["user_id"], True)
    restarted.set_disabled(admin["user_id"], other["user_id"], False)
    assert restarted.authenticate(token) is None



def add_domain_route(app):
    @app.post("/domain-protected")
    async def domain(request: Request):
        if request.headers.get("authorization") != "Bearer separate-domain-credential":
            from fastapi import HTTPException
            raise HTTPException(403, "Domain credential required")
        return {"state": request.scope["state"]}


def test_cookie_and_domain_credentials_remain_independent(service, admin):
    async def scenario():
        app = application(service)
        add_domain_route(app)
        domain = {"Authorization": "Bearer separate-domain-credential"}
        async with client(app) as anonymous:
            assert (await anonymous.post("/domain-protected", headers=domain)).status_code == 401
        async with client(app) as web:
            result = await login(web)
            csrf = {"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]}
            # Disabling native account bearer support must not disable independent
            # domain credentials carried alongside an authenticated browser cookie.
            service.config.allow_bearer_sessions = False
            assert (await web.post("/domain-protected", headers={**domain, "Origin": ORIGIN})).status_code == 403
            assert (await web.post("/domain-protected", headers=domain)).status_code == 403
            assert (await web.post("/domain-protected", headers=csrf)).status_code == 403
            result = await web.post("/domain-protected", headers={**domain, **csrf})
            assert result.status_code == 200
            cookie = web.cookies.get(service.config.cookie_name)
            assert cookie not in result.text
            assert "csrf_hash" not in result.text
            assert "account_token" not in result.json()["state"]
            assert result.json()["state"]["account_bearer"] is False
    asyncio.run(scenario())


def test_explicit_native_account_and_domain_tokens_are_independent(service, admin):
    async def scenario():
        app = application(service)
        add_domain_route(app)
        issued = service.login("owner", PASSWORD, "explicit-native")
        account = {"X-LKA-Account-Token": issued["token"]}
        domain = {"Authorization": "Bearer separate-domain-credential"}
        async with client(app) as web:
            assert (await web.post("/domain-protected", headers=domain)).status_code == 401
            assert (await web.post("/domain-protected", headers=account)).status_code == 403
            result = await web.post("/domain-protected", headers={**account, **domain})
            assert result.status_code == 200
            assert result.json()["state"]["account_bearer"] is True
            assert issued["token"] not in result.text
            assert "separate-domain-credential" not in result.text
            assert (await web.post("/domain-protected", headers={**account, **domain, "Origin": ORIGIN})).status_code == 401
            service.config.allow_bearer_sessions = False
            assert (await web.post("/domain-protected", headers={**account, **domain})).status_code == 401
            service.config.allow_bearer_sessions = True
            service.revoke_session(admin["user_id"], issued["session_id"])
            assert (await web.post("/domain-protected", headers={**account, **domain})).status_code == 401
    asyncio.run(scenario())


def test_websocket_authentication_revocation_and_node_transport(service, admin):
    async def scenario():
        issued = service.login("owner", PASSWORD, "websocket")
        seen = []
        receive_count = 0
        downstream_received_revoked_message = False

        async def downstream(scope, receive, send):
            nonlocal downstream_received_revoked_message
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.send", "text": "first"})
            service.revoke_session(admin["user_id"], issued["session_id"])
            await receive()
            downstream_received_revoked_message = True

        async def receive():
            nonlocal receive_count
            receive_count += 1
            return {"type": "websocket.receive", "text": "attempt write after revocation"}

        async def send(message):
            seen.append(message)

        app = SimpleNamespace(state=SimpleNamespace(account_service=service, device_service=None))
        scope = {"type": "websocket", "path": "/socket", "scheme": "wss", "app": app,
                 "headers": [(b"x-lka-account-token", issued["token"].encode())]}
        await AccountAuthMiddleware(downstream)(scope, receive, send)
        assert seen == [{"type": "websocket.accept"}, {"type": "websocket.send", "text": "first"},
                        {"type": "websocket.close", "code": 4401}]
        assert receive_count == 1 and downstream_received_revoked_message is False
        assert issued["token"] not in str(scope["state"])
        cookie_session = service.login("owner", PASSWORD, "cookie-websocket")
        cookie = (service.config.cookie_name + "=" + cookie_session["token"]).encode()
        for path, headers, code in [("/socket", [], 4401),
                                    ("/socket", [(b"cookie", cookie), (b"origin", ORIGIN.encode())], 4403),
                                    ("/node/ping", [(b"authorization", b"Bearer any-device")], 4403)]:
            seen.clear()
            await AccountAuthMiddleware(downstream)({**scope, "path": path, "headers": headers, "state": {}}, receive, send)
            assert seen == [{"type": "websocket.close", "code": code}]
    asyncio.run(scenario())
