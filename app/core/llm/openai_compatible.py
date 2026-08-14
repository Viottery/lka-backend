"""Async OpenAI-compatible chat completions client."""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from typing import Any

from app.core.llm.errors import (
    LLMAuthenticationError,
    LLMClientError,
    LLMNetworkError,
    LLMProviderHTTPError,
    LLMRateLimitError,
    LLMResponseParseError,
    LLMTimeoutError,
)
from app.core.llm.models import LLMRequest, LLMResponse, LLMResponseMode, LLMStreamEvent


class OpenAICompatibleLLMClient:
    """OpenAI-compatible client with async wrappers around stdlib HTTP calls."""

    def __init__(
        self,
        *,
        name: str,
        provider_name: str,
        base_url: str,
        api_key: str | None,
        default_model: str,
        available_models: list[str] | None = None,
        timeout_seconds: int = 60,
        supports_stream: bool = True,
        supports_json_mode: bool = False,
    ) -> None:
        if not api_key:
            raise LLMClientError(f"Missing API key for LLM client: {name}")
        self.name = name
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.available_models = available_models or [default_model]
        self.timeout_seconds = timeout_seconds
        self.supports_stream = supports_stream
        self.supports_json_mode = supports_json_mode

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response_payload = await asyncio.to_thread(
            self._post_chat_completion,
            request,
            False,
        )
        content, usage = self._message_content(response_payload)
        return LLMResponse(
            provider=self.provider_name,
            client_name=self.name,
            model=self._model_for(request),
            status="completed",
            content=content,
            prompt_summary=request.prompt_summary,
            response_mode=request.response_mode,
            usage=usage,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        if not self.supports_stream:
            response = await self.complete(
                request.model_copy(update={"response_mode": LLMResponseMode.TEXT})
            )
            yield self._stream_event("llm_completed", request, response.content)
            return

        response = await asyncio.to_thread(self._open_stream, request)
        snapshot = ""
        yield self._stream_event("llm_started", request, snapshot)
        try:
            while True:
                line = await asyncio.to_thread(response.readline)
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text or not text.startswith("data:"):
                    continue
                data = text.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                delta = self._stream_delta(data)
                if not delta:
                    continue
                snapshot += delta
                yield self._stream_event("llm_delta", request, snapshot, delta=delta)
        finally:
            response.close()

        yield self._stream_event("llm_completed", request, snapshot)

    def _post_chat_completion(self, request: LLMRequest, stream: bool) -> dict[str, Any]:
        http_request = self._build_request(request, stream=stream)
        try:
            with urllib.request.urlopen(
                http_request,
                timeout=self.timeout_seconds,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
        except (TimeoutError, socket.timeout) as exc:
            raise LLMTimeoutError(f"LLM provider request timed out: {exc}") from exc
        except urllib.error.URLError as exc:
            raise LLMNetworkError(f"LLM provider network request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LLMResponseParseError(f"LLM provider returned invalid JSON: {exc}") from exc
        raise LLMResponseParseError("LLM provider returned no response.")

    def _open_stream(self, request: LLMRequest):
        http_request = self._build_request(request, stream=True)
        try:
            return urllib.request.urlopen(http_request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
        except (TimeoutError, socket.timeout) as exc:
            raise LLMTimeoutError(f"LLM provider stream timed out: {exc}") from exc
        except urllib.error.URLError as exc:
            raise LLMNetworkError(f"LLM provider stream failed: {exc}") from exc
        raise LLMResponseParseError("LLM provider stream returned no response.")

    def _build_request(self, request: LLMRequest, *, stream: bool) -> urllib.request.Request:
        payload: dict[str, Any] = {
            "model": self._model_for(request),
            "messages": [message.model_dump() for message in request.messages],
            "temperature": request.temperature,
            "stream": stream,
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.require_json and self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}

        return urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

    def _model_for(self, request: LLMRequest) -> str:
        return request.model or self.default_model

    def _message_content(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            message = payload["choices"][0]["message"]
            content = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseParseError(
                "LLM provider response did not include message content."
            ) from exc
        usage = payload.get("usage")
        return content, usage if isinstance(usage, dict) else {}

    def _stream_delta(self, data: str) -> str:
        try:
            payload = json.loads(data)
            delta = payload["choices"][0].get("delta") or {}
            content = delta.get("content")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            return ""
        return content if isinstance(content, str) else ""

    def _stream_event(
        self,
        event_type: str,
        request: LLMRequest,
        snapshot: str,
        *,
        delta: str = "",
    ) -> LLMStreamEvent:
        return LLMStreamEvent(
            event_type=event_type,  # type: ignore[arg-type]
            stage=str(request.metadata.get("stage") or "llm"),
            client_name=self.name,
            provider=self.provider_name,
            model=self._model_for(request),
            delta=delta,
            content_snapshot=snapshot,
        )

    def _raise_http_error(self, exc: urllib.error.HTTPError) -> None:
        error_body = exc.read().decode("utf-8", errors="replace")
        message = f"LLM provider returned HTTP {exc.code}: {error_body}"
        retry_after = exc.headers.get("Retry-After")
        if exc.code == 429:
            raise LLMRateLimitError(
                status_code=exc.code,
                message=message,
                retry_after=retry_after,
            ) from exc
        if exc.code in {401, 403}:
            raise LLMAuthenticationError(
                status_code=exc.code,
                message=message,
                retry_after=retry_after,
            ) from exc
        raise LLMProviderHTTPError(
            status_code=exc.code,
            message=message,
            retry_after=retry_after,
        ) from exc
