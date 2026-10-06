from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes.messages import attachment_content
from app.core.background_jobs import BackgroundJobStore
from app.domains.message_attachments import cache_key
from app.domains.message_history import MessageHistoryService


def make_service(tmp_path):
    path = tmp_path / "messages.sqlite"
    service = MessageHistoryService(path, BackgroundJobStore(path))
    service.ensure_schema()
    policy = service.set_policy({"platform": "qq", "account_id": "8336",
        "conversation_type": "group", "conversation_id": "g1", "media_enabled": True, "record_enabled": True})
    message = {"platform": "qq", "account_id": "8336", "conversation_type": "group",
        "conversation_id": "g1", "message_id": "m1", "received_at": 200,
        "attachments": [{"ordinal": 0, "kind": "image", "file_name": "photo.png"}]}
    return service, policy, message


def media_row(**changes):
    row = {"platform": "qq", "account_id": "8336", "conversation_type": "group",
        "conversation_id": "g1", "message_id": "m1", "policy_revision": 1, "ordinal": 0,
        "state": "cached", "mime_type": "image/png", "size_bytes": 4,
        "sha256": "a" * 64, "expires_at": int(time.time()) + 3600}
    row.update(changes)
    return row


def test_policy_migration_disables_media_and_retains_history(tmp_path):
    service, policy, message = make_service(tmp_path)
    service.import_messages([message])
    with sqlite3.connect(service.db_path) as conn:
        conn.execute("ALTER TABLE message_history_policies DROP COLUMN media_enabled")
    service.ensure_schema()
    assert service.list_policies()[0]["media_enabled"] is False
    assert len(service.history(policy["conversation_key"])["messages"]) == 1
    assert service.attachments()["attachments"] == []


def test_immutable_refs_and_old_v1_retries(tmp_path):
    service, _, message = make_service(tmp_path)
    assert len(service.import_messages([message])["acknowledged"]) == 1
    assert len(service.import_messages([message])["acknowledged"]) == 1
    old = {key: value for key, value in message.items() if key != "attachments"}
    assert len(service.import_messages([old])["acknowledged"]) == 1
    conflict = {**message, "attachments": [{"ordinal": 0, "kind": "video"}]}
    assert service.import_messages([conflict])["rejected"][0]["reason"] == "identity_conflict"
    assert service.attachments()["attachments"][0]["attachment_id"] == "message_attachment_" + cache_key("qq", "8336", "m1", 0)
    assert len(service.attachments()["attachments"]) == 1


def test_invalid_refs_do_not_import_parent_and_no_importer_paths(tmp_path):
    service, _, message = make_service(tmp_path)
    for refs in ([{"ordinal": 0, "kind": "image", "url": "https://bad"}],
                 [{"ordinal": 0, "kind": "image"}] * 2):
        assert service.import_messages([{**message, "attachments": refs}])["rejected"][0]["reason"] == "invalid_message"
    assert service.recent()["messages"] == []
    assert service.attachments()["attachments"] == []


def test_media_update_requires_parent_ordinal_policy_and_safe_mime(tmp_path):
    service, _, message = make_service(tmp_path)
    assert service.update_media([media_row()])["rejected"][0]["reason"] == "missing_attachment"
    service.import_messages([message])
    assert service.update_media([media_row(ordinal=1)])["rejected"]
    assert service.update_media([media_row(policy_revision=2)])["rejected"][0]["reason"] == "policy_fenced"
    assert service.update_media([media_row(conversation_id="other")])["rejected"]
    assert service.update_media([media_row(mime_type="text/html")])["rejected"][0]["reason"] == "invalid_cached_media"
    assert service.update_media([media_row(url="https://bad")])["rejected"][0]["reason"] == "invalid_media"
    row = media_row()
    assert service.update_media([row])["acknowledged"]
    assert service.update_media([row])["acknowledged"]
    assert service.update_media([{**row, "sha256": "b" * 64}])["rejected"][0]["reason"] == "immutable_cached_media"
    assert service.attachments()["attachments"][0]["updated_at"]


def test_cached_state_never_regresses_and_expiry_never_revives(tmp_path):
    service, _, message = make_service(tmp_path)
    service.import_messages([message])
    assert service.update_media([media_row()])["acknowledged"]
    assert service.update_media([media_row(state="pending")])["rejected"][0]["reason"] == "state_conflict"
    with sqlite3.connect(service.db_path) as conn:
        conn.execute("UPDATE message_history_attachments SET expires_at=?", (int(time.time()) - 1,))
    assert service.attachments()["attachments"][0]["state"] == "expired"
    assert service.update_media([media_row()])["rejected"][0]["reason"] == "state_conflict"
    expired = {key: value for key, value in media_row(state="expired").items()
               if key not in {"mime_type", "size_bytes", "sha256", "expires_at"}}
    assert service.update_media([expired])["acknowledged"]
    assert service.attachments()["attachments"][0]["sha256"] == "a" * 64
    assert service.attachments()["attachments"][0]["mime_type"] == "image/png"
    assert service.update_media([media_row()])["rejected"]


def test_attachment_reads_require_both_scopes_and_current_policy(tmp_path):
    service, policy, message = make_service(tmp_path)
    service.import_messages([message])
    item = service.attachments()["attachments"][0]
    assert service.attachments(allowed_sources=[])["attachments"] == []
    with pytest.raises(PermissionError):
        service.attachment(item["attachment_id"], allowed_accounts=[])
    grants = {"allowed_sources": [policy["source_id"]], "allowed_accounts": [policy["account_scope_id"]]}
    assert service.attachments(query="photo", kind="image", **grants)["attachments"]
    assert service.attachments(kind="video", **grants)["attachments"] == []
    service.set_policy({"platform": "qq", "account_id": "8336", "conversation_type": "group",
        "conversation_id": "g1", "expected_revision": 1, "record_enabled": False, "media_enabled": True})
    with pytest.raises(PermissionError):
        service.attachment(item["attachment_id"])
    assert service.update_media([media_row(policy_revision=2)])["rejected"][0]["reason"] == "policy_fenced"


def test_media_disabled_policy_fences_updates(tmp_path):
    service, _, message = make_service(tmp_path)
    service.import_messages([message])
    service.set_policy({"platform": "qq", "account_id": "8336", "conversation_type": "group",
        "conversation_id": "g1", "expected_revision": 1, "media_enabled": False})
    assert service.update_media([media_row(policy_revision=2)])["rejected"][0]["reason"] == "policy_fenced"
    assert service.attachments()["attachments"] == []


def test_legacy_policy_update_preserves_media_and_explicit_false_disables(tmp_path):
    service, _, _ = make_service(tmp_path)
    value = {"platform": "qq", "account_id": "8336", "conversation_type": "group",
             "conversation_id": "g1", "expected_revision": 1}
    assert service.set_policy(value)["media_enabled"] is True
    assert service.set_policy({**value, "expected_revision": 2, "media_enabled": False})["media_enabled"] is False
    assert service.set_policy({**value, "expected_revision": 3})["media_enabled"] is False
    assert service.set_policy({**value, "conversation_id": "new", "expected_revision": 0})["media_enabled"] is False


def test_content_requires_ui_cached_file_and_rejects_symlinks(tmp_path):
    service, _, message = make_service(tmp_path)
    service.import_messages([message])
    item = service.attachments()["attachments"][0]
    settings = SimpleNamespace(messages_media_cache_dir=str(tmp_path), parsed_cors_origins=list)
    runtime = SimpleNamespace(message_history=service, settings=settings)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={"host": "127.0.0.1:8765"},
                              app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    with pytest.raises(HTTPException) as failure:
        attachment_content(request, item["attachment_id"])
    assert failure.value.status_code == 404
    service.update_media([media_row()])
    path = tmp_path / (cache_key("qq", "8336", "m1", 0) + ".bin")
    path.write_bytes(b"data")
    response = attachment_content(request, item["attachment_id"])
    assert Path(response.path) == path
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    async def consume(response):
        events = []
        async def send(event):
            events.append(event)
        await response({"type": "http", "method": "GET"}, None, send)
        return events
    events = asyncio.run(consume(response))
    assert events[0]["status"] == 200
    assert b"".join(event.get("body", b"") for event in events) == b"data"
    path.unlink()
    path.symlink_to(tmp_path / "messages.sqlite")
    # A replacement between route construction and ASGI streaming stays denied.
    assert asyncio.run(consume(response))[0]["status"] == 404
    with pytest.raises(HTTPException):
        attachment_content(request, item["attachment_id"])
    request.client.host = "192.168.1.1"
    with pytest.raises(HTTPException) as failure:
        attachment_content(request, item["attachment_id"])
    assert failure.value.status_code == 403
