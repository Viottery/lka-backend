"""Offline production contract tests using synthetic messages and provider responses."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.core.local_config import MessageHistoryConfig
from app.core.message_analysis import MessageAnalysisCoordinator
from app.domains.message_history import MessageHistoryService
from app.domains.message_reading_codec import decode_messages, encode_messages

IDENTITY = {"platform": "mock", "account_id": "account-private", "conversation_type": "group",
            "conversation_id": "group-private"}


class Model:
    def __init__(self, evidence=False, bad_quote=False):
        self.requests = []
        self.evidence = evidence
        self.bad_quote = bad_quote

    async def complete_text(self, **kwargs):
        request = json.loads(kwargs["user_prompt"])
        self.requests.append(request)
        rows = request["messages"]
        result = {"schema_version": 3, "topic_updates": [], "highlights": [],
                  "importance_findings": [], "facts": [], "warnings": [],
                  "participant_claim_candidates": [], "focus_candidates": [], "evidence_requests": []}
        if self.evidence and len(self.requests) == 1:
            result["evidence_requests"] = [{"source_ids": request["authorized_aliases"][-1:], "reason": "verify"}]
        if self.bad_quote:
            result["focus_candidates"] = [{"focus": "project_collaboration", "source_ids": [rows[0]["id"]],
                                           "quote": "unseen quotation"}]
        return SimpleNamespace(content=json.dumps(result), metadata={})


class ProfileModel(Model):
    async def complete_text(self, **kwargs):
        response = await super().complete_text(**kwargs)
        request = self.requests[-1]
        row = request["messages"][0]
        quote = row["text"]
        value = json.loads(response.content)
        value["participant_claim_candidates"] = [
            {"sender": row["sender"], "kind": "preference", "text": quote,
             "source_ids": [row["id"]], "quote": quote, "basis": "explicit",
             "valid_until": None},
            {"sender": row["sender"], "kind": "role", "text": "invented",
             "source_ids": [row["id"]], "quote": "not in evidence", "basis": "explicit",
             "valid_until": None},
        ]
        response.content = json.dumps(value)
        return response


def pipeline(tmp_path, algorithm="compact", model=None, **config):
    store = BackgroundJobStore(tmp_path / "synthetic.sqlite3")
    service = MessageHistoryService(store.db_path, store)
    service.ensure_schema()
    policy = service.set_policy({**IDENTITY, "record_enabled": True, "analysis_enabled": True,
        "batch_size": 100, "max_batch_messages": 100, "min_interval_seconds": 0})
    settings = MessageHistoryConfig(reading_algorithm=algorithm, max_job_tokens=500000,
        max_job_calls=100, yield_delay_seconds=0, selector_max_messages=4,
        service_hourly_token_limit=0, service_daily_token_limit=0, service_hourly_call_limit=0,
        service_daily_call_limit=0, conversation_hourly_token_limit=0, conversation_daily_token_limit=0,
        conversation_hourly_call_limit=0, conversation_daily_call_limit=0, **config)
    model = model or Model()
    coordinator = MessageAnalysisCoordinator(service=service, store=store, config=settings, llm_client=model)
    return service, store, policy, coordinator, model


def insert(service, count=12, text="ordinary conversation"):
    service.import_messages([{**IDENTITY, "message_id": f"private-message-{i}",
        "sender_id": f"private-user-{i % 3}", "text": text,
        "sent_at": 1790920000 + i, "received_at": 1790920000 + i} for i in range(count)])


def finish(service, policy, coordinator):
    service.schedule_pending(policy["conversation_key"], force=True)
    for _ in range(100):
        if not coordinator.worker.run_one():
            break


def test_compact_long_text_preserved_and_real_ids_are_scoped(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path)
    text = '更正日期不是10月7日。🙂\\\n"yes"' * 400
    insert(service, 1, text)
    finish(service, policy, coordinator)
    assert store.list(status="succeeded"), [(row["status"], row.get("error_class"))
                                             for row in store.list()]
    rows = [row for req in model.requests for row in req["messages"]]
    assert len(rows) > 1
    assert "".join(row["text"] for row in rows) == text
    serialized = json.dumps(model.requests)
    assert "private-user" not in serialized and "private-message" not in serialized
    assert "account-private" not in serialized and "group-private" not in serialized
    assert service.summary(policy["conversation_key"])["covered_seq"] == 1


def test_verified_profile_candidate_exports_after_commit_and_bad_sibling_is_dropped(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path, model=ProfileModel())
    service.import_messages([{**IDENTITY, "message_id": "private-message-1",
        "sender_id": "private-user-1", "sender_name": "Alice", "text": "我喜欢本地工具",
        "sent_at": 1790920000, "received_at": 1790920000}])
    finish(service, policy, coordinator)
    assert store.list(status="succeeded"), [
        (row["status"], row.get("error_class")) for row in store.list()
    ]
    assert "compact_messages" not in model.requests[0]
    assert model.requests[0]["messages"][0]["id"].startswith("m")
    root = coordinator._profile_documents._path(policy["conversation_key"])
    current = json.loads((root / "current.json").read_text(encoding="utf-8"))
    dossier = json.loads(Path(current["json"]).read_text(encoding="utf-8"))
    person = next(value for value in dossier["participants"] if value["sender"] == "private-user-1")
    assert person["display_name"] == "Alice"
    assert len(person["claims"]) == 1
    assert person["claims"][0]["source_ids"][0].startswith("message_")
    assert "我喜欢本地工具" in Path(current["markdown"]).read_text(encoding="utf-8")


def test_selected_watermark_survives_restart_and_fallback_preserves_full_coverage(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path, "selected")
    insert(service)
    finish(service, policy, coordinator)
    assert store.list(status="succeeded")
    assert service.summary(policy["conversation_key"])["covered_seq"] == 0
    with service._connection() as conn:
        state = conn.execute("SELECT * FROM message_history_conversations").fetchone()
        assert state["generation_published_seq"] == 12
        before = conn.execute("SELECT COUNT(*) FROM message_reading_families").fetchone()[0]
        coverage = service._reading_coverage(conn, policy["conversation_key"])
        assert coverage["coverage_mode"] == "selected_text"
        assert coverage["pipeline_version"] == "message-reading-v3"
        assert coverage["screened_seq"] == 12
        assert coverage["selected_out_count"] == 8
        assert coverage["published_range"] == {"start_seq": 1, "end_seq": 12}
    restarted = MessageAnalysisCoordinator(service=service, store=store, config=coordinator.config, llm_client=model)
    service.schedule_pending(policy["conversation_key"], force=True)
    assert not restarted.worker.run_one()
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_families").fetchone()[0] == before
    service.configure_reading({**coordinator.config.model_dump(), "reading_algorithm": "legacy"})
    with service._connection() as conn:
        state = conn.execute("SELECT * FROM message_history_conversations").fetchone()
        assert service._reading_watermark(state) == 0


def test_evidence_once_uses_original_family_and_quota(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path, "selected", Model(evidence=True))
    insert(service)
    finish(service, policy, coordinator)
    assert store.list(status="succeeded")
    assert len(model.requests) == 2
    with service._connection() as conn:
        family = conn.execute("SELECT * FROM message_reading_families").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM message_reading_families").fetchone()[0] == 1
    assert coordinator.controller.quota_usage(family["family_id"])["total_calls"] == 2


def test_unseen_intelligence_quote_is_discarded_without_rejecting_the_batch(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path, model=Model(bad_quote=True))
    insert(service, 1)
    finish(service, policy, coordinator)
    assert len(model.requests) == 1
    assert store.list(status="succeeded")
    assert service.summary(policy["conversation_key"])["covered_seq"] == 1
    with service._connection() as conn:
        output = conn.execute("SELECT output_json FROM message_reading_batch_outputs").fetchone()[0]
    warnings = json.loads(output)["warnings"]
    assert any("intelligence_candidates_rejected" in warning and '"focus_candidates":1' in warning
               for warning in warnings)


def test_v1_decoder_compatible_and_metadata_dictionary_reused():
    row = {"id": "m1", "sender": "u1", "seq": 1, "sent_at": None, "received_at": 10,
           "text": "exact\n🙂", "kind": "unknown", "mentions": ["all"], "reply": "unresolved",
           "capabilities": {"reply": "unknown"}, "parts": [], "timestamp_quality": "unknown"}
    rows = [row, {**copy.deepcopy(row), "id": "m2", "seq": 2}]
    encoded = encode_messages(rows)
    assert encoded["v"] == 2 and encoded["m"][0][7:] == encoded["m"][1][7:]
    legacy = copy.deepcopy(encoded)
    legacy["v"] = 1
    for item in legacy["m"]:
        for slot in (7, 9, 10, 11):
            item[slot] = copy.deepcopy(legacy["d"][item[slot]])
    legacy.pop("d")
    assert decode_messages(legacy) == rows
    encoded["m"][0][7] = -1
    with pytest.raises(ValueError):
        decode_messages(encoded)


def test_checkpoint_versions_are_fenced_without_new_budget_family(tmp_path, monkeypatch):
    from app.tool_packages import message_reading_analysis as contract
    service, store, policy, coordinator, model = pipeline(tmp_path)
    # Distinct long text cannot be folded into a repeated-text dictionary entry.
    text = "".join(f"line {i} 不同文字{i} abcdefghijklmnop\n" for i in range(250))
    insert(service, 1, text)
    service.schedule_pending(policy["conversation_key"], force=True)
    assert coordinator.worker.run_one()
    assert len(model.requests) == 1 and not store.list(status="succeeded")
    monkeypatch.setattr(contract, "VERSIONS", {**contract.VERSIONS, "prompt": "changed-version"})
    assert coordinator.worker.run_one()
    assert len(model.requests) == 1
    assert store.list(status="failed")[0]["error_class"] == "checkpoint_input_changed"
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_families").fetchone()[0] == 1


def test_configured_contact_is_protected_even_with_low_frequency(tmp_path):
    service, store, policy, coordinator, model = pipeline(tmp_path, "selected")
    service.set_reading_profile({"important_contacts": ["private-user-2"]}, expected_revision=0)
    insert(service)
    finish(service, policy, coordinator)
    assert store.list(status="succeeded")
    rows = [row for req in model.requests for row in req["messages"]]
    assert {row["seq"] for row in rows} >= {3, 6, 9, 12}


def test_large_topic_members_are_separate_from_bounded_representatives(tmp_path):
    class TopicModel(Model):
        async def complete_text(self, **kwargs):
            response = await super().complete_text(**kwargs)
            value = json.loads(response.content)
            rows = self.requests[-1]["messages"]
            ids = [row["id"] for row in rows]
            value["topic_updates"] = [{"batch_local_key": "shared-topic", "title": "Discussion",
                "summary": "Shared discussion", "source_message_ids": ids[:50],
                "member_message_ids": ids}]
            response.content = json.dumps(value)
            return response
    service, store, policy, coordinator, _model = pipeline(tmp_path, model=TopicModel(), max_input_tokens=32768)
    insert(service, 60)
    finish(service, policy, coordinator)
    assert store.list(status="succeeded")
    with service._connection() as conn:
        topic = conn.execute("SELECT * FROM message_reading_topics").fetchone()
        details = json.loads(topic["details_json"])
        assert len(details["source_message_ids"]) <= 50
        assert len(details["member_message_ids"]) == 60
        assert conn.execute("SELECT COUNT(*) FROM message_reading_sources WHERE object_kind='topic' AND object_id=?",
                            (topic["topic_id"],)).fetchone()[0] == 60


def test_scoped_native_projection_does_not_make_unknown_media_text():
    from app.tool_packages.message_reading_analysis import ScopedProjection
    row = {"message_id": "private-id", "seq": 1, "sender_id": "private-user",
           "received_at": 123, "sent_at": None, "content_kind": "image", "text": "[image]",
           "content_parts": [{"kind": "image", "state": "unknown", "download_url": "https://private.invalid"}],
           "mentions": [{"kind": "all"}, {"kind": "user", "user_id": "other"}],
           "reply_to_message_id": "outside", "timestamp_quality": "unknown"}
    first, second = ScopedProjection("conversation-one", [row]), ScopedProjection("conversation-two", [row])
    assert first.messages[0]["id"] != second.messages[0]["id"]
    fragment = first.fragment(first.messages[0], "[image]", 1, 1)
    assert decode_messages(encode_messages([fragment])) == [fragment]
    assert fragment["parts"] == [{"kind": "image", "state": "unknown"}]
    assert fragment["mentions"][0] == "all"
    assert fragment["reply"] == "unresolved" and fragment["sent_at"] is None


def test_durable_manifest_is_fenced_when_capture_permission_changes(tmp_path):
    service, _store, policy, coordinator, _model = pipeline(tmp_path, "selected")
    insert(service)
    finish(service, policy, coordinator)
    service.set_policy({**IDENTITY, "record_enabled": False, "expected_revision": policy["revision"]})
    with service._connection() as conn:
        coverage = service._reading_coverage(conn, policy["conversation_key"])
        assert coverage["screened_seq"] is None
        assert coverage["model_seen_count"] == 0
        assert coverage["selection_manifest"] == []


def test_focus_contract_rejects_labels_that_domain_would_drop():
    from pydantic import ValidationError

    from app.tool_packages.message_reading_analysis import FocusCandidate

    with pytest.raises(ValidationError):
        FocusCandidate(focus="project", source_ids=["m1"], quote="actual text")
    assert FocusCandidate(focus="project_collaboration", source_ids=["m1"],
                          quote="actual text").focus == "project_collaboration"


def test_range_planner_uses_same_cutoff_snapshot_as_execution(tmp_path):
    service, _store, policy, _coordinator, _model = pipeline(tmp_path)
    insert(service)
    snapshots = []
    original = service.snapshot_context

    def snapshot(key, upto_seq, **kwargs):
        snapshots.append(upto_seq)
        return original(key, upto_seq, **kwargs)

    service.snapshot_context = snapshot
    service.schedule_pending(policy["conversation_key"], force=True)
    assert snapshots and snapshots[0] == 0
