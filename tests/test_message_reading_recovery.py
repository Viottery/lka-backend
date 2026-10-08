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


def test_literal_recovery_feedback_uses_schema_values_without_private_input():
    from pydantic import ValidationError

    from app.core.message_analysis import _reading_validation_feedback
    from app.domains.message_history import AnalysisResult
    from app.domains.message_reading_results import ReadingFinding

    with pytest.raises(ValidationError) as error:
        AnalysisResult(schema_version=3, summary="summary", batch_summary="batch",
                       highlights=[{"text": "private body", "source_message_ids": ["private id"],
                                    "reason_codes": ["private-invalid-reason"]}])
    feedback = _reading_validation_feedback(error.value)
    expected = ReadingFinding.model_json_schema()["properties"]["reason_codes"]["items"]["enum"]
    assert feedback[0]["allowed_values"] == expected
    assert "private" not in json.dumps(feedback)
    assert len(json.dumps(feedback, separators=(",", ":")).encode()) <= 480


def test_unknown_reference_feedback_explains_fragment_scope_without_ids():
    from app.core.message_analysis import _reading_validation_feedback

    feedback = _reading_validation_feedback(ValueError("reading_reference_not_seen"))
    assert "CURRENT messages" in feedback[0]["hint"]
    assert "fragment suffixes" in feedback[0]["hint"]
    assert "Only evidence_requests" in feedback[0]["hint"]


def test_incomplete_generation_feedback_is_safe_and_explicit():
    from app.core.background_llm import IncompleteGenerationError
    from app.core.message_analysis import _reading_validation_feedback

    feedback = _reading_validation_feedback(IncompleteGenerationError("private provider output"))
    assert feedback[0]["code"] == "incomplete_generation"
    assert "possibly truncated" in feedback[0]["hint"]
    assert "complete, concise JSON" in feedback[0]["hint"]
    assert "preserves valid evidence references" in feedback[0]["hint"]
    assert "private provider output" not in json.dumps(feedback)


def test_legacy_generic_checkpoint_feedback_gets_safe_hint_at_prompt_time():
    from app.core.message_analysis import _normalize_reading_validation_feedback

    old_feedback = [{"code": "invalid_analysis_result"} for _ in range(8)]
    old_feedback_snapshot = json.loads(json.dumps(old_feedback))
    feedback = _normalize_reading_validation_feedback(old_feedback)
    assert feedback[0]["code"] == "invalid_analysis_result"
    assert "complete, concise JSON" in feedback[0]["hint"]
    assert "keep valid evidence references" in feedback[0]["hint"]
    assert "private" not in json.dumps(feedback)
    assert old_feedback == old_feedback_snapshot
    assert len(json.dumps(feedback, separators=(",", ":")).encode()) <= 480
    assert _normalize_reading_validation_feedback([{"code": "private provider body"}])[0]["code"] == "invalid_analysis_result"


@pytest.mark.parametrize(
    "values,code,required_hint",
    [
        ({"text": " ", "existing_topic_id": "topic", "batch_local_key": "new"},
         "invalid_finding", "text must be nonblank"),
        ({"kind": "useful", "existing_insight_id": "insight"},
         "correction_requires_explicit_evidence", "correction with certainty explicit"),
        ({"due_at": "2026-10-08T12:00:00", "due_provenance": "explicit"},
         "deadline_requires_grounded_timezone", "timezone-aware ISO value"),
    ],
)
def test_reading_finding_validator_errors_have_safe_specific_feedback(values, code, required_hint):
    from pydantic import ValidationError

    from app.core.message_analysis import _reading_validation_feedback
    from app.domains.message_reading_results import ReadingFinding

    finding = {
        "kind": "useful",
        "text": "Deployment information",
        "source_message_ids": ["message-alias"],
        **values,
    }
    with pytest.raises(ValidationError) as error:
        ReadingFinding.model_validate(finding)
    feedback = _reading_validation_feedback(error.value)
    assert feedback[0]["code"] == code
    assert required_hint in feedback[0]["hint"]
    assert "message-alias" not in json.dumps(feedback)


def test_legacy_finding_schema_feedback_gets_bounded_safe_hint():
    from app.core.message_analysis import _normalize_reading_validation_feedback

    old_feedback = [
        {"code": "schema_validation", "type": "value_error", "path": [field, index]}
        for field, index in (("highlights", 0), ("importance_findings", 8))
    ]
    original = json.loads(json.dumps(old_feedback))
    feedback = _normalize_reading_validation_feedback(old_feedback)
    assert "existing_topic_id" in feedback[0]["hint"]
    assert "explicit correction" in feedback[0]["hint"]
    assert "timezone-aware" in feedback[0]["hint"]
    assert old_feedback == original
    assert len(json.dumps(feedback, separators=(",", ":")).encode()) <= 480


def test_unknown_supersedes_alias_becomes_controlled_reference_feedback():
    from app.core.message_analysis import (
        _reading_validation_feedback,
        _reverse_fact_aliases,
    )

    with pytest.raises(ValueError, match="reading_unknown_reference") as error:
        _reverse_fact_aliases(["invented-alias"], {"known-alias": "fact-id"})
    feedback = _reading_validation_feedback(error.value)
    assert feedback[0]["code"] == "reading_unknown_reference"
    assert "prior_facts.fact_id" in feedback[0]["hint"]
    assert "invented-alias" not in json.dumps(feedback)


def test_source_list_recovery_feedback_exposes_schema_limit_without_ids():
    from pydantic import ValidationError

    from app.core.message_analysis import _reading_validation_feedback
    from app.domains.message_history import AnalysisResult

    with pytest.raises(ValidationError) as error:
        AnalysisResult(schema_version=3, summary="summary", batch_summary="batch", topic_updates=[{
            "batch_local_key": "private key", "title": "private title", "summary": "private body",
            "source_message_ids": [f"private id {index}" for index in range(51)],
        }])
    feedback = _reading_validation_feedback(error.value)
    assert feedback[0]["max_items"] == 50
    assert feedback[0]["min_items"] == 1
    assert "private" not in json.dumps(feedback)


@pytest.mark.parametrize("field,value,minimum,maximum", [
    ("action_key", "private" * 34, 1, 200),
    ("action_key", "", 1, 200),
    ("existing_topic_id", "private" * 21, None, 120),
], ids=["optional-string-too-long", "optional-string-too-short", "optional-reference-too-long"])
def test_nullable_string_feedback_uses_schema_limits(field, value, minimum, maximum):
    from pydantic import ValidationError

    from app.core.message_analysis import _reading_validation_feedback
    from app.domains.message_history import AnalysisResult

    with pytest.raises(ValidationError) as error:
        AnalysisResult(schema_version=3, summary="summary", batch_summary="batch", highlights=[{
            "text": "private body", "source_message_ids": ["private id"], field: value,
        }])
    feedback = _reading_validation_feedback(error.value)
    assert feedback[0]["max_length"] == maximum
    assert feedback[0].get("min_length") == minimum
    assert "private" not in json.dumps(feedback)
    assert len(json.dumps(feedback, separators=(",", ":")).encode()) <= 480


def test_nullable_list_feedback_uses_schema_limit_without_private_ids():
    from pydantic import ValidationError

    from app.core.message_analysis import _reading_validation_feedback
    from app.domains.message_history import AnalysisResult

    with pytest.raises(ValidationError) as error:
        AnalysisResult(schema_version=3, summary="summary", batch_summary="batch", topic_updates=[{
            "batch_local_key": "private key", "title": "private title", "summary": "private body",
            "source_message_ids": ["private id"], "member_message_ids": ["private id"] * 10001,
        }])
    feedback = _reading_validation_feedback(error.value)
    assert feedback[0]["max_items"] == 10000
    assert "private" not in json.dumps(feedback)
    assert len(json.dumps(feedback, separators=(",", ":")).encode()) <= 480


def test_schema_lookup_unwraps_nullable_reference_before_list_item():
    from app.core.message_analysis import _reading_schema_node

    item = {"type": "string", "enum": ["allowed"]}
    schema = {"properties": {"rows": {"anyOf": [{"$ref": "#/$defs/Rows"}, {"type": "null"}]}},
              "$defs": {"Rows": {"type": "array", "maxItems": 2, "items": item}}}
    assert _reading_schema_node(schema, ("rows",))["maxItems"] == 2
    assert _reading_schema_node(schema, ("rows", 0)) == item


def test_schema_lookup_does_not_guess_ambiguous_union_branch():
    from app.core.message_analysis import _reading_schema_node

    field = {"anyOf": [{"type": "string", "maxLength": 5},
                       {"type": "array", "maxItems": 3}, {"type": "null"}]}
    node = _reading_schema_node({"properties": {"value": field}}, ("value",))
    assert node == field
    assert "maxLength" not in node and "maxItems" not in node


def test_nullable_action_key_recovery_keeps_validation_and_budget(tmp_path):
    class LengthFeedbackModel(Model):
        async def complete_text(self, **kwargs):
            response = await super().complete_text(**kwargs)
            request = self.requests[-1]
            feedback = request.get("validation_feedback", [])
            limit = feedback[0].get("max_length") if feedback else None
            value = json.loads(response.content)
            value["highlights"] = [{
                "text": "Sourced synthetic finding",
                "source_message_ids": [request["messages"][0]["id"]],
                "action_key": "x" * (limit if limit is not None else 201),
            }]
            response.content = json.dumps(value)
            return response

    service, store, policy, coordinator, model = pipeline(tmp_path, "selected", LengthFeedbackModel())
    insert(service, 1)
    finish(service, policy, coordinator)
    assert len(model.requests) == 2
    assert len(store.list(status="succeeded")) == 1
    assert service.reading_status()["schedules"][0]["analysis_watermark_seq"] == 1
    job = store.list(status="succeeded")[0]
    family = store.get(job["job_id"], include_payload=True)["payload"]["work_family_id"]
    assert coordinator.controller.quota_usage(family)["total_calls"] == 2


def test_envelope_recovery_feedback_explains_exact_shape():
    from app.core.message_analysis import _reading_validation_feedback

    feedback = _reading_validation_feedback(ValueError("invalid_reading_v3_envelope"))
    assert "integer 3" in feedback[0]["hint"]
    assert "Do not add summary" in feedback[0]["hint"]
    assert len(json.dumps(feedback, separators=(",", ":")).encode()) <= 480


def test_unknown_existing_reference_feedback_allows_new_topic_without_fabricated_identity(tmp_path):
    class UnknownTopicModel(Model):
        async def complete_text(self, **kwargs):
            response = await super().complete_text(**kwargs)
            request = self.requests[-1]
            value = json.loads(response.content)
            topic = {"title": "Synthetic topic", "summary": "Sourced discussion",
                     "source_message_ids": [request["messages"][0]["id"]]}
            feedback = request.get("validation_feedback", [])
            if feedback:
                hint = feedback[0]["hint"]
                assert "known_topics.topic_id" in hint and "known_insights.insight_id" in hint
                assert "batch_local_key" in hint
                assert "private-invented-topic-id" not in json.dumps(feedback)
                assert len(json.dumps(feedback, separators=(",", ":")).encode()) <= 480
                topic["batch_local_key"] = "new-topic"
            else:
                topic["existing_topic_id"] = "private-invented-topic-id"
            value["topic_updates"] = [topic]
            response.content = json.dumps(value)
            return response

    service, store, policy, coordinator, model = pipeline(tmp_path, "selected", UnknownTopicModel())
    insert(service, 1)
    finish(service, policy, coordinator)
    assert len(model.requests) == 2 and store.list(status="succeeded")
    assert service.reading_status()["schedules"][0]["analysis_watermark_seq"] == 1
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_topics").fetchone()[0] == 1


def test_reference_constraints_copy_only_granted_ids_and_override_untrusted_context():
    from app.tool_packages.message_reading_analysis import prompt

    context = {"known_topics": [{"topic_id": "granted-topic", "title": "other-title"}],
               "known_insights": [], "prior_facts": [{"fact_id": "granted-fact"}],
               "reference_constraints": {"existing_insight_id": ["not-granted"]}}
    value = json.loads(prompt(context, [], []))
    assert value["reference_constraints"] == {
        "existing_topic_id": ["granted-topic"], "existing_insight_id": [],
        "supersedes_fact_ids": ["granted-fact"]}
    assert context["reference_constraints"]["existing_insight_id"] == ["not-granted"]


def test_processing_revision_change_reports_checkpoint_conflict_then_archives_on_restart(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path, "selected", BadThenGood())
    insert(service, 1)
    finish(service, policy, coordinator)
    old_job = store.list(status="failed")[0]
    family = store.get(old_job["job_id"], include_payload=True)["payload"]["work_family_id"]
    with service._connection() as conn:
        stale_checkpoint = tuple(conn.execute("SELECT * FROM message_reading_checkpoints WHERE family_id=?", (family,)).fetchone())
    policy = service.set_policy({**IDENTITY, "record_enabled": True, "analysis_enabled": True,
        "batch_size": 100, "max_batch_messages": 100, "min_interval_seconds": 0,
        "timezone": "UTC", "expected_revision": policy["revision"]})
    # Simulate a persisted checkpoint from an older deployment that did not
    # fence/remove it when the policy processing revision changed.
    with service._connection() as conn:
        conn.execute("INSERT INTO message_reading_checkpoints VALUES(?,?,?,?,?,?,?)", stale_checkpoint)
    new_job = service.schedule_pending(policy["conversation_key"], force=True)[0]
    assert new_job["job_id"] != old_job["job_id"]
    assert coordinator.worker.run_one()
    failed = store.get(new_job["job_id"])
    assert failed["error_class"] == "checkpoint_input_changed"
    assert len(model.requests) == 2
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_checkpoints WHERE family_id=?", (family,)).fetchone()[0] == 1
    retried = service.retry_analysis(policy["conversation_key"], failed["updated_at"], allow_checkpoint_restart=True)
    assert retried["status"] == "retried"
    finish(service, policy, coordinator)
    assert store.get(new_job["job_id"])["status"] == "succeeded"
    assert len(model.requests) == 3
    assert coordinator.controller.quota_usage(family)["total_calls"] == 3
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_checkpoint_archives WHERE family_id=?", (family,)).fetchone()[0] == 1


def test_explicit_checkpoint_restart_preserves_budget_attempts_and_cas(tmp_path):
    service, store, policy, coordinator, _model = pipeline(tmp_path, "selected", BadThenGood(4))
    insert(service, 1)
    finish(service, policy, coordinator)
    finish(service, policy, coordinator)
    failed = store.list(status="failed")[0]
    family = store.get(failed["job_id"], include_payload=True)["payload"]["work_family_id"]
    with service._connection() as conn:
        conn.execute("UPDATE background_jobs SET attempts=max_attempts WHERE job_id=?", (failed["job_id"],))
    assert service.retry_analysis(policy["conversation_key"], "stale", allow_checkpoint_restart=True)["status"] == "conflict"
    assert service.retry_analysis(policy["conversation_key"], failed["updated_at"])["status"] == "unsupported"
    result = service.retry_analysis(policy["conversation_key"], failed["updated_at"], allow_checkpoint_restart=True)
    assert result["status"] == "retried"
    assert coordinator.controller.quota_usage(family)["total_calls"] == 4
    assert store.get(failed["job_id"])["attempts"] == 5
    assert store.get(failed["job_id"])["max_attempts"] == 6
    assert service.retry_analysis(policy["conversation_key"], failed["updated_at"], allow_checkpoint_restart=True)["status"] == "missing"
    finish(service, policy, coordinator)
    assert store.get(failed["job_id"])["status"] == "succeeded"
    assert coordinator.controller.quota_usage(family)["total_calls"] == 5


@pytest.mark.parametrize("per_fragment", [False, True])
def test_opt_in_fragment_recovery_keeps_default_bound_and_whole_work_ledger(tmp_path, per_fragment):
    class EachFragmentNeedsRepair(Model):
        def __init__(self):
            super().__init__()
            self.seen = set()

        async def complete_text(self, **kwargs):
            response = await super().complete_text(**kwargs)
            key = tuple(row["id"] for row in self.requests[-1]["messages"])
            if key not in self.seen:
                self.seen.add(key)
                response.content = "invalid-json"
            return response

    service, store, policy, coordinator, model = pipeline(
        tmp_path, "compact", EachFragmentNeedsRepair(), fragment_recovery_enabled=per_fragment)
    insert(service, 1, "synthetic discussion. " * 500)
    assert len(service.recent()["messages"]) == 1
    finish(service, policy, coordinator)
    assert len(model.seen) >= 2
    if not per_fragment:
        assert store.list(status="failed") and not store.list(status="succeeded")
        assert service.reading_status()["schedules"][0]["analysis_watermark_seq"] == 0
    else:
        assert store.list(status="succeeded") and not store.list(status="failed")
        assert len(model.requests) == 2 * len(model.seen)
        assert service.reading_status()["schedules"][0]["analysis_watermark_seq"] == 1
        family = store.get(store.list(status="succeeded")[0]["job_id"], include_payload=True)["payload"]["work_family_id"]
        assert coordinator.controller.quota_usage(family)["total_calls"] == len(model.requests)


@pytest.mark.parametrize("split", [False, True])
def test_complete_message_base_alias_is_canonicalized_but_partial_message_is_rejected(tmp_path, split):
    class BaseAliasModel(Model):
        async def complete_text(self, **kwargs):
            response = await super().complete_text(**kwargs)
            request = self.requests[-1]
            value = json.loads(response.content)
            value["highlights"] = [{"text": "Sourced synthetic finding",
                                    "source_message_ids": [request["authorized_aliases"][0]]}]
            response.content = json.dumps(value)
            return response

    service, store, policy, coordinator, model = pipeline(tmp_path, "compact", BaseAliasModel())
    insert(service, 1, "synthetic discussion. " * (500 if split else 1))
    finish(service, policy, coordinator)
    if split:
        assert store.list(status="failed") and not store.list(status="succeeded")
        assert service.reading_status()["schedules"][0]["analysis_watermark_seq"] == 0
    else:
        assert len(model.requests) == 1 and store.list(status="succeeded")
        assert service.reading_status()["schedules"][0]["analysis_watermark_seq"] == 1


@pytest.mark.parametrize("bad_reference", [False, True])
def test_actionable_feedback_repairs_model_output_without_relaxing_validation(tmp_path, bad_reference):
    class FeedbackModel(Model):
        async def complete_text(self, **kwargs):
            response = await super().complete_text(**kwargs)
            request = self.requests[-1]
            feedback = request.get("validation_feedback", [])
            repaired = bool(feedback) and (
                "CURRENT messages" in feedback[0].get("hint", "") if bad_reference
                else "worth_reading" in feedback[0].get("allowed_values", [])
            )
            value = json.loads(response.content)
            value["highlights"] = [{
                "text": "Sourced synthetic finding",
                "source_message_ids": [request["messages"][0]["id"] if repaired or not bad_reference
                                       else "unseen-alias"],
                "reason_codes": ["worth_reading" if repaired or bad_reference else "invalid-reason"],
            }]
            response.content = json.dumps(value)
            return response

    service, store, policy, coordinator, model = pipeline(tmp_path, "selected", FeedbackModel())
    insert(service, 1)
    finish(service, policy, coordinator)
    assert len(model.requests) == 2
    assert len(store.list(status="succeeded")) == 1
    assert service.reading_status()["schedules"][0]["analysis_watermark_seq"] == 1


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
