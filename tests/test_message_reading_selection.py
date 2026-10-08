import pytest

from app.domains.message_reading_selection import select_message_candidates


def row(i, **kwargs):
    return {"id": f"m{i}", "sender": "peer", "seq": i, "received_at": i * 1000,
            "sent_at": i * 1000, "text": "not a new issue", "kind": "text", "mentions": [],
            "reply": None, "capabilities": {"reply": "unknown"}, "parts": [], **kwargs}


def test_soft_signals_do_not_blanket_protect_busy_chat():
    messages = [row(i, reply="missing", kind="image") for i in range(140)]
    selected = select_message_candidates(messages)
    assert len(selected["messages"]) == 40
    assert not selected["protected_ids"]
    assert all("unresolved_reply_unknown" in d["reasons"] for d in selected["decisions"])


def test_hard_priority_ancestors_self_and_exploration_overflow():
    messages = [row(i, text="Please submit") for i in range(10)]
    messages += [row(i) for i in range(10, 30)]
    result = select_message_candidates(messages, max_messages=5, exploration_fraction=.2)
    assert len(result["protected_ids"]) == 10
    assert len(result["messages"]) == 11
    assert sum("seeded_exploration" in d["reasons"] for d in result["decisions"]) == 1
    chain = [row(0, sender="self"), row(1, reply="m0"), row(2, reply="m1", text="Correction: gate B")]
    result = select_message_candidates(chain + [row(i) for i in range(3, 20)], max_messages=4, self_ids=("self",))
    assert {"m0", "m1", "m2"} <= set(result["protected_ids"])
    assert "reply_to_self" in result["decisions"][1]["reasons"]


def test_mentions_contacts_keywords_deterministic_and_nonmutating():
    messages = [row(i) for i in range(80)]
    messages[10]["mentions"] = ["self"]
    messages[20]["mentions"] = ["all"]
    messages[30]["sender"] = "boss"
    messages[40]["text"] = "rocket"
    result = select_message_candidates(messages, self_ids=("self",), protected_senders=("boss",), protected_keywords=("rocket",))
    assert {"m10", "m20", "m30", "m40"} <= set(result["protected_ids"])
    assert result == select_message_candidates(messages, self_ids=("self",), protected_senders=("boss",), protected_keywords=("rocket",))
    assert messages[40]["text"] == "rocket"


def test_cycles_terminate_and_malformed_fails():
    messages = [row(0, reply="m1", text="Please review"), row(1, reply="m0")]
    assert len(select_message_candidates(messages)["messages"]) == 2
    with pytest.raises(ValueError):
        select_message_candidates([row(0), row(0)])
    with pytest.raises(ValueError):
        select_message_candidates(messages, max_messages=True)
