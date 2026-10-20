"""Transport failures retain diagnostics without hidden provider retries."""

import asyncio
import http.client
import json
from unittest.mock import Mock

import pytest

from app.core.llm.audit import classify_openai_sdk_exception
from app.core.llm.errors import LLMNetworkError, LLMTimeoutError
from app.core.llm.models import LLMMessage, LLMRequest
from app.core.llm.openai_compatible import OpenAICompatibleLLMClient


@pytest.fixture
def client():
    return OpenAICompatibleLLMClient(
        name="test", provider_name="test", base_url="https://example.invalid/v1",
        api_key="test-key", default_model="test-model",
    )


@pytest.fixture
def llm_request():
    return LLMRequest(messages=[LLMMessage(role="user", content="hello")], prompt_summary="transport-test")


class Response:
    def __init__(self, error, lines=()):
        self.headers = {"x-request-id": "req_transport", "authorization": "must-not-leak"}
        self.error = error
        self.lines = iter(lines)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def read(self):
        raise self.error

    def readline(self):
        try:
            return next(self.lines)
        except StopIteration:
            raise self.error


def failures():
    return [http.client.RemoteDisconnected("closed"),
            http.client.IncompleteRead(b"partial", 10), ConnectionResetError("reset")]


@pytest.mark.parametrize("failure", failures())
@pytest.mark.parametrize("stream", [False, True])
def test_open_failure_is_classified_and_preserves_cause(monkeypatch, client, llm_request, failure, stream):
    urlopen = Mock(side_effect=failure)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    async def call():
        if stream:
            return [event async for event in client.stream(llm_request)]
        return await client.complete(llm_request)

    with pytest.raises(LLMNetworkError) as caught:
        asyncio.run(call())
    assert caught.value.__cause__ is failure
    assert classify_openai_sdk_exception(caught.value) == ("network_failed", True)
    assert urlopen.call_count == 1


@pytest.mark.parametrize("failure", failures())
def test_nonstream_read_failure_retains_response_identity(monkeypatch, client, llm_request, failure):
    response = Response(failure)
    urlopen = Mock(return_value=response)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(LLMNetworkError) as caught:
        asyncio.run(client.complete(llm_request))
    error = caught.value
    assert error.__cause__ is failure
    assert error.headers == {"x-request-id": "req_transport"}
    assert error.provider_request_id == "req_transport"
    assert error.usage == {}
    assert error.partial_content == ""
    assert response.closed
    assert urlopen.call_count == 1


@pytest.mark.parametrize("failure", [*failures(), TimeoutError("slow")])
def test_stream_read_failure_retains_partial_tools_and_usage(monkeypatch, client, llm_request, failure):
    usage = {"prompt_tokens": 12, "completion_tokens": 4}
    chunks = [
        {"choices": [{"delta": {"content": "partial", "tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "lookup", "arguments": "{"}},
        ]}}]},
        {"choices": [], "usage": usage},
    ]
    response = Response(failure, [f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks])
    urlopen = Mock(return_value=response)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    events = []

    async def collect():
        async for event in client.stream(llm_request):
            events.append(event)

    expected = LLMTimeoutError if isinstance(failure, TimeoutError) else LLMNetworkError
    with pytest.raises(expected) as caught:
        asyncio.run(collect())
    error = caught.value
    assert error.__cause__ is failure
    assert error.headers == {"x-request-id": "req_transport"}
    assert error.provider_request_id == "req_transport"
    assert error.partial_content == "partial"
    assert error.usage == usage
    assert error.metadata["usage"] == usage
    assert error.tool_calls[0]["name"] == "lookup"
    assert error.tool_calls[0]["raw_arguments"] == "{"
    assert error.stream_output_emitted is True
    assert [event.event_type for event in events] == ["llm_started", "llm_delta"]
    assert response.closed
    assert urlopen.call_count == 1


@pytest.mark.parametrize("stream", [False, True])
def test_programming_error_is_not_reclassified(monkeypatch, client, llm_request, stream):
    failure = ValueError("programming error")
    response = Response(failure)
    monkeypatch.setattr("urllib.request.urlopen", Mock(return_value=response))

    async def call():
        if stream:
            return [event async for event in client.stream(llm_request)]
        return await client.complete(llm_request)

    with pytest.raises(ValueError) as caught:
        asyncio.run(call())
    assert caught.value is failure
    assert response.closed
