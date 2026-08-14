from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.core.local_config import LocalAppConfig, MailProviderConfig, OutlookMailConfig
from app.domains.mail import MailService
from app.integrations.outlook import OutlookAuthError, OutlookService
from app.storage.db import connect, init_db


class FakeGraphTransport:
    def __init__(self) -> None:
        self.posted_forms: list[tuple[str, dict[str, str]]] = []
        self.get_urls: list[str] = []
        self.raise_pending = False

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        self.posted_forms.append((url, data))
        if url.endswith("/devicecode"):
            return {
                "device_code": "device-code",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900,
                "interval": 5,
                "message": "Open the verification URL and enter the code.",
            }
        if self.raise_pending:
            raise OutlookAuthError("authorization_pending: user has not finished auth")
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }

    def get_json(
        self,
        url: str,
        *,
        access_token: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert access_token == "access-token"
        self.get_urls.append(url)
        if url.endswith("/me?$select=mail,userPrincipalName,displayName"):
            return {
                "mail": "user@example.com",
                "userPrincipalName": "fallback@example.com",
                "displayName": "Outlook User",
            }
        if "/me/mailFolders/Inbox/messages/delta?" in url:
            return {
                "value": [
                    {
                        "id": "message-001",
                        "subject": "Contract review deadline",
                        "from": {"emailAddress": {"address": "sender@example.com"}},
                        "toRecipients": [
                            {"emailAddress": {"address": "user@example.com"}},
                        ],
                        "ccRecipients": [],
                        "receivedDateTime": "2026-08-03T09:30:00Z",
                        "body": {"content": "Please review the contract by Friday."},
                        "hasAttachments": True,
                    }
                ],
                "@odata.deltaLink": "https://graph.test/delta-token",
            }
        if url == "https://graph.test/delta-token":
            return {"value": [], "@odata.deltaLink": "https://graph.test/delta-token-2"}
        if "/me/messages/message-001/attachments?" in url:
            return {
                "value": [
                    {
                        "id": "attachment-001",
                        "name": "contract.pdf",
                        "contentType": "application/pdf",
                        "size": 2048,
                        "isInline": False,
                    }
                ]
            }
        raise AssertionError(f"Unexpected Graph URL: {url}")


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: FakeGraphTransport):
    monkeypatch.setenv("TEST_MS_GRAPH_CLIENT_ID", "client-id")
    db_path = tmp_path / "lka.sqlite3"
    token_path = tmp_path / "outlook_token.json"
    init_db(db_path)

    def conn() -> sqlite3.Connection:
        return connect(db_path)

    local_config = LocalAppConfig(
        mail=MailProviderConfig(
            outlook=OutlookMailConfig(
                enabled=True,
                client_id_env="TEST_MS_GRAPH_CLIENT_ID",
                token_store_path=token_path,
                sync_folder="Inbox",
            )
        )
    )
    mail_service = MailService(conn)
    return OutlookService(conn, mail_service, local_config, transport), db_path, token_path


def test_outlook_device_auth_start_and_pending_complete(tmp_path, monkeypatch):
    transport = FakeGraphTransport()
    service, _db_path, _token_path = _service(tmp_path, monkeypatch, transport)

    start_result = service.start_device_auth()

    assert start_result.device_code == "device-code"
    assert start_result.user_code == "ABCD-EFGH"
    assert transport.posted_forms[0][1]["scope"] == "User.Read Mail.Read offline_access"

    transport.raise_pending = True
    complete_result = service.complete_device_auth(device_code="device-code")

    assert complete_result.status == "pending"
    assert complete_result.error == "authorization_pending"


def test_outlook_sync_persists_body_attachment_metadata_and_state(tmp_path, monkeypatch):
    transport = FakeGraphTransport()
    service, db_path, token_path = _service(tmp_path, monkeypatch, transport)

    complete_result = service.complete_device_auth(device_code="device-code")

    assert complete_result.status == "authorized"
    token_payload = json.loads(token_path.read_text(encoding="utf-8"))
    assert token_payload["access_token"] == "access-token"
    assert token_payload["refresh_token"] == "refresh-token"
    assert token_payload["expires_at"] > 0

    sync_result = service.sync_messages(folder="Inbox", limit=10, max_pages=1)

    assert sync_result.status == "completed"
    assert sync_result.imported_messages == 1
    assert sync_result.imported_attachments == 1
    assert sync_result.sync_mode == "delta"
    assert sync_result.delta_link == "https://graph.test/delta-token"
    conn = sqlite3.connect(db_path)
    try:
        message = conn.execute(
            "SELECT subject, body_text FROM mail_messages WHERE external_id = ?",
            ("message-001",),
        ).fetchone()
        attachment = conn.execute(
            "SELECT name, content_type, size, is_downloaded FROM mail_attachments WHERE external_id = ?",
            ("attachment-001",),
        ).fetchone()
        sync_state = conn.execute(
            "SELECT status, folder, next_link, delta_link, last_result_payload FROM mail_sync_state"
        ).fetchone()
    finally:
        conn.close()

    assert message == ("Contract review deadline", "Please review the contract by Friday.")
    assert attachment == ("contract.pdf", "application/pdf", 2048, 0)
    assert sync_state[0] == "completed"
    assert sync_state[1] == "Inbox"
    assert sync_state[2] is None
    assert sync_state[3] == "https://graph.test/delta-token"
    assert json.loads(sync_state[4])["imported_attachments"] == 1
    assert any("/me/mailFolders/Inbox/messages/delta?" in url for url in transport.get_urls)
    assert any("/me/messages/message-001/attachments?" in url for url in transport.get_urls)


def test_outlook_sync_uses_saved_delta_link_for_next_sync(tmp_path, monkeypatch):
    transport = FakeGraphTransport()
    service, db_path, _token_path = _service(tmp_path, monkeypatch, transport)

    service.complete_device_auth(device_code="device-code")
    first_result = service.sync_messages(folder="Inbox", limit=10, max_pages=1)
    second_result = service.sync_messages(folder="Inbox", limit=10, max_pages=1)

    assert first_result.imported_messages == 1
    assert second_result.imported_messages == 0
    assert second_result.delta_link == "https://graph.test/delta-token-2"
    assert "https://graph.test/delta-token" in transport.get_urls
    conn = sqlite3.connect(db_path)
    try:
        sync_state = conn.execute(
            "SELECT delta_link, last_result_payload FROM mail_sync_state"
        ).fetchone()
    finally:
        conn.close()

    assert sync_state[0] == "https://graph.test/delta-token-2"
    assert json.loads(sync_state[1])["delta_link"] == "https://graph.test/delta-token-2"
