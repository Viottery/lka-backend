from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.domains.memory import (
    MemoryConflictError,
    MemoryInput,
    MemoryPublicationLeaseError,
    MemoryService,
    MemorySourceInput,
)


def _service(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()
    return service


def _source(service, ref="session:1", *, trusted=True, expires_at=None):
    return service.register_source(MemorySourceInput(
        source_type="conversation", source_ref=ref, trusted_source=trusted,
        expires_at=expires_at,
    ))


def _create(service, source_id, text="Prefer concise responses", **kwargs):
    return service.create(MemoryInput(content=text, source_id=source_id, **kwargs))


def test_project_identity_and_search_are_isolated(tmp_path):
    service = _service(tmp_path)
    project_a = service.resolve_project("/work/a")
    project_b = service.resolve_project("/work/b")
    assert project_a == service.resolve_project("/work/a")
    assert project_a != project_b

    source = _source(service)
    _create(service, source, "Use pytest", scope="project", project_id=project_a)
    assert service.search("pytest", project_id=project_a)
    assert service.search("pytest", project_id=project_b) == []
    if os.name != "nt":
        assert service.resolve_project("/work/Case") != service.resolve_project("/work/case")


def test_concurrent_project_resolution_has_one_identity(tmp_path):
    service = _service(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(service.resolve_project, [tmp_path / "project"] * 20))
    assert len(set(results)) == 1


def test_untrusted_source_creates_candidate_only(tmp_path):
    service = _service(tmp_path)
    record = _create(service, _source(service, trusted=False))
    assert record.status == "candidate"
    assert service.list(statuses=("active",)) == []


def test_duplicate_memory_links_provenance_without_duplicate_entry(tmp_path):
    service = _service(tmp_path)
    first_source = _source(service, "session:one")
    second_source = _source(service, "session:two")
    first = _create(service, first_source, dedupe_key="response-style")
    duplicate = _create(service, second_source, dedupe_key="response-style")
    assert duplicate.memory_id == first.memory_id
    assert set(duplicate.source_ids) == {first_source, second_source}
    assert [event["event_type"] for event in service.events(first.memory_id)] == [
        "created", "duplicate_source_linked"
    ]


def test_same_source_retry_is_a_true_noop_and_does_not_add_history(tmp_path):
    service = _service(tmp_path)
    source = _source(service, "session:retry", trusted=False)
    first = _create(
        service, source, "Prefer concise replies", memory_type="preference",
        confidence=0.9, dedupe_key="same-source-retry",
    )
    repeated = _create(
        service, source, "Prefer concise replies", memory_type="preference",
        confidence=0.9, dedupe_key="same-source-retry",
    )
    assert repeated.memory_id == first.memory_id
    assert repeated.version == first.version == 1
    assert len(service.events(first.memory_id)) == 1


def test_repeat_promotion_requires_confident_new_evidence_and_live_sources(tmp_path):
    service = _service(tmp_path)
    first_source = _source(service, "session:first", trusted=False)
    second_source = _source(service, "session:second", trusted=False)
    first = _create(
        service, first_source, "Prefer concise replies", memory_type="preference",
        confidence=0.9, dedupe_key="confidence-gate",
    )
    second = _create(
        service, second_source, "Prefer concise replies", memory_type="preference",
        confidence=0.4, dedupe_key="confidence-gate",
    )
    assert second.status == "candidate"
    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute(
            "UPDATE memory_sources SET expires_at=? WHERE source_id IN (?,?)",
            ("2000-01-01T00:00:00+00:00", first_source, second_source),
        )
        conn.commit()
    finally:
        conn.close()
    third_source = _source(service, "session:third", trusted=False)
    third = _create(
        service, third_source, "Prefer concise replies", memory_type="preference",
        confidence=0.95, dedupe_key="confidence-gate",
    )
    assert third.memory_id == first.memory_id
    assert third.status == "candidate"


def test_independent_repeated_preference_evidence_promotes_candidate(tmp_path):
    service = _service(tmp_path)
    first = _create(
        service, _source(service, "conversation:one", trusted=False),
        "Prefer concise responses", memory_type="preference", confidence=0.9,
        dedupe_key="stable-preference",
    )
    assert first.status == "candidate"
    repeated = _create(
        service, _source(service, "conversation:two", trusted=False),
        "Prefer concise responses", memory_type="preference", confidence=0.9,
        dedupe_key="stable-preference",
    )
    assert repeated.status == "active"
    assert service.events(first.memory_id)[-1]["event_type"] == "promoted_by_independent_evidence"


def test_confirmation_promotes_existing_candidate_but_not_retracted_memory(tmp_path):
    service = _service(tmp_path)
    first = _create(service, _source(service, "session:candidate", trusted=False))
    assert first.status == "candidate"
    confirmed = _create(
        service, _source(service, "session:confirmed", trusted=False),
        user_confirmed=True,
    )
    assert confirmed.memory_id == first.memory_id
    assert confirmed.status == "active"
    assert service.events(first.memory_id)[-1]["event_type"] == "promoted_by_confirmation"

    retracted = service.retract(confirmed.memory_id, expected_version=confirmed.version)
    repeat = _create(
        service, _source(service, "session:later", trusted=False), user_confirmed=True,
    )
    assert retracted.status == repeat.status == "retracted"


def test_revocation_preserves_claim_supported_by_another_source_and_is_idempotent(tmp_path):
    service = _service(tmp_path)
    first_source = _source(service, "session:one")
    second_source = _source(service, "session:two")
    first = _create(service, first_source, dedupe_key="same")
    _create(service, second_source, dedupe_key="same")
    assert service.revoke_source(first_source) == 0
    assert service.get(first.memory_id).status == "active"
    assert service.search("concise", scope="global")
    assert service.revoke_source(first_source) == 0
    assert service.revoke_source(second_source) == 1
    assert service.revoke_source(second_source) == 0
    assert service.get(first.memory_id).status == "retracted"


def test_correction_preserves_old_version_and_uses_cas(tmp_path):
    service = _service(tmp_path)
    original = _create(service, _source(service))
    corrected = service.correct(original.memory_id, content="Prefer detailed responses",
                                expected_version=1)
    assert corrected.status == "active"
    assert corrected.supersedes_id == original.memory_id
    assert service.get(original.memory_id).status == "superseded"
    assert service.get(original.memory_id).version == 2
    with pytest.raises(MemoryConflictError):
        service.correct(original.memory_id, content="stale correction", expected_version=1)


def test_expired_memories_and_expired_sources_are_not_searchable(tmp_path):
    service = _service(tmp_path)
    expired = "2000-01-01T00:00:00+00:00"
    source = _source(service, "session:expired", expires_at=expired)
    with pytest.raises(ValueError, match="expired"):
        _create(service, source, "old fact")
    live_source = _source(service, "session:live")
    record = _create(service, live_source, "temporary fact", expires_at=expired)
    assert record.status == "active"
    assert service.search("temporary") == []
    assert service.list(include_expired=True)


def test_source_revocation_retracts_derived_memories(tmp_path):
    service = _service(tmp_path)
    source = _source(service)
    record = _create(service, source)
    assert service.revoke_source(source) == 1
    assert service.get(record.memory_id).status == "retracted"
    assert service.search("concise") == []


def test_retraction_version_conflict_and_export(tmp_path):
    service = _service(tmp_path)
    record = _create(service, _source(service))
    with pytest.raises(MemoryConflictError):
        service.retract(record.memory_id, expected_version=2)
    retracted = service.retract(record.memory_id, expected_version=1)
    assert retracted.status == "retracted"
    assert service.export()["memories"] == []
    assert len(service.events(record.memory_id)) == 2


def test_create_failure_rolls_back_entry_and_events(tmp_path):
    service = _service(tmp_path)
    source = _source(service)
    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute("CREATE TRIGGER fail_memory_event BEFORE INSERT ON memory_events "
                     "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        _create(service, source)
    conn = sqlite3.connect(service.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_publication_lease_accepts_only_current_live_owner_and_epoch(tmp_path):
    from app.core.background_jobs import BackgroundJobStore

    service = _service(tmp_path)
    jobs = BackgroundJobStore(service.db_path)
    jobs.ensure_schema()
    job = jobs.enqueue("memory_extract", "session", "turn-1", {})
    claimed = jobs.claim("worker-a", 60)
    assert claimed["job_id"] == job["job_id"]
    source = _source(service, "source:leased", trusted=False)
    payload = MemoryInput(content="leased preference", source_id=source)
    created = service.create(
        payload,
        publication_lease=(job["job_id"], "worker-a", claimed["lease_epoch"]),
    )
    assert created.status == "candidate"
    assert service.create(
        payload,
        publication_lease=(job["job_id"], "worker-a", claimed["lease_epoch"]),
    ).version == created.version
    with pytest.raises(MemoryPublicationLeaseError) as duplicate_error:
        service.create(
            payload,
            publication_lease=(job["job_id"], "wrong-owner", claimed["lease_epoch"]),
        )
    assert duplicate_error.value.code == "publication_lease_fenced"

    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute(
            "UPDATE background_jobs SET lease_expires_at=? WHERE job_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job["job_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    taken_over = jobs.claim("worker-b", 60)
    assert taken_over["lease_epoch"] == claimed["lease_epoch"] + 1
    next_source = _source(service, "source:stale-worker", trusted=False)
    with pytest.raises(MemoryPublicationLeaseError) as error:
        service.create(
            MemoryInput(content="stale publication", source_id=next_source),
            publication_lease=(job["job_id"], "worker-a", claimed["lease_epoch"]),
        )
    assert error.value.code == "publication_lease_fenced"
    assert service.list(statuses=("candidate",)) == [created]


def test_publication_lease_rejects_expired_or_missing_job_but_default_is_compatible(tmp_path):
    from app.core.background_jobs import BackgroundJobStore

    service = _service(tmp_path)
    jobs = BackgroundJobStore(service.db_path)
    jobs.ensure_schema()
    source = _source(service, "source:without-job")
    # Existing user and non-queue callers retain their pre-lease behavior.
    created = _create(service, source, "normal publication")
    assert created.status == "active"
    with pytest.raises(MemoryPublicationLeaseError) as error:
        service.create(
            MemoryInput(content="no job", source_id=source),
            publication_lease=("missing-job", "worker", 1),
        )
    assert error.value.code == "publication_lease_missing"

    job = jobs.enqueue("memory_extract", "session", "turn-expired", {})
    claimed = jobs.claim("worker", 60)
    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute("UPDATE background_jobs SET lease_expires_at=? WHERE job_id=?",
                     ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job["job_id"]))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(MemoryPublicationLeaseError) as error:
        service.create(
            MemoryInput(content="expired publication", source_id=source),
            publication_lease=(job["job_id"], "worker", claimed["lease_epoch"]),
        )
    assert error.value.code == "publication_lease_expired"


def test_explicit_project_move_keeps_identity_but_reused_old_path_does_not(tmp_path):
    service = _service(tmp_path)
    original, moved = tmp_path / "old", tmp_path / "new"
    project = service.resolve_project(original)
    service.bind_project_path(project, moved, old_path=original)
    assert service.resolve_project(moved) == project
    assert service.resolve_project(original, create=False) is None
    reused = service.resolve_project(original)
    assert reused != project
    with pytest.raises(ValueError, match="Old workspace"):
        service.bind_project_path(project, tmp_path / "third", old_path=original)
    with pytest.raises(ValueError, match="another project"):
        service.bind_project_path(project, original, old_path=moved)
    assert service.resolve_project(moved) == project


def test_only_new_confirmed_source_can_extend_expired_claim_and_retry_is_idempotent(tmp_path):
    service = _service(tmp_path)
    original = _create(service, _source(service, "original", trusted=False), "Old deadline",
                       expires_at="2000-01-01T00:00:00+00:00", user_confirmed=True)
    assert service.get_active(original.memory_id) is None
    inferred = _create(service, _source(service, "model", trusted=False), "Old deadline",
                       expires_at="2100-01-01T00:00:00+00:00")
    assert inferred.expires_at == original.expires_at
    assert service.get_active(original.memory_id) is None
    confirmed_source = _source(service, "confirmed", trusted=False)
    renewed = _create(service, confirmed_source, "Old deadline",
                      expires_at="2100-01-01T00:00:00+00:00", user_confirmed=True)
    assert renewed.expires_at == "2100-01-01T00:00:00+00:00"
    assert service.get_active(renewed.memory_id) is not None
    assert service.events(renewed.memory_id)[-1]["event_type"] == "expiry_extended"
    retried = _create(service, confirmed_source, "Old deadline",
                      expires_at="2100-01-01T00:00:00+00:00", user_confirmed=True)
    assert retried.version == renewed.version
