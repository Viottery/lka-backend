"""Optional same-origin workbench hosting: static files public, every API still gated."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.account_auth import AccountAuthMiddleware
from app.api.device_connection_auth import DeviceConnectionAuthMiddleware
from app.api.routes.distributed_client import router
from app.core.local_config import ClientConfig, LocalAppConfig
from app.domains.accounts import AccountConfig
from app.domains.device_connections import DeviceConnectionConfig


def hosted_app(tmp_path, workbench_dir, *, node=False):
    (tmp_path / "chat.html").write_text("<!doctype html><title>workbench</title>", encoding="utf-8")
    (tmp_path / "sw.js").write_text("self.addEventListener('fetch',()=>);", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "notes.py").write_text("print(1)", encoding="utf-8")
    config = SimpleNamespace(
        client=ClientConfig(workbench_dir=str(workbench_dir) if workbench_dir else ""),
        # A replica names its hub so node pages can reach 真理.
        sync=SimpleNamespace(enabled=True, role="replica" if node else "hub", hub_url="https://hub.test.ts.net/" if node else None),
        connections=SimpleNamespace(node_id="pc" if node else "", enabled=node),
        remote_execution=SimpleNamespace(enabled=False),
    )
    app = FastAPI()
    app.state.runtime = SimpleNamespace(local_app_config=config)
    accounts = SimpleNamespace(config=AccountConfig(enabled=True), authenticate=lambda token: None)
    app.state.account_service = accounts
    if node:
        app.state.device_connection_service = SimpleNamespace(
            config=DeviceConnectionConfig(enabled=True, node_id="pc", allowed_origins=["https://pc.test.ts.net"]))
    app.add_middleware(AccountAuthMiddleware)
    app.add_middleware(DeviceConnectionAuthMiddleware)
    app.include_router(router)

    @app.get("/sessions")
    def sessions():
        return {"sessions": []}
    return app


def fetch(app, path, method="GET"):
    async def run():
        async with AsyncClient(transport=ASGITransport(app=app, client=("100.80.1.2", 5000)),
                               base_url="https://hub.test.ts.net") as client:
            return await client.request(method, path)
    return asyncio.run(run())


@pytest.mark.parametrize("node", [False, True])
def test_static_workbench_is_public_but_api_stays_gated(tmp_path, node):
    app = hosted_app(tmp_path, tmp_path, node=node)
    page = fetch(app, "/desktop-pet/chat.html")
    assert page.status_code == 200 and "workbench" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert fetch(app, "/desktop-pet/sw.js").headers["service-worker-allowed"] == "/desktop-pet/"
    assert fetch(app, "/desktop-pet").status_code == 307
    assert fetch(app, "/sessions").status_code == 401
    assert fetch(app, "/desktop-pet/chat.html", "POST").status_code in {401, 403, 405}
    assert fetch(app, "/client/info").json()["workbench"] is True


def test_hidden_unknown_and_traversal_paths_are_not_served(tmp_path):
    app = hosted_app(tmp_path, tmp_path)
    for path in ("/desktop-pet/.env", "/desktop-pet/notes.py", "/desktop-pet/%2e%2e/chat.html", "/desktop-pet/missing.js"):
        assert fetch(app, path).status_code == 404, path
    # A normalized "../" leaves the static prefix and meets the normal API gate.
    assert fetch(app, "/desktop-pet/../chat.html").status_code == 401


def test_disabled_by_default(tmp_path):
    app = hosted_app(tmp_path, None)
    assert fetch(app, "/desktop-pet/chat.html").status_code == 404
    assert fetch(app, "/client/info").json()["workbench"] is False


def test_workbench_dir_must_be_absolute():
    with pytest.raises(ValueError):
        LocalAppConfig.model_validate({"client": {"workbench_dir": "relative/path"}})


def test_replica_workbench_csp_allows_its_hub_without_broadening_scripts(tmp_path):
    app = hosted_app(tmp_path, tmp_path, node=True)
    app.state.runtime.local_app_config.sync.hub_url = "https://hub.test.ts.net:8443/"
    page = fetch(app, "/desktop-pet/chat.html")
    policy = page.headers["content-security-policy"]
    directives = dict(part.strip().split(" ", 1) for part in policy.split(";") if part.strip())
    assert directives["connect-src"] == "'self' https://hub.test.ts.net:8443"
    assert directives["script-src"] == "'self'"
    assert directives["default-src"] == "'self'"
    app.state.runtime.local_app_config.sync.enabled = False
    assert "connect-src 'self';" in fetch(app, "/desktop-pet/chat.html").headers["content-security-policy"]


@pytest.mark.parametrize("hub_url", ["http://hub.test.ts.net", "https://hub.test.ts.net;connect-src *", "https://user:secret@hub.test.ts.net"])
def test_workbench_csp_does_not_interpolate_invalid_hub_origins(tmp_path, hub_url):
    app = hosted_app(tmp_path, tmp_path, node=True)
    app.state.runtime.local_app_config.sync.hub_url = hub_url
    assert "connect-src 'self';" in fetch(app, "/desktop-pet/chat.html").headers["content-security-policy"]
