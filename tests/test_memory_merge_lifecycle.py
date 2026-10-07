from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.domains.memory import (
    MemoryConflictError,
    MemoryInput,
    MemoryPublicationLeaseError,
    MemoryPublicationSuppressed,
    MemoryService,
    MemorySourceInput,
)


def service_for(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()
    return service


def add_memory(service, ref, content, *, scope="global", project_id=None, memory_type="fact",
               sensitivity="personal", expires_at=None, metadata=None, trusted=True):
    source_id = service.register_source(MemorySourceInput(
        source_type="conversation", source_ref=ref, trusted_source=trusted,
    ))
    return service.create(MemoryInput(
        content=content, source_id=source_id, scope=scope, project_id=project_id,
        memory_type=memory_type, sensitivity=sensitivity, expires_at=expires_at,
        metadata=metadata or {},
    ))


def merge(service, target, *others, content="Merged durable memory"):
    records = [target, *others]
    return service.merge_records({r.memory_id: r.version for r in records},
                                 target_memory_id=target.memory_id, content=content)


def test_merge_keeps_full_history_sources_and_payload_free_metadata(tmp_path):
    service = service_for(tmp_path)
    target = add_memory(service, "one", "Original target")
    other = add_memory(service, "two", "Original source")
    before_target_sources = target.source_ids
    before_other_sources = other.source_ids
    result = merge(service, target, other)

    assert result.memory_id == target.memory_id
    assert result.content == "Merged durable memory"
    assert result.status == "active" and result.version == target.version + 1
    assert set(result.source_ids) == set(before_target_sources + before_other_sources)
    assert service.get(other.memory_id).status == "superseded"
    assert [event["event_type"] for event in service.events(target.memory_id)] == ["created", "memory_merged"]
    assert service.events(target.memory_id)[-1]["payload"]["merge_id"] == result.metadata["last_merge_id"]
    history = service.merges()
    assert len(history) == 1
    assert set(history[0]) == {"merge_id", "target_memory_id", "member_ids", "after_versions",
                               "after_statuses", "created_at", "undone_at"}
    assert "content" not in json.dumps(history)
    assert service.get_merge(history[0]["merge_id"]) == history[0]
    assert service.merges(scope="project", project_id="unrelated") == []


def test_merge_retry_is_idempotent_and_stale_any_member_rolls_back(tmp_path):
    service = service_for(tmp_path)
    first = add_memory(service, "one", "First")
    second = add_memory(service, "two", "Second")
    versions = {first.memory_id: first.version, second.memory_id: second.version}
    done = service.merge_records(versions, target_memory_id=first.memory_id, content="Combined")
    retry = service.merge_records(versions, target_memory_id=first.memory_id, content="Combined")
    assert retry.version == done.version
    assert len(service.merges()) == 1

    a = add_memory(service, "three", "Third")
    b = add_memory(service, "four", "Fourth")
    service.correct(b.memory_id, content="Fourth corrected", expected_version=b.version)
    with pytest.raises(MemoryConflictError):
        service.merge_records({a.memory_id: a.version, b.memory_id: b.version},
                              target_memory_id=a.memory_id, content="Must roll back")
    assert service.get(a.memory_id).version == a.version
    assert len(service.merges()) == 1


@pytest.mark.parametrize("withdrawal", ["learning", "expiry", "source"])
def test_cached_merge_retry_revalidates_target_eligibility(tmp_path, withdrawal):
    service = service_for(tmp_path)
    target = add_memory(service, "retry-target", "Retry target")
    other = add_memory(service, "retry-other", "Retry other")
    versions = {target.memory_id: target.version, other.memory_id: other.version}
    merged = service.merge_records(versions, target_memory_id=target.memory_id, content="Merged retry")
    conn = sqlite3.connect(service.db_path)
    if withdrawal == "expiry":
        conn.execute("UPDATE memory_entries SET expires_at='2000-01-01T00:00:00+00:00' WHERE memory_id=?",
                     (target.memory_id,))
    elif withdrawal == "source":
        conn.execute("UPDATE memory_sources SET status='revoked' WHERE source_id IN (?,?)",
                     (target.source_ids[0], other.source_ids[0]))
    conn.commit()
    conn.close()
    if withdrawal == "learning":
        service.set_learning_enabled(scope="global", project_id=None, enabled=False)
    with pytest.raises(MemoryPublicationSuppressed):
        service.merge_records(versions, target_memory_id=target.memory_id, content="Merged retry")
    assert service.get(target.memory_id).version == merged.version

@pytest.mark.parametrize("change", ["scope", "type", "sensitivity", "expiry"])
def test_merge_refuses_incompatible_members(tmp_path, change):
    service = service_for(tmp_path)
    expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    target = add_memory(service, "one", "Target", expires_at=expiry)
    options = {"scope": "global", "project_id": None, "memory_type": "fact",
               "sensitivity": "personal", "expires_at": expiry}
    if change == "scope":
        options.update(scope="project", project_id=service.resolve_project(tmp_path / "project"))
    elif change == "type":
        options["memory_type"] = "preference"
    elif change == "sensitivity":
        options["sensitivity"] = "high"
    elif change == "expiry":
        options["expires_at"] = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    other = add_memory(service, "two", "Other", **options)
    with pytest.raises(MemoryPublicationSuppressed):
        merge(service, target, other)
    assert service.get(target.memory_id).version == target.version
    assert service.get(other.memory_id).status == "active"


def test_merge_refuses_revoked_or_expired_sources_and_never_promotes_candidate(tmp_path):
    service = service_for(tmp_path)
    target = add_memory(service, "one", "Target")
    expired = add_memory(service, "two", "Expired")
    conn = sqlite3.connect(service.db_path)
    conn.execute("UPDATE memory_sources SET expires_at=? WHERE source_id=?",
                 ("2000-01-01T00:00:00+00:00", expired.source_ids[0]))
    conn.commit()
    conn.close()
    with pytest.raises(MemoryPublicationSuppressed):
        merge(service, target, expired)

    mixed = add_memory(service, "mixed", "Mixed valid and historical source")
    historical_expired = service.register_source(MemorySourceInput(
        source_type="conversation", source_ref="mixed-expired",
        expires_at="2000-01-01T00:00:00+00:00",
    ))
    historical_revoked = service.register_source(MemorySourceInput(
        source_type="conversation", source_ref="mixed-revoked",
    ))
    conn = sqlite3.connect(service.db_path)
    conn.execute("UPDATE memory_sources SET status='revoked' WHERE source_id=?", (historical_revoked,))
    conn.executemany("INSERT INTO memory_entry_sources VALUES(?,?)",
                     [(mixed.memory_id, historical_expired), (mixed.memory_id, historical_revoked)])
    conn.commit()
    conn.close()
    mixed_result = merge(service, mixed, target)
    assert set(mixed_result.source_ids) >= {mixed.source_ids[0], historical_expired, historical_revoked}
    target = service.get(target.memory_id)

    revoked = add_memory(service, "revoked", "Revoked source")
    conn = sqlite3.connect(service.db_path)
    conn.execute("UPDATE memory_sources SET status='revoked' WHERE source_id=?", (revoked.source_ids[0],))
    conn.commit()
    conn.close()
    with pytest.raises(MemoryPublicationSuppressed):
        merge(service, target, revoked)

    expired_record = add_memory(service, "record-expiry", "Expired record")
    conn = sqlite3.connect(service.db_path)
    conn.execute("UPDATE memory_entries SET expires_at='2000-01-01T00:00:00+00:00' WHERE memory_id=?",
                 (expired_record.memory_id,))
    conn.commit()
    conn.close()
    with pytest.raises(MemoryPublicationSuppressed):
        merge(service, target, expired_record)

    candidate = add_memory(service, "three", "Candidate", trusted=False)
    with pytest.raises(MemoryPublicationSuppressed):
        merge(service, target, candidate)
    assert service.get(candidate.memory_id).status == "candidate"

    disabled = add_memory(service, "disabled", "Learning disabled")
    service.set_learning_enabled(scope="global", project_id=None, enabled=False)
    with pytest.raises(MemoryPublicationSuppressed):
        merge(service, target, disabled)


def test_merge_rejects_review_and_conflicting_hints(tmp_path):
    service = service_for(tmp_path)
    reviewed = add_memory(service, "review", "Reviewed", metadata={"needs_review": True})
    peer = add_memory(service, "peer", "Peer")
    with pytest.raises(MemoryPublicationSuppressed):
        merge(service, reviewed, peer)

    concise = add_memory(service, "concise", "Concise", memory_type="preference",
                          metadata={"conflict_hints": [{"slot": "response_detail", "polarity": "concise", "condition": ""}]})
    detailed = add_memory(service, "detailed", "Detailed", memory_type="preference",
                          metadata={"conflict_hints": [{"slot": "response_detail", "polarity": "detailed", "condition": ""}]})
    conn = sqlite3.connect(service.db_path)
    row = conn.execute("SELECT metadata FROM memory_entries WHERE memory_id=?", (detailed.memory_id,)).fetchone()
    details = json.loads(row[0])
    details.pop("needs_review", None)
    details.pop("conflict_ids", None)
    conn.execute("UPDATE memory_entries SET status='active',metadata=? WHERE memory_id=?",
                 (json.dumps(details), detailed.memory_id))
    conn.commit()
    conn.close()
    with pytest.raises(MemoryPublicationSuppressed):
        merge(service, concise, detailed)


def test_undo_restores_all_records_and_refuses_after_later_correction(tmp_path):
    service = service_for(tmp_path)
    target = add_memory(service, "one", "Target original")
    other = add_memory(service, "two", "Other original")
    merge(service, target, other)
    history = service.merges()[0]
    restored = service.undo_merge(history["merge_id"], expected_versions=history["after_versions"])
    assert {record.memory_id: (record.content, record.status) for record in restored} == {
        target.memory_id: ("Target original", "active"), other.memory_id: ("Other original", "active")
    }
    assert all(record.version == history["after_versions"][record.memory_id] + 1 for record in restored)
    assert {record.memory_id: record.source_ids for record in restored} == {
        target.memory_id: target.source_ids, other.memory_id: other.source_ids
    }

    target2 = add_memory(service, "three", "Target two")
    other2 = add_memory(service, "four", "Other two")
    merge(service, target2, other2)
    history2 = next(item for item in service.merges() if item["target_memory_id"] == target2.memory_id)
    corrected = service.correct(target2.memory_id, content="Corrected after merge", expected_version=history2["after_versions"][target2.memory_id])
    with pytest.raises(MemoryConflictError):
        service.undo_merge(history2["merge_id"], expected_versions=history2["after_versions"])
    assert corrected.content == "Corrected after merge"
    assert service.get(target2.memory_id).status == "superseded"


def test_undo_fences_pair_until_a_member_is_corrected(tmp_path):
    service = service_for(tmp_path)
    target = add_memory(service, "undo-one", "Original one")
    other = add_memory(service, "undo-two", "Original two")
    merge(service, target, other)
    history = service.merges()[0]
    restored = service.undo_merge(history["merge_id"], expected_versions=history["after_versions"])
    by_id = {record.memory_id: record for record in restored}
    assert by_id[target.memory_id].metadata["consolidation_undo_peers"] == [other.memory_id]
    assert by_id[other.memory_id].metadata["consolidation_undo_peers"] == [target.memory_id]
    with pytest.raises(MemoryPublicationSuppressed, match="separated by merge undo"):
        merge(service, by_id[target.memory_id], by_id[other.memory_id])
    assert service.get(target.memory_id).content == "Original one"
    assert service.get(other.memory_id).content == "Original two"
    assert set(service.get(target.memory_id).source_ids) == set(target.source_ids)

    correction = service.correct(target.memory_id, content="Corrected new revision",
                                 expected_version=by_id[target.memory_id].version)
    assert "consolidation_undo_peers" not in correction.metadata
    remixed = merge(service, correction, by_id[other.memory_id], content="New revision may reconcile")
    assert remixed.content == "New revision may reconcile"


def test_lease_is_rechecked_before_merge_commit(tmp_path):
    service = service_for(tmp_path)
    target = add_memory(service, "one", "Target")
    other = add_memory(service, "two", "Other")
    conn = sqlite3.connect(service.db_path)
    conn.execute("CREATE TABLE background_jobs(job_id TEXT,status TEXT,lease_owner TEXT,lease_epoch INTEGER,lease_expires_at TEXT,deadline TEXT)")
    conn.execute("INSERT INTO background_jobs VALUES('job','running','worker',1,?,NULL)",
                 ((datetime.now(UTC) + timedelta(minutes=5)).isoformat(),))
    conn.execute("CREATE TRIGGER cancel_merge_job AFTER INSERT ON memory_merges "
                 "BEGIN UPDATE background_jobs SET status='cancelled' WHERE job_id='job'; END")
    conn.commit()
    conn.close()
    with pytest.raises(MemoryPublicationLeaseError):
        service.merge_records({target.memory_id: target.version, other.memory_id: other.version},
                              target_memory_id=target.memory_id, content="Combined",
                              publication_lease=("job", "worker", 1))
    assert service.get(target.memory_id).version == target.version
    assert service.get(other.memory_id).status == "active"
    assert service.merges() == []
