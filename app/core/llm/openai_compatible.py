"""Async OpenAI-compatible chat completions client."""

from __future__ import annotations

import asyncio
import json
import queue
import socket
import threading
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
from app.core.llm.audit import (
    audit_headers,
    classify_provider_error,
    parse_error_body,
    provider_request_id_from_headers,
    retry_after_from_headers,
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
        response_payload, response_headers = await _run_blocking(
            self._post_chat_completion,
            request,
            False,
        )
        content, usage, finish_reason = self._message_content(response_payload)
        headers = audit_headers(response_headers)
        return LLMResponse(
            provider=self.provider_name,
            client_name=self.name,
            model=self._model_for(request),
            status="completed",
            content=content,
            prompt_summary=request.prompt_summary,
            response_mode=request.response_mode,
            usage=usage,
            finish_reason=finish_reason,
            provider_request_id=provider_request_id_from_headers(headers),
            metadata={"headers": headers},
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        if not self.supports_stream:
            response = await self.complete(
                request.model_copy(update={"response_mode": LLMResponseMode.TEXT})
            )
            yield self._stream_event("llm_completed", request, response.content)
            return

        response = await _run_blocking(self._open_stream, request)
        snapshot = ""
        yield self._stream_event("llm_started", request, snapshot)
        try:
            while True:
                line = await _run_blocking(response.readline)
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

    def _post_chat_completion(
        self,
        request: LLMRequest,
        stream: bool,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        http_request = self._build_request(request, stream=stream)
        try:
            with urllib.request.urlopen(
                http_request,
                timeout=self.timeout_seconds,
            ) as response:
                headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
                return json.loads(response.read().decode("utf-8")), headers
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

    def _message_content(
        self,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str | None]:
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseParseError(
                "LLM provider response did not include message content."
            ) from exc
        usage = payload.get("usage")
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        return content, usage if isinstance(usage, dict) else {}, finish_reason

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
        headers = audit_headers(exc.headers)
        retry_after = retry_after_from_headers(headers)
        error_info = parse_error_body(error_body)
        category, retriable = classify_provider_error(
            http_status=exc.code,
            provider_error_type=error_info.get("type")
            if isinstance(error_info.get("type"), str)
            else None,
            provider_error_code=error_info.get("code")
            if isinstance(error_info.get("code"), str)
            else None,
        )
        common_kwargs = {
            "status_code": exc.code,
            "message": message,
            "retry_after": retry_after,
            "headers": headers,
            "error_body": error_body,
            "provider_error_type": error_info.get("type")
            if isinstance(error_info.get("type"), str)
            else None,
            "provider_error_code": error_info.get("code")
            if isinstance(error_info.get("code"), str)
            else None,
            "provider_error_param": error_info.get("param")
            if isinstance(error_info.get("param"), str)
            else None,
            "provider_error_event_id": error_info.get("event_id")
            if isinstance(error_info.get("event_id"), str)
            else None,
            "error_category": category,
            "is_retriable": retriable,
        }
        if exc.code == 429:
            raise LLMRateLimitError(
                **common_kwargs,
            ) from exc
        if exc.code in {401, 403}:
            raise LLMAuthenticationError(
                **common_kwargs,
            ) from exc
        raise LLMProviderHTTPError(**common_kwargs) from exc


async def _run_blocking(func, *args):
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            result_queue.put((True, func(*args)))
        except BaseException as exc:
            result_queue.put((False, exc))

    threading.Thread(target=target, name="lka-llm-http", daemon=True).start()
    while True:
        try:
            ok, value = result_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue
        if ok:
            return value
        raise value
