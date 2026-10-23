"""One isolated acceptance journey across a hub, two computers and a mobile API client."""
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.domains.device_connections import DeviceConnectionConfig
from app.domains.remote_execution import RemoteExecutionConfig
from app.domains.shared_sync import SharedSyncConfig
from app.integrations.cloud_sync import CloudSyncError
from tests.test_distributed_runtime import (  # noqa: F401 - shared pytest fixture
    connect_browser,
    connected,
)
from tests.test_unified_entry_quality import ScriptedClient, operation


def test_two_computers_share_memory_and_phone_supervises_targeted_execution(request, tmp_path, monkeypatch):
    hub_client, cloud, computer_a, runtime_a, _workspace_a = request.getfixturevalue("connected")
    phone = TestClient(hub_client.app, base_url="https://hub.example.ts.net", headers={
        "Authorization": hub_client.headers["Authorization"],
        "User-Agent": "Isolated acceptance client (Mobile)",
    })
    enrollment = phone.post("/devices", json={
        "device_id": "pc-b", "name": "Computer B", "platform": "linux",
        "systems": [{"environment_id": "linux", "workspace_ids": ["work"]}],
        "workspace_ids": ["work"], "scopes": ["heartbeat", "sync.read", "connections.poll",
            "execution.poll", "execution.run", "execution.result"],
        "advertised_url": "https://pc-b.example.ts.net",
    })
    assert enrollment.status_code == 201, enrollment.text
    hub_id = phone.get("/sync/status").json()["hub_id"]
    workspace_b = tmp_path / "work-b"
    workspace_b.mkdir()
    config_b = LocalAppConfig(
        sync=SharedSyncConfig(enabled=True, role="replica", hub_id=hub_id,
            hub_url="https://hub.example.ts.net", token_env="ISOLATED_NODE_B_TOKEN"),
        connections=DeviceConnectionConfig(enabled=True, node_id="pc-b",
            allowed_origins=["https://pc-b.example.ts.net"], workspace_roots={"work": str(workspace_b)}),
        remote_execution=RemoteExecutionConfig(enabled=True, environment_id="linux",
            workspace_mappings={"work": str(workspace_b)}),
    )
    config_b.message_history.enabled = False
    config_b.memory.background_enabled = False
    config_b.embedding.enabled = False
    settings_b = Settings(_env_file=None, LKA_DATA_DIR=tmp_path / "node-b",
        LKA_LOCAL_CONFIG=tmp_path / "missing.toml", LKA_WORKSPACE_ROOTS=str(tmp_path))
    monkeypatch.setattr(Settings, "load_local_config", lambda _: config_b)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings_b)
    from app.api import main

    monkeypatch.setattr(main, "get_settings", lambda: settings_b)
    app_b = main.create_app()
    runtime_b = app_b.state.runtime
    computer_b = TestClient(app_b, base_url="https://pc-b.example.ts.net")
    monkeypatch.setenv("ISOLATED_NODE_B_TOKEN", enrollment.json()["token"])

    def hub_transport(method, path, payload, token):
        response = phone.request(method, path, json=payload, headers={"Authorization": "Bearer " + token})
        if response.status_code >= 400:
            raise CloudSyncError(f"cloud_http_{response.status_code}")
        return response.json()

    runtime_b.cloud_replica_sync._transport = hub_transport
    runtime_b.remote_execution_worker._transport = hub_transport
    try:
        assert len({cloud.db_path, runtime_a.db_path, runtime_b.db_path}) == 3
        memory = phone.post("/memories", json={"content": "Prefer concise answers across my computers"})
        assert memory.status_code < 300, memory.text
        connect_browser(phone, computer_a, runtime_a)
        grant_b = phone.post("/devices/pc-b/connections", json={"workspace_ids": ["work"]})
        assert grant_b.status_code == 201, grant_b.text
        runtime_b.cloud_replica_sync.run_once(max_pages=100)
        redeemed_b = computer_b.post("/connections/redeem", json={"grant": grant_b.json()["grant"]},
            headers={"Origin": "https://pc-b.example.ts.net"})
        assert redeemed_b.status_code == 200, redeemed_b.text
        computer_b.headers.update({"Origin": "https://pc-b.example.ts.net",
                                   "X-CSRF-Token": redeemed_b.json()["csrf_token"]})
        runtime_a.remote_execution_worker.run_once()
        runtime_b.remote_execution_worker.run_once()

        ordinary = []
        for client, runtime, label in ((computer_a, runtime_a, "A_LOCAL_ONLY"),
                                       (computer_b, runtime_b, "B_LOCAL_ONLY")):
            created = client.post("/sessions", json={"title": label})
            assert created.status_code < 300, created.text
            sid = created.json()["session"]["session_id"]
            runtime.agent_turn_loop.llm_client = ScriptedClient([
                operation("final_answer", reason="This computer's ordinary conversation"), label,
            ])
            answer = client.post("/agent/turn", json={"session_id": sid, "user_input": label})
            assert answer.status_code == 200, answer.text
            assert answer.json()["answer"] == label
            recalled = runtime.agent_turn_loop.memory_context_provider(sid, None, "Use my answer preferences")
            assert any(item["content"] == "Prefer concise answers across my computers" for item in recalled["items"])
            assert recalled["shared_memory_cache"]["reads_allow_stale"]
            ordinary.append(sid)
        assert computer_a.get(f"/sessions/{ordinary[1]}").status_code in {403, 404}
        assert computer_b.get(f"/sessions/{ordinary[0]}").status_code in {403, 404}
        assert phone.get(f"/sessions/{ordinary[0]}").status_code == 404
        assert phone.get(f"/sessions/{ordinary[1]}").status_code == 404
        assert all(item["session_id"] != ordinary[1] for item in computer_a.get("/sessions").json()["sessions"])
        assert all(item["session_id"] != ordinary[0] for item in computer_b.get("/sessions").json()["sessions"])

        manager = phone.post("/managers", json={"title": "Phone assistant"})
        assert manager.status_code == 201, manager.text
        manager_id = manager.json()["session"]["session_id"]
        activated = phone.patch(f"/managers/{manager_id}", json={"expected_revision": 1, "status": "active"})
        assert activated.status_code == 200, activated.text
        cloud.local_app_config.agent.unified_entry_enabled = True
        cloud.agent_turn_loop.unified_entry_enabled = True
        cloud.agent_turn_loop.llm_client = ScriptedClient([
            operation("expand_package", package_name="management"),
            operation("tool_call", tool_name="management.start_task", tool_input={
                "command_id": "phone-to-b", "purpose": "Answer on computer B without using tools",
                "execution_target": {"device_id": "pc-b", "environment_id": "linux", "workspace_id": "work"},
            }),
            operation("final_answer", reason="The explicitly targeted task was accepted"),
            "The task was dispatched to computer B.",
        ])
        delegated = phone.post("/agent/turn", json={"session_id": manager_id,
            "user_input": "Use computer B's Linux work workspace to answer this assistant task.",
            "safety_review_mode": "skip"})
        assert delegated.status_code == 200, delegated.text
        tasks = phone.get("/remote-execution/tasks", params={"owner_session_id": manager_id})
        assert tasks.status_code == 200, tasks.text
        assert len(tasks.json()["items"]) == 1, {
            key: delegated.json().get(key) for key in ("answer", "tool_events", "decision_events")
        }
        task = tasks.json()["items"][0]
        assert task["node_id"] == "pc-b" and task["environment_id"] == "linux"
        runtime_a.remote_execution_worker.run_once()
        assert runtime_a.agent_run_manager.get_run(task["run_id"]) is None
        runtime_b.agent_turn_loop.llm_client = ScriptedClient([
            operation("final_answer", reason="Executed only on the selected computer"), "B_ASSISTANT_RESULT",
        ])
        runtime_b.remote_execution_worker.run_once()
        assert runtime_b.agent_run_manager.get_run(task["run_id"]) is not None
        assert runtime_b.agent_dispatch.run_one("computer-b-test")
        runtime_b.remote_execution_worker.run_once()
        published = phone.get(f"/remote-execution/tasks/{task['task_id']}")
        assert published.status_code == 200 and published.json()["state"] == "completed", published.text
        history = phone.get(f"/sessions/{task['session_id']}")
        assert history.status_code == 200, history.text
        assert any(message["role"] == "agent" and message["content"] == "B_ASSISTANT_RESULT"
                   for message in history.json()["messages"])
        assert cloud.agent_run_manager.get_run(task["run_id"]).result_snapshot["answer"] == "B_ASSISTANT_RESULT"
        assert computer_b.get(f"/sessions/{task['session_id']}").status_code == 403
        assert computer_b.get(f"/agent/runs/{task['run_id']}").status_code == 403
        assert runtime_a.agent_run_manager.get_run(task["run_id"]) is None
        with closing(runtime_a._conn()) as conn:
            assert conn.execute("SELECT count(*) FROM remote_execution_node_tasks").fetchone()[0] == 0

        unavailable_calls = []
        def unavailable(*args):
            unavailable_calls.append(args)
            raise CloudSyncError("cloud_unavailable")
        runtime_a.cloud_replica_sync._transport = unavailable
        runtime_a.remote_execution_worker._transport = unavailable
        with pytest.raises(CloudSyncError):
            runtime_a.cloud_replica_sync.run_once()
        with pytest.raises(CloudSyncError):
            runtime_a.remote_execution_worker.run_once()
        calls_before_chat = len(unavailable_calls)
        offline_session = computer_a.post("/sessions", json={"title": "A new offline conversation"})
        assert offline_session.status_code < 300, offline_session.text
        offline_id = offline_session.json()["session"]["session_id"]
        runtime_a.agent_turn_loop.llm_client = ScriptedClient([
            operation("final_answer", reason="Ordinary chat requires no hub transport"), "A_OFFLINE_NEW_TURN",
        ])
        offline = computer_a.post("/agent/turn", json={"session_id": offline_id, "user_input": "Answer locally while the hub is unreachable"})
        assert offline.status_code == 200 and offline.json()["answer"] == "A_OFFLINE_NEW_TURN", offline.text
        assert len(unavailable_calls) == calls_before_chat
        cached = runtime_a.agent_turn_loop.memory_context_provider(offline_id, None, "Use my preferences")
        assert any(item["content"] == "Prefer concise answers across my computers" for item in cached["items"])
        assert computer_b.get(f"/sessions/{offline_id}").status_code in {403, 404}
    finally:
        computer_b.close()
        runtime_b.stop()
        phone.close()
