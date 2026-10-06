from __future__ import annotations

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.domains.message_history import (
    AnalysisResult,
    MessageHistoryService,
    PolicyRevisionConflict,
)


def _service(tmp_path):
    path = tmp_path / "messages.sqlite3"
    jobs = BackgroundJobStore(path)
    service = MessageHistoryService(path, jobs)
    service.ensure_schema()
    return service, jobs


def _policy(service, *, conversation_id="c1", **changes):
    value = {
        "platform": "mock", "account_id": "acct", "conversation_type": "private",
        "conversation_id": conversation_id, "expected_revision": 0,
        "record_enabled": True, "analysis_enabled": False,
    }
    value.update(changes)
    return service.set_policy(value)


def _message(message_id, *, conversation_id="c1", text="hello", sent_at=100, **changes):
    value = {
        "platform": "mock", "account_id": "acct", "message_id": message_id,
        "conversation_type": "private", "conversation_id": conversation_id,
        "sender_id": "u1", "sender_name": "User", "text": text,
        "sent_at": sent_at, "received_at": 200, "content_kind": "text",
    }
    value.update(changes)
    return value


def test_import_is_whitelist_gated_and_deduplicates_without_overwrite(tmp_path):
    service, _ = _service(tmp_path)
    denied = service.import_messages([_message("m0")])
    assert denied["rejected"][0]["reason"] == "not_whitelisted"
    _policy(service)
    first = service.import_messages([_message("m1")])
    duplicate = service.import_messages([_message("m1", received_at=999)])
    service.import_messages([_message("m-invalid-time", sent_at=0)])
    invalid_time_duplicate = service.import_messages([_message("m-invalid-time", sent_at=-1)])
    conflict = service.import_messages([_message("m1", text="changed")])
    assert len(first["acknowledged"]) == 1
    assert len(duplicate["acknowledged"]) == 1
    assert len(invalid_time_duplicate["acknowledged"]) == 1
    assert conflict["rejected"][0]["reason"] == "identity_conflict"
    assert service.recent()["messages"][0]["text"] == "hello"


def test_sequences_and_invalid_sent_time_are_distinct_from_ingest_time(tmp_path):
    service, _ = _service(tmp_path)
    policy = _policy(service)
    service.import_messages([_message("late", sent_at=0), _message("later", sent_at=101)])
    rows = service.history(policy["conversation_key"])["messages"]
    assert [row["seq"] for row in rows] == [2, 1]
    invalid_row = rows[1]
    assert invalid_row["sent_at"] is None
    assert invalid_row["timestamp_quality"] == "invalid"
    assert invalid_row["received_at"] == 200 and invalid_row["ingested_at"]


def test_history_paginates_by_sequence_even_when_received_times_are_out_of_order(tmp_path):
    service, _ = _service(tmp_path)
    policy = _policy(service)
    service.import_messages([
        _message("m1", received_at=300), _message("m2", received_at=100), _message("m3", received_at=200),
    ])
    page = service.history(policy["conversation_key"], limit=2)
    assert [row["seq"] for row in page["messages"]] == [3, 2]
    assert page["next_before_seq"] == 2 and page["has_more"]
    older = service.history(policy["conversation_key"], limit=2, before_seq=page["next_before_seq"])
    assert [row["seq"] for row in older["messages"]] == [1]


def test_unsupported_content_never_persists_supplied_text(tmp_path):
    service, _ = _service(tmp_path)
    policy = _policy(service)
    service.import_messages([_message("image", content_kind="unsupported", text="hidden payload")])
    row = service.history(policy["conversation_key"])["messages"][0]
    assert row["text"] == "" and row["content_kind"] == "unsupported"


def test_policy_revision_cas_revocation_blocks_reads_and_future_import(tmp_path):
    service, _ = _service(tmp_path)
    policy = _policy(service)
    service.import_messages([_message("m1")])
    with pytest.raises(PolicyRevisionConflict):
        service.set_policy({
            "platform": "mock", "account_id": "acct", "conversation_type": "private",
            "conversation_id": "c1", "expected_revision": 0,
        })
    revoked = service.set_policy({
        "platform": "mock", "account_id": "acct", "conversation_type": "private",
        "conversation_id": "c1", "record_enabled": False, "expected_revision": policy["revision"],
    })
    assert revoked["revision"] == 2
    denied = service.import_messages([_message("m2")])
    assert denied["rejected"][0]["reason"] == "not_whitelisted"
    with pytest.raises(PermissionError):
        service.history(policy["conversation_key"])


def test_scope_filters_are_dynamic_and_empty_scope_denies(tmp_path):
    service, _ = _service(tmp_path)
    policy = _policy(service)
    service.import_messages([_message("m1", text="needle", sender_id="u1")])
    assert service.search("needle", policy["conversation_key"], sender_id="u1")["messages"]
    assert not service.search("needle", policy["conversation_key"], sender_id="u2")["messages"]
    assert service.recent(allowed_sources=[])["messages"] == []
    assert service.source_inventory()[0]["source_id"] == policy["source_id"]
    assert service.account_inventory()[0]["account_scope_id"] == policy["account_scope_id"]
    assert service.summary(policy["conversation_key"], allowed_sources=[]) is None


def test_fixed_batches_and_atomic_publish_with_fact_provenance(tmp_path):
    service, jobs = _service(tmp_path)
    policy = _policy(service, analysis_enabled=True, batch_size=2, max_batch_messages=2)
    service.import_messages([_message("m1"), _message("m2", text="second"), _message("m3"), _message("m4")])
    queued = jobs.list(kind="message_analysis", scope_id=policy["conversation_key"])
    assert len(queued) == 1
    job = jobs.claim("worker", 30, kinds=("message_analysis",))
    batch = service.load_analysis_batch(job)
    assert [m["seq"] for m in batch["messages"]] == [1, 2]
    result = AnalysisResult(
        batch_summary="two messages", summary="rolling", facts=[{
            "kind": "fact", "text": "They discussed something", "source_message_ids":[batch["messages"][0]["message_id"]],
            "certainty": "explicit",
        }],
    )
    assert service.publish_analysis(job, result)
    assert jobs.get(job["job_id"])["status"] == "succeeded"
    assert not jobs.complete(job["job_id"], "worker", job["lease_epoch"])
    assert service.summary(policy["conversation_key"])["covered_seq"] == 2
    assert service.facts(policy["conversation_key"])["facts"][0]["text"] == "They discussed something"
    fact = service.facts(policy["conversation_key"])["facts"][0]
    assert fact["active"] and fact["source_refs"][0]["seq"] == 1
    queued_after = jobs.list(kind="message_analysis", scope_id=policy["conversation_key"])
    assert len(queued_after) == 1  # Publication records intent; the due scanner dispatches later.


def test_correction_marks_old_fact_superseded_without_deleting_it(tmp_path):
    service, jobs = _service(tmp_path)
    policy = _policy(service, analysis_enabled=True, batch_size=1, min_interval_seconds=0)
    service.import_messages([_message("m1")])
    first = jobs.claim("worker", 30, kinds=("message_analysis",))
    source1 = service.load_analysis_batch(first)["messages"][0]["message_id"]
    assert service.publish_analysis(first, {"batch_summary":"first", "summary":"first", "facts":[{
        "kind":"fact", "text":"The time is 3pm", "source_message_ids":[source1], "certainty":"explicit",
    }]})
    jobs.complete(first["job_id"], "worker", first["lease_epoch"])
    original = service.facts(policy["conversation_key"])["facts"][0]["fact_id"]
    service.import_messages([_message("m2", text="Correction: the time is 4pm")])
    second = jobs.claim("worker", 30, kinds=("message_analysis",))
    source2 = service.load_analysis_batch(second)["messages"][0]["message_id"]
    assert service.publish_analysis(second, {"batch_summary":"correction", "summary":"4pm", "facts":[{
        "kind":"correction", "text":"The time is 4pm", "source_message_ids":[source2],
        "certainty":"explicit", "supersedes_fact_ids":[original],
    }]})
    facts = service.facts(policy["conversation_key"])["facts"]
    old = next(item for item in facts if item["fact_id"] == original)
    correction = next(item for item in facts if item["kind"] == "correction")
    assert not old["active"] and correction["active"]
    assert old["superseded_by"] == [correction["fact_id"]]


def test_publication_rejects_out_of_batch_sources_and_stale_lease(tmp_path):
    service, jobs = _service(tmp_path)
    _policy(service, analysis_enabled=True, batch_size=1)
    service.import_messages([_message("m1")])
    first = jobs.claim("worker-a", 30, kinds=("message_analysis",))
    with pytest.raises(ValueError, match="fact_source_outside_batch"):
        service.publish_analysis(first, {
            "batch_summary":"x", "summary":"x", "facts":[{
                "kind":"fact", "text":"bad reference", "source_message_ids":["wrong"], "certainty":"explicit",
            }],
        })
    jobs.fail(first["job_id"], "worker-a", first["lease_epoch"], "temporary", False)
    jobs.retry_controlled(first["job_id"], jobs.get(first["job_id"])["updated_at"], {"message_analysis"})
    second = jobs.claim("worker-b", 30, kinds=("message_analysis",))
    assert not service.publish_analysis(first, {"batch_summary":"stale", "summary":"stale"})
    assert service.publish_analysis(second, {"batch_summary":"ok", "summary":"ok"})


def test_manual_force_processes_tail_and_retries_same_failed_job(tmp_path):
    service, jobs = _service(tmp_path)
    policy = _policy(service, analysis_enabled=True, batch_size=5)
    service.import_messages([_message("m1")])
    assert jobs.list(kind="message_analysis") == []
    queued = service.schedule_pending(policy["conversation_key"], force=True)
    assert queued and queued[0]["payload"]["start_seq"] == 1
    job = jobs.claim("worker", 30, kinds=("message_analysis",))
    jobs.fail(job["job_id"], "worker", job["lease_epoch"], "temporary", False)
    before = jobs.get(job["job_id"])
    retried = service.retry_analysis(policy["conversation_key"], before["updated_at"])
    assert retried["status"] == "retried" and retried["job"]["job_id"] == job["job_id"]


def test_start_from_now_sets_immutable_atomic_analysis_floor(tmp_path):
    service, jobs = _service(tmp_path)
    policy = _policy(service)
    service.import_messages([_message(f"old-{n}") for n in range(5)])

    enabled = service.set_policy({
        "platform": "mock", "account_id": "acct", "conversation_type": "private",
        "conversation_id": "c1", "expected_revision": policy["revision"],
        "record_enabled": True, "analysis_enabled": True, "start_from_now": True,
    })
    assert jobs.list(kind="message_analysis", scope_id=policy["conversation_key"]) == []
    coverage = service.coverage(policy["conversation_key"])
    assert coverage["analysis_baseline_floor_seq"] == 5
    assert coverage["analysis_watermark_seq"] == 5
    assert coverage["excluded_history_count"] == 5
    assert coverage["pending_messages"] == 0

    service.import_messages([_message("new-1")])
    service.schedule_pending(policy["conversation_key"], force=True)
    queued = jobs.list(kind="message_analysis", scope_id=policy["conversation_key"])
    assert queued
    assert jobs.get(queued[0]["job_id"], include_payload=True)["payload"]["start_seq"] == 6
    job = jobs.claim("worker", 30, kinds=("message_analysis",))
    assert jobs.fail(job["job_id"], "worker", job["lease_epoch"], "model_error", False)

    # A later disable/re-enable cannot move the floor over this unfinished work.
    disabled = service.set_policy({
        "platform": "mock", "account_id": "acct", "conversation_type": "private",
        "conversation_id": "c1", "expected_revision": enabled["revision"],
        "record_enabled": True, "analysis_enabled": False,
    })
    with pytest.raises(ValueError, match="start_from_now_only_allowed"):
        service.set_policy({
            "platform": "mock", "account_id": "acct", "conversation_type": "private",
            "conversation_id": "c1", "expected_revision": disabled["revision"],
            "record_enabled": True, "analysis_enabled": True, "start_from_now": True,
        })
    coverage = service.coverage(policy["conversation_key"])
    assert coverage["analysis_watermark_seq"] == 5
    assert coverage["pending_messages"] == 1


def test_selected_publication_does_not_erase_excluded_history(tmp_path):
    service, jobs = _service(tmp_path)
    policy = _policy(service)
    service.import_messages([_message("old-1"), _message("old-2")])
    enabled = service.set_policy({
        "platform": "mock", "account_id": "acct", "conversation_type": "private",
        "conversation_id": "c1", "expected_revision": policy["revision"],
        "record_enabled": True, "analysis_enabled": True, "start_from_now": True,
    })
    service.import_messages([_message("new-1")])
    service.configure_reading({"reading_algorithm": "selected"})
    service.schedule_pending(enabled["conversation_key"], force=True)
    job = jobs.claim("worker", 30, kinds=("message_analysis",))
    assert job and job["payload"]["start_seq"] == 3
    assert service.publish_analysis(job, {
        "batch_summary": "Selected batch", "summary": "Selected batch",
        "reading_manifest": {"coverage_mode": "selected_text"},
    })
    coverage = service.coverage(enabled["conversation_key"])
    assert coverage["generation_published_seq"] == 3
    assert coverage["analysis_watermark_seq"] == 3
    assert coverage["excluded_history_count"] == 2
    assert coverage["legacy_covered_seq"] == 0
    listing = service.list_conversations()["conversations"][0]
    assert listing["pending_count"] == 0 and listing["legacy_pending_count"] == 3


def test_start_from_now_preserves_empty_conversation_zero_floor(tmp_path):
    service, _ = _service(tmp_path)
    policy = _policy(service)
    enabled = service.set_policy({
        "platform": "mock", "account_id": "acct", "conversation_type": "private",
        "conversation_id": "c1", "expected_revision": policy["revision"],
        "record_enabled": True, "analysis_enabled": True, "start_from_now": True,
    })
    coverage = service.coverage(enabled["conversation_key"])
    assert coverage["analysis_baseline_floor_seq"] == 0
    assert coverage["excluded_history_through_seq"] == 0
    assert coverage["excluded_history_count"] == 0


def test_cutover_failed_batch_retry_keeps_original_work_family(tmp_path):
    service, jobs = _service(tmp_path)
    policy = _policy(service)
    service.import_messages([_message(f"old-{n}") for n in range(3)])
    enabled = service.set_policy({
        "platform": "mock", "account_id": "acct", "conversation_type": "private",
        "conversation_id": "c1", "expected_revision": policy["revision"],
        "record_enabled": True, "analysis_enabled": True, "start_from_now": True,
    })
    service.import_messages([_message("new-1")])
    job = service.schedule_pending(enabled["conversation_key"], force=True)[0]
    family_id = job["payload"]["work_family_id"]
    assert job["payload"]["start_seq"] == 4
    claimed = jobs.claim("worker-a", 30, kinds=("message_analysis",))
    assert jobs.fail(claimed["job_id"], "worker-a", claimed["lease_epoch"], "work_budget_exhausted", False)

    raised = service.raise_work_limits(family_id, 1, 40000, 5)
    assert raised["revision"] == 2
    resumed = jobs.get(job["job_id"], include_payload=True)
    assert resumed["status"] == "queued"
    assert resumed["payload"]["work_family_id"] == family_id

    claimed_again = jobs.claim("worker-b", 30, kinds=("message_analysis",))
    assert jobs.fail(claimed_again["job_id"], "worker-b", claimed_again["lease_epoch"], "provider_error", False)
    failed = jobs.get(job["job_id"])
    retried = service.retry_analysis(enabled["conversation_key"], failed["updated_at"])
    assert retried["status"] == "retried"
    assert retried["job"]["job_id"] == job["job_id"]
    retried_job = jobs.get(job["job_id"], include_payload=True)
    assert retried_job["payload"]["work_family_id"] == family_id
    with service._connection() as conn:
        family = conn.execute("SELECT * FROM message_reading_families WHERE family_id=?", (family_id,)).fetchone()
        assert (family["revision"], family["max_tokens"], family["max_calls"]) == (2, 40000, 5)
