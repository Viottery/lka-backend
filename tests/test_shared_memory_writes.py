"""Hub/A/B use actual isolated schemas; transport failures are deterministic."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes.shared_memory_writes import MemoryMutationRequest, enqueue
from app.domains.accounts import AccountConfig, AccountService
from app.domains.devices import DeviceConfig, DeviceEnrollment, DeviceService, DeviceSystem
from app.domains.memory import MemoryService
from app.domains.shared_memory_cache import SharedMemoryCache
from app.domains.shared_memory_writes import (
    LocalMemoryMutation,
    SharedMemoryMutation,
    SharedMemoryMutationOutbox,
    SharedMemoryWriteService,
)
from app.domains.shared_sync import SharedSyncConfig, SharedSyncService, SyncConflictError
from app.integrations.cloud_sync import CloudSyncError
from app.integrations.shared_memory_writes import SharedMemoryWriteSynchronizer
from app.storage.shared_replica import SharedReplicaStore


def pull(sync, replica):
    for _ in range(100):
        page = sync.pull(after=replica.status()["cursor"], limit=100)
        replica.apply_page(page)
        if not page["has_more"]:
            return
    raise AssertionError("journal did not drain")


def create_payload(identity="create-1", **values):
    return {"mutation_id": identity, "operation": "create", "expected_version": 0,
            "content": "Prefer concise answers", "confirmed": True, **values}


@pytest.fixture
def cluster(tmp_path):
    memory = MemoryService(tmp_path / "hub.sqlite3")
    memory.ensure_schema()

    def factory():
        return sqlite3.connect(memory.db_path, timeout=10)

    accounts = AccountService(factory, AccountConfig(enabled=True))
    user = accounts.create_admin("owner", "synthetic-password-only")
    human = {"user_id": user["user_id"], "session_id": "verified-node-human-session", "authorization_epoch": 1}
    devices = DeviceService(factory, DeviceConfig(enabled=True))
    config = SharedSyncConfig(enabled=True, hub_id="hub-test", memory_write_enabled=True)
    sync = SharedSyncService(factory, config, devices)
    hub = SharedMemoryWriteService(memory, sync)
    nodes = {}
    for name in ("A", "B"):
        enrollment = DeviceEnrollment(
            device_id=name, name=name, platform="linux", environment_id="wsl",
            systems=[DeviceSystem(environment_id="wsl", platform="linux")],
            scopes=["heartbeat", "sync.read", "sync.memory.write"], workspace_ids=[], capabilities=[],
            advertised_url=None,
        )
        enrolled = devices.enroll(enrollment)
        principal = devices.authenticate(enrolled["token"])
        local = MemoryService(tmp_path / f"{name}-local.sqlite3")
        local.ensure_schema()
        replica = SharedReplicaStore(tmp_path / f"{name}-replica.sqlite3", "hub-test")
        cache = SharedMemoryCache(local, replica)
        outbox = SharedMemoryMutationOutbox(tmp_path / f"{name}-outbox.sqlite3",
                                          hub_id="hub-test", node_id=name, cache=cache)
        nodes[name] = SimpleNamespace(local=local, cache=cache, replica=replica,
                                      outbox=outbox, principal=principal)
    return SimpleNamespace(hub=hub, memory=memory, sync=sync, devices=devices,
                           accounts=accounts, human=human, config=config, **nodes)


def queue(cluster, node, payload, human):
    return node.outbox.enqueue(payload, {**human, "node_id": node.outbox.node_id} if human else {})


def send(cluster, node):
    payload = node.outbox.pending()[0]
    receipt = cluster.hub.submit(node.principal, payload)
    node.outbox.acknowledge(payload, receipt)
    return receipt


def seed(cluster):
    queue(cluster, cluster.A, create_payload(), cluster.human)
    receipt = send(cluster, cluster.A)
    for node in (cluster.A, cluster.B):
        pull(cluster.sync, node.replica)
    return cluster.memory.get(receipt["result"]["memory_id"])


def edit(node, target, identity, content="Prefer detailed answers", operation="update"):
    return {"mutation_id": identity, "operation": operation,
            "memory_id": node.cache.cloud_id(target.memory_id), "expected_version": target.version,
            "content": content if operation != "delete" else None, "confirmed": True,
            "scope": target.scope, "project_id": target.project_id}


def test_offline_create_is_durable_visible_pending_and_never_recall(cluster):
    c = cluster
    entry = queue(c, c.A, create_payload(), c.human)
    assert entry["status"] == "queued" and entry["recall_effective"] is False
    assert c.A.local.list() == c.memory.list() == c.A.cache.list() == []
    reopened = SharedMemoryMutationOutbox(c.A.outbox.path, hub_id="hub-test", node_id="A", cache=c.A.cache)
    assert reopened.pending() == c.A.outbox.pending()
    receipt = send(c, c.A)
    assert receipt["status"] == "accepted"
    record = c.memory.get(receipt["result"]["memory_id"])
    assert record.status == "active" and record.sensitivity == "normal"
    with closing(c.memory._connect()) as conn:
        source = conn.execute("SELECT * FROM memory_sources WHERE source_id=?", (record.source_ids[0],)).fetchone()
        assert source["source_type"] == "user_api" and source["trusted_source"] == 0
    pull(c.sync, c.A.replica)
    assert c.A.cache.get_active(c.A.cache.cloud_id(record.memory_id)).content == record.content
    assert c.A.local.list() == []


def test_dropped_ack_and_restart_retries_exact_mutation_once(cluster, monkeypatch):
    c = cluster
    queue(c, c.A, create_payload(), c.human)
    monkeypatch.setenv(c.config.token_env, "synthetic-token")
    config = c.config.model_copy(update={"role": "replica"})
    first = True

    def transport(method, path, payload, token):
        nonlocal first
        assert path == "/node/sync/memory-mutations"
        receipt = c.hub.submit(c.A.principal, payload)
        if first:
            first = False
            raise CloudSyncError("cloud_unavailable")
        return receipt

    delivery = SharedMemoryWriteSynchronizer(config, c.A.outbox, transport=transport)
    with pytest.raises(CloudSyncError):
        delivery.flush_once(node_id="A")
    assert c.A.outbox.status()["queued"] == 1 and len(c.memory.list()) == 1
    restarted = SharedMemoryWriteSynchronizer(config, c.A.outbox, transport=transport)
    assert restarted.flush_once(node_id="A")["delivered"] == 1
    assert len(c.memory.list()) == 1 and c.A.outbox.status()["accepted"] == 1
    with closing(c.memory._connect()) as conn:
        assert conn.execute("SELECT count(*) FROM memory_events").fetchone()[0] == 1
    changed = {**c.A.outbox.list(c.human["user_id"])["items"][0]["proposal"], "content": "mutated retry"}
    with pytest.raises(SyncConflictError):
        c.hub.submit(c.A.principal, changed)


def test_concurrent_nodes_preserve_conflict_then_explicit_new_mutation_rebases(cluster):
    c = cluster
    original = seed(c)
    for node, text in ((c.A, "Prefer short replies"), (c.B, "Prefer long replies")):
        queue(c, node, edit(node, original, "edit-" + node.outbox.node_id, text), c.human)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda node: send(c, node), (c.A, c.B)))
    accepted = next(item for item in receipts if item["status"] == "accepted")
    conflict = next(item for item in receipts if item["status"] == "conflict")
    assert conflict["reason"] == "version_conflict"
    assert conflict["current"]["status"] == "superseded"
    assert conflict["current"]["replacement"]["memory_id"] == accepted["result"]["memory_id"]
    loser = c.A if conflict["node_id"] == "A" else c.B
    proposal = loser.outbox.get(conflict["mutation_id"], c.human["user_id"])["proposal"]
    assert conflict["proposal"]["content"] == proposal["content"]
    current = c.memory.get(accepted["result"]["memory_id"])
    assert c.memory.get(original.memory_id).status == "superseded"
    assert len(c.memory.list()) == 1
    pull(c.sync, loser.replica)
    queue(c, loser, edit(loser, current, "explicit-rebase", proposal["content"]), c.human)
    assert send(c, loser)["status"] == "accepted"
    assert c.memory.list()[0].content == proposal["content"]
    assert loser.outbox.get(conflict["mutation_id"], c.human["user_id"])["status"] == "conflict"


def test_offline_update_never_resurrects_deleted_memory(cluster):
    c = cluster
    record = seed(c)
    queue(c, c.B, edit(c.B, record, "offline-stale-update"), c.human)
    queue(c, c.A, edit(c.A, record, "delete", operation="delete"), c.human)
    assert send(c, c.A)["result"]["status"] == "retracted"
    receipt = send(c, c.B)
    assert receipt["status"] == "conflict"
    assert receipt["current"]["status"] == "retracted"
    assert "content" not in receipt["current"]
    assert c.memory.list() == []
    pull(c.sync, c.B.replica)
    assert c.B.cache.list() == []


def test_revoked_node_and_disabled_human_checked_again_at_commit(cluster):
    c = cluster
    queue(c, c.A, create_payload(), c.human)
    c.devices.revoke("A", c.A.principal["revision"])
    with pytest.raises(PermissionError, match="credential"):
        send(c, c.A)
    assert c.memory.list() == [] and c.A.outbox.status()["queued"] == 1
    queue(c, c.B, create_payload("from-b"), c.human)
    other = c.accounts.create_admin("second-owner", "synthetic-password-only", actor_user_id=c.human["user_id"])
    c.accounts.set_disabled(other["user_id"], c.human["user_id"], True)
    with pytest.raises(PermissionError, match="human account"):
        send(c, c.B)
    assert c.memory.list() == []


def test_source_purge_propagates_and_blocks_pending_edit(cluster):
    c = cluster
    record = seed(c)
    queue(c, c.B, edit(c.B, record, "stale-source"), c.human)
    c.memory.revoke_source(record.source_ids[0])
    receipt = send(c, c.B)
    assert receipt["status"] == "conflict"
    assert receipt["current"]["content_unavailable"]
    pull(c.sync, c.B.replica)
    assert c.B.cache.list() == []
    assert c.memory.list() == []


def test_project_binding_namespace_and_user_isolation(cluster, tmp_path):
    c = cluster
    cloud_project = c.memory.resolve_project(tmp_path / "hub-project")
    local_project = c.A.local.resolve_project(tmp_path / "local-project")
    c.A.cache.project_bindings[local_project] = cloud_project
    data = create_payload("project", scope="project", project_id=local_project)
    queue(c, c.A, data, c.human)
    assert c.A.outbox.pending()[0]["project_id"] == cloud_project
    receipt = send(c, c.A)
    pull(c.sync, c.A.replica)
    record = c.A.cache.list(scope="project", project_id=local_project)[0]
    assert record.memory_id.startswith("cloud_") and record.project_id == local_project
    queue(c, c.A, {**edit(c.A, record, "project-edit"), "memory_id": record.memory_id}, c.human)
    assert c.A.outbox.pending()[0]["memory_id"] == receipt["result"]["memory_id"]
    with pytest.raises(PermissionError, match="this hub"):
        queue(c, c.A, {**edit(c.A, record, "bad-id"), "memory_id": receipt["result"]["memory_id"]}, c.human)
    with pytest.raises(PermissionError, match="binding"):
        queue(c, c.B, create_payload("unbound", scope="project", project_id="arbitrary"), c.human)
    assert c.A.outbox.list("other-user")["items"] == []
    with pytest.raises(KeyError):
        c.A.outbox.get("project", "other-user")
    with pytest.raises(SyncConflictError, match="different hub/node"):
        SharedMemoryMutationOutbox(c.A.outbox.path, hub_id="another-hub", node_id="A", cache=c.A.cache)


def test_explicit_human_consent_and_write_optin_cannot_come_from_model_payload(cluster):
    c = cluster
    with pytest.raises(ValidationError):
        LocalMemoryMutation.model_validate(create_payload(confirmed=False))
    with pytest.raises(ValidationError):
        LocalMemoryMutation.model_validate(create_payload(source_type="user_api", trusted_source=True))
    with pytest.raises(PermissionError, match="human session"):
        queue(c, c.A, create_payload(), {})
    queue(c, c.A, create_payload(), c.human)
    payload = c.A.outbox.pending()[0]
    with pytest.raises(ValidationError):
        SharedMemoryMutation.model_validate({**payload, "consent": {**payload["consent"], "origin": "model"}})
    c.sync.config = c.config.model_copy(update={"memory_write_enabled": False})
    with pytest.raises(PermissionError, match="disabled"):
        send(c, c.A)
    c.sync.config = c.config
    with closing(c.memory._connect()) as conn, conn:
        conn.execute("UPDATE cloud_devices SET profile=json_set(profile,'$.scopes',json('[\"heartbeat\"]')) WHERE device_id='A'")
    with pytest.raises(PermissionError, match="scope"):
        send(c, c.A)
    assert c.memory.list() == []


def test_domain_write_and_receipt_roll_back_atomically(cluster, monkeypatch):
    c = cluster
    queue(c, c.A, create_payload(), c.human)
    original = c.memory.create_in_transaction

    def interrupted(conn, payload, **kwargs):
        original(conn, payload, **kwargs)
        raise RuntimeError("simulated failure before receipt commit")

    monkeypatch.setattr(c.memory, "create_in_transaction", interrupted)
    with pytest.raises(RuntimeError):
        send(c, c.A)
    assert c.memory.list() == []
    with closing(c.memory._connect()) as conn:
        assert conn.execute("SELECT count(*) FROM memory_sources").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM shared_memory_mutations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM shared_sync_journal").fetchone()[0] == 0
    monkeypatch.setattr(c.memory, "create_in_transaction", original)
    assert send(c, c.A)["status"] == "accepted"


def test_local_control_rejects_anonymous_and_client_identity_fields(cluster):
    c = cluster
    runtime = SimpleNamespace(local_app_config=SimpleNamespace(sync=c.config.model_copy(update={"role": "replica"})),
                              memory_service=c.A.local)
    state = SimpleNamespace(runtime=runtime, shared_memory_write_outbox=c.A.outbox)
    request = SimpleNamespace(app=SimpleNamespace(state=state), state=SimpleNamespace())
    body = MemoryMutationRequest.model_validate(create_payload())
    with pytest.raises(HTTPException) as exc:
        enqueue(body, request)
    assert exc.value.status_code == 401
    with pytest.raises(ValidationError):
        MemoryMutationRequest.model_validate(create_payload(consent={"user_id": "victim"}))
    request.state.device_connection = {**c.human, "node_id": "A"}
    assert enqueue(body, request)["status"] == "queued"


def test_revoked_human_epoch_blocks_offline_queue_and_does_not_starve_new_consent(cluster, monkeypatch):
    from app.domains.device_connections import DeviceConnectionConfig, DeviceConnectionService

    c = cluster
    connections = DeviceConnectionService(c.devices._conn_factory,
                                          DeviceConnectionConfig(enabled=True), device_service=c.devices)
    queue(c, c.A, create_payload("before-revocation"), c.human)
    connections.revoke_authorization(c.human["user_id"], "A")
    with pytest.raises(PermissionError, match="authorization revoked"):
        send(c, c.A)
    assert c.memory.list() == []
    fresh = {**c.human, "authorization_epoch": 2, "session_id": "new-verified-connection"}
    queue(c, c.A, create_payload("after-new-human-confirmation"), fresh)
    monkeypatch.setenv(c.config.token_env, "synthetic-token")

    def transport(method, path, payload, token):
        try:
            return c.hub.submit(c.A.principal, payload)
        except PermissionError:
            raise CloudSyncError("cloud_http_403") from None

    delivery = SharedMemoryWriteSynchronizer(c.config.model_copy(update={"role": "replica"}),
                                            c.A.outbox, transport=transport)
    result = delivery.flush_once(node_id="A")
    assert result["rejected"] == result["delivered"] == 1
    assert len(c.memory.list()) == 1
    old = c.A.outbox.get("before-revocation", c.human["user_id"])
    assert old["status"] == "rejected" and old["receipt"] is None
    assert old["rejection_reason"] == "cloud_http_403"
    assert old["proposal"]["consent"]["authorization_epoch"] == 1
    assert c.A.outbox.pending() == []


@pytest.mark.parametrize("category", ["cloud_unavailable", "cloud_http_401"])
def test_uncertain_delivery_and_node_authentication_keep_queue_retryable(cluster, monkeypatch, category):
    c = cluster
    queue(c, c.A, create_payload(), c.human)
    monkeypatch.setenv(c.config.token_env, "synthetic-token")

    def unavailable(*args):
        raise CloudSyncError(category)

    delivery = SharedMemoryWriteSynchronizer(c.config.model_copy(update={"role": "replica"}),
                                            c.A.outbox, transport=unavailable)
    with pytest.raises(CloudSyncError):
        delivery.flush_once(node_id="A")
    assert c.A.outbox.status()["queued"] == 1
    assert c.A.outbox.status()["rejected"] == 0


def test_committed_receipt_survives_lost_ack_then_human_revocation(cluster):
    from app.domains.device_connections import DeviceConnectionConfig, DeviceConnectionService

    c = cluster
    connections = DeviceConnectionService(c.devices._conn_factory,
                                          DeviceConnectionConfig(enabled=True), device_service=c.devices)
    queue(c, c.A, create_payload("committed-before-revoke"), c.human)
    pending = c.A.outbox.pending()[0]
    receipt = c.hub.submit(c.A.principal, pending)
    connections.revoke_authorization(c.human["user_id"], "A")
    # Replaying a receipt creates no new effect and must not look like a failed
    # mutation that the user should recreate under a fresh identity.
    assert c.hub.submit(c.A.principal, pending) == receipt
    c.A.outbox.acknowledge(pending, receipt)
    assert c.A.outbox.get("committed-before-revoke", c.human["user_id"])["status"] == "accepted"
    assert len(c.memory.list()) == 1


def test_known_source_purge_also_redacts_historical_conflict_snapshot(cluster):
    c = cluster
    record = seed(c)
    queue(c, c.A, edit(c.A, record, "first-change"), c.human)
    queue(c, c.B, edit(c.B, record, "conflicting-change"), c.human)
    accepted = send(c, c.A)
    assert send(c, c.B)["status"] == "conflict"
    pull(c.sync, c.B.replica)
    current = c.B.outbox.get("conflicting-change", c.human["user_id"])["receipt"]["current"]["replacement"]
    assert current["content"] == "Prefer detailed answers"
    replacement = c.memory.get(accepted["result"]["memory_id"])
    c.memory.revoke_source(replacement.source_ids[0])
    pull(c.sync, c.B.replica)
    current = c.B.outbox.get("conflicting-change", c.human["user_id"])["receipt"]["current"]["replacement"]
    assert current["content_unavailable"] and "content" not in current
    assert c.B.outbox.get("conflicting-change", c.human["user_id"])["proposal"]["content"]


def test_retry_same_local_mutation_after_mirror_supersedes_target_returns_original_entry(cluster):
    c = cluster
    record = seed(c)
    proposal = edit(c.A, record, "local-retry")
    queue(c, c.A, proposal, c.human)
    send(c, c.A)
    pull(c.sync, c.A.replica)
    assert c.A.cache.get_active(c.A.cache.cloud_id(record.memory_id)) is None
    assert queue(c, c.A, proposal, c.human)["status"] == "accepted"
    with pytest.raises(SyncConflictError):
        queue(c, c.A, {**proposal, "content": "changed retry"}, c.human)
