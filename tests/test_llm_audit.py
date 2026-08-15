from __future__ import annotations

from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import asyncio
import json

import pytest

from app.core.llm.audit import (
    build_stream_error_call_record,
    classify_openai_sdk_exception,
    classify_provider_error,
    parse_error_body,
)
from app.core.llm.errors import (
    LLMAuthenticationError,
    LLMProviderHTTPError,
    LLMRateLimitError,
)
from app.core.llm.openai_compatible import OpenAICompatibleLLMClient
from app.core.llm.models import LLMMessage, LLMRequest


@pytest.mark.parametrize(
    ("status_code", "category", "is_retriable"),
    [
        (400, "bad_request", False),
        (401, "authentication_failed", False),
        (403, "permission_denied", False),
        (404, "not_found", False),
        (408, "timeout", True),
        (409, "conflict", True),
        (418, "provider_http_error", False),
        (422, "unprocessable_entity", False),
        (429, "rate_limited", True),
        (500, "provider_internal_error", True),
        (503, "provider_internal_error", True),
    ],
)
def test_provider_http_status_maps_to_canonical_category(
    status_code,
    category,
    is_retriable,
):
    assert classify_provider_error(http_status=status_code) == (
        category,
        is_retriable,
    )


@pytest.mark.parametrize(
    ("exception_name", "category", "is_retriable"),
    [
        ("BadRequestError", "bad_request", False),
        ("AuthenticationError", "authentication_failed", False),
        ("PermissionDeniedError", "permission_denied", False),
        ("NotFoundError", "not_found", False),
        ("ConflictError", "conflict", True),
        ("UnprocessableEntityError", "unprocessable_entity", False),
        ("RateLimitError", "rate_limited", True),
        ("InternalServerError", "provider_internal_error", True),
        ("APIConnectionError", "network_failed", True),
        ("APITimeoutError", "timeout", True),
        ("APIResponseValidationError", "response_validation_failed", False),
        ("LengthFinishReasonError", "output_length_limit", False),
        ("ContentFilterFinishReasonError", "content_filter", False),
        ("OAuthError", "oauth_failed", False),
        ("SubjectTokenProviderError", "oauth_subject_token_failed", False),
        ("InvalidWebhookSignatureError", "invalid_webhook_signature", False),
        ("WebSocketConnectionClosedError", "websocket_closed", True),
        ("WebSocketQueueFullError", "websocket_queue_full", True),
        ("APIError", "provider_error", False),
        ("OpenAIError", "provider_error", False),
    ],
)
def test_openai_sdk_exception_name_maps_to_canonical_category(
    exception_name,
    category,
    is_retriable,
):
    exc_type = type(exception_name, (Exception,), {})
    assert classify_openai_sdk_exception(exc_type("boom")) == (
        category,
        is_retriable,
    )


@pytest.mark.parametrize(
    ("status_code", "category", "is_retriable"),
    [
        (408, "timeout", True),
        (409, "conflict", True),
        (429, "rate_limited", True),
        (500, "provider_internal_error", True),
        (418, "provider_http_error", False),
    ],
)
def test_openai_sdk_api_status_error_uses_status_code(
    status_code,
    category,
    is_retriable,
):
    class APIStatusError(Exception):
        def __init__(self):
            super().__init__("status error")
            self.status_code = status_code

    assert classify_openai_sdk_exception(APIStatusError()) == (
        category,
        is_retriable,
    )


def test_http_error_body_parser_extracts_openai_compatible_fields():
    parsed = parse_error_body(
        {
            "error": {
                "type": "invalid_request_error",
                "code": "missing_required_parameter",
                "message": "Missing input.",
                "param": "messages",
                "event_id": "evt_123",
            }
        }
    )

    assert parsed["type"] == "invalid_request_error"
    assert parsed["code"] == "missing_required_parameter"
    assert parsed["message"] == "Missing input."
    assert parsed["param"] == "messages"
    assert parsed["event_id"] == "evt_123"


def test_openai_compatible_http_error_preserves_headers_and_error_body_fields():
    client = OpenAICompatibleLLMClient(
        name="test-client",
        provider_name="openai-compatible",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        default_model="test-model",
    )
    exc = _http_error(
        status=429,
        body=(
            '{"error":{"type":"rate_limit_error","code":"rate_limit_exceeded",'
            '"message":"Slow down.","param":"requests","event_id":"evt_rate"}}'
        ),
        headers={
            "Retry-After": "2",
            "x-request-id": "req_123",
            "x-ratelimit-limit-requests": "500",
            "x-ratelimit-remaining-requests": "0",
        },
    )

    with pytest.raises(LLMRateLimitError) as raised:
        client._raise_http_error(exc)

    error = raised.value
    assert error.status_code == 429
    assert error.retry_after == "2"
    assert error.headers["x-request-id"] == "req_123"
    assert error.headers["x-ratelimit-limit-requests"] == "500"
    assert error.provider_error_type == "rate_limit_error"
    assert error.provider_error_code == "rate_limit_exceeded"
    assert error.provider_error_param == "requests"
    assert error.provider_error_event_id == "evt_rate"
    assert error.error_category == "rate_limited"
    assert error.is_retriable is True


def test_openai_compatible_success_response_preserves_headers_usage_and_finish_reason(
    monkeypatch,
):
    def fake_urlopen(request, timeout):
        assert request.headers["Authorization"] == "Bearer test-key"
        return _fake_http_response(
            {
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 3,
                    "total_tokens": 14,
                },
            },
            headers={
                "x-request-id": "req_success",
                "openai-processing-ms": "42",
                "x-ratelimit-remaining-tokens": "1000",
            },
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleLLMClient(
        name="test-client",
        provider_name="openai-compatible",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        default_model="test-model",
    )

    payload, headers = client._post_chat_completion(
        LLMRequest(
            messages=[LLMMessage(role="user", content="hello")],
            prompt_summary="success-test",
        ),
        stream=False,
    )
    content, usage, finish_reason = client._message_content(payload)

    assert content == "ok"
    assert finish_reason == "stop"
    assert usage["prompt_tokens"] == 11
    assert usage["completion_tokens"] == 3
    assert usage["total_tokens"] == 14
    assert headers["x-request-id"] == "req_success"
    assert headers["openai-processing-ms"] == "42"
    assert headers["x-ratelimit-remaining-tokens"] == "1000"


def test_openai_compatible_http_error_uses_status_over_conflicting_body_type():
    client = OpenAICompatibleLLMClient(
        name="test-client",
        provider_name="openai-compatible",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        default_model="test-model",
    )
    exc = _http_error(
        status=401,
        body='{"error":{"type":"server_error","code":"internal","message":"Nope."}}',
        headers={"x-request-id": "req_auth"},
    )

    with pytest.raises(LLMAuthenticationError) as raised:
        client._raise_http_error(exc)

    assert raised.value.error_category == "authentication_failed"
    assert raised.value.is_retriable is False
    assert raised.value.provider_error_type == "server_error"
    assert raised.value.headers["x-request-id"] == "req_auth"


def test_openai_compatible_generic_http_error_maps_unprocessable_entity():
    client = OpenAICompatibleLLMClient(
        name="test-client",
        provider_name="openai-compatible",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        default_model="test-model",
    )
    exc = _http_error(
        status=422,
        body='{"error":{"type":"invalid_request_error","message":"Bad entity."}}',
        headers={"x-request-id": "req_422"},
    )

    with pytest.raises(LLMProviderHTTPError) as raised:
        client._raise_http_error(exc)

    assert raised.value.error_category == "unprocessable_entity"
    assert raised.value.is_retriable is False
    assert raised.value.headers["x-request-id"] == "req_422"


def test_openai_compatible_stream_parses_chat_completion_delta_chunks(monkeypatch):
    def fake_urlopen(request, timeout):
        assert json.loads(request.data.decode("utf-8"))["stream"] is True
        return _fake_stream_response(
            [
                'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"world"}}]}\n\n',
                "data: [DONE]\n\n",
            ]
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleLLMClient(
        name="test-client",
        provider_name="openai-compatible",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        default_model="test-model",
    )

    async def collect():
        events = []
        async for event in client.stream(
            LLMRequest(
                messages=[LLMMessage(role="user", content="hello")],
                prompt_summary="stream-test",
                metadata={"stage": "answer"},
            )
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())

    assert [event.event_type for event in events] == [
        "llm_started",
        "llm_delta",
        "llm_delta",
        "llm_completed",
    ]
    assert [event.delta for event in events if event.event_type == "llm_delta"] == [
        "Hello ",
        "world",
    ]
    assert events[-1].content_snapshot == "Hello world"


def test_stream_error_call_record_preserves_partial_content_and_event_fields():
    record = build_stream_error_call_record(
        llm_call_id="llm_call_stream",
        stage="answer",
        started_at="2026-08-15T00:00:00+00:00",
        duration_ms=10,
        client_name="openai",
        provider="openai",
        model="gpt-test",
        response_mode="stream",
        error_event={
            "type": "error",
            "event_id": "event_890",
            "sequence_number": 4,
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_event",
                "message": "Invalid event.",
                "param": "input",
                "event_id": "event_123",
            },
        },
        partial_content="partial answer",
        run_id="agent_run_test",
        trace_id="agent_turn_test",
        session_id="session_test",
    )

    assert record.status == "partial_failed"
    assert record.partial is True
    assert record.error_category == "bad_request"
    assert record.is_retriable is False
    assert record.provider_error_code == "invalid_event"
    assert record.provider_error_param == "input"
    assert record.content_length == len("partial answer")
    assert record.metadata["sequence_number"] == 4


def _http_error(*, status: int, body: str, headers: dict[str, str]) -> HTTPError:
    message = Message()
    for key, value in headers.items():
        message[key] = value
    return HTTPError(
        url="https://example.invalid/v1/chat/completions",
        code=status,
        msg="provider error",
        hdrs=message,
        fp=BytesIO(body.encode("utf-8")),
    )


def _fake_http_response(payload: dict, *, headers: dict[str, str]):
    class FakeHTTPResponse:
        def __init__(self):
            self.headers = Message()
            for key, value in headers.items():
                self.headers[key] = value

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    return FakeHTTPResponse()


def _fake_stream_response(lines: list[str]):
    class FakeStreamResponse:
        def __init__(self):
            self.lines = [line.encode("utf-8") for line in lines]
            self.index = 0
            self.closed = False

        def readline(self):
            if self.index >= len(self.lines):
                return b""
            line = self.lines[self.index]
            self.index += 1
            return line

        def close(self):
            self.closed = True

    return FakeStreamResponse()
