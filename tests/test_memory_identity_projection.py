from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from app.core.memory_consolidation import MemoryConsolidator
from app.core.memory_reconciliation import scoped_memories
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput


def _publish(memory: MemoryService, ref: str, *, metadata=None, source_type="user_message", source_metadata=None,
             expires_at=None, memory_type="fact", scope="global", project_id=None):
    source_id = memory.register_source(MemorySourceInput(
        source_type=source_type, source_ref=ref, trusted_source=True,
        expires_at=expires_at, metadata=source_metadata or {},
    ))
    return memory.create(MemoryInput(
        content=f"memory {ref}", memory_type=memory_type, source_id=source_id,
        sensitivity="normal", user_confirmed=True, metadata=metadata or {},
        scope=scope, project_id=project_id,
    ))


def test_identity_projection_matches_scoped_memories_and_signature(tmp_path):
    memory = MemoryService(tmp_path / "identity.sqlite3")
    memory.ensure_schema()
    for index in range(4):
        _publish(memory, f"source-{index}")
    conflict_hints = [{"slot": "response_detail", "polarity": "concise", "condition": ""}]
    opposite_hints = [{"slot": "response_detail", "polarity": "detailed", "condition": ""}]
    conflicted = _publish(memory, "conflict-a", memory_type="preference",
                          metadata={"conflict_hints": conflict_hints})
    _publish(memory, "conflict-b", memory_type="preference", metadata={"conflict_hints": opposite_hints})
    revoked = _publish(memory, "revoked")
    expired_source = _publish(memory, "expired-source")
    expired_entry = _publish(memory, "expired-entry")
    replica = _publish(memory, "replica", source_type="node_memory_replica",
                       source_metadata={"published_status": "candidate"})
    active_replica = _publish(memory, "active-replica", source_type="node_memory_replica",
                              source_metadata={"published_status": "active"})

    with memory._connect() as conn:
        conn.execute("UPDATE memory_sources SET status='revoked' WHERE source_id=?", (revoked.source_ids[0],))
        conn.execute("UPDATE memory_sources SET expires_at=? WHERE source_id=?",
                     ("2000-01-01T00:00:00+00:00", expired_source.source_ids[0]))
        conn.execute("UPDATE memory_entries SET expires_at=? WHERE memory_id=?",
                     ("2000-01-01T00:00:00+00:00", expired_entry.memory_id))
        conn.commit()

    expected = sorted((row.memory_id, row.version) for row in scoped_memories(memory, "global", None)
                      if row.sensitivity in {"normal", "public"})
    projected = sorted((memory_id, version) for memory_id, version, sensitivity
                       in memory.active_identities(scope="global") if sensitivity in {"normal", "public"})
    assert projected == expected
    assert conflicted.memory_id not in {memory_id for memory_id, _ in projected}
    assert revoked.memory_id not in {memory_id for memory_id, _ in projected}
    assert expired_source.memory_id not in {memory_id for memory_id, _ in projected}
    assert expired_entry.memory_id not in {memory_id for memory_id, _ in projected}
    assert replica.memory_id not in {memory_id for memory_id, _ in projected}
    assert active_replica.memory_id in {memory_id for memory_id, _ in projected}
    assert MemoryConsolidator(memory).signature("global", None) == hashlib.sha256(
        json.dumps(expected).encode()
    ).hexdigest()


def test_identity_projection_preserves_review_metadata_and_peer_scope(tmp_path):
    memory = MemoryService(tmp_path / "review-boundaries.sqlite3")
    memory.ensure_schema()
    with memory._connect() as conn:
        conn.execute("INSERT INTO memory_projects(project_id,created_at) VALUES('p1','now'),('p2','now')")
        conn.commit()

    hints_a = [{"slot": "response_detail", "polarity": "concise", "condition": ""}]
    hints_b = [{"slot": "response_detail", "polarity": "detailed", "condition": ""}]
    cross_project_owner = _publish(memory, "p1-owner", scope="project", project_id="p1")
    cross_project_peer = _publish(memory, "p2-peer", scope="project", project_id="p2")
    expired_peer_owner = _publish(memory, "expired-peer-owner")
    expired_peer = _publish(memory, "expired-peer")
    revoked_peer_owner = _publish(memory, "revoked-peer-owner")
    revoked_peer = _publish(memory, "revoked-peer")
    malformed_missing = _publish(memory, "review-missing")
    malformed_type = _publish(memory, "review-type")
    empty_conflicts = _publish(memory, "review-empty")
    stale_conflict = _publish(memory, "review-stale-id")

    with memory._connect() as conn:
        for memory_id, metadata in (
            (cross_project_owner.memory_id, {"needs_review": True, "conflict_ids": [cross_project_peer.memory_id],
                                             "conflict_hints": hints_a}),
            (cross_project_peer.memory_id, {"conflict_hints": hints_b}),
            (expired_peer_owner.memory_id, {"needs_review": True, "conflict_ids": [expired_peer.memory_id],
                                            "conflict_hints": hints_a}),
            (expired_peer.memory_id, {"conflict_hints": hints_b}),
            (revoked_peer_owner.memory_id, {"needs_review": True, "conflict_ids": [revoked_peer.memory_id],
                                            "conflict_hints": hints_a}),
            (revoked_peer.memory_id, {"conflict_hints": hints_b}),
            (malformed_missing.memory_id, {"needs_review": True}),
            (malformed_type.memory_id, {"needs_review": True, "conflict_ids": "peer-id"}),
            (empty_conflicts.memory_id, {"needs_review": True, "conflict_ids": []}),
            (stale_conflict.memory_id, {"needs_review": True, "conflict_ids": ["missing-peer"]}),
        ):
            conn.execute("UPDATE memory_entries SET metadata=? WHERE memory_id=?",
                         (json.dumps(metadata), memory_id))
        conn.execute("UPDATE memory_sources SET expires_at=? WHERE source_id=?",
                     ((datetime.now(UTC) - timedelta(days=1)).isoformat(), expired_peer.source_ids[0]))
        conn.execute("UPDATE memory_sources SET status='revoked' WHERE source_id=?", (revoked_peer.source_ids[0],))
        conn.commit()

    expected = sorted((row.memory_id, row.version) for row in scoped_memories(memory, "global", None)
                      if row.sensitivity in {"normal", "public"})
    projected = sorted((memory_id, version) for memory_id, version, sensitivity
                       in memory.active_identities(scope="global") if sensitivity in {"normal", "public"})
    assert projected == expected
    ids = {memory_id for memory_id, _ in projected}
    assert expired_peer_owner.memory_id in ids
    assert revoked_peer_owner.memory_id in ids
    assert malformed_missing.memory_id not in ids
    assert malformed_type.memory_id not in ids
    assert empty_conflicts.memory_id in ids
    assert stale_conflict.memory_id in ids  # Old metadata clears when its peer is gone.

    project_expected = sorted((row.memory_id, row.version)
                              for row in scoped_memories(memory, "project", "p1")
                              if row.sensitivity in {"normal", "public"})
    project_projected = sorted((memory_id, version) for memory_id, version, sensitivity
                               in memory.active_identities(scope="project", project_id="p1")
                               if sensitivity in {"normal", "public"})
    assert project_projected == project_expected
    assert cross_project_owner.memory_id in {memory_id for memory_id, _ in project_projected}


class _CountingConnection:
    def __init__(self, conn):
        self.conn = conn
        self.selects = 0

    @property
    def row_factory(self):
        return self.conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.conn.row_factory = value

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("SELECT"):
            self.selects += 1
        return self.conn.execute(sql, *args)

    def close(self):
        self.conn.close()


def _select_count_for_list_and_projection(tmp_path, count):
    memory = MemoryService(tmp_path / f"count-{count}.sqlite3")
    memory.ensure_schema()
    for index in range(count):
        _publish(memory, f"source-{index}")

    original_connect = memory._connect
    observed = []

    def counted_connect():
        conn = _CountingConnection(original_connect())
        observed.append(conn)
        return conn

    memory._connect = counted_connect
    assert len(memory.list(scope="global", limit=1000)) == count
    list_selects = observed[-1].selects
    memory.active_identities(scope="global")
    projection_selects = observed[-1].selects
    return list_selects, projection_selects


def test_list_and_identity_projection_select_counts_are_bounded(tmp_path):
    small = _select_count_for_list_and_projection(tmp_path, 10)
    large = _select_count_for_list_and_projection(tmp_path, 100)
    assert small == large
    assert small[0] <= 3
    assert small[1] == 1
