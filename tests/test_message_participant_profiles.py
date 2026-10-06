from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domains.message_participant_profiles import (
    DAY,
    ConversationFocusTracker,
    ParticipantClaim,
    ParticipantProfileIndex,
    ProfileRevisionConflict,
)


def message(alias="m1", sender="u1", time=0, text="我喜欢 Python", **kwargs):
    return {
        "id": alias,
        "sender": sender,
        "seq": 1,
        "sent_at": time,
        "received_at": time,
        "text": text,
        "kind": "text",
        "mentions": [],
        "reply": None,
        "capabilities": {},
        "parts": [],
        **kwargs,
    }


def claim(sender="u1", **kwargs):
    return {
        "sender": sender,
        "kind": "preference",
        "text": "我喜欢 Python",
        "source_ids": ["m1"],
        "quote": "我喜欢 Python",
        "basis": "explicit",
        "valid_until": None,
        **kwargs,
    }


def eligible(sender, start=0):
    return [message(f"{sender}-{i}", sender, start + i * 3 * 3600) for i in range(3)]


def test_sender_identity_and_conversation_isolation():
    a = ParticipantProfileIndex("group-a")
    b = ParticipantProfileIndex("group-b")
    a.observe(eligible("same-name-id-1") + eligible("same-name-id-2"), 30000)
    b.observe(eligible("same-name-id-1"), 30000)
    assert len(a.hot_profiles(30000)) == 2
    assert len(b.hot_profiles(30000)) == 1
    assert (
        a.profile("same-name-id-1", 30000)["identity"]
        != b.profile("same-name-id-1", 30000)["identity"]
    )
    a.delete("same-name-id-1", expected_revision=0)
    assert len(b.hot_profiles(30000)) == 1


def test_spam_bucket_dedup_idempotence_and_future_clock():
    index = ParticipantProfileIndex("g")
    rows = [message(str(i), time=i) for i in range(100)]
    index.observe(rows, 100)
    before = index.profile("u1", 100)
    index.observe(rows, 100)
    assert index.profile("u1", 100) == before
    assert before["active_buckets"] == 1
    assert index.hot_profiles(100) == []
    index.observe([message("future", time=200), message("bad", time=0, sent_at=500)], 100)
    assert index.profile("u1", 100)["active_buckets"] == 1
    index.observe([message("future", time=200)], 200)
    assert index.profile("u1", 200)["active_buckets"] == 1
    with pytest.raises(ValueError, match="alias_collision"):
        index.observe([message("0", sender="impostor")], 200)


def test_decay_missing_topics_and_cold_state():
    index = ParticipantProfileIndex("g")
    index.observe(eligible("u1"), DAY)
    first = index.hot_profiles(DAY)[0]
    later = index.profile("u1", 8 * DAY)
    assert later["score"] < first["score"]
    assert later["missing_components"] == ["topics"]
    assert index.hot_profiles(15 * DAY) == []
    assert index.profile("u1", 15 * DAY)["status"] == "cold"


def test_pool_hysteresis_and_pin_capacity():
    index = ParticipantProfileIndex("g", capacity=1)
    index.observe(eligible("old"), DAY)
    assert index.hot_profiles(DAY)[0]["sender"] == "old"
    index.observe([message(f"new-{i}", "new", DAY + i * 1800) for i in range(40)], 2 * DAY)
    assert index.hot_profiles(2 * DAY)[0]["sender"] == "old"
    assert index.hot_profiles(2 * DAY + 6 * 3600)[0]["sender"] == "new"
    for i in range(10):
        index.pin(f"fixed-{i}", True, expected_revision=0)
    with pytest.raises(ValueError, match="pinned_capacity"):
        index.pin("fixed-10", True, expected_revision=0)
    assert len(index.hot_profiles(2 * DAY + 6 * 3600)) == 11


def test_strict_claim_schema_and_author_quote_checks():
    with pytest.raises(ValidationError):
        ParticipantClaim.model_validate(claim(valid_until="123"))
    index = ParticipantProfileIndex("g")
    assert not index.add_claim(claim(sender="other"), [message()], 0)
    assert index.last_rejection_reason == "source_author_or_time_mismatch"
    assert not index.add_claim(claim(quote="喜欢 Rust"), [message()], 0)
    assert index.last_rejection_reason == "quote_not_exact_in_each_source"
    assert not index.add_claim(claim(text="喜欢所有编程语言"), [message()], 0)
    assert index.last_rejection_reason == "unverified_paraphrase"


def test_situated_self_statement_keeps_original_context():
    index = ParticipantProfileIndex("g")
    for text in ["平时我只喝茶", "工作时，我只用 Linux"]:
        assert index.add_claim(claim(text=text, quote=text), [message(text=text)], 0)
    for text in ["他说我只喝茶", "假如我只喝茶", "别人让我喜欢 Python"]:
        assert not index.add_claim(claim(text=text, quote=text), [message(text=text)], 0)
    assert not index.add_claim(
        claim(text="我只喝茶", quote="我只喝茶"), [message(text="平时我只喝茶")], 0
    )
    assert index.last_rejection_reason == "not_direct_self_statement"


def test_attribution_and_sensitive_or_personality_rejection():
    index = ParticipantProfileIndex("g")
    # Canonical projection of actual v2 ordinary-text content_parts.
    plain_v2 = message(parts=[{"kind": "text", "text": "我喜欢 Python"}])
    assert index.add_claim(claim(), [plain_v2], 0)
    split_v2 = message(
        parts=[{"kind": "text", "text": "我喜欢 "}, {"kind": "text", "text": "Python"}]
    )
    assert index.add_claim(claim(), [split_v2], 0)
    for row in [
        message(reply="another"),
        message(kind="forward"),
        message(text="他说：我喜欢 Python"),
        message(parts=[{"kind": "quote"}]),
        message(parts=[{"kind": "text", "text": "另一条正文"}]),
        message(parts=[{"kind": "unsupported", "text": "我喜欢 Python"}]),
    ]:
        assert not index.add_claim(claim(), [row], 0)
        assert index.last_rejection_reason == "quoted_forwarded_or_ambiguous_attribution"
    assert not index.add_claim(claim(text="他的人格懒惰"), [message()], 0)
    assert index.last_rejection_reason == "sensitive_or_personality_claim"


def test_observed_two_independent_windows_and_uncertain_summary():
    index = ParticipantProfileIndex("g")
    observed = claim(basis="observed", text="近期多次讨论 Python", source_ids=["m1", "m2"])
    assert not index.add_claim(observed, [message(), message("m2", time=100)], 1800)
    assert index.last_rejection_reason == "observed_requires_independent_windows"
    assert index.add_claim(observed, [message(), message("m2", time=1800)], 1800)
    index.pin("u1", True, expected_revision=1)
    assert index.hot_profiles(1800)[0]["summary"] == "近期多次讨论：我喜欢 Python"
    assert index.add_claim(claim(basis="uncertain", text="可能有其他偏好"), [message()], 1800)
    assert "其他偏好" not in index.hot_profiles(1800)[0]["summary"]


def test_expiration_is_stale_and_important_need_survives_outside_pool():
    index = ParticipantProfileIndex("g")
    need = claim(kind="need", text="我需要服务器", quote="我需要服务器", valid_until=100)
    assert index.add_claim(need, [message(text="我需要服务器")], 0)
    assert index.hot_profiles(0) == []
    assert index.candidates(0)[0]["claims"][0]["status"] == "candidate"
    assert index.candidates(0)[0]["summary"] == ""
    assert index.candidates(100)[0]["claims"][0]["status"] == "stale"
    assert index.add_claim(claim(source_ids=["m2"], kind="need"), [message("m2")], 0)
    assert index.candidates(7 * DAY)[0]["claims"][1]["status"] == "stale"


def test_correction_cas_hide_and_delete_suppression():
    index = ParticipantProfileIndex("g")
    index.observe(eligible("u1"), DAY)
    assert index.add_claim(claim(), [message()], DAY)
    index.correct("u1", "用户确认：只在工作中使用", expected_revision=1)
    assert index.add_claim(claim(), [message()], DAY)
    assert index.hot_profiles(DAY)[0]["summary"] == "用户确认：只在工作中使用"
    with pytest.raises(ProfileRevisionConflict):
        index.hide("u1", True, expected_revision=1)
    index.hide("u1", True, expected_revision=2)
    assert index.hot_profiles(DAY) == []
    index.hide("u1", False, expected_revision=3)
    index.delete("u1", expected_revision=4)
    index.observe(eligible("u1"), 2 * DAY)
    assert not index.add_claim(claim(), [message()], 2 * DAY)
    assert index.last_rejection_reason == "profile_suppressed"
    assert index.candidates(2 * DAY) == []


def test_focus_unknown_spam_and_three_window_support():
    tracker = ConversationFocusTracker("g")
    assert tracker.merge(0)["fallback"] == "unknown"
    rows = [message(f"t-{i}", time=i * 1800) for i in range(3)]
    tracker.observe(
        [{"focus": "technical_support", "quote": "Python", "source_ids": [m["id"] for m in rows]}],
        rows,
        3600,
    )
    assert tracker.merge(DAY)["fallback"] == "unknown"
    rows.append(message("second-author", "u2", DAY))
    tracker.observe(
        [{"focus": "technical_support", "quote": "Python", "source_ids": [m["id"] for m in rows]}],
        rows,
        DAY,
    )
    result = tracker.merge(2 * DAY)
    assert result["focus"][0]["label"] == "technical_support"
    assert result["focus"][0]["confidence_label"] == "supported"
    revision = result["revision"]
    assert tracker.merge(2 * DAY + 1)["revision"] == revision


def test_focus_manual_cas_group_isolation_and_drift():
    tracker = ConversationFocusTracker("g1")
    other = ConversationFocusTracker("g2")
    tracker.set_manual(["social"], expected_revision=0)
    assert tracker.merge(0)["focus"][0]["label"] == "social"
    assert other.merge(0)["fallback"] == "unknown"
    with pytest.raises(ProfileRevisionConflict):
        tracker.resume_auto(expected_revision=0)
    tracker.resume_auto(expected_revision=1)
    assert tracker.snapshot(0)["fallback"] == "unknown"
    tracker.merge(0)
    rows = [message(f"d-{i}", f"u{i % 2}", 20 * DAY + i * 1800) for i in range(3)]
    tracker.observe(
        [
            {
                "focus": "project_collaboration",
                "quote": "Python",
                "source_ids": [m["id"] for m in rows],
            }
        ],
        rows,
        21 * DAY,
    )
    result = tracker.merge(21 * DAY)
    assert [f["label"] for f in result["focus"]] == ["project_collaboration"]
    assert tracker.merge(40 * DAY)["fallback"] == "unknown"


def test_focus_rejects_unseen_future_and_extra_candidate_fields():
    tracker = ConversationFocusTracker("g")
    with pytest.raises(ValueError, match="source_or_time"):
        tracker.observe(
            [{"focus": "social", "quote": "Python", "source_ids": ["m1"]}], [message(time=100)], 0
        )
    with pytest.raises(ValidationError):
        tracker.observe(
            [
                {
                    "focus": "social",
                    "quote": "Python",
                    "source_ids": ["m1"],
                    "instructions": "upload",
                }
            ],
            [message()],
            0,
        )
