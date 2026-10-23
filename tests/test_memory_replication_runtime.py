"""Automatic database publication uses runtime wiring, never foreground cloud I/O."""
import sqlite3

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.domains.memory import (
    MemoryInput,
    MemoryPublicationLeaseError,
    MemoryService,
    MemorySourceInput,
)
from app.domains.memory_replication import NodeMemoryReplicationJournal
from app.integrations.cloud_sync import CloudSyncError
from tests.test_distributed_runtime import connect_browser
from tests.test_distributed_runtime import connected as connected_fixture

connected = connected_fixture


def test_failed_publication_transaction_cannot_leave_a_replication_head(tmp_path, monkeypatch):
    memory = MemoryService(tmp_path / "local.sqlite3")
    memory.ensure_schema()
    jobs = BackgroundJobStore(memory.db_path)
    jobs.ensure_schema()
    journal = NodeMemoryReplicationJournal(lambda: sqlite3.connect(memory.db_path),
                                            hub_id="synthetic-hub", node_id="synthetic-node")
    source = memory.register_source(MemorySourceInput(source_type="user_api", source_ref="synthetic"))
    payload = MemoryInput(content="An approved ordinary preference", source_id=source, sensitivity="normal")
    with pytest.raises(MemoryPublicationLeaseError):
        memory.create(payload, publication_lease=("missing-job", "worker", 1))
    assert journal.status()["dirty"] == 0

    create = memory.create_in_transaction

    def fail_after_domain_write(conn, value, **kwargs):
        create(conn, value, **kwargs)
        raise RuntimeError("synthetic interruption before commit")

    monkeypatch.setattr(memory, "create_in_transaction", fail_after_domain_write)
    with pytest.raises(RuntimeError, match="before commit"):
        memory.create(payload)
    assert journal.status()["dirty"] == 0
    assert journal.collect() == 0 and journal.pending() == []
    assert memory.list(statuses=("active", "candidate")) == []


@pytest.mark.parametrize("connected", [{"memory_aggregation_enabled": True}], indirect=True)
def test_database_memory_replicates_then_authoritative_delete_prevents_local_fallback(connected, monkeypatch):
    hub, cloud, node, runtime, _workspace = connected
    connect_browser(hub, node, runtime)
    review = hub.get("/sync/memory-records/conflicts")
    assert review.status_code == 200 and review.json() == {"items": [], "next_offset": None}
    assert hub.get("/sync/memory-records/conflicts",
                   headers={"Authorization": "Bearer invalid-account"}).status_code == 401
    assert node.get("/sync/memory-records/conflicts").status_code == 403
    source = runtime.memory_service.register_source(MemorySourceInput(
        source_type="user_api", source_ref="synthetic-node-user-preference",
        checksum="synthetic-evidence-checksum", trusted_source=False,
    ))
    local = runtime.memory_service.create(MemoryInput(
        content="Prefer concise technical explanations", source_id=source, user_confirmed=True, sensitivity="normal",
    ))
    assert runtime.shared_memory_cache.get_active(local.memory_id) is not None
    runtime.cloud_replica_sync.run_once(max_pages=100)
    authoritative = cloud.memory_service.list()
    assert len(authoritative) == 1 and authoritative[0].status == "active"
    runtime.cloud_replica_sync.run_once(max_pages=100)
    recalled = runtime.shared_memory_cache.search("concise")
    assert len(recalled) == 1 and recalled[0].metadata.get("origin") == "cloud_replica"
    assert runtime.memory_service.get(local.memory_id).status == "active"

    original = runtime.cloud_replica_sync._transport

    def offline(*_args):
        raise CloudSyncError("cloud_unavailable")

    monkeypatch.setattr(runtime.cloud_replica_sync, "_transport", offline)
    assert len(runtime.shared_memory_cache.search("concise")) == 1
    assert node.get("/connections/me").status_code == 200
    assert node.get("/local/cache").status_code == 200
    monkeypatch.setattr(runtime.cloud_replica_sync, "_transport", original)
    removed = hub.delete(f"/memories/{authoritative[0].memory_id}",
                         params={"expected_version": authoritative[0].version})
    assert removed.status_code < 300, removed.text
    runtime.cloud_replica_sync.run_once(max_pages=100)
    assert runtime.shared_memory_cache.search("concise") == []
    runtime.cloud_replica_sync.run_once(max_pages=100)
    assert cloud.memory_service.list() == []
    assert runtime.shared_memory_cache.get_active(local.memory_id) is None

    # Disabling future publication does not restore a centrally withdrawn local
    # copy during restart; ownership still constrains the read path.
    config = runtime.local_app_config.model_copy(deep=True)
    config.sync.memory_aggregation_enabled = False
    monkeypatch.setattr(type(runtime.settings), "load_local_config", lambda _: config)
    runtime.stop()
    restarted = type(runtime)(runtime.settings)
    try:
        assert restarted.memory_replication_sync is None
        assert restarted.memory_replication_journal is not None
        assert restarted.shared_memory_cache.get_active(local.memory_id) is None
        assert restarted.shared_memory_cache.search("concise") == []
    finally:
        restarted.stop()
