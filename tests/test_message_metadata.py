from __future__ import annotations

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.domains.message_history import MessageHistoryService
from app.domains.message_metadata import MessageMetadataRevisionConflict


def _service(tmp_path):
    path = tmp_path / "messages.sqlite3"
    service = MessageHistoryService(path, BackgroundJobStore(path))
    service.ensure_schema()
    return service


def _policy(service, conversation_id, *, conversation_type="group"):
    return service.set_policy({"platform": "mock", "account_id": "acct", "conversation_type": conversation_type,
                               "conversation_id": conversation_id, "record_enabled": True, "expected_revision": 0})


def _message(message_id, conversation_id, *, received_at=100, sender_name="Alice", text="hello"):
    return {"platform": "mock", "account_id": "acct", "message_id": message_id,
            "conversation_type": "group", "conversation_id": conversation_id, "sender_id": "u1",
            "sender_name": sender_name, "text": text, "received_at": received_at}


def test_manual_alias_cas_wins_over_imported_name_and_exact_resolve(tmp_path):
    service = _service(tmp_path)
    policy = _policy(service, "g1")
    assert service.import_conversation_metadata([{
        "platform": "mock", "account_id": "acct", "conversation_type": "group", "conversation_id": "g1",
        "capture_epoch": policy["capture_epoch"], "platform_name": "Mock Chat", "display_name": "Ops Team",
        "cache_provenance": {"adapter": "cache-v1"},
    }]) == {"accepted": 1, "rejected": 0}
    updated = service.update_conversation_metadata(policy["conversation_key"], expected_revision=0,
                                                   user_alias="My Ops Team")
    assert updated["display_name"] == "My Ops Team"
    assert updated["platform_name"] == "Mock Chat" and updated["revision"] == 1
    assert service.resolve_conversations("  MY   OPS TEAM ")["matches"][0]["conversation_key"] == policy["conversation_key"]
    assert service.resolve_conversations("Ops Team")["matches"][0]["conversation_key"] == policy["conversation_key"]
    assert service.resolve_conversations("ops") ["matches"] == []
    with pytest.raises(MessageMetadataRevisionConflict):
        service.update_conversation_metadata(policy["conversation_key"], expected_revision=0, display_name="Wrong")


def test_import_requires_current_epoch_and_never_creates_whitelist(tmp_path):
    service = _service(tmp_path)
    row = {"platform": "mock", "account_id": "acct", "conversation_type": "group", "conversation_id": "g1",
           "capture_epoch": 1, "display_name": "Cached"}
    assert service.import_conversation_metadata([row]) == {"accepted": 0, "rejected": 1}
    policy = _policy(service, "g1")
    row["capture_epoch"] = policy["capture_epoch"]
    assert service.import_conversation_metadata([row])["accepted"] == 1
    disabled = service.set_policy({"platform": "mock", "account_id": "acct", "conversation_type": "group",
                                   "conversation_id": "g1", "record_enabled": False,
                                   "expected_revision": policy["revision"]})
    assert disabled["capture_epoch"] != row["capture_epoch"]
    assert service.import_conversation_metadata([row]) == {"accepted": 0, "rejected": 1}


def test_message_context_is_bounded_scoped_and_reports_untrusted_neighbor_messages(tmp_path):
    service = _service(tmp_path)
    policy = _policy(service, "g1")
    service.import_messages([_message(f"m{i}", "g1", received_at=i, text=f"line {i}") for i in range(1, 5)])
    found = service.search("line", policy["conversation_key"], sender="ali", since=2, until=3)
    assert [row["received_at"] for row in found["messages"]] == [3, 2]
    anchor_id = found["messages"][0]["message_id"]
    context = service.get_message_context(anchor_id, before=1, after=1)
    assert [item["seq"] for item in context["messages"]] == [2, 3, 4]
    assert context["anchor_message_id"] == anchor_id and context["untrusted"]
    assert context["coverage"]["capture_epoch"] == policy["capture_epoch"]
    with pytest.raises(ValueError):
        service.get_message_context(anchor_id, before=26)
    with pytest.raises(PermissionError):
        service.get_message(anchor_id, allowed_sources=[])


def test_ambiguous_exact_aliases_return_bounded_candidates(tmp_path):
    service = _service(tmp_path)
    first = _policy(service, "same")
    second = service.set_policy({"platform": "mock", "account_id": "acct2", "conversation_type": "group",
                                 "conversation_id": "same", "record_enabled": True, "expected_revision": 0})
    resolved = service.resolve_conversations("same")
    assert resolved["ambiguous"] and {item["conversation_key"] for item in resolved["matches"]} == {
        first["conversation_key"], second["conversation_key"]}


def test_current_capture_search_filters_stale_rows_before_limit(tmp_path):
    service = _service(tmp_path)
    policy = _policy(service, "g1")
    service.import_messages([_message("old", "g1", received_at=999999, text="needle old")])
    revoked = service.set_policy({"platform": "mock", "account_id": "acct", "conversation_type": "group",
                                  "conversation_id": "g1", "record_enabled": False,
                                  "expected_revision": policy["revision"]})
    current = service.set_policy({"platform": "mock", "account_id": "acct", "conversation_type": "group",
                                  "conversation_id": "g1", "record_enabled": True,
                                  "expected_revision": revoked["revision"]})
    service.import_messages([_message("new", "g1", received_at=200, text="needle current")])

    historical_page = service.search("needle", current["conversation_key"], limit=1)
    current_page = service.search("needle", current["conversation_key"], limit=1, current_capture_only=True)
    conversation = service.list_conversations()["conversations"][0]
    assert historical_page["messages"][0]["provider_message_id"] == "old"
    assert current_page["messages"][0]["provider_message_id"] == "new"
    assert conversation["message_count"] == 2
    assert conversation["current_message_count"] == 1
