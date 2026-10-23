"""Durable, provenance-preserving replication of local learned memories."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.domains import memory_replication as replication_domain
from app.domains.devices import DeviceConfig, DeviceEnrollment, DeviceService, DeviceSystem
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput
from app.domains.memory_replication import (
    MemoryReplicationService,
    NodeMemoryReplicationJournal,
)
from app.domains.shared_memory_cache import SharedMemoryCache
from app.domains.shared_sync import SharedSyncConfig, SharedSyncService
from app.integrations.cloud_sync import CloudSyncError
from app.integrations.memory_replication import MemoryReplicationSynchronizer
from app.storage.shared_replica import SharedReplicaStore


@pytest.fixture
def cluster(tmp_path):
    hub = MemoryService(tmp_path / "hub.sqlite3")
    hub.ensure_schema()

    def hub_factory():
        return sqlite3.connect(hub.db_path, timeout=10)

    sync_config = SharedSyncConfig(enabled=True, hub_id="replication-hub",
                                   memory_aggregation_enabled=True)
    devices = DeviceService(hub_factory, DeviceConfig(enabled=True))
    sync = SharedSyncService(hub_factory, sync_config, devices)
    service = MemoryReplicationService(hub, sync)
    nodes = {}
    for name in ("A", "B"):
        enrolled = devices.enroll(DeviceEnrollment(
            device_id=name, name=name, platform="linux", environment_id="test",
            systems=[DeviceSystem(environment_id="test", platform="linux")],
            scopes=["heartbeat", "sync.read", "sync.memory.aggregate"],
        ))
        principal = devices.authenticate(enrolled["token"])
        local = MemoryService(tmp_path / f"{name}.sqlite3")
        local.ensure_schema()
        replica = SharedReplicaStore(tmp_path / f"{name}-replica.sqlite3", "replication-hub")
        cache = SharedMemoryCache(local, replica)

        def factory(path=local.db_path):
            return sqlite3.connect(path, timeout=10)

        journal = NodeMemoryReplicationJournal(
            factory, hub_id="replication-hub", node_id=name, cache=cache,
        )
        cache.replication_filter = journal.local_visible
        config = SimpleNamespace(sync=sync_config.model_copy(update={"role": "replica"}))
        synchronizer = MemoryReplicationSynchronizer(config.sync, journal)
        nodes[name] = SimpleNamespace(local=local, replica=replica, cache=cache, principal=principal,
                                      journal=journal, synchronizer=synchronizer)
    return SimpleNamespace(hub=hub, sync=sync, service=service, **nodes)


def learn(memory, content, source_ref, *, memory_type="preference", metadata=None,
          sensitivity="normal", scope="global", project_id=None, active=True,
          source_expires_at=None, memory_expires_at=None,
          checksum="sha256:synthetic-checksum"):
    source_id = memory.register_source(MemorySourceInput(
        source_type="agent_learning", source_ref=source_ref,
        checksum=checksum, expires_at=source_expires_at,
        metadata={"status": "active"},
    ))
    return memory.create(MemoryInput(
        content=content, memory_type=memory_type, scope=scope, project_id=project_id,
        source_id=source_id, confidence=0.8, sensitivity=sensitivity,
        metadata=metadata or {}, extraction_model="test-model", user_confirmed=active,
        expires_at=memory_expires_at,
    ))


def deliver(node, hub_service):
    results = []
    for payload in node.journal.pending():
        receipt = hub_service.submit(node.principal, payload)
        node.journal.acknowledge(payload, receipt)
        results.append(receipt)
    return results


def pull(sync, replica):
    for _ in range(100):
        page = sync.pull(after=replica.status()["cursor"], limit=100)
        replica.apply_page(page)
        if not page["has_more"]:
            return
    raise AssertionError("replication journal did not drain")


def test_collect_emits_safe_monotonic_journal_and_excludes_private_source_refs(cluster):
    record = learn(cluster.A.local, "Prefer concise answers", "private://raw-source")
    assert cluster.A.journal.collect() >= 1
    first = cluster.A.journal.pending()[0]
    assert first["operation"] == "upsert"
    assert first["origin_id"] == record.memory_id
    assert first["revision"] > 0 and first["mutation_id"]
    assert first["record"]["status"] in {"active", "candidate"}
    serialized = str(first)
    assert "private://raw-source" not in serialized
    assert "trusted" not in first and "user_confirmed" not in first and "human_ui" not in first
    assert cluster.A.journal.collect() == 0
    assert cluster.A.journal.pending()[0] == first


def test_opaque_legacy_source_checksum_is_not_replicated(cluster):
    learn(cluster.A.local, "A memory with legacy checksum", "private://raw-source",
          checksum="private-checksum-value")
    cluster.A.journal.collect()
    payload = cluster.A.journal.pending()[0]
    assert "private-checksum-value" not in str(payload)
    assert payload["sources"][0]["checksum"].startswith("opaque-sha256:")


def test_concurrent_exact_dedup_keeps_single_hub_claim_and_both_provenances(cluster):
    learn(cluster.A.local, "Prefers concise answers", "a-source")
    learn(cluster.B.local, "Prefers concise answers", "b-source")
    cluster.A.journal.collect()
    cluster.B.journal.collect()
    def submit(node):
        payload = node.journal.pending()[0]
        return cluster.service.submit(node.principal, payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(submit, (cluster.A, cluster.B)))
    for node, receipt in ((cluster.A, a), (cluster.B, b)):
        node.journal.acknowledge(node.journal.pending()[0], receipt)
    assert a["status"] == b["status"] == "accepted"
    assert a["result"]["memory_id"] == b["result"]["memory_id"]
    assert len(cluster.hub.list()) == 1
    record = cluster.hub.get(a["result"]["memory_id"])
    assert len(record.source_ids) == 2


def test_candidate_stays_candidate_on_hub_without_user_confirmation(cluster):
    learn(cluster.A.local, "Maybe prefers formal tone", "candidate-source", active=False)
    cluster.A.journal.collect()
    receipt = deliver(cluster.A, cluster.service)[0]
    assert receipt["status"] == "accepted"
    assert receipt["result"]["status"] == "candidate"
    assert cluster.hub.get(receipt["result"]["memory_id"]).status == "candidate"


def test_duplicate_candidates_do_not_promote_each_other(cluster):
    learn(cluster.A.local, "Maybe prefers concise answers", "candidate-a", active=False)
    learn(cluster.B.local, "Maybe prefers concise answers", "candidate-b", active=False)
    for node in (cluster.A, cluster.B):
        node.journal.collect()
        deliver(node, cluster.service)
    entries = cluster.hub.list(statuses=("active", "candidate"))
    assert len(entries) == 1
    assert entries[0].status == "candidate"


def test_contradictory_preference_hints_are_review_candidates(cluster):
    hints_a = [{"slot": "response_detail", "polarity": "concise", "condition": ""}]
    hints_b = [{"slot": "response_detail", "polarity": "detailed", "condition": ""}]
    learn(cluster.A.local, "Usually wants short replies", "short-source", metadata={"conflict_hints": hints_a})
    learn(cluster.B.local, "Usually wants detailed replies", "long-source", metadata={"conflict_hints": hints_b})
    for node in (cluster.A, cluster.B):
        node.journal.collect()
        deliver(node, cluster.service)
    entries = cluster.hub.list(statuses=("active", "candidate"))
    assert len(entries) == 2
    assert sum(item.status == "active" for item in entries) == 1
    reviewed = [item for item in entries if item.status == "candidate"]
    assert len(reviewed) == 1
    assert reviewed[0].metadata.get("needs_review")


def test_source_withdrawal_keeps_claim_when_another_origin_still_supports_it(cluster):
    record_a = learn(cluster.A.local, "Prefers concise answers", "a-shared")
    learn(cluster.B.local, "Prefers concise answers", "b-shared")
    for node in (cluster.A, cluster.B):
        node.journal.collect()
        deliver(node, cluster.service)
    source_id = record_a.source_ids[0]
    cluster.A.local.revoke_source(source_id)
    assert cluster.A.journal.collect() >= 1
    receipt = deliver(cluster.A, cluster.service)[0]
    assert receipt["status"] == "withdrawn"
    assert cluster.hub.get(receipt["result"]["memory_id"]).status == "active"


def test_hub_edit_and_retraction_fence_old_and_new_origin_updates(cluster):
    learn(cluster.A.local, "Prefers concise answers", "a-original")
    cluster.A.journal.collect()
    receipt = deliver(cluster.A, cluster.service)[0]
    cloud_id = receipt["result"]["memory_id"]
    changed = cluster.hub.correct(
        cloud_id, content="Prefers brief answers", expected_version=receipt["result"]["version"],
    )
    learn(cluster.B.local, "Prefers concise answers", "b-late")
    cluster.B.journal.collect()
    stale = deliver(cluster.B, cluster.service)[0]
    assert stale["status"] in {"stale", "conflict"}
    cluster.hub.retract(changed.memory_id, expected_version=changed.version)
    learn(cluster.A.local, "Prefers concise answers", "a-new-origin")
    cluster.A.journal.collect()
    after_delete = deliver(cluster.A, cluster.service)[-1]
    assert after_delete["status"] in {"stale", "conflict"}
    assert cluster.hub.list() == []


def test_same_origin_content_change_cannot_escape_human_edit_fence(cluster):
    origin = learn(cluster.A.local, "Prefers concise answers", "same-origin")
    cluster.A.journal.collect()
    receipt = deliver(cluster.A, cluster.service)[0]
    human_source = cluster.hub.register_source(MemorySourceInput(
        source_type="user_api", source_ref="human-cloud-correction", trusted_source=True,
    ))
    corrected = cluster.hub.correct(
        receipt["result"]["memory_id"], content="Prefers brief answers",
        expected_version=receipt["result"]["version"], source_id=human_source,
    )
    # A domain merge can update the existing local identity in place. This SQL
    # models that domain-level mutation without coupling the regression to a
    # separate merge fixture.
    with sqlite3.connect(cluster.A.local.db_path) as conn:
        conn.execute(
            "UPDATE memory_entries SET content=?,version=version+1 WHERE memory_id=?",
            ("Usually prefers very concise answers", origin.memory_id),
        )
    assert cluster.A.journal.collect() >= 1
    conflict = deliver(cluster.A, cluster.service)[0]
    assert conflict["status"] == "conflict"
    assert len(cluster.hub.list()) == 1
    assert cluster.hub.get(corrected.memory_id).content == "Prefers brief answers"


def test_timezone_aware_expiry_exports_future_and_withdraws_after_expiration(cluster, monkeypatch):
    future = learn(
        cluster.A.local, "Future expiry claim", "future-expiry",
        memory_expires_at="2026-10-25T08:00:00+08:00",
    )
    past = learn(
        cluster.A.local, "Already expired claim", "past-expiry",
        memory_expires_at="2020-01-01T08:00:00+08:00",
    )
    assert cluster.A.journal.collect() == 1
    payload = cluster.A.journal.pending()[0]
    assert payload["origin_id"] == future.memory_id
    assert payload["record"]["expires_at"] == "2026-10-25T00:00:00+00:00"
    assert all(item["origin_id"] != past.memory_id for item in cluster.A.journal.pending())
    accepted = deliver(cluster.A, cluster.service)[0]
    monkeypatch.setattr(replication_domain, "_now", lambda: "2026-10-25T01:00:00+00:00")
    assert cluster.A.journal.collect() >= 1
    withdrawal = next(item for item in cluster.A.journal.pending() if item["origin_id"] == future.memory_id)
    assert withdrawal["operation"] == "withdraw"
    assert deliver(cluster.A, cluster.service)[0]["status"] == "withdrawn"
    assert cluster.hub.get(accepted["result"]["memory_id"]).status == "retracted"


def test_candidate_tombstone_does_not_own_local_record_until_active_mirror(cluster):
    candidate = learn(
        cluster.A.local, "Maybe prefers concise replies", "candidate-origin", active=False,
    )
    cluster.A.journal.collect()
    deliver(cluster.A, cluster.service)
    pull(cluster.sync, cluster.A.replica)
    assert cluster.A.journal.local_visible(candidate) is True

    learn(cluster.B.local, "Maybe prefers concise replies", "active-origin", active=True)
    cluster.B.journal.collect()
    active = deliver(cluster.B, cluster.service)[0]
    assert active["result"]["status"] == "active"
    pull(cluster.sync, cluster.A.replica)
    assert cluster.A.journal.local_visible(candidate) is False


def test_expired_source_without_sql_write_withdraws_one_origin_only(cluster, monkeypatch):
    record = learn(
        cluster.A.local, "Shared claim with independent support", "expires-source",
        source_expires_at="2026-10-25T08:00:00+08:00",
    )
    learn(cluster.B.local, "Shared claim with independent support", "live-source")
    for node in (cluster.A, cluster.B):
        node.journal.collect()
        deliver(node, cluster.service)
    monkeypatch.setattr(replication_domain, "_now", lambda: "2026-10-25T01:00:00+00:00")
    assert cluster.A.journal.collect() >= 1
    payload = next(item for item in cluster.A.journal.pending() if item["origin_id"] == record.memory_id)
    assert payload["operation"] == "withdraw"
    assert deliver(cluster.A, cluster.service)[0]["status"] == "withdrawn"
    assert len(cluster.hub.list()) == 1
    assert cluster.hub.list()[0].status == "active"


def test_lost_ack_restart_replays_immutable_payload_once(cluster, monkeypatch):
    learn(cluster.A.local, "Prefers concise answers", "durable-source")
    monkeypatch.setenv("LKA_NODE_TOKEN", "synthetic-node-token")
    first = True

    def transport(method, path, payload, token):
        nonlocal first
        assert (method, path) == ("POST", "/node/sync/memory-records")
        receipt = cluster.service.submit(cluster.A.principal, payload)
        if first:
            first = False
            raise CloudSyncError("cloud_unavailable")
        return receipt

    cluster.A.synchronizer._transport = transport
    with pytest.raises(CloudSyncError):
        cluster.A.synchronizer.flush_once(node_id="A")
    pending = cluster.A.journal.pending()[0]
    restarted = NodeMemoryReplicationJournal(
        lambda: sqlite3.connect(cluster.A.local.db_path, timeout=10),
        hub_id="replication-hub", node_id="A", cache=cluster.A.cache,
    )
    assert restarted.pending()[0] == pending
    restarted_sync = MemoryReplicationSynchronizer(
        cluster.sync.config.model_copy(update={"role": "replica"}), restarted,
        transport=lambda method, path, payload, token: cluster.service.submit(cluster.A.principal, payload),
    )
    result = restarted_sync.flush_once(node_id="A")
    assert result["delivered"] == 1
    assert restarted.status()["accepted"] == 1
    assert len(cluster.hub.list()) == 1


def test_project_records_require_explicit_binding_and_publish_hub_project_id(cluster, tmp_path):
    local_project = cluster.A.local.resolve_project(tmp_path / "local-project")
    hub_project = cluster.hub.resolve_project(tmp_path / "hub-project")
    record = learn(
        cluster.A.local, "Project prefers concise summaries", "project-origin",
        scope="project", project_id=local_project,
    )
    cluster.A.journal.project_bindings[local_project] = hub_project
    cluster.A.cache.project_bindings[local_project] = hub_project
    assert cluster.A.journal.collect() >= 1
    payload = cluster.A.journal.pending()[0]
    assert payload["record"]["project_id"] == hub_project
    assert payload["record"]["scope"] == "project"
    receipt = deliver(cluster.A, cluster.service)[0]
    pull(cluster.sync, cluster.A.replica)
    mirrored = cluster.hub.get(receipt["result"]["memory_id"])
    assert mirrored.project_id == hub_project
    assert cluster.A.journal.local_visible(record) is False


def test_local_duplicate_is_suppressed_after_mirror_and_cloud_delete(cluster):
    record = learn(cluster.A.local, "Prefers concise answers", "origin")
    cluster.A.journal.collect()
    receipt = deliver(cluster.A, cluster.service)[0]
    pull(cluster.sync, cluster.A.replica)
    assert cluster.A.cache.list()  # mirrored authoritative entry
    assert cluster.A.journal.local_visible(record) is False
    cluster.hub.retract(receipt["result"]["memory_id"],
                        expected_version=receipt["result"]["version"])
    pull(cluster.sync, cluster.A.replica)
    assert cluster.A.cache.list() == []
    assert cluster.A.journal.local_visible(record) is False



def test_conflict_review_only_exposes_latest_proposal_and_withdrawal_removes_it(cluster):
    record = learn(cluster.A.local, "Prefers concise answers", "review-origin")
    cluster.A.journal.collect()
    first = deliver(cluster.A, cluster.service)[0]
    original = cluster.hub.get(first["result"]["memory_id"])
    source = cluster.hub.register_source(MemorySourceInput(source_type="user_api", source_ref="cloud-human-review", trusted_source=True))
    human = cluster.hub.correct(original.memory_id, content="Prefer human cloud wording", expected_version=original.version, source_id=source)
    with cluster.A.local._connect() as conn:
        conn.execute("UPDATE memory_entries SET content=?,version=version+1 WHERE memory_id=?", ("Node proposes new wording", record.memory_id))
    cluster.A.journal.collect()
    assert deliver(cluster.A, cluster.service)[0]["status"] == "conflict"
    review = cluster.service.list_conflicts(limit=1)
    assert len(review["items"]) == 1 and review["next_offset"] is None
    assert review["items"][0]["proposal"]["content"] == "Node proposes new wording"
    assert "sources" not in review["items"][0] and "source_ref" not in str(review)
    assert cluster.hub.get_active(human.memory_id)
    cluster.A.local.revoke_source(record.source_ids[0])
    cluster.A.journal.collect()
    assert deliver(cluster.A, cluster.service)[0]["status"] == "withdrawn"
    assert cluster.service.list_conflicts()["items"] == []


def test_blocked403_recovers_after_grant_and_private_revision_supersedes_blocked_packet(cluster, monkeypatch):
    first = learn(cluster.A.local, "403 later recovers", "blocked-scope")
    private = learn(cluster.A.local, "Do not upload this after privacy change", "later-private")
    monkeypatch.setenv("LKA_NODE_TOKEN", "synthetic-node-token")
    authorized = False
    sent = []

    def transport(method, path, payload, token):
        sent.append(payload)
        if not authorized:
            raise CloudSyncError("cloud_http_403")
        return cluster.service.submit(cluster.A.principal, payload)

    cluster.A.synchronizer._transport = transport
    result = cluster.A.synchronizer.flush_once(node_id="A")
    assert result["rejected"] == 2 and cluster.A.journal.status()["blocked"] == 2
    with cluster.A.local._connect() as conn:
        conn.execute("UPDATE memory_entries SET sensitivity='personal',version=version+1 WHERE memory_id=?", (private.memory_id,))
    authorized = True
    sent.clear()
    result = cluster.A.synchronizer.flush_once(node_id="A")
    assert result["delivered"] == 2
    assert any(item["origin_id"] == first.memory_id and item["operation"] == "upsert" for item in sent)
    assert all(item["operation"] == "withdraw" for item in sent if item["origin_id"] == private.memory_id)
    assert [item.content for item in cluster.hub.list()] == ["403 later recovers"]


def test_first_recall_after_mirrored_active_source_expires_does_not_fall_back_local(cluster, monkeypatch):
    from datetime import UTC, datetime

    import app.domains.memory as memory_domain
    import app.storage.shared_replica as replica_domain

    local = learn(cluster.A.local, "No expired mirror fallback", "ownership-race")
    cluster.A.journal.collect()
    receipt = deliver(cluster.A, cluster.service)[0]
    # A cloud source policy can be stricter than the originating node's source.
    # Its update still uses the real shared journal triggers in this fixture.
    with cluster.hub._connect() as conn:
        conn.execute("UPDATE memory_sources SET expires_at=? WHERE source_id IN "
                     "(SELECT source_id FROM memory_entry_sources WHERE memory_id=?)",
                     ("2030-01-01T00:00:00+00:00", receipt["result"]["memory_id"]))
    pull(cluster.sync, cluster.A.replica)
    # Deliberately do not recall anything before the active source expires.
    monkeypatch.setattr(memory_domain, "_now", lambda: "2031-01-01T00:00:00+00:00")

    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2031, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(replica_domain, "datetime", FutureDateTime)
    assert cluster.A.cache._records(scope="global") == []
    assert cluster.A.journal.local_visible(local) is False
    assert cluster.A.cache.list() == []
