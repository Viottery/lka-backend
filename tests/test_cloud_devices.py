import asyncio
import json
import sqlite3

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.routes.devices import node_router, router
from app.domains.devices import (
    DeviceConfig,
    DeviceConflictError,
    DeviceEnrollment,
    DeviceService,
    DeviceUnavailableError,
)


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "devices.sqlite3"
    factory = lambda: sqlite3.connect(path)
    return DeviceService(factory, DeviceConfig(enabled=True)), factory


def enroll(service, identity="pc", **kwargs):
    return service.enroll(
        dict(
            device_id=identity,
            name=identity,
            platform="linux",
            systems=[{"environment_id": "wsl", "workspace_ids": ["project"]}],
            capabilities=["files.read"],
            workspace_ids=["project"],
            scopes=["heartbeat", "sync.read"],
            **kwargs,
        )
    )


def test_registry_persistence_and_no_secrets(registry):
    service, factory = registry
    result = enroll(service)
    assert result["token"]
    persisted = DeviceService(factory, DeviceConfig(enabled=True))
    principal = persisted.authenticate(result["token"])
    assert principal["node_id"] == "pc"
    assert "token" not in persisted.get("pc")
    assert "token_hash" not in json.dumps(persisted.list())
    with factory() as conn:
        assert (
            result["token"]
            not in conn.execute("SELECT token_hash FROM cloud_devices").fetchone()[0]
        )
    assert not DeviceService(factory, DeviceConfig()).authenticate(result["token"])


def test_rotate_revoke_fence_and_revision(registry):
    service, factory = registry
    enrolled = enroll(service)
    stale = service.authenticate(enrolled["token"])
    rotated = service.rotate("pc", 1)
    assert service.authenticate(enrolled["token"]) is None
    with pytest.raises(PermissionError):
        service.heartbeat(stale)
    fresh = service.authenticate(rotated["token"])
    with factory() as conn:
        conn.row_factory = sqlite3.Row
        service.require_node(conn, fresh, "sync.read")
        with pytest.raises(PermissionError):
            service.require_node(conn, fresh, "sync.propose")
    with pytest.raises(DeviceConflictError):
        service.revoke("pc", 1)
    service.revoke("pc", 2)
    assert service.authenticate(rotated["token"]) is None
    with pytest.raises(PermissionError):
        service.heartbeat(fresh)


def test_selection_fixed_auto_constraints_and_owner(registry):
    service, factory = registry
    a = enroll(service, "a")
    b = enroll(service, "b")
    service.heartbeat(service.authenticate(b["token"]))
    fixed = {
        "mode": "fixed",
        "device_id": "a",
        "allowed_device_ids": ["a", "b"],
        "environment_id": "wsl",
        "workspace_id": "project",
        "capabilities": ["files.read"],
    }
    service.save_selection("u", fixed)
    with pytest.raises(DeviceUnavailableError):
        service.resolve("u")
    service.save_selection("u", {**fixed, "mode": "auto", "expected_revision": 1})
    assert service.resolve("u")["node_id"] == "b"
    service.heartbeat(service.authenticate(a["token"]))
    assert service.resolve("u")["node_id"] == "a"
    assert DeviceService(factory, service.config).get_selection("u")["revision"] == 2
    with pytest.raises(KeyError):
        service.get_selection("other")
    for change in (
        {"environment_id": "windows"},
        {"workspace_id": "unknown"},
        {"capabilities": ["exec"]},
    ):
        selection = service.get_selection("u")
        service.save_selection(
            "u", {**fixed, "mode": "auto", **change, "expected_revision": selection["revision"]}
        )
        with pytest.raises(DeviceUnavailableError):
            service.resolve("u")
    with factory() as conn:
        conn.execute("UPDATE cloud_devices SET last_seen='2000-01-01T00:00:00+00:00'")
    assert not service.get("a")["online"]


def test_heartbeat_cannot_escalate_or_spoof(registry):
    service, _ = registry
    enrolled = enroll(service)
    app = FastAPI()
    app.state.device_service = service
    app.include_router(router)
    app.include_router(node_router)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.device = service.authenticate(
            request.headers.get("authorization", "").removeprefix("Bearer ")
        )
        if request.headers.get("x-admin") == "yes":
            request.state.account = {"user_id": "admin"}
        return await call_next(request)

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            headers = {"authorization": f"Bearer {enrolled['token']}"}
            for extra in (
                {"scopes": ["sync.propose"]},
                {"capabilities": ["exec"]},
                {"device_id": "other"},
                {"workspace_ids": ["secret"]},
            ):
                assert (
                    await client.post("/node/heartbeat", headers=headers, json=extra)
                ).status_code == 422
            assert (await client.get("/devices", headers=headers)).status_code == 401
            assert (
                await client.post(
                    "/node/heartbeat", headers=headers, json={"environment_id": "other"}
                )
            ).status_code == 403
            response = await client.post("/node/heartbeat", headers=headers, json={})
            assert response.status_code == 200
            assert response.json()["last_seen"]
            assert "credential_version" not in response.json()
            assert "token" not in (await client.get("/devices", headers={"x-admin": "yes"})).text

    asyncio.run(exercise())


def test_registry_rejects_unsafe_url_and_workspace_grants(registry):
    service, _ = registry
    for url in (
        "http://a.ts.net",
        "https://example.com",
        "https://a.ts.net/private",
        "https://a.ts.net?x=1",
    ):
        with pytest.raises(ValidationError):
            enroll(service, advertised_url=url)
    with pytest.raises(ValidationError):
        DeviceEnrollment(
            device_id="pc",
            name="pc",
            platform="linux",
            systems=[{"environment_id": "wsl", "workspace_ids": ["unapproved"]}],
        )


def test_concurrent_admin_rotation_has_one_winner(registry):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service, _ = registry
    enroll(service)
    barrier = Barrier(2)

    def rotate():
        barrier.wait(timeout=5)
        try:
            return service.rotate("pc", 1)
        except DeviceConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: rotate(), range(2)))
    winners = [item for item in results if item]
    assert len(winners) == 1
    assert service.authenticate(winners[0]["token"])["credential_version"] == 2
    assert service.get("pc")["revision"] == 2
