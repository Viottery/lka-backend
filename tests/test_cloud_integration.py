"""Offline cloud/node integration against real domain schemas and isolated data."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.domains.accounts import AccountConfig
from app.domains.devices import DeviceConfig
from app.domains.node_outbox import NodeProposalOutbox
from app.domains.shared_sync import SharedSyncConfig, SyncConflictError
from app.integrations import cloud_sync
from app.integrations.cloud_sync import CloudReplicaSynchronizer, CloudSyncError, _NoRedirect
from app.storage.shared_replica import SharedReplicaStore

PASSWORD = "isolated-test-password-2026"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    config = LocalAppConfig(
        auth=AccountConfig(enabled=True, allowed_origins=["https://cloud.test"], allow_bearer_sessions=True),
        devices=DeviceConfig(enabled=True), sync=SharedSyncConfig(enabled=True),
    )
    config.memory.enabled = False
    config.message_history.enabled = False
    config.management.enabled = True
    settings = Settings(_env_file=None, LKA_DATA_DIR=tmp_path / "hub",
                        LKA_WORKSPACE_ROOTS=str(tmp_path), LKA_LOCAL_CONFIG=tmp_path / "none.toml")
    monkeypatch.setattr(Settings, "load_local_config", lambda _: config)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    from app.api import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    runtime = app.state.runtime
    runtime.account_service.create_admin("owner", PASSWORD)
    client = TestClient(app, base_url="https://cloud.test")
    response = client.post("/auth/login", headers={"X-LKA-Client": "native"},
                           json={"username": "owner", "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = "Bearer " + response.json()["token"]
    try:
        yield client, runtime
    finally:
        client.close()
        runtime.stop()


def enroll(client, identity="desktop"):
    result = client.post("/devices", json={
        "device_id": identity, "name": identity, "platform": "windows",
        "systems": [{"environment_id": "wsl", "workspace_ids": ["project-a"]}],
        "workspace_ids": ["project-a"], "capabilities": ["files.read"],
        "scopes": ["heartbeat", "sync.read", "sync.propose"],
    })
    assert result.status_code == 201, result.text
    return result.json()


def test_real_hub_sync_replica_and_revocation(hub, tmp_path, monkeypatch):
    client, runtime = hub
    assert client.get("/sessions", headers={"Authorization": "Bearer not-a-session"}).status_code == 401
    enrolled = enroll(client)
    token = enrolled["token"]
    assert client.get("/memories", headers={"Authorization": "Bearer " + token}).status_code == 401
    memory = client.post("/memories", json={"content": "Prefer short technical explanations."})
    assert memory.status_code < 300, memory.text
    memory_id = memory.json()["memory_id"]
    manager = runtime.management_service.create(make_default=True)["session"]["session_id"]
    goal = runtime.management_goals.create_goal(manager, objective="Track deployment progress")
    cloud_status = client.get("/sync/status").json()
    cfg = SharedSyncConfig(enabled=True, role="replica", hub_url="https://cloud.test",
                           hub_id=cloud_status["hub_id"], page_size=2)
    replica_path = tmp_path / "node" / "shared-replica.sqlite3"
    replica = SharedReplicaStore(replica_path, cfg.hub_id)
    outbox = NodeProposalOutbox(replica_path)
    seen = []

    def transport(method, path, payload, credential):
        response = client.request(method, path, json=payload,
                                  headers={"Authorization": "Bearer " + credential})
        seen.append((method, path))
        if response.status_code >= 400:
            raise CloudSyncError(f"cloud_http_{response.status_code}")
        return response.json()

    monkeypatch.setenv(cfg.token_env, token)
    worker = CloudReplicaSynchronizer(cfg, replica, outbox, transport=transport)
    worker.run_once(max_pages=100)
    assert [r["memory_id"] for r in replica.list_records("memory_entries")["items"]] == [memory_id]
    assert any(r["goal_id"] == goal["goal_id"] for r in replica.list_records("manager_goals")["items"])
    assert replica.status()["cursor"] == client.get("/sync/status").json()["cursor"]
    with closing(runtime._conn()) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "replica_records" not in tables
    with closing(sqlite3.connect(replica_path)) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='manager_controllers'").fetchone()
    deleted = client.request("DELETE", "/memories/" + memory_id,
                             params={"expected_version": memory.json()["version"]}, json={})
    assert deleted.status_code < 300, deleted.text
    worker.run_once(max_pages=100)
    assert replica.list_records("memory_entries")["items"] == []
    assert token not in json.dumps(worker.status())
    assert client.post("/devices/desktop/revoke", json={"expected_revision": 1}).status_code == 200
    with pytest.raises(CloudSyncError, match="cloud_http_401"):
        worker.run_once()
    assert seen[0] == ("GET", "/node/sync/status")


def test_offline_proposal_retries_keep_identity_and_cannot_activate(hub, tmp_path, monkeypatch):
    client, runtime = hub
    token = enroll(client)["token"]
    hub_id = client.get("/sync/status").json()["hub_id"]
    cfg = SharedSyncConfig(enabled=True, role="replica", hub_url="https://cloud.test", hub_id=hub_id)
    path = tmp_path / "node.sqlite3"
    outbox = NodeProposalOutbox(path)
    proposal = {"proposal_id": "offline-note", "kind": "memory_candidate", "text": "A quoted observation."}
    outbox.enqueue(proposal)
    with pytest.raises(SyncConflictError):
        outbox.enqueue({**proposal, "text": "Changed under the same identity"})
    dropped = False

    def transport(method, route, payload, credential):
        nonlocal dropped
        response = client.request(method, route, json=payload,
                                  headers={"Authorization": "Bearer " + credential})
        assert response.status_code < 300, response.text
        if route.endswith("/proposals") and not dropped:
            dropped = True
            raise CloudSyncError("cloud_unavailable")
        return response.json()

    monkeypatch.setenv(cfg.token_env, token)
    worker = CloudReplicaSynchronizer(cfg, SharedReplicaStore(path, hub_id), outbox, transport=transport)
    with pytest.raises(CloudSyncError):
        worker.run_once()
    assert outbox.status() == {"pending": 1, "submitted": 0}
    worker.run_once(max_pages=100)
    assert NodeProposalOutbox(path).status() == {"pending": 0, "submitted": 1}
    assert len(runtime.sync_service.proposals()["items"]) == 1
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM memory_entries").fetchone()[0] == 0


def test_transport_rejects_wrong_hub_before_sending_proposals(tmp_path, monkeypatch):
    cfg = SharedSyncConfig(enabled=True, role="replica", hub_url="https://cloud.test", hub_id="expected")
    replica = SharedReplicaStore(tmp_path / "node.db", cfg.hub_id)
    outbox = NodeProposalOutbox(replica.path)
    outbox.enqueue({"kind": "assistant_note", "text": "Private pending note"})
    calls = []

    def transport(method, path, payload, token):
        calls.append(path)
        return {"hub_id": "other"}

    monkeypatch.setenv(cfg.token_env, "test-device-credential")
    worker = CloudReplicaSynchronizer(cfg, replica, outbox, transport=transport)
    with pytest.raises(CloudSyncError, match="identity_mismatch"):
        worker.run_once()
    assert calls == ["/node/sync/status"] and outbox.status()["pending"] == 1
    with pytest.raises(CloudSyncError, match="redirect_rejected"):
        _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test")


def test_cloud_config_fails_closed_and_default_is_unchanged():
    assert not LocalAppConfig().auth.enabled
    assert not LocalAppConfig().devices.enabled
    assert not LocalAppConfig().sync.enabled
    for overrides in ({"devices": {"enabled": True}}, {"sync": {"enabled": True}},
                      {"sync": {"enabled": True, "role": "replica"}}):
        with pytest.raises(ValidationError):
            LocalAppConfig(**overrides)
    replica = {"enabled": True, "role": "replica", "hub_id": "hub", "hub_url": "https://cloud.test"}
    with pytest.raises(ValidationError, match="second management"):
        LocalAppConfig(sync=replica, management={"enabled": True})
    assert LocalAppConfig(sync=replica).sync.role == "replica"
    for url in ("http://cloud.test", "https://user:pass@cloud.test", "https://cloud.test/?token=bad"):
        with pytest.raises(ValidationError):
            SharedSyncConfig(hub_url=url)


def test_failed_proposal_does_not_starve_shared_data(hub, tmp_path, monkeypatch):
    client, runtime = hub
    profile = enroll(client)
    runtime.device_service.update("desktop", {**{key: profile[key] for key in (
        "device_id", "name", "platform", "systems", "workspace_ids", "capabilities",
    )}, "scopes": ["heartbeat", "sync.read"], "expected_revision": 1})
    hub_id = client.get("/sync/status").json()["hub_id"]
    cfg = SharedSyncConfig(enabled=True, role="replica", hub_url="https://cloud.test", hub_id=hub_id)
    replica = SharedReplicaStore(tmp_path / "node.db", hub_id)
    outbox = NodeProposalOutbox(replica.path)
    outbox.enqueue({"kind": "memory_candidate", "text": "Unapproved local suggestion"})

    def transport(method, route, payload, credential):
        response = client.request(method, route, json=payload,
                                  headers={"Authorization": "Bearer " + credential})
        if response.status_code >= 400:
            raise CloudSyncError(f"cloud_http_{response.status_code}")
        return response.json()

    monkeypatch.setenv(cfg.token_env, profile["token"])
    worker = CloudReplicaSynchronizer(cfg, replica, outbox, transport=transport)
    for text in ("First shared memory", "Second shared memory"):
        assert client.post("/memories", json={"content": text}).status_code < 300
        with pytest.raises(CloudSyncError, match="cloud_http_403"):
            worker.run_once(max_pages=100)
        assert replica.status()["cursor"] == runtime.sync_service.status()["cursor"]
    assert len(replica.list_records("memory_entries")["items"]) == 2
    assert outbox.status() == {"pending": 1, "submitted": 0}


@pytest.mark.parametrize("raw,expected", [
    (b'{"ok":true}', None), (b'[]', "cloud_invalid_response"),
    (b'not-json', "cloud_invalid_response"), (b'x' * 1048577, "cloud_response_too_large"),
])
def test_https_transport_bounds_response_and_ignores_proxy(tmp_path, monkeypatch, raw, expected):
    cfg = SharedSyncConfig(enabled=True, role="replica", hub_url="https://cloud.test", hub_id="hub")
    replica = SharedReplicaStore(tmp_path / "node.db", "hub")
    worker = CloudReplicaSynchronizer(cfg, replica, NodeProposalOutbox(replica.path))

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert limit == cfg.max_page_bytes + 1
            return raw[:limit]

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://cloud.test/node/sync/status"
            assert request.get_header("Authorization") == "Bearer synthetic-node-token"
            assert timeout == cfg.request_timeout_seconds
            return Response()

    def build(*handlers):
        assert any(isinstance(h, ProxyHandler) and h.proxies == {} for h in handlers)
        assert any(isinstance(h, _NoRedirect) for h in handlers)
        return Opener()

    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid")
    monkeypatch.setattr(cloud_sync, "build_opener", build)
    if expected:
        with pytest.raises(CloudSyncError, match=expected):
            worker._request("GET", "/node/sync/status", None, "synthetic-node-token")
    else:
        assert worker._request("GET", "/node/sync/status", None, "synthetic-node-token") == {"ok": True}


@pytest.mark.parametrize("error,expected", [
    (HTTPError("https://secret.invalid", 403, "sensitive-body", {}, None), "cloud_http_403"),
    (URLError("sensitive-proxy-details"), "cloud_unavailable"),
])
def test_https_transport_redacts_failures(tmp_path, monkeypatch, error, expected):
    cfg = SharedSyncConfig(hub_url="https://cloud.test", hub_id="hub")
    replica = SharedReplicaStore(tmp_path / "node.db", "hub")
    worker = CloudReplicaSynchronizer(cfg, replica, NodeProposalOutbox(replica.path))

    class Opener:
        def open(self, *args, **kwargs):
            raise error

    monkeypatch.setattr(cloud_sync, "build_opener", lambda *args: Opener())
    with pytest.raises(CloudSyncError) as caught:
        worker._request("GET", "/node/sync/status", None, "synthetic-node-token")
    assert str(caught.value) == expected
