"""Microsoft Graph Outlook read-only sync service."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from hashlib import sha1
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from app.core.local_config import LocalAppConfig, OutlookMailConfig
from app.core.mail import MailAccountInput, MailAttachmentInput, MailMessageInput, MailService

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
LOGIN_BASE_URL = "https://login.microsoftonline.com"


def _now_epoch() -> int:
    return int(time.time())


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class OutlookServiceError(RuntimeError):
    """Base error for Outlook service failures."""


class OutlookConfigError(OutlookServiceError):
    """Raised when local Outlook config is incomplete or unsupported."""


class OutlookAuthError(OutlookServiceError):
    """Raised when Microsoft identity platform auth returns an unrecoverable error."""


class OutlookRemoteError(OutlookServiceError):
    """Raised when Microsoft Graph returns an unexpected error."""


class GraphHTTPTransport(Protocol):
    """Small transport seam used to keep Outlook sync unit-testable."""

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        """POST form data and return a decoded JSON payload."""

    def get_json(
        self,
        url: str,
        *,
        access_token: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET JSON from Microsoft Graph with a bearer token."""


class UrllibGraphHTTPTransport:
    """Stdlib HTTP transport to avoid adding a dependency for the first sync slice."""

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._open_json(request)

    def get_json(
        self,
        url: str,
        *,
        access_token: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        return self._open_json(request)

    def _open_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as json_error:
                raise OutlookRemoteError(f"HTTP {exc.code}: {body}") from json_error
            error = payload.get("error")
            description = payload.get("error_description") or payload.get("message")
            raise OutlookAuthError(f"{error}: {description}") from exc
        except urllib.error.URLError as exc:
            raise OutlookRemoteError(str(exc)) from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise OutlookRemoteError("Remote service returned non-JSON response.") from exc


class OutlookAuthStartResult(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str


class OutlookAuthCompleteResult(BaseModel):
    status: str
    expires_at: int | None = None
    error: str | None = None


class OutlookSyncResult(BaseModel):
    account_id: str
    folder: str
    imported_messages: int
    imported_attachments: int
    status: str = "completed"
    next_link: str | None = None
    delta_link: str | None = None
    sync_mode: str = "delta"


class OutlookService:
    """Read-only Microsoft Graph integration for Outlook mail."""

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        mail_service: MailService,
        local_config: LocalAppConfig,
        transport: GraphHTTPTransport | None = None,
    ) -> None:
        self._conn_factory = conn_factory
        self._mail_service = mail_service
        self._local_config = local_config
        self._transport = transport or UrllibGraphHTTPTransport()

    def start_device_auth(self) -> OutlookAuthStartResult:
        config = self._outlook_config()
        payload = self._transport.post_form(
            self._identity_url(config, "devicecode"),
            {
                "client_id": self._client_id(config),
                "scope": " ".join(config.scopes),
            },
        )
        return OutlookAuthStartResult(
            device_code=str(payload["device_code"]),
            user_code=str(payload["user_code"]),
            verification_uri=str(payload.get("verification_uri") or payload["verification_url"]),
            expires_in=int(payload["expires_in"]),
            interval=int(payload.get("interval", 5)),
            message=str(payload["message"]),
        )

    def complete_device_auth(self, *, device_code: str) -> OutlookAuthCompleteResult:
        config = self._outlook_config()
        try:
            token_payload = self._transport.post_form(
                self._identity_url(config, "token"),
                {
                    "client_id": self._client_id(config),
                    "grant_type": DEVICE_CODE_GRANT,
                    "device_code": device_code,
                },
            )
        except OutlookAuthError as exc:
            message = str(exc)
            if message.startswith("authorization_pending"):
                return OutlookAuthCompleteResult(status="pending", error="authorization_pending")
            raise

        expires_at = self._store_token_payload(config, token_payload)
        return OutlookAuthCompleteResult(status="authorized", expires_at=expires_at)

    def sync_messages(
        self,
        *,
        folder: str | None = None,
        limit: int = 25,
        max_pages: int = 1,
    ) -> OutlookSyncResult:
        config = self._outlook_config()
        access_token = self._valid_access_token(config)
        account = self._load_account(access_token)
        sync_folder = folder or config.sync_folder
        sync_state = self._load_sync_state(
            provider="outlook",
            account_id=_stable_account_id(account),
            folder=sync_folder,
        )
        sync_payload = self._fetch_delta_messages(
            access_token=access_token,
            folder=sync_folder,
            limit=limit,
            max_pages=max_pages,
            sync_state=sync_state,
        )
        import_result = self._mail_service.import_messages(
            account=account,
            messages=sync_payload["messages"],
        )
        result = OutlookSyncResult(
            account_id=import_result.account_id,
            folder=sync_folder,
            imported_messages=import_result.imported_messages,
            imported_attachments=import_result.imported_attachments,
            next_link=sync_payload["next_link"],
            delta_link=sync_payload["delta_link"],
        )
        self._record_sync_state(result)
        return result

    def _outlook_config(self) -> OutlookMailConfig:
        config = self._local_config.mail.outlook
        if config.auth_method != "device_code":
            raise OutlookConfigError("Only Outlook device_code auth_method is supported.")
        self._client_id(config)
        return config

    def _client_id(self, config: OutlookMailConfig) -> str:
        client_id = config.resolved_client_id()
        if not client_id:
            raise OutlookConfigError(
                f"Outlook client id is missing. Set environment variable {config.client_id_env}."
            )
        return client_id

    def _identity_url(self, config: OutlookMailConfig, action: str) -> str:
        tenant_id = urllib.parse.quote(config.tenant_id.strip() or "consumers", safe="")
        return f"{LOGIN_BASE_URL}/{tenant_id}/oauth2/v2.0/{action}"

    def _token_store_path(self, config: OutlookMailConfig) -> Path:
        return config.token_store_path.expanduser()

    def _read_token_payload(self, config: OutlookMailConfig) -> dict[str, Any]:
        token_path = self._token_store_path(config)
        if not token_path.exists():
            raise OutlookConfigError("Outlook token is missing. Run device auth first.")
        with token_path.open("r", encoding="utf-8") as token_file:
            return json.load(token_file)

    def _store_token_payload(self, config: OutlookMailConfig, token_payload: dict[str, Any]) -> int:
        expires_at = _now_epoch() + int(token_payload.get("expires_in", 3600)) - 60
        stored_payload = dict(token_payload)
        stored_payload["expires_at"] = expires_at
        token_path = self._token_store_path(config)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with token_path.open("w", encoding="utf-8") as token_file:
            json.dump(stored_payload, token_file, ensure_ascii=False, indent=2, sort_keys=True)
        return expires_at

    def _valid_access_token(self, config: OutlookMailConfig) -> str:
        token_payload = self._read_token_payload(config)
        expires_at = int(token_payload.get("expires_at", 0))
        if expires_at <= _now_epoch():
            token_payload = self._refresh_token(config, token_payload)
        access_token = token_payload.get("access_token")
        if not access_token:
            raise OutlookConfigError("Stored Outlook token does not contain access_token.")
        return str(access_token)

    def _refresh_token(
        self,
        config: OutlookMailConfig,
        token_payload: dict[str, Any],
    ) -> dict[str, Any]:
        refresh_token = token_payload.get("refresh_token")
        if not refresh_token:
            raise OutlookConfigError("Stored Outlook token is expired and has no refresh_token.")
        refreshed = self._transport.post_form(
            self._identity_url(config, "token"),
            {
                "client_id": self._client_id(config),
                "grant_type": "refresh_token",
                "refresh_token": str(refresh_token),
                "scope": " ".join(config.scopes),
            },
        )
        if "refresh_token" not in refreshed:
            refreshed["refresh_token"] = refresh_token
        self._store_token_payload(config, refreshed)
        return refreshed

    def _load_account(self, access_token: str) -> MailAccountInput:
        payload = self._transport.get_json(
            f"{GRAPH_BASE_URL}/me?$select=mail,userPrincipalName,displayName",
            access_token=access_token,
        )
        email_address = payload.get("mail") or payload.get("userPrincipalName")
        if not email_address:
            raise OutlookRemoteError("Microsoft Graph /me response did not include an email address.")
        return MailAccountInput(
            provider="outlook",
            email_address=str(email_address),
            display_name=payload.get("displayName"),
        )

    def _fetch_delta_messages(
        self,
        *,
        access_token: str,
        folder: str,
        limit: int,
        max_pages: int,
        sync_state: dict[str, str | None] | None,
    ) -> dict[str, Any]:
        messages: list[MailMessageInput] = []
        next_link = sync_state.get("next_link") if sync_state else None
        delta_link = sync_state.get("delta_link") if sync_state else None
        page_url = next_link or delta_link or self._messages_delta_url(
            folder=folder,
            limit=limit,
        )
        latest_next_link: str | None = None
        latest_delta_link: str | None = None
        for _ in range(max_pages):
            if not page_url:
                break
            payload = self._transport.get_json(
                page_url,
                access_token=access_token,
                headers={"Prefer": 'outlook.body-content-type="text"'},
            )
            for raw_message in payload.get("value", []):
                if isinstance(raw_message, dict) and "@removed" not in raw_message:
                    messages.append(self._map_message(access_token, folder, raw_message))
            latest_next_link = payload.get("@odata.nextLink")
            latest_delta_link = payload.get("@odata.deltaLink")
            page_url = latest_next_link
        return {
            "messages": messages,
            "next_link": latest_next_link,
            "delta_link": latest_delta_link or (None if latest_next_link else delta_link),
        }

    def _fetch_messages(
        self,
        *,
        access_token: str,
        folder: str,
        limit: int,
        max_pages: int,
    ) -> list[MailMessageInput]:
        messages: list[MailMessageInput] = []
        page_url: str | None = self._messages_url(folder=folder, limit=limit)
        for _ in range(max_pages):
            if not page_url:
                break
            payload = self._transport.get_json(
                page_url,
                access_token=access_token,
                headers={"Prefer": 'outlook.body-content-type="text"'},
            )
            for raw_message in payload.get("value", []):
                messages.append(self._map_message(access_token, folder, raw_message))
            page_url = payload.get("@odata.nextLink")
        return messages

    def _messages_delta_url(self, *, folder: str, limit: int) -> str:
        encoded_folder = urllib.parse.quote(folder, safe="")
        query = urllib.parse.urlencode(
            {
                "$top": str(limit),
                "$select": ",".join(
                    [
                        "id",
                        "subject",
                        "from",
                        "toRecipients",
                        "ccRecipients",
                        "receivedDateTime",
                        "body",
                        "hasAttachments",
                    ]
                ),
            }
        )
        return f"{GRAPH_BASE_URL}/me/mailFolders/{encoded_folder}/messages/delta?{query}"

    def _messages_url(self, *, folder: str, limit: int) -> str:
        encoded_folder = urllib.parse.quote(folder, safe="")
        query = urllib.parse.urlencode(
            {
                "$top": str(limit),
                "$select": ",".join(
                    [
                        "id",
                        "subject",
                        "from",
                        "toRecipients",
                        "ccRecipients",
                        "receivedDateTime",
                        "body",
                        "hasAttachments",
                    ]
                ),
                "$orderby": "receivedDateTime desc",
            }
        )
        return f"{GRAPH_BASE_URL}/me/mailFolders/{encoded_folder}/messages?{query}"

    def _map_message(
        self,
        access_token: str,
        folder: str,
        raw_message: dict[str, Any],
    ) -> MailMessageInput:
        external_id = str(raw_message["id"])
        return MailMessageInput(
            external_id=external_id,
            folder=folder,
            subject=str(raw_message.get("subject") or ""),
            sender=self._email_address(raw_message.get("from")),
            to=self._recipient_addresses(raw_message.get("toRecipients")),
            cc=self._recipient_addresses(raw_message.get("ccRecipients")),
            received_at=raw_message.get("receivedDateTime"),
            body_text=str((raw_message.get("body") or {}).get("content") or ""),
            attachments=self._fetch_attachment_metadata(access_token, external_id)
            if raw_message.get("hasAttachments")
            else [],
        )

    def _fetch_attachment_metadata(
        self,
        access_token: str,
        message_external_id: str,
    ) -> list[MailAttachmentInput]:
        encoded_message_id = urllib.parse.quote(message_external_id, safe="")
        query = urllib.parse.urlencode(
            {"$select": "id,name,contentType,size,isInline"}
        )
        payload = self._transport.get_json(
            f"{GRAPH_BASE_URL}/me/messages/{encoded_message_id}/attachments?{query}",
            access_token=access_token,
        )
        return [
            MailAttachmentInput(
                external_id=str(attachment["id"]),
                name=str(attachment.get("name") or "unnamed_attachment"),
                content_type=attachment.get("contentType"),
                size=attachment.get("size"),
            )
            for attachment in payload.get("value", [])
        ]

    def _email_address(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        email_payload = payload.get("emailAddress") or {}
        if not isinstance(email_payload, dict):
            return ""
        return str(email_payload.get("address") or email_payload.get("name") or "")

    def _recipient_addresses(self, payload: Any) -> list[str]:
        if not isinstance(payload, list):
            return []
        return [address for item in payload if (address := self._email_address(item))]

    def _record_sync_state(self, result: OutlookSyncResult) -> None:
        conn = self._conn_factory()
        try:
            conn.execute(
                """
                INSERT INTO mail_sync_state(
                    provider, account_id, folder, status, last_sync_at,
                    next_link, delta_link, last_result_payload
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, account_id, folder) DO UPDATE SET
                    status=excluded.status,
                    last_sync_at=excluded.last_sync_at,
                    next_link=excluded.next_link,
                    delta_link=excluded.delta_link,
                    last_result_payload=excluded.last_result_payload
                """,
                (
                    "outlook",
                    result.account_id,
                    result.folder,
                    result.status,
                    _now_iso(),
                    result.next_link,
                    result.delta_link,
                    result.model_dump_json(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _load_sync_state(
        self,
        *,
        provider: str,
        account_id: str,
        folder: str,
    ) -> dict[str, str | None] | None:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                SELECT next_link, delta_link
                FROM mail_sync_state
                WHERE provider = ? AND account_id = ? AND folder = ?
                """,
                (provider, account_id, folder),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "next_link": row["next_link"],
            "delta_link": row["delta_link"],
        }


def _stable_account_id(account: MailAccountInput) -> str:
    digest = sha1(f"{account.provider}|{account.email_address.lower()}".encode("utf-8"))
    return f"mail_account_{digest.hexdigest()[:12]}"
