"""Strict partial delivery and source-independent watch identity regressions."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.core.multi_agent import TaskResultStatus
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.watch_execution import WatchExecutionResult
from app.core.watch_scheduler import WatchScheduler, _merge_previous_observations
from app.domains.watch import WatchInput
from app.domains.watch_briefing import _excerpt_supports, normalize_briefing
from evals.lka_evals.live_budget import LiveBudget
from scripts.eval_runtime_memory_quality import isolated_config
from scripts.eval_runtime_watch_quality import run_probe
from tests.test_eval_runtime_watch_quality import fixture_service


def payload(claim="The venue is North.", **fields):
    return json.dumps({"summary": "Observations", "changes": [{
        "claim": claim, "evidence_refs": ["document-one"], **fields}],
        "unchanged": [], "unconfirmed": [], "decisions": []})


def normalize(summary, previous=None, ref="document-one", excerpt="The venue is North."):
    return normalize_briefing(summary=summary, evidence=[{
        "ref": ref, "evidence_id": ref, "excerpt": excerpt}],
        previous=previous, importance_rules={})


def test_subject_identity_survives_new_source_and_prior_replay():
    first = normalize(payload(subject_key="review/venue", event_id="message-one"))
    prior = _merge_previous_observations([{**first, "created_at": "2026-10-01"}])
    assert prior["changes"][0]["subject_key"] == "review/venue"
    second = normalize(payload(subject_key="review/venue", event_id="message-two"), prior)
    assert not second["changes"] and len(second["unchanged"]) == 1
    third_payload = json.loads(payload("The venue is South.",
        subject_key="review/venue", event_id="message-three"))
    third_payload["changes"][0]["evidence_refs"] = ["document-two"]
    third = normalize(json.dumps(third_payload), second, "document-two", "The venue is South.")
    assert third["changes"][0]["event_key"] == first["changes"][0]["event_key"]
    assert third["changes"][0]["previous_observation"] == "The venue is North."
    assert first["changes"][0]["event_key"] == hashlib.sha256(b"review/venue").hexdigest()


def test_canonical_prior_digest_is_not_hashed_again_and_legacy_id_stays_compatible():
    first = normalize(payload(event_id="legacy-one"))
    key = first["changes"][0]["event_key"]
    replay = normalize(payload(event_key=key), first)
    assert replay["unchanged"][0]["event_key"] == key
    assert key == hashlib.sha256(b"legacy-one").hexdigest()


@pytest.mark.parametrize("fields", [
    {"current_observation": "The venue is not North."},
    {"current_observation": ""}, {"current_observation": None}, {"current_observation": []},
    {"subject_key": "x" * 201}, {"subject_key": " "}, {"subject_key": 12},
])
def test_invalid_display_or_identity_never_becomes_verified(fields):
    result = normalize(payload(**fields))
    assert not result["changes"] and not result["unchanged"]
    assert result["unconfirmed"]


def test_quoted_anchor_does_not_verify_outer_negation_or_foreign_reference():
    result = normalize(payload('It is false that "The venue is North."'))
    assert not result["changes"]
    foreign = normalize(payload(subject_key="review/venue"), ref="foreign-document")
    assert not foreign["changes"]
    assert foreign["unconfirmed"][0]["evidence_check"] == "citation_mismatch"


@pytest.mark.parametrize(("claim", "source"), [
    ("approved", "unapproved"), ("批准", "未批准"),
    ("approved", "The request was not approved."),
    ("The request was approved", "It is not true that the request was approved."),
])
def test_fragment_cannot_strip_qualifiers_or_negation(claim, source):
    assert not normalize(payload(claim), excerpt=source)["changes"]


def test_complete_negated_clause_is_literal_evidence_not_positive_inference():
    source = "The request was not approved."
    assert normalize(payload(source), excerpt=source)["changes"]
    inverted = normalize(payload(source, current_observation="The request was approved."), excerpt=source)
    assert not inverted["changes"]


def test_question_punctuation_cannot_be_removed_to_invent_assertion():
    assert not normalize(payload("Approved"), excerpt="Approved?")["changes"]
    assert normalize(payload("Approved?"), excerpt="Approved?")["changes"]


def test_uncited_unknown_remains_unconfirmed_without_fabricated_quote():
    data = json.loads(payload())
    data["unconfirmed"] = [{"claim": "Owner is unknown within the inspected evidence.",
        "evidence_refs": []}]
    result = normalize(json.dumps(data))
    assert len(result["changes"]) == 1
    assert len(result["unconfirmed"]) == 1
    assert result["unconfirmed"][0]["evidence_refs"] == []


def test_real_scheduler_three_slots_publish_budget_partial_without_changing_child_status(tmp_path):
    config, service = fixture_service(tmp_path, budget_partial=True)
    report = run_probe(tmp_path / "derived", LiveBudget(tmp_path / "budget.sqlite3"),
        config=config, injected_service=service)
    assert report["mechanical_checks_pass"], report["mechanical_checks"]
    rows = report["occurrences"]
    assert [r["child_task_status"] for r in rows] == ["partial", "partial", "completed"]
    assert [r["classification_counts"]["changes"] for r in rows] == [1, 0, 1]
    assert rows[1]["classification_counts"]["unchanged"] == 1
    assert all(any(x.get("reason") == "child_budget_finish" for x in r["briefing"]["unconfirmed"])
        for r in rows[:2])
    assert all(r["occurrence"]["status"] == "succeeded" for r in rows)
    keys = [r["briefing"]["changes" if i != 1 else "unchanged"][0]["event_key"]
        for i, r in enumerate(rows)]
    assert len(set(keys)) == 1
    assert rows[2]["briefing"]["changes"][0]["previous_observation"] == rows[0]["briefing"]["changes"][0]["claim"]
    assert rows[2]["briefing"]["changes"][0]["evidence_refs"] != rows[0]["briefing"]["changes"][0]["evidence_refs"]
    for row in rows[:2]:
        parent = next(r for r in report["runs"] if r["run"]["run_id"] == row["occurrence"]["run_id"])
        assert any(e["type"] == "watch_partial_delivery" for e in parent["events"])


@pytest.fixture
def scheduler_runtime(tmp_path):
    config = isolated_config(LocalAppConfig())
    config = config.model_copy(update={"memory": config.memory.model_copy(
        update={"enabled": False, "background_enabled": False})})
    settings = Settings(LKA_DATA_DIR=tmp_path / "data", LKA_WORKSPACE_ROOTS=str(tmp_path))
    with patch.object(Settings, "load_local_config", return_value=config):
        runtime = LocalKnowledgeAgentRuntime(settings)
    try:
        yield runtime
    finally:
        runtime.stop()


@pytest.mark.parametrize(("status", "missing", "failure", "summary"), [
    ("partial", (), None, payload()),
    ("partial", ("child_budget_finish", "child_answer_missing"), None, payload()),
    ("partial", ("child_budget_finish",), "provider_error", payload()),
    ("partial", ("child_budget_finish",), None, " "),
    ("partial", ("child_budget_finish",), None, "{}"),
    ("partial", ("child_budget_finish",), None, payload().replace('"changes": [', '"invalid": [')),
    ("blocked", ("child_budget_finish",), None, payload()),
    ("failed", ("child_budget_finish",), None, payload()),
    ("cancelled", ("child_budget_finish",), None, payload()),
    ("timed_out", ("child_budget_finish",), None, payload()),
])
def test_scheduler_does_not_publish_other_failures_or_incomplete_json(
    scheduler_runtime, status, missing, failure, summary,
):
    runtime = scheduler_runtime

    class Adapter:
        called = False

        async def run_async(self, **kwargs):
            self.called = True
            return WatchExecutionResult(status=TaskResultStatus(status), summary=summary,
                run_id=kwargs["parent_run_id"], child_run_id="gate-fixture", snapshot_id="gate-snapshot",
                missing_requirements=missing, failure_category=failure)

    watch = runtime.watch_service.create(WatchInput(title="Review", goal="Follow review updates",
        timezone="UTC", daily_time="08:00", categories=["mail"], scope={"web_enabled": False,
            "source_ids": ["gate-fixture-source"], "account_ids": ["gate-fixture-account"]}))
    slot = datetime.now(UTC) - timedelta(minutes=1)
    runtime.watch_service.create_occurrence(watch["watch_id"], slot)
    adapter = Adapter()
    assert WatchScheduler(runtime, runtime.watch_service, adapter=adapter).run_one(owner="gate-test")
    assert adapter.called
    assert runtime.watch_service.get_occurrence(watch["watch_id"], slot)["status"] == "failed"
    assert not runtime.watch_service.list_briefings(watch_id=watch["watch_id"])


def test_subject_key_two_hundred_characters_is_forwarded_without_truncation():
    key = "x" * 200
    first = normalize(payload(subject_key=key))
    prior = _merge_previous_observations([{**first, "created_at": "2026-10-01"}])
    assert prior["changes"][0]["subject_key"] == key
    assert normalize(payload(subject_key=key), prior)["unchanged"]


@pytest.mark.parametrize(("source", "clipped"), [
    ("Ticket price is 12.50 dollars.", "Ticket price is 12"),
    ("Version v3.14 is unsupported.", "Version v3"),
    ("Use config.py only for inspection.", "Use config"),
    ("Approved... only after further review.", "Approved"),
    ("Balance: -12.50 dollars.", "Balance: 12.50 dollars."),
    ("Ticket price: $12.50.", "Ticket price: €12.50."),
    ("Ratio: 1/2.", "Ratio: 1.2."),
])
def test_decimal_or_version_dot_is_not_a_clause_boundary(source, clipped):
    assert not _excerpt_supports(clipped, source)
    assert _excerpt_supports(source, source)
