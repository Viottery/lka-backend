import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.domains.message_history import MessageHistoryService


@pytest.fixture
def setup(tmp_path, monkeypatch):
    instant = [datetime.now(UTC)]
    monkeypatch.setattr("app.domains.message_history._now", lambda: instant[0].isoformat(timespec="microseconds"))
    path = tmp_path / "reading.sqlite"
    jobs = BackgroundJobStore(path)
    service = MessageHistoryService(path, jobs)
    service.ensure_schema()
    service.configure_reading({"yield_delay_seconds": 0})
    return service, jobs, instant


def policy(service, conversation_id="c", **kwargs):
    return service.set_policy({"platform": "fake", "account_id": "a", "conversation_type": "group",
        "conversation_id": conversation_id, "record_enabled": True, "analysis_enabled": True,
        "batch_size": 2, **kwargs})


def ingest(service, count=1, conversation_id="c", prefix="m"):
    return service.import_messages([{"platform": "fake", "account_id": "a", "conversation_type": "group",
        "conversation_id": conversation_id, "message_id": f"{prefix}{i}", "text": "hello", "received_at": 1}
        for i in range(count)])


def running(setup):
    service, jobs, _ = setup
    policy(service)
    ingest(service, 2)
    job = jobs.claim("w", 60, kinds=("message_analysis",))
    return service, jobs, job, service.load_reading_progress(job)


def test_pending_age_survives_arrivals_and_restart(setup):
    service, jobs, now = setup
    p = policy(service, batch_size=50)
    ingest(service)
    first = service.reading_status()["schedules"][0]["pending_since"]
    now[0] += timedelta(seconds=800)
    ingest(service, prefix="later")
    other = MessageHistoryService(service.db_path, jobs)
    other.ensure_schema()
    assert other.reading_status()["schedules"][0]["pending_since"] == first
    assert other.schedule_pending() == []
    now[0] += timedelta(seconds=101)
    assert len(other.schedule_pending(p["conversation_key"])) == 1


def test_threshold_does_not_fix_range_size(setup):
    service, jobs, _ = setup
    policy(service)
    ingest(service, 10)
    assert jobs.claim("w", 60, kinds=("message_analysis",))["payload"]["end_seq"] == 10


def test_manual_bypasses_trigger_but_not_pause_or_permission(setup):
    service, _, _ = setup
    p = policy(service, auto_analyze=False)
    ingest(service)
    assert service.schedule_pending() == []
    state = service.set_reading_paused(True, 1)
    assert service.schedule_pending(p["conversation_key"], force=True) == []
    service.set_reading_paused(False, state["revision"])
    assert len(service.schedule_pending(p["conversation_key"], force=True)) == 1


def test_dynamic_plan_shrinks_prefix_and_oversize_blocks_only_head(setup):
    service, jobs, _ = setup
    service.configure_reading({}, lambda context: 0 if context["policy"]["conversation_id"] == "c" else 1)
    p = policy(service)
    policy(service, conversation_id="other")
    ingest(service, 3)
    ingest(service, 3, conversation_id="other", prefix="other")
    rows = jobs.list(kind="message_analysis")
    assert len(rows) == 2
    assert next(r for r in rows if r["scope_id"] == p["conversation_key"])["error_class"] == "input_too_large"
    assert jobs.claim("w", 60, kinds=("message_analysis",))["payload"]["end_seq"] == 1


def test_checkpoint_yields_same_job_without_coverage_or_attempt_cost(setup):
    service, jobs, job, progress = running(setup)
    digest = "a" * 64
    assert service.save_reading_progress(job, 0, digest, {"cursor": 1, "last_input_digest": digest, "accumulator": {}}, 1, progress["service_epoch"])
    assert jobs.get(job["job_id"])["status"] == "queued"
    assert jobs.get(job["job_id"])["attempts"] == 0
    assert service.summary(job["scope_id"])["covered_seq"] == 0
    next_job = jobs.claim("new", 60, kinds=("message_analysis",))
    assert next_job["job_id"] == job["job_id"]
    assert service.load_reading_progress(next_job)["cursor"] == 1
    assert not service.save_reading_progress(job, 1, digest, {}, 2, progress["service_epoch"])


def test_recovery_checkpoint_keeps_cursor_and_checks_chain(setup):
    service, jobs, job, progress = running(setup)
    assert service.save_reading_progress(job, 0, "a" * 64, {"recovery_pending": True}, 0, progress["service_epoch"])
    job = jobs.claim("new", 60, kinds=("message_analysis",))
    assert service.load_reading_progress(job)["checkpoint"]["recovery_pending"]
    assert not service.save_reading_progress(job, 0, "b" * 64, {"previous_input_digest": "wrong"}, 1, progress["service_epoch"])
    assert service.save_reading_progress(job, 0, "b" * 64, {"previous_input_digest": "a" * 64}, 1, progress["service_epoch"])


def test_pause_fences_calls_but_preserves_valid_checkpoint(setup):
    service, jobs, job, progress = running(setup)
    assert service.save_reading_progress(job, 0, "a" * 64, {}, 1, progress["service_epoch"])
    current = jobs.claim("next", 60, kinds=("message_analysis",))
    paused = service.set_reading_paused(True, 1)
    assert service.load_reading_progress(current) is None
    service.set_reading_paused(False, paused["revision"])
    assert not service.save_reading_progress(current, 1, "b" * 64, {}, 2, progress["service_epoch"])
    resumed = jobs.claim("resumed", 60, kinds=("message_analysis",))
    resumed_progress = service.load_reading_progress(resumed)
    assert resumed_progress["cursor"] == 1 and resumed_progress["service_epoch"] > progress["service_epoch"]


def test_processing_change_reuses_family_and_invalidates_checkpoint(setup):
    service, jobs, job, progress = running(setup)
    assert service.save_reading_progress(job, 0, "a" * 64, {}, 1, progress["service_epoch"])
    p = service.list_policies()[0]
    service.set_policy({**{k: p[k] for k in ("platform", "account_id", "conversation_type", "conversation_id")},
        "expected_revision": p["revision"], "analysis_enabled": True, "processing_schema_version": 2})
    service.schedule_pending(force=True)
    new = jobs.claim("new", 60, kinds=("message_analysis",))
    fresh = service.load_reading_progress(new)
    assert fresh["family_id"] == progress["family_id"] and fresh["cursor"] == 0


def test_schedule_update_preserves_permissions_and_checkpoint(setup):
    service, _jobs, job, progress = running(setup)
    p = service.list_policies()[0]
    revised = service.set_policy({**{k: p[k] for k in ("platform", "account_id", "conversation_type", "conversation_id")},
        "expected_revision": p["revision"], "analysis_enabled": True, "min_interval_seconds": 100})
    assert revised["capture_epoch"] == p["capture_epoch"] and revised["processing_revision"] == p["processing_revision"]
    assert service.save_reading_progress(job, 0, "a" * 64, {}, 1, progress["service_epoch"])


def test_control_and_limit_cas(setup):
    service, _jobs, _job, progress = running(setup)
    with pytest.raises(ValueError, match="service_revision_conflict"):
        service.set_reading_paused(True, 0)
    raised = service.raise_work_limits(progress["family_id"], 1, 40000, 5)
    assert raised["revision"] == 2
    with pytest.raises(ValueError, match="work_revision_conflict"):
        service.raise_work_limits(progress["family_id"], 1, 50000, 6)
    with pytest.raises(ValueError, match="must_not_decrease"):
        service.raise_work_limits(progress["family_id"], 2, 30000, 5)


def test_partial_schedule_update_validates_effective_not_default_fields(setup):
    service, _, _ = setup
    p = policy(service, min_interval_seconds=0, max_wait_seconds=200)
    identity = {k: p[k] for k in ("platform", "account_id", "conversation_type", "conversation_id")}
    p = service.set_policy({**identity, "expected_revision": p["revision"], "max_wait_seconds": 100})
    assert p["min_interval_seconds"] == 0 and p["max_wait_seconds"] == 100
    with pytest.raises(ValueError, match="max_wait_seconds"):
        service.set_policy({**identity, "expected_revision": p["revision"], "min_interval_seconds": 150})
    assert service.list_policies()[0]["revision"] == p["revision"]


def test_replacement_cannot_shrink_range_to_reset_suffix_budget(setup):
    service, jobs, _ = setup
    p = policy(service)
    ingest(service, 4)
    first = jobs.claim("initial", 60, kinds=("message_analysis",))
    family = service.load_reading_progress(first)["family_id"]
    service.configure_reading({}, lambda context: 1)
    identity = {k: p[k] for k in ("platform", "account_id", "conversation_type", "conversation_id")}
    service.set_policy({**identity, "expected_revision": p["revision"], "processing_schema_version": 2})
    latest = next(row for row in jobs.list() if row["status"] == "failed")
    payload = jobs.get(latest["job_id"], include_payload=True)["payload"]
    assert payload["work_family_id"] == family and payload["end_seq"] == first["payload"]["end_seq"] == 4
    assert latest["error_class"] == "input_too_large"


def test_publish_records_successor_intent_without_dispatch(setup):
    service, jobs, now = setup
    p = policy(service, max_batch_messages=2)
    ingest(service, 4)
    job = jobs.claim("w", 60, kinds=("message_analysis",))
    epoch = service.load_reading_progress(job)["service_epoch"]
    assert service.publish_analysis(job, {"batch_summary": "done", "summary": "done", "facts": []}, service_epoch=epoch)
    assert len(jobs.list(kind="message_analysis")) == 1
    assert service.schedule_pending() == []
    now[0] += timedelta(seconds=301)
    successor = service.schedule_pending(p["conversation_key"])[0]
    assert successor["payload"]["start_seq"] == 3
    assert successor["payload"]["work_family_id"] != job["payload"]["work_family_id"]


def test_bounded_scan_rotates_past_failed_head(setup):
    service, _jobs, now = setup
    service.configure_reading({"due_scan_limit": 1})
    first = policy(service, conversation_id="first", auto_analyze=False)
    second = policy(service, conversation_id="second", auto_analyze=False)
    ingest(service, 2, conversation_id="first", prefix="first")
    ingest(service, 2, conversation_id="second", prefix="second")
    failed = service.schedule_pending(first["conversation_key"], force=True)[0]
    with service._connection() as conn:
        conn.execute("UPDATE background_jobs SET status='failed' WHERE job_id=?", (failed["job_id"],))
        conn.execute("UPDATE message_history_policies SET auto_analyze=1")
    now[0] += timedelta(seconds=1)
    assert service.schedule_pending()[0]["scope_id"] == second["conversation_key"]


@pytest.mark.parametrize("old,new", [(True, 1), (0, True), (-1, 0), (0, 2), ("0", 1)])
def test_checkpoint_rejects_invalid_cursor(setup, old, new):
    service, _, job, progress = running(setup)
    with pytest.raises(ValueError, match="invalid_checkpoint_cursor"):
        service.save_reading_progress(job, old, "a" * 64, {}, new, progress["service_epoch"])


def test_legacy_task_usage_ids_cover_all_same_head_replacements(setup):
    service, jobs, job, progress = running(setup)
    legacy = dict(job["payload"])
    legacy.pop("work_family_id")
    with service._connection() as conn:
        conn.execute("UPDATE background_jobs SET payload_json=? WHERE job_id=?", (json.dumps(legacy), job["job_id"]))
        older = jobs.enqueue("message_analysis", job["scope_id"], "legacy-old", legacy, conn=conn)
        conn.execute("UPDATE background_jobs SET status='failed' WHERE job_id=?", (older["job_id"],))
    job["payload"] = legacy
    fresh = service.load_reading_progress(job)
    assert set(fresh["legacy_task_ids"]) == {job["job_id"], older["job_id"]}
    assert fresh["family_id"] == progress["family_id"]


def test_exhausted_retry_requires_explicit_limit_increase(setup):
    service, jobs, job, progress = running(setup)
    with service._connection() as conn:
        conn.execute("UPDATE background_jobs SET status='failed',error_class='work_budget_exhausted',lease_owner=NULL,lease_expires_at=NULL WHERE job_id=?", (job["job_id"],))
    failed = jobs.get(job["job_id"])
    assert service.retry_analysis(job["scope_id"], failed["updated_at"])["status"] == "unsupported"
    service.raise_work_limits(progress["family_id"], 1, progress["work_token_limit"], progress["work_call_limit"])
    assert jobs.get(job["job_id"])["status"] == "failed"
    service.raise_work_limits(progress["family_id"], 2, progress["work_token_limit"] + 1, progress["work_call_limit"])
    assert jobs.get(job["job_id"])["status"] == "queued"
    assert jobs.get(job["job_id"])["attempts"] == failed["attempts"]
