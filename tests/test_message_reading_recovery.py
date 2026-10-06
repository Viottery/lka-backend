"""Durable recovery/replay of synthetic v3 failures, with no paid calls."""
import json

import pytest
from test_message_reading_production_v3 import IDENTITY, Model, finish, insert, pipeline

from app.core.background_jobs import BackgroundJobFailure, _classify_worker_error
from app.core.message_analysis import MessageAnalysisCoordinator
from app.domains.message_history import MessageHistoryService


class BadThenGood(Model):
    def __init__(self, bad_calls=2):
        super().__init__()
        self.bad_calls = bad_calls

    async def complete_text(self, **kwargs):
        response = await super().complete_text(**kwargs)
        if len(self.requests) <= self.bad_calls:
            response.content = "invalid-json-private-text"
        return response


def test_payload_free_worker_failure_class():
    assert _classify_worker_error(BackgroundJobFailure("checkpoint_input_changed")) == ("checkpoint_input_changed", False)
    with pytest.raises(ValueError):
        BackgroundJobFailure("message body!")


def test_failed_head_rebuilds_once_then_publishes_successor(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path, "selected", BadThenGood())
    insert(service, 2)
    finish(service, policy, coordinator)
    failed = store.list(status="failed")[0]
    family = store.get(failed["job_id"], include_payload=True)["payload"]["work_family_id"]
    assert coordinator.controller.quota_usage(family)["total_calls"] == 2
    assert "validation_feedback" in model.requests[1]
    assert "invalid-json-private-text" not in str(model.requests[1]["validation_feedback"])
    # Recovery is serialized in the scheduler, not a new job/free quota family.
    recovered = service.schedule_pending(policy["conversation_key"], force=True)[0]
    assert recovered["job_id"] == failed["job_id"] and recovered["status"] == "queued"
    assert coordinator.controller.quota_usage(family)["total_calls"] == 2
    assert coordinator.worker.run_one()
    assert store.get(failed["job_id"])["status"] == "succeeded"
    assert coordinator.controller.quota_usage(family)["total_calls"] == 3
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_checkpoint_archives").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM message_reading_families WHERE family_id=?", (family,)).fetchone()
        assert row["recovery_count"] == 1 and row["max_calls"] == 100
    insert(service, 3)
    finish(service, policy, coordinator)
    assert len(store.list(status="succeeded")) == 2
    assert service.reading_status()["schedules"][0]["analysis_watermark_seq"] == 3


def test_recovery_is_bounded_across_restart_and_manual_retry(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path, "selected", BadThenGood(100))
    insert(service, 1)
    finish(service, policy, coordinator)
    service.schedule_pending(policy["conversation_key"], force=True)
    for _ in range(10):
        if not coordinator.worker.run_one():
            break
    assert len(model.requests) == 4
    failed = store.list(status="failed")[0]
    assert failed["error_class"] == "model_recovery_exhausted"
    restarted = MessageHistoryService(service.db_path, store)
    restarted.ensure_schema()
    other = MessageAnalysisCoordinator(service=restarted, store=store, config=coordinator.config, llm_client=model)
    for _ in range(3):
        assert restarted.schedule_pending(policy["conversation_key"], force=True)[0]["status"] == "failed"
        assert not other.worker.run_one()
    assert restarted.retry_analysis(policy["conversation_key"], failed["updated_at"])["status"] == "unsupported"
    assert len(model.requests) == 4
    assert restarted.reading_status()["schedules"][0]["analysis_watermark_seq"] == 0


def test_explicit_work_increase_grants_one_more_rebuild_without_resetting_usage(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path, "selected", BadThenGood(4))
    insert(service, 1)
    finish(service, policy, coordinator)
    finish(service, policy, coordinator)
    failed = store.list(status="failed")[0]
    family = store.get(failed["job_id"], include_payload=True)["payload"]["work_family_id"]
    with service._connection() as conn:
        row = conn.execute("SELECT * FROM message_reading_families WHERE family_id=?", (family,)).fetchone()
    assert row["recovery_count"] == 1 and len(model.requests) == 4
    service.raise_work_limits(family, row["revision"], row["max_tokens"], row["max_calls"])
    assert store.get(failed["job_id"])["status"] == "failed"
    raised = service.raise_work_limits(family, row["revision"] + 1, row["max_tokens"], row["max_calls"] + 1)
    assert raised["recovery_count"] == 2
    assert coordinator.controller.quota_usage(family)["total_calls"] == 4
    finish(service, policy, coordinator)
    assert store.get(failed["job_id"])["status"] == "succeeded" and len(model.requests) == 5
    assert model.requests[-1]["validation_feedback"] == [{"code": "invalid_json"}]


@pytest.mark.parametrize("unknown_member", [False, True])
def test_current_evidence_representatives_can_complete_member_set_without_new_evidence(tmp_path, unknown_member):
    class MembersModel(Model):
        async def complete_text(self, **kwargs):
            response = await super().complete_text(**kwargs)
            value = json.loads(response.content)
            rows = self.requests[-1]["messages"]
            value["topic_updates"] = [{"batch_local_key": "topic", "title": "Discussion", "summary": "Sourced discussion",
                "source_message_ids": [rows[0]["id"]], "member_message_ids": ["unseen-alias"] if unknown_member else []}]
            response.content = json.dumps(value)
            return response
    service, store, policy, coordinator, model = pipeline(tmp_path, "selected", MembersModel())
    insert(service, 1)
    finish(service, policy, coordinator)
    if unknown_member:
        assert store.list(status="failed") and not store.list(status="succeeded")
        assert len(model.requests) == 2 and service.reading_status()["schedules"][0]["analysis_watermark_seq"] == 0
        return
    assert store.list(status="succeeded") and len(model.requests) == 1
    with service._connection() as conn:
        details = json.loads(conn.execute("SELECT details_json FROM message_reading_topics").fetchone()[0])
    assert details["member_message_ids"] == details["source_message_ids"]


def test_topic_validator_feedback_contains_only_known_code_and_field_path():
    from pydantic import ValidationError

    from app.core.message_analysis import _reading_validation_feedback
    from app.domains.message_reading_results import TopicUpdate
    with pytest.raises(ValidationError) as error:
        TopicUpdate(title="private title", summary="private text", source_message_ids=["private id"])
    feedback = _reading_validation_feedback(error.value)
    assert feedback[0]["code"] == "topic_requires_one_identity"
    assert "private" not in str(feedback)


def test_rebuilding_checkpoint_does_not_reset_exhausted_call_budget(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path, "selected", BadThenGood())
    insert(service, 1)
    finish(service, policy, coordinator)
    failed = store.list(status="failed")[0]
    family = store.get(failed["job_id"], include_payload=True)["payload"]["work_family_id"]
    with service._connection() as conn:
        conn.execute("UPDATE message_reading_families SET max_calls=2 WHERE family_id=?", (family,))
    service.schedule_pending(policy["conversation_key"], force=True)
    assert coordinator.worker.run_one()
    assert len(model.requests) == 2
    assert store.get(failed["job_id"])["error_class"] == "work_budget_exhausted"
    assert coordinator.controller.quota_usage(family)["total_calls"] == 2


def test_checkpoint_version_change_archived_and_replayed_same_family(tmp_path, monkeypatch):
    from app.tool_packages import message_reading_analysis as contract
    service, store, policy, coordinator, model = pipeline(tmp_path)
    insert(service, 1, "".join(f"distinct line {i} 中文abcdefghijk\n" for i in range(250)))
    service.schedule_pending(policy["conversation_key"], force=True)
    assert coordinator.worker.run_one()
    monkeypatch.setattr(contract, "VERSIONS", {**contract.VERSIONS, "prompt": "new-contract"})
    assert coordinator.worker.run_one()
    failed = store.list(status="failed")[0]
    assert failed["error_class"] == "checkpoint_input_changed"
    before = len(model.requests)
    assert service.retry_analysis(policy["conversation_key"], failed["updated_at"])["status"] == "retried"
    finish(service, policy, coordinator)
    assert len(model.requests) > before and store.get(failed["job_id"])["status"] == "succeeded"
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_families").fetchone()[0] == 1
        archive = json.loads(conn.execute("SELECT checkpoint_json FROM message_reading_checkpoint_archives").fetchone()[0])
        assert json.loads(archive["cursor_json"]) == 1
    assert service.summary(policy["conversation_key"])["covered_seq"] == 1


def test_prior_facts_frozen_during_continuation(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path)
    insert(service, 1, "".join(f"different sentence {i} 中文abcdefghijk\n" for i in range(250)))
    service.schedule_pending(policy["conversation_key"], force=True)
    assert coordinator.worker.run_one()
    original = service.load_analysis_batch
    def changed(job):
        batch = original(job)
        if batch:
            batch["previous_facts"] = [{"fact_id": "external-change", "text": "new context"}]
        return batch
    service.load_analysis_batch = changed
    finish(service, policy, coordinator)
    assert store.list(status="succeeded") and not store.list(status="failed")
    assert all(request["prior_facts"] == [] for request in model.requests)


@pytest.mark.parametrize("error", ["work_budget_exhausted", "provider_http_401", "input_too_large"])
def test_nonrecoverable_failures_never_receive_new_allowance(tmp_path, error):
    service, _store, policy, coordinator, model = pipeline(tmp_path, "selected")
    insert(service, 1)
    job = service.schedule_pending(policy["conversation_key"], force=True)[0]
    with service._connection() as conn:
        conn.execute("UPDATE background_jobs SET status='failed',error_class=? WHERE job_id=?", (error, job["job_id"]))
    assert service.schedule_pending(policy["conversation_key"], force=True)[0]["status"] == "failed"
    assert not coordinator.worker.run_one() and not model.requests


def test_recovery_respects_pause_and_retry_cas(tmp_path):
    service, store, policy, coordinator, _ = pipeline(tmp_path, "selected", BadThenGood())
    insert(service, 1)
    finish(service, policy, coordinator)
    failed = store.list(status="failed")[0]
    assert service.retry_analysis(policy["conversation_key"], "old-revision")["status"] == "conflict"
    state = service.set_reading_paused(True, 1)
    assert service.schedule_pending(policy["conversation_key"], force=True) == []
    assert service.retry_analysis(policy["conversation_key"], failed["updated_at"])["status"] == "unsupported"
    service.set_reading_paused(False, state["revision"])
    service.set_policy({**IDENTITY, "record_enabled": False, "expected_revision": policy["revision"]})
    assert service.retry_analysis(policy["conversation_key"], failed["updated_at"])["status"] == "not_allowed"


def test_replay_has_fixed_target_and_bypasses_cadence_not_pause(tmp_path):
    service, _store, policy, coordinator, _ = pipeline(tmp_path, "selected")
    policy = service.set_policy({**IDENTITY, "expected_revision": policy["revision"], "record_enabled": True,
        "analysis_enabled": True, "auto_analyze": False, "max_batch_messages": 2,
        "min_interval_seconds": 1800, "max_wait_seconds": 7200})
    insert(service, 5)
    paused = service.set_reading_paused(True, 1)
    replay = service.request_reading_replay(policy["conversation_key"], policy["revision"])
    assert replay["status"] == "paused" and replay["through_seq"] == 5
    with pytest.raises(ValueError, match="conflict"):
        service.request_reading_replay(policy["conversation_key"], policy["revision"] - 1)
    service.set_reading_paused(False, paused["revision"])
    insert(service, 6)
    for _ in range(6):
        service.schedule_pending(policy["conversation_key"])
        coordinator.worker.run_one()
    state = service.reading_status()["schedules"][0]
    assert state["analysis_watermark_seq"] == 5
    assert state["replay_pending"] == 0 and state["pending_messages"] == 1
    assert service.schedule_pending(policy["conversation_key"]) == []
    with service._connection() as conn:
        payloads = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM background_jobs")]
    assert all(p["end_seq"] <= 5 for p in payloads)
