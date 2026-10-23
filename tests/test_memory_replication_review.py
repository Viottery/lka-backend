"""Independent regressions for authoritative replica activation and source lifecycle."""
from datetime import UTC, datetime

import pytest

from app.domains.memory import MemoryInput, MemorySourceInput
from tests.test_memory_replication import cluster as cluster_fixture
from tests.test_memory_replication import deliver, learn, pull


@pytest.fixture
def cluster(tmp_path):
    return cluster_fixture.__wrapped__(tmp_path)


def test_expired_active_origin_cannot_activate_remaining_candidate_origin(cluster, monkeypatch):
    learn(cluster.A.local, "Prefers concise answers", "active-origin")
    learn(cluster.B.local, "Prefers concise answers", "candidate-origin", active=False)
    cluster.A.journal.collect()
    cluster.B.journal.collect()
    payload = cluster.A.journal.pending()[0]
    payload["sources"][0]["expires_at"] = "2030-01-01T00:00:00+00:00"
    first = cluster.service.submit(cluster.A.principal, payload)
    deliver(cluster.B, cluster.service)
    memory_id = first["result"]["memory_id"]
    assert cluster.hub.get_active(memory_id) is not None
    monkeypatch.setattr("app.domains.memory._now", lambda: "2031-01-01T00:00:00+00:00")
    # B's candidate source remains live; it cannot inherit A's expired
    # activation authority merely because exact dedup linked their provenance.
    assert cluster.hub.get_active(memory_id) is None
    assert cluster.hub.list() == []


def test_offline_mirror_expiry_does_not_inherit_candidate_support(cluster, monkeypatch):
    source = cluster.A.local.register_source(MemorySourceInput(
        source_type="agent_learning", source_ref="expiring-active-origin",
        expires_at="2030-01-01T00:00:00+00:00",
    ))
    cluster.A.local.create(MemoryInput(content="Prefers concise answers", source_id=source,
                                       memory_type="preference", sensitivity="normal", user_confirmed=True))
    learn(cluster.B.local, "Prefers concise answers", "candidate-origin", active=False)
    for node in (cluster.A, cluster.B):
        node.journal.collect()
        deliver(node, cluster.service)
    for node in (cluster.A, cluster.B):
        pull(cluster.sync, node.replica)
        assert len(node.cache.search("concise")) == 1
        assert node.cache.search("concise")[0].metadata["origin"] == "cloud_replica"

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = cls(2031, 1, 1, tzinfo=UTC)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr("app.storage.shared_replica.datetime", FutureClock)
    monkeypatch.setattr("app.domains.memory._now", lambda: "2031-01-01T00:00:00+00:00")
    # No further hub pull or node withdrawal runs: the previously downloaded
    # cache must enforce source expiry while disconnected.
    for node in (cluster.A, cluster.B):
        assert node.replica.read_memories(scope="global") == []
        assert node.cache.list() == []
        assert node.cache.search("concise") == []
