"""HTTP acceptance of human edits, local auth revocation and live host trust."""
import pytest

from app.core.tools import ToolContext
from app.integrations.cloud_sync import CloudSyncError
from tests.test_distributed_runtime import connect_browser
from tests.test_distributed_runtime import connected as connected_fixture


@pytest.fixture
def connected(tmp_path, monkeypatch, request):
    yield from connected_fixture.__wrapped__(tmp_path, monkeypatch, request)


def test_human_memory_queue_uses_runtime_wiring_and_mirror(connected):
    hub, cloud, node, runtime, _workspace = connected
    connect_browser(hub, node, runtime)
    payload = {"mutation_id": "human-create", "operation": "create", "expected_version": 0,
               "content": "Prefer concise explanations", "confirmed": True}
    queued = node.post("/local/shared-memory/mutations", json=payload)
    assert queued.status_code == 201, queued.text
    assert queued.json()["status"] == "queued"
    assert not cloud.memory_service.list()
    assert not node.get("/local/shared-memory/records").json()["memories"]
    runtime.cloud_replica_sync.run_once(max_pages=100)
    receipt = node.get("/local/shared-memory/mutations/human-create").json()
    assert receipt["status"] == "accepted"
    assert len(cloud.memory_service.list()) == 1
    runtime.cloud_replica_sync.run_once(max_pages=100)
    shared = node.get("/local/shared-memory/records").json()["memories"]
    assert len(shared) == 1 and shared[0]["content"] == payload["content"]
    assert shared[0]["memory_id"].startswith("cloud_")
    malformed = node.post("/local/shared-memory/mutations", json={**payload, "user_id": "forged"})
    assert malformed.status_code == 422
    node.headers.pop("X-CSRF-Token")
    assert node.post("/local/shared-memory/mutations", json=payload).status_code == 403


def test_offline_auth_survives_network_error_then_online_revoke(connected, monkeypatch):
    hub, _cloud, node, runtime, _workspace = connected
    connect_browser(hub, node, runtime)
    original = runtime.cloud_replica_sync._transport

    def offline(*_args):
        raise CloudSyncError("cloud_unavailable")

    monkeypatch.setattr(runtime.cloud_replica_sync, "_transport", offline)
    with pytest.raises(CloudSyncError):
        runtime.cloud_replica_sync.run_once()
    assert node.get("/connections/me").status_code == 200
    assert hub.post("/devices/pc/connections/revoke").status_code == 200
    assert node.get("/connections/me").status_code == 200
    monkeypatch.setattr(runtime.cloud_replica_sync, "_transport", original)
    runtime.cloud_replica_sync.run_once(max_pages=100)
    assert node.get("/connections/me").status_code == 401


def test_live_host_grant_revocation_is_wired(connected):
    _hub, _cloud, _node, runtime, _workspace = connected
    assert runtime.tool_executor.host_execution_guard is not None
    context = ToolContext(session_id="synthetic", run_id="synthetic")
    assert "not authorized" in runtime.tool_executor.host_execution_guard(context)
    runtime.local_app_config.remote_execution.shell_trust = "host_user"
    runtime.local_app_config.remote_execution.allowed_tools = ["bash.run"]
    assert "delegated node task" in runtime.tool_executor.host_execution_guard(context)


def test_revoked_human_consent_rejects_already_queued_memory_write(connected):
    hub, cloud, node, runtime, _workspace = connected
    connect_browser(hub, node, runtime)
    queued = node.post("/local/shared-memory/mutations", json={
        "mutation_id": "queued-before-revoke", "operation": "create", "expected_version": 0,
        "content": "This edit must not commit after revocation", "confirmed": True,
    })
    assert queued.status_code == 201, queued.text
    user_id = queued.json()["proposal"]["consent"]["user_id"]
    assert hub.post("/devices/pc/connections/revoke").status_code == 200
    runtime.cloud_replica_sync.run_once(max_pages=100)
    assert node.get("/connections/me").status_code == 401
    receipt = runtime.shared_memory_write_outbox.get("queued-before-revoke", user_id)
    assert receipt["status"] == "rejected" and receipt["rejection_reason"] == "cloud_http_403"
    assert not cloud.memory_service.list()
    assert not runtime.shared_memory_cache._records(scope="global")


def test_live_host_grant_rejects_constraints_acquired_after_admission(connected):
    _hub, cloud, _node, runtime, _workspace = connected
    config = runtime.local_app_config.remote_execution
    config.shell_trust = "host_user"
    config.allowed_tools = ["bash.run"]
    worker = runtime.remote_execution_worker
    runtime.cloud_replica_sync.run_once(max_pages=100)
    worker.run_once()
    manager = cloud.management_service.create()["session"]["session_id"]
    cloud.management_service.update(manager, expected_revision=1, status="active")
    parent = cloud.create_agent_run(session_id=manager, user_input="Inspect the approved host", resumable=True)
    cloud.agent_run_manager.mark_running(parent.run_id)
    caller = ToolContext(session_id=manager, run_id=parent.run_id, trace_id=parent.trace_id,
                         safety_review_approved=True)
    receipt = cloud.managed_tasks.start_task(caller, command_id="host-scope-review",
        purpose="Inspect the approved host", execution_target={"device_id": "pc", "workspace_id": "work"})
    worker.run_once()
    run = runtime.agent_run_manager.get_run(receipt["run_id"])
    assert run.metadata["context_views"]["tool"]["host_execution_authority"]
    context = ToolContext(session_id=run.session_id, run_id=run.run_id, safety_review_mode="manual")
    assert runtime.tool_executor.host_execution_guard(context) is None
    runtime.tool_registry.register_effect_constraint("new-domain-limit", blocked_domains=("messages",),
                                                   block_unrestricted=False)
    runtime.tool_executor.constraint_store.add([("run", run.run_id)], {"new-domain-limit"})
    assert runtime.tool_executor.host_execution_guard(context) is not None
