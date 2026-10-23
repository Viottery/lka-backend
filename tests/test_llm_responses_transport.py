import asyncio
import io
import json
from typing import ClassVar

import pytest

from app.core.llm import LLMMessage, LLMRequest, build_llm_service
from app.core.llm.errors import LLMProviderStreamError
from app.core.local_config import LLMProviderConfig


def service(monkeypatch):
    monkeypatch.setenv("TEST_RESPONSES_KEY", "test-key")
    return build_llm_service(
        LLMProviderConfig(
            provider="openai_compatible",
            api_key_env="TEST_RESPONSES_KEY",
            model="chat-model",
            available_models=["chat-model", "responses-model"],
            model_api_protocol_overrides={"responses-model": "responses"},
            supports_json_mode=True,
        )
    )


def request(**updates):
    return LLMRequest(
        model="responses-model",
        messages=[LLMMessage(role="user", content="Return JSON.")],
        prompt_summary="protocol-test",
        max_output_tokens=100,
        require_json=True,
        **updates,
    )


def output(text="OK", status="completed"):
    return {
        "status": status,
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": text},
                ],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


class Response(io.BytesIO):
    headers: ClassVar = {"x-request-id": "req_responses"}


@pytest.mark.parametrize(
    "model,endpoint",
    [
        ("chat-model", "/chat/completions"),
        ("responses-model", "/responses"),
    ],
)
def test_registry_routes_exact_models_and_parses_nonstream(monkeypatch, model, endpoint):
    llm = service(monkeypatch)
    seen = []

    def urlopen(req, timeout):
        seen.append(req)
        body = (
            output()
            if endpoint == "/responses"
            else {
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            }
        )
        return Response(json.dumps(body).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    response = asyncio.run(llm.complete(request().model_copy(update={"model": model})))
    assert response.content == "OK"
    assert response.status == "completed"
    assert seen[0].full_url.endswith(endpoint)
    body = json.loads(seen[0].data)
    if endpoint == "/responses":
        assert body["max_output_tokens"] == 100
        assert body["text"]["format"] == {"type": "json_object"}
        assert "messages" not in body
        assert response.usage == {"input_tokens": 10, "output_tokens": 2}
        assert response.provider_request_id == "req_responses"
    else:
        assert body["max_tokens"] == 100
        assert "input" not in body


def stream(monkeypatch, events):
    llm = service(monkeypatch)
    wire = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
    response = Response(wire)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)

    async def collect():
        return [e async for e in llm.stream(request())]

    result = asyncio.run(collect())
    assert response.closed
    return result


def test_responses_stream_skips_lifecycle_and_retains_usage(monkeypatch):
    events = stream(
        monkeypatch,
        [
            {"type": "response.created"},
            {"type": "response.output_text.delta", "delta": "OK"},
            {"type": "response.completed", "response": output()},
        ],
    )
    assert [e.event_type for e in events] == ["llm_started", "llm_delta", "llm_completed"]
    assert events[-1].content_snapshot == "OK"
    assert events[-1].metadata["usage"]["input_tokens"] == 10
    assert events[-1].metadata["status"] == "completed"
    assert not events[-1].metadata["partial"]


@pytest.mark.parametrize("terminal", [None, "response.incomplete"])
def test_responses_stream_eof_and_incomplete_remain_partial(monkeypatch, terminal):
    wire = [{"type": "response.output_text.delta", "delta": "part"}]
    if terminal:
        wire.append({"type": terminal, "response": output("part", "incomplete")})
    events = stream(monkeypatch, wire)
    assert events[-1].metadata["status"] == "incomplete"
    assert events[-1].metadata["partial"]
    assert events[-1].content_snapshot == "part"


def test_responses_stream_provider_failure_is_not_completed(monkeypatch):
    events = stream(
        monkeypatch,
        [
            {"type": "response.output_text.delta", "delta": "part"},
            {
                "type": "response.failed",
                "response": {"error": {"code": "upstream_failed", "message": "unavailable"}},
            },
        ],
    )
    assert events[-1].event_type == "llm_failed"
    assert events[-1].content_snapshot == "part"
    assert events[-1].error == "unavailable"
    assert events[-1].metadata["error_event"]["error"]["code"] == "upstream_failed"


def test_responses_nonstream_incomplete_is_partial(monkeypatch):
    llm = service(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: Response(
            json.dumps(output("part", "incomplete")).encode(),
        ),
    )
    response = asyncio.run(llm.complete(request()))
    assert response.partial and response.status == "incomplete"
    assert response.finish_reason == "length"


def test_responses_nonstream_failure_preserves_provider_diagnostics(monkeypatch):
    llm = service(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: Response(json.dumps({
        "status": "failed", "error": {"message": "unavailable", "code": "server_error"},
        "usage": {"input_tokens": 10},
    }).encode()))
    with pytest.raises(LLMProviderStreamError) as caught:
        asyncio.run(llm.complete(request()))
    assert caught.value.provider_request_id == "req_responses"
    assert caught.value.error_event["error"]["code"] == "server_error"
    assert caught.value.usage == {"input_tokens": 10}
