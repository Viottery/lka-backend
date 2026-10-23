"""Real API/runtime wiring with synthetic hub and node databases, no live network."""
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.core.tools import ToolContext
from app.domains.accounts import AccountConfig
from app.domains.device_connections import DeviceConnectionConfig
from app.domains.devices import DeviceConfig
from app.domains.remote_execution import RemoteExecutionConfig
from app.domains.shared_sync import SharedSyncConfig
from app.integrations.cloud_sync import CloudSyncError
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def connected(tmp_path, monkeypatch, request):
    apps = []
    aggregation = getattr(request, "param", {}).get("memory_aggregation_enabled", False)

    def make(config, name):
        config.message_history.enabled = False
        config.memory.background_enabled = False
        config.embedding.enabled = False
        settings = Settings(_env_file=None, LKA_DATA_DIR=tmp_path / name,
                            LKA_LOCAL_CONFIG=tmp_path / "missing.toml", LKA_WORKSPACE_ROOTS=str(tmp_path))
        monkeypatch.setattr(Settings, "load_local_config", lambda _: config)
        monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
        from app.api import main

        monkeypatch.setattr(main, "get_settings", lambda: settings)
        app = main.create_app()
        apps.append(app)
        return app

    hub_config = LocalAppConfig(auth=AccountConfig(enabled=True, allow_bearer_sessions=True),
                               devices=DeviceConfig(enabled=True),
                               sync=SharedSyncConfig(enabled=True, memory_write_enabled=True,
                                                     memory_aggregation_enabled=aggregation),
                               connections=DeviceConnectionConfig(enabled=True),
                               remote_execution=RemoteExecutionConfig(enabled=True))
    hub_config.management.enabled = True
    hub = make(hub_config, "hub")
    hub.state.runtime.account_service.create_admin("owner", "synthetic-admin-password")
    client = TestClient(hub, base_url="https://hub.example.ts.net")
    login = client.post("/auth/login", headers={"X-LKA-Client": "native"},
                        json={"username": "owner", "password": "synthetic-admin-password"})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = "Bearer " + login.json()["token"]
    enrolled = client.post("/devices", json={
        "device_id": "pc", "name": "PC", "platform": "linux",
        "systems": [{"environment_id": "linux", "workspace_ids": ["work"]}],
        "workspace_ids": ["work"], "scopes": ["heartbeat", "sync.read", "sync.memory.write",
            "sync.memory.aggregate", "connections.poll",
            "execution.poll", "execution.run", "execution.result"],
        "advertised_url": "https://pc.example.ts.net",
    })
    assert enrolled.status_code == 201, enrolled.text
    hub_id = client.get("/sync/status").json()["hub_id"]
    workspace = tmp_path / "work"
    workspace.mkdir()
    node = make(LocalAppConfig(sync=SharedSyncConfig(enabled=True, role="replica", hub_id=hub_id,
                     hub_url="https://hub.example.ts.net", token_env="ISOLATED_NODE_TOKEN",
                     memory_write_enabled=True, memory_aggregation_enabled=aggregation),
                  connections=DeviceConnectionConfig(enabled=True, node_id="pc",
                     allowed_origins=["https://pc.example.ts.net"], workspace_roots={"work": str(workspace)}),
                  remote_execution=RemoteExecutionConfig(enabled=True, environment_id="linux",
                     workspace_mappings={"work": str(workspace)})), "node")
    node_client = TestClient(node, base_url="https://pc.example.ts.net")

    def transport(method, path, payload, token):
        response = client.request(method, path, json=payload,
                                  headers={"Authorization": "Bearer " + token})
        if response.status_code >= 400:
            raise CloudSyncError(f"cloud_http_{response.status_code}")
        return response.json()

    monkeypatch.setenv("ISOLATED_NODE_TOKEN", enrolled.json()["token"])
    worker = node.state.runtime.cloud_replica_sync
    worker._transport = transport
    node.state.runtime.remote_execution_worker._transport = transport
    try:
        yield client, hub.state.runtime, node_client, node.state.runtime, workspace
    finally:
        node_client.close()
        client.close()
        for app in apps:
            app.state.runtime.stop()


def connect_browser(hub, node, runtime):
    response = hub.post("/devices/pc/connections", json={"workspace_ids": ["work"]})
    assert response.status_code == 201, response.text
    runtime.cloud_replica_sync.run_once(max_pages=100)
    redeemed = node.post("/connections/redeem", json={"grant": response.json()["grant"]},
                         headers={"Origin": "https://pc.example.ts.net"})
    assert redeemed.status_code == 200, redeemed.text
    node.headers.update({"Origin": "https://pc.example.ts.net", "X-CSRF-Token": redeemed.json()["csrf_token"]})


def test_offline_ordinary_turn_uses_cache_and_direct_workspace(connected):
    hub, _cloud, node, runtime, workspace = connected
    memory = hub.post("/memories", json={"content": "Prefer short answers"})
    assert memory.status_code < 300, memory.text
    connect_browser(hub, node, runtime)
    node.headers.pop("X-CSRF-Token")
    resumed = node.get("/connections/me")
    assert resumed.status_code == 200 and resumed.json()["csrf_token"]
    node.headers["X-CSRF-Token"] = resumed.json()["csrf_token"]
    assert node.get("/client/info").json()["role"] == "replica"
    assert node.get("/local/workspaces").json() == {"items": [{"workspace_id": "work"}]}
    session = node.post("/sessions", json={"title": "Local offline task"})
    assert session.status_code < 300, session.text
    sid = session.json()["session"]["session_id"]
    policy = runtime.session_service.run_policy(sid)
    assert not policy.allow_default_workspace and policy.allowed_paths == [str(workspace)]
    context = runtime.agent_turn_loop.memory_context_provider(sid, None, "Use my preferences")
    assert any(item["content"] == "Prefer short answers" for item in context["items"])
    assert context["shared_memory_cache"]["reads_allow_stale"]
    assert node.get("/local/cache").json()["reads_allow_stale"]
    bound = node.post(f"/sessions/{sid}/workspace-id", json={"workspace_id": "work"})
    assert bound.status_code == 200, bound.text
    uploaded = node.put("/local/workspaces/work/file?path=note.txt", content=b"local content")
    assert uploaded.status_code == 201, uploaded.text
    assert node.get("/local/workspaces/work/file?path=note.txt").content == b"local content"
    assert node.put("/local/workspaces/work/file?path=note.txt", content=b"overwrite").status_code == 409

    def unavailable(*args):
        raise CloudSyncError("cloud_unavailable")

    runtime.cloud_replica_sync._transport = unavailable
    runtime.remote_execution_worker._transport = unavailable
    with pytest.raises(CloudSyncError):
        runtime.cloud_replica_sync.run_once()
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Local task"), "Local task complete.",
    ])
    response = node.post("/agent/turn", json={"session_id": sid, "user_input": "Answer locally."})
    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "Local task complete."
    assert node.get("/sessions").status_code == 200
    assert (workspace / "note.txt").read_text() == "local content"
    assert node.get("/local/workspaces/work/entries?path=..").status_code in {403, 422}
    assert node.put("/local/workspaces/unknown/file?path=x", content=b"x").status_code == 403


def test_assistant_related_sessions_cannot_be_used_directly(connected):
    hub, cloud, node, runtime, _ = connected
    connect_browser(hub, node, runtime)
    ordinary = node.post("/sessions", json={"title": "Visible ordinary root"}).json()["session"]
    hidden = runtime.session_service.create_session(title="Cloud execution",
        creation_source="assistant", creator_session_id="cloud-manager",
        metadata={"remote_execution": {"task_id": "remote-task"}}).session
    assert node.get(f"/sessions/{hidden.session_id}").status_code == 403
    assert node.post("/agent/turn", json={"session_id": hidden.session_id, "user_input": "bypass"}).status_code == 403
    assert all(item["session_id"] != hidden.session_id for item in node.get("/sessions").json()["sessions"])
    assert node.get("/sessions?limit=1").json()["sessions"][0]["session_id"] == ordinary["session_id"]
    cloud_shadow = cloud.session_service.create_session(title="Remote shadow",
        metadata={"remote_execution": {"task_id": "t"}}).session
    assert hub.get(f"/sessions/{cloud_shadow.session_id}").status_code == 200
    assert hub.post("/agent/turn", json={"session_id": cloud_shadow.session_id, "user_input": "wrong node"}).status_code == 409
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM device_connection_sessions").fetchone()[0] == 1


def test_workspace_links_and_symlinks_cannot_escape(tmp_path):
    from app.domains.node_workspaces import NodeWorkspaceService

    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "link").symlink_to(outside, target_is_directory=True)
    service = NodeWorkspaceService({"work": str(root)})
    with pytest.raises(PermissionError):
        service.read("work", "link/secret.txt")
    with pytest.raises(PermissionError):
        service.create_file("work", "link/new.txt", b"x")
    assert not (outside / "new.txt").exists()


def test_node_lifecycle_dispatches_remote_tasks_without_local_manager(connected):
    _hub, cloud, _node, runtime, _workspace = connected
    runtime.cloud_replica_sync.run_once()
    runtime.remote_execution_worker.run_once()  # Register this environment's live worker.
    sid = cloud.management_service.create()["session"]["session_id"]
    cloud.management_service.update(sid, expected_revision=1, status="active")
    parent = cloud.create_agent_run(session_id=sid, user_input="Answer on the PC", resumable=True)
    cloud.agent_run_manager.mark_running(parent.run_id)
    cloud.session_service.append_message(session_id=sid, role="user", content=parent.user_input,
        payload={"trace_id": parent.trace_id}, message_id="real-runtime-user-message")
    receipt = cloud.managed_tasks.start_task(ToolContext(session_id=sid, run_id=parent.run_id,
        trace_id=parent.trace_id, safety_review_approved=True), command_id="lifecycle",
        purpose="Answer without using tools", execution_target={"device_id": "pc", "environment_id": "linux"})
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="No tools required"), "Executed on the PC.",
    ])
    assert not runtime.local_app_config.management.enabled
    runtime.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        task = cloud.remote_execution_service.get(receipt["remote_execution"]["task_id"])
        shadow = cloud.agent_run_manager.get_run(receipt["run_id"])
        if task["state"] == "completed" and shadow.result_snapshot is not None:
            break
        time.sleep(0.05)
    assert task["state"] == "completed", task
    assert cloud.agent_run_manager.get_run(receipt["run_id"]).result_snapshot["answer"] == "Executed on the PC."


def test_execution_replica_cannot_disable_its_session_boundary():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="direct-node session boundary"):
        LocalAppConfig(sync=SharedSyncConfig(enabled=True, role="replica", hub_id="hub",
                        hub_url="https://hub.test"),
                       remote_execution=RemoteExecutionConfig(enabled=True, environment_id="linux"))
