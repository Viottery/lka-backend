"""Offline reading projections and worker checks using temporary SQLite only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_message_analysis import IDENTITY, pipeline

from app.core.background_jobs import BackgroundJobStore
from app.domains.message_history import AnalysisResult, MessageHistoryService
from app.domains.message_reading_results import ReadingRevisionConflict

_TIMESTAMP = int(datetime.now(UTC).timestamp())


def imports(service, count, *, text):
    return service.import_messages(
        [
            {
                **IDENTITY,
                "message_id": str(index),
                "sender_id": "member",
                "text": text,
                "sent_at": _TIMESTAMP - index,
                "received_at": _TIMESTAMP + index,
            }
            for index in range(count)
        ]
    )


class ReadingModel:
    def __init__(self, bad=None):
        self.requests = []
        self.bad = bad

    async def complete_text(self, **kwargs):
        prompt = json.loads(kwargs["user_prompt"])
        self.requests.append(prompt)
        ids = list(dict.fromkeys(m["message_id"] for m in prompt["messages"]))
        topic = {
            "batch_local_key": "deployment",
            "title": "Deploy",
            "summary": "Deployment date discussed.",
            "source_message_ids": ids,
            "disagreements": ["Date not agreed"],
            "open_questions": ["Which date?"],
        }
        if prompt["known_topics"]:
            topic.pop("batch_local_key")
            topic["existing_topic_id"] = prompt["known_topics"][0]["topic_id"]
        value = {
            "schema_version": 2,
            "topic_updates": [topic],
            "highlights": [
                {
                    "kind": "useful",
                    "text": "Deployment discussion",
                    "source_message_ids": ids,
                    "importance": "ordinary",
                    "certainty": "explicit",
                }
            ],
            "importance_findings": [
                {
                    "text": "Please confirm deployment",
                    "source_message_ids": ids,
                    "importance": "critical",
                    "reason_codes": ["action_requested"],
                    "certainty": "explicit",
                }
            ],
            "facts": [],
            "warnings": [],
        }
        if self.bad == "source":
            value["topic_updates"][0]["source_message_ids"] = ["invented"]
        if self.bad == "topic":
            value["topic_updates"][0].pop("batch_local_key", None)
            value["topic_updates"][0]["existing_topic_id"] = "not-granted"
        return SimpleNamespace(content=json.dumps(value), metadata={})


def test_importance_bucket_does_not_disappear_when_semantic_kind_is_decision(tmp_path):
    class DecisionModel(ReadingModel):
        async def complete_text(self, **kwargs):
            response = await super().complete_text(**kwargs)
            value = json.loads(response.content)
            value["importance_findings"][0]["kind"] = "decision"
            response.content = json.dumps(value)
            return response
    service, _store, _policy, coordinator = pipeline(tmp_path, model=DecisionModel(), batch_size=1)
    imports(service, 1, text="Discuss deployment")
    assert coordinator.worker.run_one()
    important = service.list_insights(kind="importance")["insights"]
    assert len(important) == 1 and important[0]["kind"] == "decision"
    assert service.reading_overview()["unseen_count"] == 1
    service.set_attention(important[0]["insight_id"], 0, viewed_revision=important[0]["revision"])
    assert service.reading_overview()["unseen_count"] == 0


def drain(worker):
    for _ in range(100):
        if not worker.run_one():
            return
    raise AssertionError("unbounded work")


def test_v2_combined_output_and_stable_topic_revision(tmp_path):
    model = ReadingModel()
    service, store, policy, coordinator = pipeline(tmp_path, model=model, batch_size=1)
    imports(service, 1, text="Deploy discussion")
    assert coordinator.worker.run_one()
    first = service.list_topics()["topics"][0]
    assert first["revision"] == 1 and first["disagreements"] == ["Date not agreed"]
    assert first["heat"]["score_version"] == "observed-v1"
    assert all(i["importance"] != "critical" for i in service.list_insights()["insights"])
    imports(service, 2, text="Deploy discussion")
    assert coordinator.worker.run_one()
    second = service.list_topics()["topics"][0]
    assert second["topic_id"] == first["topic_id"] and second["revision"] == 2
    assert len(service.topic_sources(first["topic_id"])["sources"]) == 2
    coverage = service.coverage(policy["conversation_key"])
    assert coverage["analysis_covered_seq"] == 2 and not coverage["legacy_only"]
    assert coverage["baseline_start_seq"] == 1 and not coverage["complete_for_platform"]
    assert len(store.list(status="succeeded")) == 2


def test_cooled_topic_keeps_total_participants_separate_from_recent_heat(tmp_path):
    service, _store, policy, _coordinator = pipeline(tmp_path, batch_size=2)
    now = int(datetime.now(UTC).timestamp())
    service.import_messages(
        [
            {
                **IDENTITY,
                "message_id": f"old-{index}",
                "sender_id": f"member-{index}",
                "text": "Discuss deployment",
                "sent_at": _TIMESTAMP - 3 * 86400 - index,
                "received_at": _TIMESTAMP - 3 * 86400 - index,
            }
            for index in range(2)
        ]
        + [
            {
                **IDENTITY,
                "message_id": "current",
                "sender_id": "current-member",
                "text": "Discuss deployment",
                "sent_at": now,
                "received_at": now,
            }
        ]
    )
    topic_id = "message_topic_cooled"
    with service._connection() as conn:
        message_ids = [
            row[0]
            for row in conn.execute(
                "SELECT internal_message_id FROM message_history_messages WHERE conversation_key=? ORDER BY seq",
                (policy["conversation_key"],),
            )
        ]
        conn.execute(
            "INSERT INTO message_reading_topics VALUES(?,?,?,?,?,?,?,?,?)",
            (
                topic_id,
                policy["conversation_key"],
                1,
                "Deploy",
                "Deployment discussed.",
                json.dumps({"conclusions": [], "disagreements": [], "open_questions": []}),
                _TIMESTAMP - 3 * 86400 - 1,
                _TIMESTAMP - 3 * 86400,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.executemany(
            "INSERT INTO message_reading_sources VALUES(?,?,?,?,?,?,?)",
            [
                ("topic", topic_id, 1, message_id, policy["conversation_key"], None, "discussion")
                for message_id in message_ids[:2]
            ],
        )
        active_topic_id = "message_topic_active"
        conn.execute(
            "INSERT INTO message_reading_topics VALUES(?,?,?,?,?,?,?,?,?)",
            (
                active_topic_id,
                policy["conversation_key"],
                1,
                "Deploy now",
                "Deployment is active.",
                json.dumps({"conclusions": [], "disagreements": [], "open_questions": []}),
                now,
                now,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO message_reading_sources VALUES(?,?,?,?,?,?,?)",
            ("topic", active_topic_id, 1, message_ids[2], policy["conversation_key"], None, "discussion"),
        )

    topic = service.get_topic(topic_id)
    assert topic["status"] == "cooled"
    assert topic["heat"]["participant_count"] == 0
    assert topic["heat"]["recent_participant_count"] == 0
    assert topic["heat"]["total_participant_count"] == 2
    assert topic["heat"]["score"] == 0
    active_topic = service.get_topic(active_topic_id)
    assert active_topic["heat"]["participant_count"] == 1
    assert active_topic["heat"]["recent_participant_count"] == 1
    assert active_topic["heat"]["total_participant_count"] == 1
    assert active_topic["heat"]["score"] > topic["heat"]["score"]


def test_local_native_mention_empty_body_exclusions_and_watermark(tmp_path):
    store = BackgroundJobStore(tmp_path / "local.sqlite")
    service = MessageHistoryService(store.db_path, store)
    service.ensure_schema()
    policy = service.set_policy({**IDENTITY, "record_enabled": True})
    profile = service.set_reading_profile(
        {"self_ids": {"mock": ["my-id"]}, "exclusions": ["noise"]}, 0
    )
    assert profile["revision"] == 1
    message = {
        **IDENTITY,
        "message_id": "native",
        "sender_id": "other",
        "received_at": 1790920000,
        "capture_epoch": policy["capture_epoch"],
        "text": "",
        "content_kind": "unsupported",
        "mentions": [{"kind": "user", "user_id": "my-id"}],
        "metadata_capabilities": {"mentions": "supported"},
        "adapter_id": "fake",
        "adapter_version": "2",
    }
    assert service.import_messages([message], schema_version=2)["acknowledged"]
    insights = service.list_insights()["insights"]
    assert len(insights) == 1 and insights[0]["detector"] == "local_rule"
    assert insights[0]["directed_to"] == "self" and insights[0]["certainty"] == "needs_review"
    assert service.coverage(policy["conversation_key"])["local_signal_seq"] == 1
    assert service.coverage(policy["conversation_key"])["analysis_covered_seq"] is None
    assert not store.list()
    assert service.scan_local_signals()["scanned"] == 0
    service.import_messages(
        [
            {
                **IDENTITY,
                "message_id": "excluded",
                "text": "noise please deadline",
                "received_at": 1790920001,
            }
        ]
    )
    assert len(service.list_insights()["insights"]) == 1


def test_recovery_scan_survives_import_scanner_gap_and_pause(tmp_path, monkeypatch):
    service, store, policy, _coordinator = pipeline(tmp_path, batch_size=1)
    service.set_reading_paused(True, service.reading_status()["revision"])
    original = service.scan_local_signals
    monkeypatch.setattr(service, "scan_local_signals", lambda *a, **k: None)
    imports(service, 1, text="please confirm deadline")
    assert service.coverage(policy["conversation_key"])["local_signal_seq"] == 0
    monkeypatch.setattr(service, "scan_local_signals", original)
    service.schedule_pending()
    assert service.coverage(policy["conversation_key"])["local_signal_seq"] == 1
    assert service.list_insights()["insights"] and not store.list()


@pytest.mark.parametrize("bad", ["source", "topic"])
def test_invalid_reading_reference_has_no_partial_publication(tmp_path, bad):
    service, store, policy, coordinator = pipeline(tmp_path, model=ReadingModel(bad), batch_size=1)
    imports(service, 1, text="Discuss deployment")
    drain(coordinator.worker)
    assert store.list(status="failed")
    assert service.coverage(policy["conversation_key"])["analysis_covered_seq"] is None
    assert not service.list_topics()["topics"]
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_history_batches").fetchone()[0] == 0


def test_checkpoint_new_local_topic_references_and_single_call_quantum(tmp_path):
    model = ReadingModel()
    service, _store, policy, coordinator = pipeline(
        tmp_path, model=model, batch_size=1, chunk_bytes=1024
    )
    imports(service, 1, text="Discuss deployment. " * 150)
    assert coordinator.worker.run_one() and len(model.requests) == 1
    assert not service.list_topics()["topics"]
    drain(coordinator.worker)
    assert len(service.list_topics()["topics"]) == 1
    assert (
        len(service.topic_sources(service.list_topics()["topics"][0]["topic_id"])["sources"]) == 1
    )
    assert any(p["staged_topics"] for p in model.requests[1:])
    assert service.coverage(policy["conversation_key"])["analysis_covered_seq"] == 1


def test_attention_cas_and_correction_reopens_same_item(tmp_path):
    service, store, _policy, coordinator = pipeline(tmp_path, model=ReadingModel(), batch_size=1)
    imports(service, 1, text="Discuss deployment")
    coordinator.worker.run_one()
    insight = service.list_insights(kind="importance")["insights"][0]
    seen = service.set_attention(insight["insight_id"], 0, viewed_revision=insight["revision"])
    assert seen["state"] == "seen"
    with pytest.raises(ReadingRevisionConflict):
        service.set_attention(insight["insight_id"], 0, dismissed_revision=insight["revision"])
    imports(service, 2, text="Discuss deployment")
    job = store.claim("test", lease_seconds=600, kinds={"message_analysis"})
    batch = service.load_analysis_batch(job)
    result = AnalysisResult(
        batch_summary="Correction",
        summary="Correction",
        schema_version=2,
        reading_snapshot={
            "known_topics": batch["known_topics"],
            "known_insights": batch["known_insights"],
        },
        importance_findings=[
            {
                "kind": "correction",
                "existing_insight_id": insight["insight_id"],
                "text": "Date changed",
                "source_message_ids": [batch["messages"][0]["message_id"]],
                "certainty": "explicit",
                "reason_codes": ["material_change"],
            }
        ],
    )
    assert service.publish_analysis(job, result)
    changed = service.get_insight(insight["insight_id"])
    assert changed["revision"] == 2 and changed["attention"]["has_new_changes"]
    assert changed["attention"]["state"] == "unseen"
    assert len(service.derived_sources("insight", insight["insight_id"])["sources"]) == 2


def test_scoped_keyset_sources_and_revocation(tmp_path):
    service, _store, policy, coordinator = pipeline(tmp_path, model=ReadingModel(), batch_size=2)
    imports(service, 2, text="Discuss deployment")
    drain(coordinator.worker)
    topic = service.list_topics()["topics"][0]
    first = service.topic_sources(topic["topic_id"], limit=1, allowed_sources=[policy["source_id"]])
    assert first["has_more"]
    second = service.topic_sources(
        topic["topic_id"],
        limit=1,
        cursor=first["next_cursor"],
        allowed_sources=[policy["source_id"]],
    )
    assert first["sources"][0]["message_id"] != second["sources"][0]["message_id"]
    with pytest.raises(ValueError, match="cursor"):
        service.topic_sources(topic["topic_id"], limit=1, cursor=first["next_cursor"])
    with pytest.raises(PermissionError):
        service.get_topic(topic["topic_id"], allowed_accounts=[])
    assert service.reading_overview(allowed_sources=[])["counts"] == {"topics": 0, "insights": 0}
    service.set_policy(
        {**IDENTITY, "record_enabled": False, "expected_revision": policy["revision"]}
    )
    with pytest.raises(PermissionError):
        service.topic_sources(topic["topic_id"])


def test_profile_cas_fences_pending_work_without_resetting_family(tmp_path):
    service, store, _policy, coordinator = pipeline(tmp_path, model=ReadingModel(), batch_size=1)
    imports(service, 1, text="Discuss deployment")
    job = store.list()[0]
    service.set_reading_profile({"keywords": ["deploy"]}, 0)
    assert store.get(job["job_id"])["status"] == "cancelled"
    with pytest.raises(ReadingRevisionConflict):
        service.set_reading_profile({}, 0)
    service.schedule_pending(force=True)
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_families").fetchone()[0] == 1
    drain(coordinator.worker)
    assert service.list_topics()["topics"]


def test_digest_throttles_changes_and_is_deterministic(tmp_path):
    model = ReadingModel()
    service, _store, policy, coordinator = pipeline(tmp_path, model=model, batch_size=1)
    imports(service, 1, text="Discuss deployment")
    drain(coordinator.worker)
    first = service.reading_digest(policy["conversation_key"])
    same = service.reading_digest(policy["conversation_key"])
    assert first == same and len(model.requests) == 1
    imports(service, 2, text="Discuss deployment")
    coordinator.worker.run_one()
    stale = service.reading_digest(policy["conversation_key"])
    assert stale["revision"] == first["revision"] and stale["stale"]
    assert stale["covered_seq"] == 1 and stale["coverage"]["analysis_covered_seq"] == 2
    assert len(model.requests) == 2


def test_legacy_summary_coverage_does_not_promote_to_reading(tmp_path):
    service, _store, policy, coordinator = pipeline(tmp_path, batch_size=1)
    imports(service, 1, text="Discuss deployment")
    coordinator.worker.run_one()
    assert service.coverage(policy["conversation_key"])["legacy_only"]
    assert service.coverage(policy["conversation_key"])["analysis_covered_seq"] is None
    assert not service.list_topics()["topics"]


def test_filters_validate_and_stale_feed_cursor(tmp_path):
    service, _store, _policy, coordinator = pipeline(tmp_path, model=ReadingModel(), batch_size=2)
    imports(service, 2, text="Discuss deployment")
    drain(coordinator.worker)
    first = service.list_insights(limit=1)
    assert first["next_cursor"]
    assert len(service.list_insights(kind="highlight")["insights"]) == 1
    assert len(service.list_insights(kind="importance")["insights"]) == 1
    with pytest.raises(ValueError):
        service.list_topics(since="2026-10-01T00:00:00")
    with pytest.raises(ValueError):
        service.list_insights(kind="not-a-kind")
    with pytest.raises(ValueError, match="cursor"):
        service.list_insights(cursor=first["next_cursor"], kind="importance")


def test_detector_and_punctuation_refinement_preserve_seen_state(tmp_path):
    service, _store, _policy, coordinator = pipeline(tmp_path, model=ReadingModel(), batch_size=1)
    imports(service, 1, text="Please confirm deployment.")
    local = service.list_insights(kind="importance")["insights"][0]
    service.set_attention(local["insight_id"], 0, viewed_revision=local["revision"])
    drain(coordinator.worker)
    enriched = service.get_insight(local["insight_id"])
    assert enriched["revision"] == local["revision"]
    assert enriched["attention"]["state"] == "seen"
    assert set(enriched["detectors"]) == {"local_rule", "model"}
    assert set(enriched["detector_explanations"]) == {"local_rule", "model"}


def test_alias_and_same_conversation_reply_signals(tmp_path):
    service, _store, policy, _coordinator = pipeline(tmp_path, batch_size=1)
    service.set_reading_profile({"self_ids": {"mock": ["self"]}, "aliases": ["Alice"]}, 0)
    base = {
        **IDENTITY,
        "capture_epoch": policy["capture_epoch"],
        "received_at": _TIMESTAMP,
        "adapter_id": "fake",
        "adapter_version": "2",
        "metadata_capabilities": {"reply": "supported"},
    }
    service.import_messages(
        [
            {**base, "message_id": "own", "sender_id": "self", "text": "hello"},
            {
                **base,
                "message_id": "reply",
                "sender_id": "other",
                "reply_to_message_id": "own",
                "text": "yes",
            },
            {**base, "message_id": "alias", "sender_id": "other", "text": "@Alice, hello"},
        ],
        schema_version=2,
    )
    findings = service.list_insights()["insights"]
    assert any(
        "reply_to_self" in i["reason_codes"] and i["directed_to"] == "self" for i in findings
    )
    assert any(
        "alias_mention" in i["reason_codes"] and i["certainty"] == "needs_review" for i in findings
    )


def test_recovery_scans_all_pending_conversations_with_bounded_sweep(tmp_path, monkeypatch):
    store = BackgroundJobStore(tmp_path / "scopes.sqlite")
    service = MessageHistoryService(store.db_path, store)
    service.ensure_schema()
    original = service.scan_local_signals
    monkeypatch.setattr(service, "scan_local_signals", lambda *a, **k: None)
    for index in range(34):
        identity = {**IDENTITY, "conversation_id": f"group-{index}"}
        service.set_policy({**identity, "record_enabled": True})
        service.import_messages(
            [
                {
                    **identity,
                    "message_id": f"m-{index}",
                    "text": "please confirm",
                    "received_at": _TIMESTAMP,
                }
            ]
        )
    monkeypatch.setattr(service, "scan_local_signals", original)
    assert service.scan_local_signals()["scanned"] == 32
    assert service.scan_local_signals()["scanned"] == 2
    assert service.reading_overview()["counts"]["insights"] == 34


def test_digest_rechecks_empty_grants(tmp_path):
    service, _store, policy, coordinator = pipeline(tmp_path, model=ReadingModel(), batch_size=1)
    imports(service, 1, text="Discuss deployment")
    drain(coordinator.worker)
    service.reading_digest(policy["conversation_key"])
    for kwargs in ({"allowed_sources": []}, {"allowed_accounts": []}):
        with pytest.raises(PermissionError):
            service.reading_digest(policy["conversation_key"], **kwargs)


def test_explicit_critical_rule_and_malformed_source_cursor(tmp_path):
    from app.domains.message_reading_results import _hash

    service, _store, _policy, coordinator = pipeline(tmp_path, model=ReadingModel(), batch_size=1)
    service.set_reading_profile({"critical_keywords": ["deployment"]}, 0)
    imports(service, 1, text="Please confirm deployment")
    assert service.list_insights(kind="importance")["insights"][0]["importance"] == "critical"
    drain(coordinator.worker)
    assert service.list_insights(kind="importance")["insights"][0]["importance"] == "critical"
    topic = service.list_topics()["topics"][0]
    forged = service._next_cursor(
        _hash(["topic", topic["topic_id"], None, None]), topic["revision"], []
    )
    with pytest.raises(ValueError, match="cursor"):
        service.topic_sources(topic["topic_id"], cursor=forged)


def test_model_action_publishes_proposal_in_reading_transaction(tmp_path):
    from app.domains.matters import MatterService
    from app.domains.message_matter_proposals import MessageMatterProposalService
    from app.storage.db import connect, init_db

    service, store, policy, coordinator = pipeline(tmp_path, model=ReadingModel(), batch_size=1)
    init_db(service.db_path)
    service.matter_proposals = MessageMatterProposalService(
        service, MatterService(lambda: connect(service.db_path))
    )
    service.set_policy(
        {**IDENTITY, "proposals_enabled": True, "expected_revision": policy["revision"]}
    )
    imports(service, 1, text="Discuss deployment")
    drain(coordinator.worker)
    assert len(service.matter_proposals.list_proposals()["proposals"]) == 1
    assert service.list_topics()["topics"]
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM matters").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM message_history_batches").fetchone()[0] == 1
    assert store.list(status="succeeded")


def test_proposal_failure_rolls_back_reading_publication(tmp_path):
    class FailedProposal:
        def publish_proposal(self, *args, **kwargs):
            assert kwargs["conn"].in_transaction
            raise ValueError("synthetic_proposal_failure")

    service, _store, policy, coordinator = pipeline(tmp_path, model=ReadingModel(), batch_size=1)
    service.matter_proposals = FailedProposal()
    service.set_policy(
        {**IDENTITY, "proposals_enabled": True, "expected_revision": policy["revision"]}
    )
    imports(service, 1, text="Discuss deployment")
    coordinator.worker.run_one()
    assert not service.list_topics()["topics"]
    assert service.coverage(policy["conversation_key"])["analysis_covered_seq"] is None
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_history_batches").fetchone()[0] == 0
