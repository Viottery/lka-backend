from concurrent.futures import ThreadPoolExecutor

import pytest

from lka_message_connector.models import CapturePolicy, MessageEnvelope
from lka_message_connector.store import OutboxFull, Store


def policy(**changes):
    return CapturePolicy(
        platform="telegram",
        account_id="account",
        conversation_type="group",
        conversation_id="chat",
        record_enabled=True,
        revision=1,
        capture_epoch=1,
        **changes,
    )


def message(number=1, **changes):
    values = {
        "platform": "telegram",
        "account_id": "account",
        "conversation_type": "group",
        "conversation_id": "chat",
        "message_id": f"chat:{number}",
        "capture_epoch": 1,
        "received_at": 100 + number,
        "sent_at": 100 + number,
        "sender_id": "sender",
        "text": f"message {number}",
        "adapter_id": "telegram",
        "adapter_version": "1",
        "metadata_capabilities": {},
    }
    values.update(changes)
    return MessageEnvelope(**values)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "connector.sqlite")
    value.replace_policies([policy()])
    return value


def ack(item):
    return {key: item[key] for key in ("platform", "account_id", "message_id")}


def test_durable_duplicate_and_identity_conflict(store):
    item = message()
    assert store.enqueue(item)
    assert not store.enqueue(item)
    assert not store.enqueue(message(received_at=999))
    assert store.pending()[0]["received_at"] == item.received_at
    assert store.read_message(item.internal_id)["received_at"] == item.received_at
    with pytest.raises(ValueError, match="identity_conflict"):
        store.enqueue(message(sender_name="changed sender"))
    with pytest.raises(ValueError, match="identity_conflict"):
        store.enqueue(message(text="edited"))
    store.replace_policies([policy(), policy().model_copy(update={"conversation_id": "different"})])
    with pytest.raises(ValueError, match="identity_conflict"):
        store.enqueue(message(conversation_id="different"))


def test_absent_disabled_and_stale_epoch(store):
    for item in (message(capture_epoch=2), message(conversation_id="different")):
        with pytest.raises(PermissionError):
            store.enqueue(item)
    store.replace_policies([policy().model_copy(update={"record_enabled": False})])
    with pytest.raises(PermissionError):
        store.enqueue(message())


@pytest.mark.parametrize("revoke", ["remove", "disable", "epoch"])
def test_revoke_regrant_never_resurrects(store, revoke):
    item = message()
    store.enqueue(item)
    store.apply_ack(store.pending(), {"acknowledged": [ack(item.model_dump())], "rejected": []})
    next_policy = {
        "remove": [],
        "disable": [policy().model_copy(update={"record_enabled": False})],
        "epoch": [policy().model_copy(update={"capture_epoch": 2})],
    }[revoke]
    store.replace_policies(next_policy)
    assert store.pending() == []
    with pytest.raises(PermissionError, match="message_unavailable"):
        store.read_message(item.internal_id)
    store.replace_policies([policy()])
    assert store.history(policy().key)["items"] == []
    with pytest.raises(PermissionError):
        store.read_message(item.internal_id)
    assert store.enqueue(message(2))
    assert store.status()["quarantined"] == 1


def test_pending_revoked_is_quarantined_and_new_epoch_capture(store):
    store.enqueue(message())
    store.replace_policies([policy().model_copy(update={"capture_epoch": 2})])
    store.enqueue(message(2, capture_epoch=2))
    assert [p["message_id"] for p in store.pending()] == ["chat:2"]
    assert store.status()["quarantined"] == 1


def test_revision_and_media_update_preserves_history(store):
    item = message()
    store.enqueue(item)
    store.replace_policies([policy().model_copy(update={"revision": 2, "media_enabled": True})])
    assert store.read_message(item.internal_id)["text"] == item.text
    assert store.policy(item.conversation).revision == 2
    assert len(store.policies()) == 1


def test_bound_and_encoded_batch_bytes(tmp_path):
    store = Store(tmp_path / "store.sqlite", max_pending=2)
    store.replace_policies([policy()])
    store.enqueue(message(1, text="中文"))
    store.enqueue(message(2))
    with pytest.raises(OutboxFull):
        store.enqueue(message(3))
    assert not store.enqueue(message(2))
    batch = store.pending(limit=1)
    import json

    size = len(
        json.dumps(
            {"schema_version": 2, "messages": batch},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    assert store.pending(max_bytes=size) == batch
    assert store.pending(max_bytes=size - 1) == []
    assert set(batch[0]) == set(MessageEnvelope.model_fields)
    store.apply_ack(batch, {"acknowledged": [ack(batch[0])], "rejected": []})
    assert store.enqueue(message(3))
    assert store.read_message(message(1).internal_id)["sync_state"] == "acked"


@pytest.mark.parametrize(
    "bad_response",
    [
        {
            "acknowledged": [
                {"platform": "telegram", "account_id": "account", "message_id": "foreign"}
            ],
            "rejected": [],
        },
        {"acknowledged": [ack(message().model_dump())] * 2, "rejected": []},
        {
            "acknowledged": [ack(message().model_dump())],
            "rejected": [dict(ack(message().model_dump()), reason="no", permanent=True)],
        },
        {
            "acknowledged": [],
            "rejected": [dict(ack(message().model_dump()), reason="no", permanent=1)],
        },
        {"acknowledged": [], "rejected": [], "unexpected": True},
        {"acknowledged": {}, "rejected": []},
    ],
)
def test_spoofed_malformed_ack_is_atomic(store, bad_response):
    store.enqueue(message())
    store.enqueue(message(2))
    with pytest.raises(ValueError):
        store.apply_ack(store.pending(), bad_response)
    assert store.status()["pending"] == 2
    assert store.status()["quarantined"] == 0


def test_ack_rejection_and_replay(store):
    for i in range(1, 4):
        store.enqueue(message(i))
    batch = store.pending()
    response = {
        "acknowledged": [ack(batch[0])],
        "rejected": [
            dict(ack(batch[1]), reason="stale_epoch", permanent=True),
            dict(ack(batch[2]), reason="retry", permanent=False),
        ],
    }
    store.apply_ack(batch, response)
    store.apply_ack(batch, response)
    assert [i["message_id"] for i in store.pending()] == ["chat:3"]
    assert store.status() == {"pending": 1, "acked": 1, "quarantined": 1, "active_conversations": 1}
    assert store.search("message")["total"] == 2
    with pytest.raises(ValueError):
        store.apply_ack(
            [dict(batch[0], text="forged")], {"acknowledged": [ack(batch[0])], "rejected": []}
        )


def test_search_history_context_pagination(store):
    for number in range(1, 7):
        store.enqueue(message(number, text=f"100%_literal {number}"))
    assert store.conversations()["items"][0]["message_count"] == 6
    assert store.search("%_literal", limit=2)["next_offset"] == 2
    assert store.search("%_literal", offset=4)["next_offset"] is None
    assert store.search("missing")["items"] == []
    assert store.search("literal", since=103, until=104, sender_id="sender")["total"] == 2
    page = store.history(policy().key, limit=2)
    assert [m["seq"] for m in page["items"]] == [5, 6]
    assert page["next_before_seq"] == 5
    assert [m["seq"] for m in store.history(policy().key, before_seq=5, limit=2)["items"]] == [3, 4]
    context = store.context(message(3).internal_id, before=2, after=2)
    assert [m["seq"] for m in context["before"]] == [1, 2]
    assert [m["seq"] for m in context["after"]] == [4, 5]
    row = context["message"]
    assert row["provider_message_id"] == "chat:3"
    assert row["message_id"] == message(3).internal_id
    assert row["conversation_key"] == policy().key


def test_every_read_rechecks_grants_and_unavailable_is_uniform(store):
    item = message()
    store.enqueue(item)
    store.replace_policies([])
    assert store.search("message")["items"] == []
    assert store.conversations()["items"] == []
    for call in (
        lambda: store.read_message(item.internal_id),
        lambda: store.read_message("missing"),
        lambda: store.context(item.internal_id),
        lambda: store.context("missing"),
        lambda: store.history(item.key),
        lambda: store.history("missing"),
        lambda: store.search("message", conversation_key=item.key),
        lambda: store.search("message", conversation_key="missing"),
    ):
        with pytest.raises(PermissionError, match="^message_unavailable$"):
            call()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x", "limit": 201},
        {"query": "x", "offset": -1},
        {"query": "x", "limit": True},
        {"query": "x", "since": 2, "until": 1},
    ],
)
def test_query_validation(store, kwargs):
    with pytest.raises(ValueError):
        store.search(**kwargs)


def test_concurrent_enqueue_allocates_unique_local_sequences(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(store.enqueue, (message(i) for i in range(1, 21))))
    assert sorted(m["seq"] for m in store.history(policy().key)["items"]) == list(range(1, 21))


def test_client_scope_filters_before_pagination_and_counts(store):
    second = policy().model_copy(update={"conversation_id": "other"})
    store.replace_policies([policy(), second])
    for i in range(1, 4):
        store.enqueue(message(i))
    store.enqueue(message(4, conversation_id="other"))
    scope = {policy().key}
    result = store.search("message", limit=2, allowed_keys=scope)
    assert result["total"] == 3
    assert [item["provider_message_id"] for item in result["items"]] == ["chat:3", "chat:2"]
    assert store.conversations(allowed_keys=scope)["total"] == 1
    assert store.search("message", allowed_keys=set())["total"] == 0
    assert store.conversations(allowed_keys=set())["total"] == 0
    for call in (
        lambda: store.read_message(message(4).internal_id, allowed_keys=scope),
        lambda: store.context(message(4).internal_id, allowed_keys=scope),
        lambda: store.history(second.key, allowed_keys=scope),
        lambda: store.search("message", conversation_key=second.key, allowed_keys=scope),
        lambda: store.read_message(message(1).internal_id, allowed_keys=set()),
    ):
        with pytest.raises(PermissionError, match="^message_unavailable$"):
            call()


def test_recapture_quarantined_identity_cannot_restore_old_epoch(store):
    item = message()
    store.enqueue(item)
    store.replace_policies([policy().model_copy(update={"capture_epoch": 2})])
    with pytest.raises(PermissionError, match="message_unavailable"):
        store.enqueue(message(capture_epoch=2, received_at=999))
    assert store.pending() == []
    assert store.status()["quarantined"] == 1
    store.replace_policies([policy()])
    with pytest.raises(PermissionError, match="message_unavailable"):
        store.enqueue(message(received_at=999))
    assert store.search("message")["items"] == []


def test_pending_rejects_batch_above_wire_limit(store):
    with pytest.raises(ValueError, match="invalid_bound"):
        store.pending(limit=101)
