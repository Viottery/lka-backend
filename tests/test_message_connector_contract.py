"""Independent connector payloads interoperate with the real backend domain.

Only schema/store code is imported: no MCP/Telegram dependency in backend tests.
"""

import importlib
from pathlib import Path

from test_message_history import _service

from app.domains.message_attachments import cache_key


def test_connector_v2_ids_metadata_ack_and_media_match_backend(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "packages/message-connector/src"
    monkeypatch.syspath_prepend(str(source))
    models = importlib.import_module("lka_message_connector.models")
    store_module = importlib.import_module("lka_message_connector.store")
    backend, _ = _service(tmp_path)
    local = store_module.Store(tmp_path / "independent.sqlite3")
    policies, messages = [], []
    for chat in ("-100101", "-100202"):
        identity = {
            "platform": "telegram",
            "account_id": "42",
            "conversation_type": "group",
            "conversation_id": chat,
        }
        policy = backend.set_policy(
            {**identity, "record_enabled": True, "media_enabled": True, "minimum_import_version": 2}
        )
        approved = models.CapturePolicy.model_validate(
            {k: policy[k] for k in models.CapturePolicy.model_fields}
        )
        assert approved.key == policy["conversation_key"]
        policies.append(approved)
        messages.extend(
            [
                models.MessageEnvelope(
                    **identity,
                    message_id=f"{chat}:{number}",
                    text="资料🙂",
                    sent_at=100,
                    received_at=101,
                    capture_epoch=approved.capture_epoch,
                    adapter_id="telegram.telethon",
                    adapter_version="1",
                    reply_to_message_id=f"{chat}:1" if number == 2 else None,
                    mentions=[{"kind": "user", "user_id": "42"}],
                    content_parts=[{"kind": "text", "text": "资料🙂"}],
                    metadata_capabilities={
                        "mentions": "supported",
                        "reply": "supported",
                        "content_parts": "supported",
                    },
                    attachments=[{"ordinal": 0, "kind": "image"}],
                )
                for number in (1, 2)
            ]
        )
    local.replace_policies(policies)
    for message in messages:
        local.enqueue(message)
    payload = local.pending()
    response = backend.import_messages(payload, schema_version=2)
    assert len(response["acknowledged"]) == 4 and not response["rejected"]
    local.apply_ack(payload, response)
    assert local.status()["acked"] == 4
    for approved in policies:
        rows = backend.history(approved.key)["messages"]
        assert rows[0]["reply_to_internal_message_id"] == rows[1]["message_id"]
        original = next(m for m in messages if m.message_id == rows[0]["provider_message_id"])
        assert original.internal_id == rows[0]["message_id"]
        media_identity = {
            **approved.conversation.model_dump(),
            "message_id": original.message_id,
            "ordinal": 0,
            "state": "cached",
            "mime_type": "image/jpeg",
            "size_bytes": 10,
            "sha256": "a" * 64,
            "expires_at": 4102444800,
            "policy_revision": approved.revision,
            "capture_epoch": approved.capture_epoch,
        }
        assert backend.update_media([media_identity], schema_version=2)["acknowledged"]
        refs = backend.attachments(approved.key)["attachments"]
        assert any(
            r["attachment_id"]
            == "message_attachment_" + cache_key("telegram", "42", original.message_id, 0)
            for r in refs
        )
