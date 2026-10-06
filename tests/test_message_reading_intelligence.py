from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.domains.message_history import AnalysisResult, MessageHistoryService
from app.domains.message_participant_profiles import (
    DAY,
    ParticipantProfileIndex,
    ProfileRevisionConflict,
)


def setup_service(tmp_path):
    path = tmp_path / "intelligence.sqlite3"
    service = MessageHistoryService(path, BackgroundJobStore(path))
    service.ensure_schema()
    clock = [DAY]
    service._reading_now = lambda: datetime.fromtimestamp(clock[0], UTC).isoformat()
    policy = service.set_policy({"platform": "mock", "account_id": "a", "conversation_type": "group",
        "conversation_id": "g", "record_enabled": True})
    return service, policy, clock


def publish(service, policy, clock, *, sender="alice", text="我喜欢 Python", intelligence=None):
    messages = [{"platform": "mock", "account_id": "a", "conversation_type": "group", "conversation_id": "g",
        "message_id": f"{sender}-{clock[0]}-{i}", "sender_id": sender, "sender_name": "Same display name",
        "text": text, "sent_at": clock[0] - 7 * 3600 + i * 3 * 3600, "received_at": clock[0]} for i in range(3)]
    service.import_messages(messages)
    with service._connection() as conn:
        rows = conn.execute("SELECT * FROM message_history_messages WHERE conversation_key=? ORDER BY seq DESC LIMIT 3", (policy["conversation_key"],)).fetchall()
        ids = [row["internal_message_id"] for row in rows]
        value = intelligence(ids) if intelligence else {"participant_claim_candidates": [{"sender": sender,
            "kind": "preference", "text": text, "quote": text, "source_ids": ids[:1], "basis": "explicit", "valid_until": None}]}
        conn.execute("BEGIN IMMEDIATE")
        service._publish_reading_intelligence(conn, {"payload": {"conversation_id": policy["conversation_key"]}}, rows,
            AnalysisResult(batch_summary="batch", summary="summary", reading_intelligence=value))
    return ids


def test_persistent_restart_scopes_and_bounded_sources(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    ids = publish(service, policy, clock)
    key = policy["conversation_key"]
    original = service.get_participant(key, "alice")
    restarted = MessageHistoryService(service.db_path, service.jobs)
    restarted._reading_now = service._reading_now
    assert restarted.get_participant(key, "alice") == original
    assert original["status"] == "hot" and original["activity_observed_seq"] == 3
    assert restarted.participant_sources(key, "alice")["sources"][0]["message_id"] == ids[0]
    assert service.list_participants(allowed_sources=[])["participants"] == []
    assert service.list_participants(allowed_accounts=["different-account"])["participants"] == []
    for scope in ({"allowed_sources": []}, {"allowed_accounts": ["different-account"]}):
        with pytest.raises(PermissionError, match="message_history_not_allowed"):
            service.get_participant(key, "alice", **scope)
    with service._connection() as conn:
        state = conn.execute("SELECT state_json FROM message_reading_intelligence").fetchone()[0]
    assert "我喜欢 Python" not in state.split('"seen":')[1]  # Dedup stores hashes, never source bodies.


def test_concurrent_cas_correction_and_delete_suppression(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    publish(service, policy, clock)
    key = policy["conversation_key"]
    revision = service.get_participant(key, "alice")["revision"]
    def correct(summary):
        try:
            service.update_participant(key, "alice", revision, "correct", summary)
            return "success"
        except ProfileRevisionConflict:
            return "conflict"
    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(correct, ["first", "second"])) == ["conflict", "success"]
    person = service.get_participant(key, "alice")
    service.update_participant(key, "alice", person["revision"], "delete")
    clock[0] += DAY
    publish(service, policy, clock)
    person = service.get_participant(key, "alice")
    assert person["status"] == "suppressed" and person["claims"] == [] and person["summary"] == ""
    assert service.list_participants()["participants"] == []
    with pytest.raises(PermissionError):
        service.participant_sources(key, "alice")


def test_revocation_capture_epoch_and_sender_attribution(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    publish(service, policy, clock, intelligence=lambda ids: {"participant_claim_candidates": [{"sender": "bob",
        "kind": "preference", "text": "我喜欢 Python", "quote": "我喜欢 Python", "source_ids": ids[:1], "basis": "explicit", "valid_until": None}]})
    key = policy["conversation_key"]
    assert service.get_participant(key, "alice")["claims"] == []
    with pytest.raises(PermissionError):
        service.get_participant(key, "bob")
    policy = service.set_policy({"platform": "mock", "account_id": "a", "conversation_type": "group",
        "conversation_id": "g", "record_enabled": False, "expected_revision": policy["revision"]})
    with pytest.raises(PermissionError):
        service.get_participant(key, "alice")
    service.set_policy({"platform": "mock", "account_id": "a", "conversation_type": "group",
        "conversation_id": "g", "record_enabled": True, "expected_revision": policy["revision"]})
    assert service.list_participants()["participants"] == []
    with pytest.raises(PermissionError):
        service.get_participant(key, "alice")


def test_claim_contested_expiry_cleanup_and_topics(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    publish(service, policy, clock)
    clock[0] += DAY
    publish(service, policy, clock, text="我喜欢 Rust")
    key = policy["conversation_key"]
    person = service.get_participant(key, "alice")
    assert {claim["status"] for claim in person["claims"]} == {"contested"}
    assert person["summary"] == ""
    clock[0] += 15 * DAY
    assert service.get_participant(key, "alice")["status"] == "cold"
    assert service.list_participants()["participants"] == []
    clock[0] += 31 * DAY
    with pytest.raises(PermissionError):
        service.get_participant(key, "alice")
    assert service.list_participants()["participants"] == []
    index = ParticipantProfileIndex("g")
    row = {"id": "m", "sender": "a", "seq": 1, "text": "topic", "kind": "text", "sent_at": 0, "received_at": 0}
    index.observe([row], 0)
    topics = [{"batch_local_key": "topic-1", "source_message_ids": ["m"]}]
    index.observe_topics(topics, [row], 0)
    index.observe_topics(topics, [row], 0)
    assert index.profile("a", 0)["topic_participation"] == 1
    assert index.profile("a", 0)["missing_components"] == []


def test_focus_manual_cas_read_is_read_only_and_source_pagination(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    publish(service, policy, clock)
    publish(service, policy, clock, sender="bob")
    key = policy["conversation_key"]
    # Pool promotion is throttled; explicitly pin the second person so this
    # pagination test has two visible participants at the same fake time.
    bob = service.get_participant(key, "bob")
    service.update_participant(key, "bob", bob["revision"], "pin")
    page = service.list_participants(limit=1)
    assert page["has_more"]
    assert service.list_participants(limit=1, cursor=page["next_cursor"])["participants"][0]["sender_id"] == "bob"
    before = service.get_conversation_focus(key)
    edited = service.set_conversation_focus(key, before["revision"], "manual", ["social"])
    assert edited["focus"][0]["label"] == "social"
    with pytest.raises(ProfileRevisionConflict):
        service.set_conversation_focus(key, before["revision"], "auto")
    with service._connection() as conn:
        state = conn.execute("SELECT state_json FROM message_reading_intelligence").fetchone()[0]
    clock[0] += DAY
    assert service.get_conversation_focus(key)["revision"] == edited["revision"]
    with service._connection() as conn:
        assert conn.execute("SELECT state_json FROM message_reading_intelligence").fetchone()[0] == state


def test_import_activity_independent_of_analysis_and_frozen_evidence(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    # Recording alone updates activity even with analysis disabled.
    service.import_messages([{"platform": "mock", "account_id": "a", "conversation_type": "group",
        "conversation_id": "g", "message_id": "only-activity", "sender_id": "a",
        "text": "hello", "sent_at": clock[0], "received_at": clock[0]}])
    key = policy["conversation_key"]
    assert service.get_participant(key, "a")["activity_observed_seq"] == 1
    publish(service, policy, clock)
    assert service.snapshot_context(key, 1)["participants"] == []
    assert service.snapshot_context(key, 4)["participants"][0]["claims"]


def test_all_pinned_senders_protected_beyond_prompt_profile_capacity(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    service.import_messages([{"platform": "mock", "account_id": "a", "conversation_type": "group",
        "conversation_id": "g", "message_id": f"activity-{i}", "sender_id": f"person-{i}",
        "text": "hello", "sent_at": clock[0], "received_at": clock[0]} for i in range(10)])
    key = policy["conversation_key"]
    for i in range(10):
        person = service.get_participant(key, f"person-{i}")
        service.update_participant(key, f"person-{i}", person["revision"], "pin")
    context = service.snapshot_context(key, 0)
    assert len(context["participants"]) == 8
    assert context["protected_senders"] == [f"person-{i}" for i in range(10)]


def test_delete_tombstone_survives_capture_epoch_change(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    publish(service, policy, clock)
    key = policy["conversation_key"]
    person = service.get_participant(key, "alice")
    service.update_participant(key, "alice", person["revision"], "delete")
    for enabled in (False, True):
        policy = service.set_policy({"platform": "mock", "account_id": "a", "conversation_type": "group",
            "conversation_id": "g", "record_enabled": enabled, "expected_revision": policy["revision"]})
    clock[0] += DAY
    publish(service, policy, clock)
    assert service.get_participant(key, "alice")["status"] == "suppressed"


def test_future_clock_and_bounded_activity_refresh(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    service.import_messages([{"platform": "mock", "account_id": "a", "conversation_type": "group",
        "conversation_id": "g", "message_id": "future", "sender_id": "future-author",
        "text": "future", "sent_at": clock[0] + DAY, "received_at": clock[0] + DAY}])
    key = policy["conversation_key"]
    assert service.list_participants()["participants"] == []
    assert service.refresh_participant_activity(key, limit=1)["deferred_messages"] == 1
    clock[0] += DAY
    assert service.refresh_participant_activity(key, limit=1)["deferred_messages"] == 0
    assert service.get_participant(key, "future-author")["active_buckets"] == 1
    assert service.refresh_participant_activity(key, limit=1)["activity_observed_seq"] == 1


def test_pinned_without_claims_and_manual_override_survive_frozen_snapshot(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    publish(service, policy, clock, intelligence=lambda ids: {})
    key = policy["conversation_key"]
    person = service.get_participant(key, "alice")
    pinned = service.update_participant(key, "alice", person["revision"], "pin")
    snapshot = service.snapshot_context(key, 0)
    assert snapshot["version"] == "participant-focus-v3"
    assert snapshot["participants"][0]["pinned"]
    assert snapshot["participants"][0]["status"] == "pinned"
    assert snapshot["participants"][0]["claims"] == []
    service.update_participant(key, "alice", pinned["revision"], "correct", "manual context")
    assert service.snapshot_context(key, 0)["participants"][0]["summary"] == "manual context"


def test_schedule_catches_activity_without_analysis_and_avoids_idle_writes(tmp_path):
    service, policy, clock = setup_service(tmp_path)
    service.import_messages([{"platform": "mock", "account_id": "a", "conversation_type": "group",
        "conversation_id": "g", "message_id": "later", "sender_id": "a", "text": "hello",
        "received_at": clock[0] + DAY}])
    clock[0] += DAY
    assert service.schedule_pending() == []
    assert service.get_participant(policy["conversation_key"], "a")["active_buckets"] == 1
    with service._connection() as conn:
        revision = conn.execute("SELECT revision FROM message_reading_intelligence").fetchone()[0]
    service.schedule_pending()
    with service._connection() as conn:
        assert conn.execute("SELECT revision FROM message_reading_intelligence").fetchone()[0] == revision
