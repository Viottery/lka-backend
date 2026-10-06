"""HTTP boundary checks for opt-in capture-epoch imports; synthetic data only."""
from __future__ import annotations

from test_message_api_tools import _client, _request

from app.core.message_analysis import message_chunks

IDENTITY = {"platform": "qq", "account_id": "self", "conversation_type": "group",
            "conversation_id": "room"}
AUTH = {"Authorization": "Bearer import-test"}


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_MESSAGES_IMPORT_TOKEN", "import-test")
    monkeypatch.delenv("LKA_MESSAGES_API_TOKEN", raising=False)
    app, service = _client(tmp_path)
    policy = service.set_policy({**IDENTITY, "record_enabled": True, "minimum_import_version": 2})
    row = {**IDENTITY, "message_id": "message", "received_at": 123,
           "capture_epoch": policy["capture_epoch"], "text": "hello", "mentions": [],
           "adapter_id": "mock", "adapter_version": "2", "metadata_capabilities": {}}
    return app, service, policy, row


def test_v2_http_and_both_v1_entrypoints_cannot_bypass_capture_epoch(tmp_path, monkeypatch):
    app, service, policy, row = _setup(tmp_path, monkeypatch)
    v1 = {key: row[key] for key in (*IDENTITY, "message_id", "received_at", "text")}
    legacy = {"self_id": "self", "message_id": "legacy", "conversation_type": "group",
              "conversation_id": "room", "sender_id": "sender", "display_name": "Sender",
              "text": "hello", "sent_at": 122, "received_at": 123}
    generic = _request(app, "POST", "/integrations/messages/import",
                       payload={"schema_version": 1, "messages": [v1]}, headers=AUTH)
    qq = _request(app, "POST", "/integrations/qq/messages/import",
                  payload={"schema_version": 1, "messages": [legacy]}, headers=AUTH)
    assert generic.json()["rejected"][0]["reason"] == "import_version_required"
    assert qq.json()["rejected"][0]["reason"] == "import_version_required"
    imported = _request(app, "POST", "/integrations/messages/import",
                        payload={"schema_version": 2, "messages": [row]}, headers=AUTH)
    assert imported.status_code == 200 and len(imported.json()["acknowledged"]) == 1
    assert service.history(policy["conversation_key"])["messages"][0]["schema_version"] == 2
    denied = _request(app, "POST", "/integrations/messages/import",
                      payload={"schema_version": 2, "messages": [row]})
    assert denied.status_code == 401


def test_stale_epoch_and_invalid_native_metadata_never_enter_history(tmp_path, monkeypatch):
    app, service, _, row = _setup(tmp_path, monkeypatch)
    for changes, reason in [({"capture_epoch": 99}, "capture_epoch_conflict"),
                            ({"mentions": [{"kind": "user", "user_id": "user"}]}, "invalid_message")]:
        response = _request(app, "POST", "/integrations/messages/import",
                            payload={"schema_version": 2, "messages": [{**row, **changes}]}, headers=AUTH)
        assert response.status_code == 200 and response.json()["rejected"][0]["reason"] == reason
    assert service.recent()["messages"] == []


def test_media_v2_cannot_enter_old_endpoint_contract(tmp_path, monkeypatch):
    app, service, policy, row = _setup(tmp_path, monkeypatch)
    policy = service.set_policy({**IDENTITY, "expected_revision": policy["revision"], "media_enabled": True})
    row["attachments"] = [{"ordinal": 0, "kind": "image"}]
    assert _request(app, "POST", "/integrations/messages/import",
                    payload={"schema_version": 2, "messages": [row]}, headers=AUTH).json()["acknowledged"]
    for version in (1, 2):
        response = _request(app, "POST", "/integrations/messages/media", headers=AUTH,
            payload={"schema_version": version, "media": [{**IDENTITY, "message_id": "message",
                "ordinal": 0, "state": "unavailable", "policy_revision": policy["revision"]}]})
        assert response.status_code == 200 and response.json()["rejected"]
    accepted = _request(app, "POST", "/integrations/messages/media", headers=AUTH,
        payload={"schema_version": 2, "media": [{**IDENTITY, "message_id": "message", "ordinal": 0,
            "state": "unavailable", "policy_revision": policy["revision"],
            "capture_epoch": policy["capture_epoch"]}]})
    assert accepted.status_code == 200 and len(accepted.json()["acknowledged"]) == 1


def test_coverage_is_truthful_and_revocation_hides_it(tmp_path, monkeypatch):
    app, service, policy, _ = _setup(tmp_path, monkeypatch)
    path = f"/messages/conversations/{policy['conversation_key']}/coverage"
    response = _request(app, "GET", path)
    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["legacy_only"] and coverage["pipeline_version"] == "legacy-v1"
    assert coverage["analysis_covered_seq"] is coverage["baseline_start_seq"] is None
    assert not coverage["complete_for_platform"]
    service.set_policy({**IDENTITY, "expected_revision": policy["revision"], "record_enabled": False})
    assert _request(app, "GET", path).status_code == 404


def test_long_message_chunks_keep_native_evidence_without_duplicating_parts():
    text = "消息🙂" * 500
    message = {"message_id": "m", "seq": 1, "text": text,
               "mentions": [{"kind": "user", "user_id": "self"}], "reply_to_message_id": "previous",
               "metadata_capabilities": {"mentions": "supported", "reply": "supported"},
               "content_parts": [{"kind": "text", "text": text}]}
    chunks = list(message_chunks([message], 1024))
    assert len(chunks) > 1
    assert "".join(part["text"] for chunk in chunks for part in chunk) == text
    assert all(part["mentions"] == message["mentions"] and "content_parts" not in part
               for chunk in chunks for part in chunk)
