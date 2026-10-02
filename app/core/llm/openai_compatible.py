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

from app.core.llm.audit import (
    audit_headers,
    classify_provider_error,
    parse_error_body,
    provider_request_id_from_headers,
    retry_after_from_headers,
)
from app.core.llm.errors import (
    LLMAuthenticationError,
    LLMClientError,
    LLMNetworkError,
    LLMProviderHTTPError,
    LLMRateLimitError,
    LLMResponseParseError,
    LLMTimeoutError,
)
from app.core.llm.models import (
    LLMReasoningEffort,
    LLMRequest,
    LLMResponse,
    LLMResponseMode,
    LLMStreamEvent,
    LLMToolCall,
)


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
        supports_function_calling: bool = False,
        supports_required_tool_choice: bool = False,
        function_calling_strict: bool = False,
        supports_reasoning_effort: bool = False,
        thinking_control: str | None = None,
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
        self.supports_function_calling = supports_function_calling
        self.supports_required_tool_choice = supports_required_tool_choice
        self.function_calling_strict = function_calling_strict
        self.supports_reasoning_effort = supports_reasoning_effort
        self.thinking_control = thinking_control

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response_payload, response_headers = await _run_blocking(
            self._post_chat_completion,
            request,
            False,
        )
        content, tool_calls, usage, finish_reason = self._message_content(response_payload)
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
            tool_calls=tool_calls,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        if not self.supports_stream:
            response = await self.complete(
                request.model_copy(update={"response_mode": LLMResponseMode.TEXT})
            )
            yield self._stream_event(
                "llm_completed",
                request,
                response.content,
                metadata=self._metadata_from_response(response),
            )
            return

        response = await _run_blocking(self._open_stream, request)
        snapshot = ""
        tool_call_parts: dict[int, dict[str, str]] = {}
        headers = audit_headers(getattr(response, "headers", {}))
        stream_metadata: dict[str, Any] = {"headers": headers}
        provider_request_id = provider_request_id_from_headers(headers)
        if provider_request_id:
            stream_metadata["provider_request_id"] = provider_request_id
        yield self._stream_event("llm_started", request, snapshot, metadata=stream_metadata)
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
                try:
                    payload = self._stream_payload(data)
                except LLMResponseParseError as exc:
                    yield self._stream_event(
                        "llm_failed",
                        request,
                        snapshot,
                        error=str(exc),
                        metadata={
                            **stream_metadata,
                            "error_event": self._stream_parse_error_event(str(exc), data),
                            "partial_content": snapshot,
                        },
                    )
                    return
                error_event = self._stream_error_event(payload)
                if error_event is not None:
                    yield self._stream_event(
                        "llm_failed",
                        request,
                        snapshot,
                        error=self._stream_error_message(error_event),
                        metadata={
                            **stream_metadata,
                            "error_event": error_event,
                            "partial_content": snapshot,
                        },
                    )
                    return
                delta, tool_call_deltas, chunk_metadata = self._stream_delta_and_metadata(payload)
                stream_metadata.update(chunk_metadata)
                for tool_call_delta in tool_call_deltas:
                    index = int(tool_call_delta["index"])
                    part = tool_call_parts.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    for key in ("id", "name", "arguments"):
                        if tool_call_delta[key]:
                            part[key] += tool_call_delta[key]
                if not delta and not tool_call_deltas:
                    continue
                snapshot += delta
                yield self._stream_event(
                    "llm_delta",
                    request,
                    snapshot,
                    delta=delta,
                    metadata={
                        **stream_metadata,
                        "tool_calls": self._tool_calls_from_parts(tool_call_parts),
                    },
                )
        finally:
            response.close()

        yield self._stream_event(
            "llm_completed",
            request,
            snapshot,
            metadata={
                **stream_metadata,
                "tool_calls": self._tool_calls_from_parts(tool_call_parts),
            },
        )

    def _post_chat_completion(
        self,
        request: LLMRequest,
        stream: bool,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        http_request = self._build_request(request, stream=stream)
        try:
            with urllib.request.urlopen(
                http_request,
                timeout=self._request_timeout(request),
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

    def _request_timeout(self, request: LLMRequest) -> float:
        value = request.metadata.get("network_timeout_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= self.timeout_seconds:
            return float(value)
        return self.timeout_seconds

    def _open_stream(self, request: LLMRequest):
        http_request = self._build_request(request, stream=True)
        try:
            return urllib.request.urlopen(http_request, timeout=self._request_timeout(request))
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
        except (TimeoutError, socket.timeout) as exc:
            raise LLMTimeoutError(f"LLM provider stream timed out: {exc}") from exc
        except urllib.error.URLError as exc:
            raise LLMNetworkError(f"LLM provider stream failed: {exc}") from exc
        raise LLMResponseParseError("LLM provider stream returned no response.")

    def _build_request(self, request: LLMRequest, *, stream: bool) -> urllib.request.Request:
        if request.reasoning_effort is not None and not self.supports_reasoning_effort:
            raise LLMClientError(
                f"LLM client {self.name} does not support reasoning_effort."
            )
        payload: dict[str, Any] = {
            "model": self._model_for(request),
            "messages": [message.model_dump() for message in request.messages],
            "temperature": request.temperature,
            "stream": stream,
        }
        if request.reasoning_effort is not None:
            payload["reasoning_effort"] = LLMReasoningEffort(request.reasoning_effort).value
        if request.thinking_enabled is not None:
            if self.thinking_control != "deepseek":
                raise LLMClientError(f"LLM client {self.name} does not support thinking control.")
            payload["thinking"] = {"type": "enabled" if request.thinking_enabled else "disabled"}
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.require_json and self.supports_json_mode and not request.tools:
            payload["response_format"] = {"type": "json_object"}
        if request.tools and self.supports_function_calling:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                        **({"strict": True} if tool.strict and self.function_calling_strict else {}),
                    },
                }
                for tool in request.tools
            ]
            payload["tool_choice"] = request.tool_choice or "auto"

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
    ) -> tuple[str, list[LLMToolCall], dict[str, Any], str | None]:
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
        return (
            content,
            self._tool_calls_from_payload(message.get("tool_calls")),
            usage if isinstance(usage, dict) else {},
            finish_reason,
        )

    def _stream_payload(self, data: str) -> dict[str, Any]:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise LLMResponseParseError(
                f"LLM provider returned invalid stream JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise LLMResponseParseError("LLM provider stream chunk was not a JSON object.")
        return payload

    def _stream_error_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        error = payload.get("error")
        if isinstance(error, dict):
            return payload
        if payload.get("type") == "error":
            return payload
        return None

    def _stream_error_message(self, error_event: dict[str, Any]) -> str:
        error = error_event.get("error") if isinstance(error_event.get("error"), dict) else {}
        message = error.get("message") if isinstance(error.get("message"), str) else None
        if message:
            return message
        return "LLM provider stream returned an error event."

    def _stream_parse_error_event(self, message: str, raw_data: str) -> dict[str, Any]:
        return {
            "type": "error",
            "error": {
                "type": "response_parse_error",
                "code": "invalid_stream_json",
                "message": message,
            },
            "raw_data_preview": raw_data[:500],
        }

    def _stream_delta_and_metadata(
        self, payload: dict[str, Any]
    ) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
        metadata: dict[str, Any] = {}
        usage = payload.get("usage")
        if isinstance(usage, dict):
            metadata["usage"] = usage
        choices = payload.get("choices")
        if choices == [] and metadata:
            return "", [], metadata
        if not isinstance(choices, list) or not choices:
            raise LLMResponseParseError("LLM provider stream chunk did not include choices.")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LLMResponseParseError("LLM provider stream choice was not an object.")
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str):
            metadata["finish_reason"] = finish_reason
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise LLMResponseParseError("LLM provider stream delta was not an object.")
        content = delta.get("content")
        tool_call_deltas: list[dict[str, str]] = []
        raw_tool_calls = delta.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for position, raw_call in enumerate(raw_tool_calls):
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function")
                function = function if isinstance(function, dict) else {}
                index = raw_call.get("index")
                tool_call_deltas.append(
                    {
                        "index": str(index if isinstance(index, int) else position),
                        "id": raw_call.get("id") if isinstance(raw_call.get("id"), str) else "",
                        "name": function.get("name") if isinstance(function.get("name"), str) else "",
                        "arguments": function.get("arguments") if isinstance(function.get("arguments"), str) else "",
                    }
                )
        return content if isinstance(content, str) else "", tool_call_deltas, metadata

    @staticmethod
    def _tool_calls_from_payload(raw_calls: Any) -> list[LLMToolCall]:
        if not isinstance(raw_calls, list):
            return []
        calls: list[LLMToolCall] = []
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                continue
            raw_arguments = function.get("arguments")
            raw_arguments = raw_arguments if isinstance(raw_arguments, str) else ""
            try:
                arguments = json.loads(raw_arguments) if raw_arguments else {}
            except json.JSONDecodeError:
                arguments = None
            calls.append(
                LLMToolCall(
                    id=raw_call.get("id") if isinstance(raw_call.get("id"), str) else None,
                    name=function["name"],
                    arguments=arguments if isinstance(arguments, dict) else None,
                    raw_arguments=raw_arguments,
                )
            )
        return calls

    def _tool_calls_from_parts(self, parts: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
        raw_calls = [
            {
                "id": part["id"] or None,
                "function": {"name": part["name"], "arguments": part["arguments"]},
            }
            for _, part in sorted(parts.items())
        ]
        return [call.model_dump(mode="json") for call in self._tool_calls_from_payload(raw_calls)]

    def _stream_event(
        self,
        event_type: str,
        request: LLMRequest,
        snapshot: str,
        *,
        delta: str = "",
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMStreamEvent:
        return LLMStreamEvent(
            event_type=event_type,  # type: ignore[arg-type]
            stage=str(request.metadata.get("stage") or "llm"),
            client_name=self.name,
            provider=self.provider_name,
            model=self._model_for(request),
            delta=delta,
            content_snapshot=snapshot,
            error=error,
            metadata=metadata or {},
        )

    def _metadata_from_response(self, response: LLMResponse) -> dict[str, Any]:
        metadata = dict(response.metadata)
        if response.provider_request_id:
            metadata["provider_request_id"] = response.provider_request_id
        if response.finish_reason:
            metadata["finish_reason"] = response.finish_reason
        if response.usage:
            metadata["usage"] = response.usage
        return metadata

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
