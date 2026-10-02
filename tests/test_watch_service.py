from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domains.watch import BriefingInput, WatchInput, WatchPatch, WatchService
from app.storage.db import connect, get_db_path


def _service(tmp_path):
    db_path = get_db_path(tmp_path / "data")
    service = WatchService(lambda: connect(db_path))
    service.initialize()
    return service


def _payload(**overrides):
    values = {
        "title": "Track launch",
        "goal": "Find official launch updates",
        "timezone": "America/Los_Angeles",
        "daily_time": "09:30",
        "categories": ["news", "web"],
        "scope": {"domains": ["example.com"]},
    }
    values.update(overrides)
    return WatchInput(**values)


def test_watch_crud_pause_resume_and_soft_delete(tmp_path):
    service = _service(tmp_path)
    watch = service.create(_payload(), now=datetime(2026, 3, 1, 10, tzinfo=UTC))

    assert watch["status"] == "active"
    assert watch["scope"] == {"domains": ["example.com"]}
    assert service.next_run(
        watch["watch_id"], after=datetime(2026, 3, 1, 10, tzinfo=UTC)
    ) == datetime(2026, 3, 1, 17, 30, tzinfo=UTC)

    updated = service.update(
        watch["watch_id"], WatchPatch(title="Track official updates", categories=["web"])
    )
    assert updated["title"] == "Track official updates"
    assert updated["version"] == 2

    paused = service.set_paused(watch["watch_id"], True)
    assert paused["status"] == "paused"
    assert service.next_run(watch["watch_id"]) is None
    assert service.set_paused(watch["watch_id"], False)["status"] == "active"

    deleted = service.delete(watch["watch_id"])
    assert deleted["status"] == "deleted"
    assert service.list() == []
    assert service.get(watch["watch_id"], include_deleted=True)["deleted_at"]
    with pytest.raises(KeyError):
        service.get(watch["watch_id"])


def test_update_watch_validates_persisted_and_patched_date_boundaries(tmp_path):
    service = _service(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 12, 31, tzinfo=UTC)
    watch = service.create(_payload(starts_at=start, ends_at=end))
    renamed = service.update(watch["watch_id"], WatchPatch(title="Renamed"))
    assert renamed["title"] == "Renamed"
    assert renamed["starts_at"] == start.isoformat()
    assert renamed["ends_at"] == end.isoformat()
    extended_end = end + timedelta(days=1)
    updated = service.update(watch["watch_id"], WatchPatch(ends_at=extended_end))
    assert updated["ends_at"] == extended_end.isoformat()
    with pytest.raises(ValueError, match="ends_at must not precede starts_at"):
        service.update(watch["watch_id"], WatchPatch(starts_at=extended_end + timedelta(days=1)))
    assert service.get(watch["watch_id"])["starts_at"] == start.isoformat()


def test_daily_schedule_is_timezone_aware_and_handles_dst_transition(tmp_path):
    service = _service(tmp_path)
    watch = service.create(
        _payload(timezone="America/New_York", daily_time="09:00"),
        now=datetime(2026, 3, 7, 15, tzinfo=UTC),
    )

    before_dst = service.next_run(watch["watch_id"], after=datetime(2026, 3, 7, 15, tzinfo=UTC))
    after_dst = service.next_run(watch["watch_id"], after=datetime(2026, 3, 8, 14, 30, tzinfo=UTC))

    assert before_dst == datetime(2026, 3, 8, 13, tzinfo=UTC)
    assert after_dst == datetime(2026, 3, 9, 13, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        service.create(_payload(timezone="Mars/Olympus"))


def test_occurrence_unique_claim_lease_recovery_and_owner_guard(tmp_path):
    service = _service(tmp_path)
    watch = service.create(_payload(), now=datetime(2026, 1, 1, tzinfo=UTC))
    scheduled = datetime(2026, 1, 1, 17, 30, tzinfo=UTC)
    first = service.create_occurrence(watch["watch_id"], scheduled)
    duplicate = service.create_occurrence(watch["watch_id"], scheduled)
    assert duplicate["occurrence_id"] == first["occurrence_id"]

    claim_time = scheduled + timedelta(minutes=1)
    claimed = service.claim_occurrence(
        owner="worker-a", now=claim_time, lease_for=timedelta(minutes=2)
    )
    assert claimed["occurrence_id"] == first["occurrence_id"]
    assert service.claim_occurrence(owner="worker-b", now=claim_time) is None
    with pytest.raises(ValueError, match="not leased"):
        service.finish_occurrence(
            first["occurrence_id"], owner="worker-b", succeeded=True, now=claim_time
        )

    recovered = service.claim_occurrence(owner="worker-b", now=claim_time + timedelta(minutes=3))
    assert recovered["lease_owner"] == "worker-b"
    finished = service.finish_occurrence(
        first["occurrence_id"],
        owner="worker-b",
        succeeded=True,
        run_id="run-1",
        evidence_refs=[{"kind": "url", "ref": "https://example.com/launch"}],
        content_fingerprint="fp-1",
        now=claim_time + timedelta(minutes=3, seconds=1),
    )
    assert finished["status"] == "succeeded"
    assert finished["run_id"] == "run-1"
    assert finished["evidence_refs"][0]["ref"].endswith("/launch")


def test_briefing_persists_evidence_and_can_be_marked_read(tmp_path):
    service = _service(tmp_path)
    watch = service.create(_payload(), now=datetime(2026, 1, 1, tzinfo=UTC))
    scheduled = datetime(2026, 1, 1, 17, 30, tzinfo=UTC)
    occurrence = service.create_occurrence(watch["watch_id"], scheduled)
    claimed = service.claim_occurrence(owner="worker", now=scheduled + timedelta(minutes=1))
    assert claimed["occurrence_id"] == occurrence["occurrence_id"]

    briefing = service.complete_with_briefing(
        occurrence["occurrence_id"],
        owner="worker",
        payload=BriefingInput(
            title="Launch update",
            summary="The official date is unchanged.",
            unchanged=[{"claim": "Launch date is May 3"}],
            evidence=[
                {
                    "kind": "url",
                    "ref": "https://example.com/launch",
                    "observed_at": "2026-01-01T17:00:00+00:00",
                }
            ],
            content_fingerprint="stable-v1",
        ),
        now=scheduled + timedelta(minutes=1, seconds=1),
    )
    assert len(service.list_briefings(unread_only=True)) == 1
    assert briefing["evidence"][0]["kind"] == "url"

    read = service.mark_briefing_read(
        briefing["briefing_id"], now=datetime(2026, 1, 1, 18, tzinfo=UTC)
    )
    assert read["read_at"] == "2026-01-01T18:00:00+00:00"
    assert service.list_briefings(unread_only=True) == []
    assert service.list_briefings()[0]["briefing_id"] == briefing["briefing_id"]


def test_briefing_must_belong_to_running_occurrence_and_db_is_durable(tmp_path):
    db_path = get_db_path(tmp_path / "data")
    service = WatchService(lambda: connect(db_path))
    service.initialize()
    watch = service.create(_payload(), now=datetime(2026, 1, 1, tzinfo=UTC))
    occurrence = service.create_occurrence(
        watch["watch_id"], datetime(2026, 1, 1, 17, 30, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="lease is lost"):
        service.complete_with_briefing(
            occurrence["occurrence_id"],
            owner="worker",
            payload=BriefingInput(title="x", summary="y"),
        )

    reopened = WatchService(lambda: connect(db_path))
    assert reopened.get(watch["watch_id"])["goal"] == "Find official launch updates"


def test_watch_rejects_unusable_and_unsafe_mixed_scope(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="needs web/news access"):
        service.create(_payload(categories=[], scope={}))
    with pytest.raises(ValueError, match="Mixed private account"):
        service.create(_payload(scope={"account_ids": ["account-1"], "source_ids": ["source-1"]}))
    allowed = service.create(
        _payload(
            scope={
                "account_ids": ["account-1"],
                "source_ids": ["source-1"],
                "allow_mixed_private_external": True,
            }
        )
    )
    assert allowed["scope"]["allow_mixed_private_external"] is True


def test_atomic_briefing_publish_requires_active_owned_lease(tmp_path):
    service = _service(tmp_path)
    watch = service.create(_payload(), now=datetime(2026, 1, 1, tzinfo=UTC))
    first_time = datetime(2026, 1, 2, 17, 30, tzinfo=UTC)
    second_time = first_time + timedelta(days=1)
    first = service.create_occurrence(watch["watch_id"], first_time)
    second = service.create_occurrence(watch["watch_id"], second_time)
    assert first["session_id"] != second["session_id"]
    service.claim_occurrence(owner="worker", now=first_time + timedelta(seconds=1))
    assert service.renew_lease(
        first["occurrence_id"], owner="worker", now=first_time + timedelta(minutes=1)
    )
    service.set_paused(watch["watch_id"], True)
    with pytest.raises(ValueError, match="lease is lost"):
        service.complete_with_briefing(
            first["occurrence_id"],
            owner="worker",
            payload=BriefingInput(title="Update", summary="Should not publish"),
            now=first_time + timedelta(minutes=2),
        )
    assert service.list_briefings(watch_id=watch["watch_id"]) == []


def test_workspace_read_scope_is_supported_and_scope_update_invalidates_occurrence(tmp_path):
    service = _service(tmp_path)
    watch = service.create(
        _payload(categories=[], scope={"workspace_paths": ["/workspace/research"]}),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    occurrence = service.create_occurrence(watch["watch_id"], datetime(2026, 1, 2, tzinfo=UTC))
    assert occurrence["scope_version"] == watch["version"]
    claimed = service.claim_occurrence(owner="worker", now=datetime(2026, 1, 2, 0, 1, tzinfo=UTC))
    assert claimed["occurrence_id"] == occurrence["occurrence_id"]

    updated = service.update(
        watch["watch_id"], WatchPatch(scope={"workspace_paths": ["/workspace/other"]})
    )
    assert updated["version"] == watch["version"] + 1
    assert service.is_current_scope(watch["watch_id"], occurrence["scope_version"]) is False
    assert service.list_occurrences(watch["watch_id"])[0]["status"] == "cancelled"
    with pytest.raises(ValueError, match="lease is lost"):
        service.complete_with_briefing(
            occurrence["occurrence_id"],
            owner="worker",
            payload=BriefingInput(title="Update", summary="Must not publish"),
        )
