"""Credential-separated, bounded calls to the existing local message contract."""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import ConnectorConfig, local_backend_url
from .models import CapturePolicy


class BackendUnavailable(RuntimeError):
    """Contains a fixed code, never a response body, token or connection URL."""


class BackendClient:
    def __init__(self, config: ConnectorConfig, *, transport=None):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=local_backend_url(config.backend_url),
            timeout=15,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        try:
            content = (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                if payload is not None
                else None
            )
            headers = {"Authorization": f"Bearer {token}"}
            if content is not None:
                headers["Content-Type"] = "application/json"
            async with self._client.stream(
                method, path, content=content, params=params, headers=headers
            ) as reply:
                if reply.status_code != 200:
                    raise BackendUnavailable("backend_http_error")
                body = bytearray()
                async for chunk in reply.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise BackendUnavailable("backend_response_too_large")
            result = json.loads(body)
            if not isinstance(result, dict):
                raise BackendUnavailable("invalid_backend_response")
            return result
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise BackendUnavailable("backend_unavailable") from None

    async def policies(self) -> list[CapturePolicy]:
        result = await self._request(
            "GET", "/integrations/messages/policies", token=self.config.import_token
        )
        try:
            if not isinstance(result.get("policies"), list):
                raise ValueError("invalid_policy_list")  # noqa: TRY004 -- wire protocol error
            policies = [CapturePolicy.model_validate(row) for row in result["policies"]]
            if len({p.key for p in policies}) != len(policies):
                raise ValueError("duplicate_policy")
            return policies
        except ValueError:
            raise BackendUnavailable("invalid_backend_policies") from None

    async def import_messages(self, rows: list[dict]) -> dict:
        return await self._request(
            "POST",
            "/integrations/messages/import",
            token=self.config.import_token,
            payload={"schema_version": 2, "messages": rows},
        )

    async def import_media(self, rows: list[dict]) -> dict:
        return await self._request(
            "POST",
            "/integrations/messages/media",
            token=self.config.import_token,
            payload={"schema_version": 2, "media": rows},
        )

    async def analysis(
        self,
        conversation_key: str,
        view: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
        sender_id: str | None = None,
    ) -> dict:
        # Derived results require management/read credentials; import authority
        # must never be promoted to body/analysis reading or control authority.
        if not self.config.api_token:
            raise PermissionError("analysis_access_not_configured")
        views = {
            "summary",
            "coverage",
            "facts",
            "digest",
            "overview",
            "topics",
            "insights",
            "participants",
            "dossiers",
            "focus",
            "participant",
            "dossier",
        }
        if view not in views:
            raise ValueError("invalid_analysis_view")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_analysis_limit")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
            raise ValueError("invalid_analysis_cursor")
        if (
            not conversation_key.startswith("message_conversation_")
            or len(conversation_key) != 85
            or any(c not in "0123456789abcdef" for c in conversation_key[21:])
        ):
            raise ValueError("invalid_conversation_key")
        params = {}
        if view in {"summary", "coverage", "facts", "digest"}:
            path = f"/messages/conversations/{conversation_key}/{view}"
            if view == "facts":
                params["limit"] = limit
        elif view == "focus":
            path = f"/messages/reading/focus/{conversation_key}"
        elif view in {"participant", "dossier"}:
            from urllib.parse import quote

            if (
                not sender_id
                or len(sender_id) > 512
                or sender_id in {".", ".."}
                or any(c in sender_id for c in "/\\?#")
            ):
                raise ValueError("invalid_participant_id")
            path = f"/messages/reading/{view}s/{conversation_key}/{quote(sender_id, safe='')}"
        else:
            path = f"/messages/reading/{view}"
            params["conversation_key"] = conversation_key
            if view != "overview":
                params["limit"] = limit
                if cursor is not None:
                    params["cursor"] = cursor
        return await self._request("GET", path, token=self.config.api_token, params=params)
