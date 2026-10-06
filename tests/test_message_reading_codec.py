"""Deterministic synthetic tests; no production database or model calls."""

import copy
import json

import pytest

from app.domains.message_reading_codec import decode_messages, encode_messages, select_messages


def message(index, text="ordinary conversation", **overrides):
    return {
        "id": f"m{index}",
        "sender": f"u{index % 3}",
        "seq": index,
        "sent_at": 1_000 + index,
        "received_at": 1_010 + index,
        "text": text,
        "kind": "text",
        "mentions": [],
        "reply": None,
        "capabilities": {"text": True, "media": "unknown"},
        "parts": [{"kind": "text", "text": text}],
        **overrides,
    }


def test_codec_roundtrip_long_unicode_unknown_and_metadata():
    text = "\n更正：不是10月6日，而是10月7日。😊\t" * 10_000
    messages = [
        message(
            1,
            text,
            sender="alice",
            sent_at=None,
            received_at=-100,
            mentions=["all", "bob"],
            reply="outside",
            parts=[
                {"kind": "text", "text": text},
                {
                    "kind": "image",
                    "text": None,
                    "state": "unknown",
                    "details": {"size": None, "numbers": [1, 2]},
                },
            ],
        ),
        message(
            2,
            text,
            sender="bob",
            sent_at=10**20,
            received_at=0,
            timestamp_quality="unknown",
            thread=None,
        ),
        message(3, "", kind="unknown", parts=[], capabilities={}),
    ]
    original = copy.deepcopy(messages)
    encoded = encode_messages(messages)
    restored = decode_messages(json.loads(json.dumps(encoded)))
    assert restored == original == messages
    assert encoded["t"] == [text, ""]
    assert json.dumps(encoded, ensure_ascii=False).count(json.dumps(text, ensure_ascii=False)) == 1
    restored[0]["parts"][1]["details"]["numbers"].append(3)
    assert messages == original


def test_codec_empty_and_dictionary_reuse():
    assert decode_messages(encode_messages([])) == []
    rows = [
        message(1, "same", thread="m0", timestamp_quality="adapter"),
        message(2, "same", mentions=["all"], reply="m1"),
    ]
    encoded = encode_messages(rows)
    assert encoded["t"] == ["same"]
    assert "same" not in json.dumps(encoded["m"])
    assert decode_messages(encoded) == rows


def test_self_statement_and_short_followup_preserve_bounded_context():
    rows = [message(i, f"闲聊{i}") for i in range(30)]
    rows[7] = message(7, "我只用 Linux")
    rows[14] = message(14, "新电脑可以考虑这款")
    rows[20] = message(20, "怎么不给我提建议")
    result = select_messages(rows, max_messages=4, context_radius=0)
    retained = {row["id"] for row in result["messages"]}
    assert {"m7", "m14", "m20", "m22"} <= retained
    assert "m23" not in retained
    assert result["coverage_mode"] == "selected_text"
    assert len(result["messages"]) > 4


def test_codec_rejects_unsupported_projection_and_corrupt_references():
    for invalid in [
        message(1, extra=True),
        message(1, sent_at=True),
        message(1, capabilities={"size": float("nan")}),
    ]:
        with pytest.raises(ValueError):
            encode_messages([invalid])
    with pytest.raises(ValueError):
        encode_messages([message(1), message(1)])
    encoded = encode_messages([message(1)])
    encoded["m"][0][5] = -1
    with pytest.raises(ValueError):
        decode_messages(encoded)


def test_small_selection_keeps_original_rows_and_full_coverage():
    messages = [message(index) for index in range(3)]
    result = select_messages(messages)
    assert result["messages"] == messages
    assert result["messages"][0] is messages[0]
    assert result["coverage_mode"] == "full_text"
    assert result["deferred_ids"] == []
    assert all(item["decision"] == "retained" for item in result["decisions"])


def test_all_signals_and_protected_overflow_survive():
    messages = [
        message(0, mentions=["all"]),
        message(1, reply="m0"),
        message(2, "截止2026-10-07"),
        message(3, "请提交方案"),
        message(4, "更正日期"),
        message(5, "不行"),
        message(6, "[link]"),
        message(7, kind="image", parts=[{"kind": "image", "state": "unknown"}]),
        message(8, "ordinary filler"),
    ]
    result = select_messages(messages, max_messages=2, context_radius=0)
    assert result["protected_ids"] == [f"m{index}" for index in range(8)]
    assert len(result["messages"]) == 8
    assert result["coverage_mode"] == "selected_text"
    assert result["decisions"][-1]["decision"] == "sampled_out"
    assert all(
        "protected_overflow_requires_fragmentation" in row["reasons"]
        for row in result["decisions"][:-1]
    )


def test_reply_ancestors_and_bounded_neighbor_context():
    messages = [message(index) for index in range(8)]
    messages[2]["reply"] = "m0"
    messages[5]["reply"] = "m2"
    messages[7]["reply"] = "missing"
    messages[7]["sent_at"] = 100_000
    result = select_messages(messages, max_messages=1)
    assert set(result["protected_ids"]) == {"m0", "m1", "m2", "m3", "m4", "m5", "m6", "m7"}
    assert "unresolved_reply" in result["decisions"][7]["reasons"]
    assert "reply_context" in result["decisions"][0]["reasons"]
    isolated = [message(1), message(2, "please", sent_at=100_000), message(3)]
    result = select_messages(isolated, max_messages=1)
    assert result["protected_ids"] == ["m2"]
    unresolved = [message(0, id="unresolved"), message(1, reply="unresolved")]
    result = select_messages(unresolved, max_messages=1, context_radius=0)
    assert result["protected_ids"] == ["m1"]
    threaded = [message(0), message(1, thread="m0")]
    result = select_messages(threaded, max_messages=1, context_radius=0)
    assert result["protected_ids"] == ["m0", "m1"]
    assert "thread_context" in result["decisions"][0]["reasons"]


def test_deterministic_sampling_exploration_and_low_frequency_sender():
    messages = [message(index, sender="frequent") for index in range(80)]
    messages[40]["sender"] = "rare"
    first = select_messages(messages, max_messages=8, seed="fixed")
    assert first == select_messages(messages, max_messages=8, seed="fixed")
    assert len(first["messages"]) == 8
    assert "rare" in {row["sender"] for row in first["messages"]}
    assert any("seeded_exploration" in row["reasons"] for row in first["decisions"])
    assert first["messages"] != select_messages(messages, max_messages=8, seed="other")["messages"]
    assert [row["seq"] for row in first["messages"]] == sorted(
        row["seq"] for row in first["messages"]
    )


def test_new_link_protects_first_occurrence_without_folding_new_authors():
    messages = [
        message(index, "https://example.test/resource", sender=f"u{index}") for index in range(10)
    ]
    result = select_messages(messages, max_messages=3, context_radius=0)
    assert result["protected_ids"] == ["m0"]
    assert len(result["decisions"]) == 10
    assert decode_messages(encode_messages(messages)) == messages


def test_sampling_threshold_and_invalid_configuration():
    messages = [message(index) for index in range(10)]
    assert select_messages(messages, max_messages=2, sampling_threshold=10)["messages"] == messages
    assert len(select_messages(messages, max_messages=2, sampling_threshold=0)["messages"]) == 2
    for kwargs in [
        {"max_messages": 0},
        {"sampling_threshold": -1},
        {"exploration_fraction": 1.1},
        {"context_radius": -1},
    ]:
        with pytest.raises(ValueError):
            select_messages(messages, **kwargs)


def test_known_topic_hint_is_not_membership_or_permission_and_keeps_signals():
    rows = [message(i, "ordinary filler") for i in range(30)]
    rows[15] = message(15, "Linux graphics stack")
    rows[25] = message(25, "截止10月7日")
    result = select_messages(rows, max_messages=4, context_radius=0, known_topics=("Linux",))
    selected = {row["id"] for row in result["messages"]}
    assert {"m15", "m25"} <= selected
    assert "known_topic_lexical_candidate" in result["decisions"][15]["reasons"]
    assert "m15" not in result["protected_ids"]
    assert all("topic_id" not in row for row in result["decisions"])
