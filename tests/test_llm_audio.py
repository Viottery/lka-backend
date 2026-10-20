"""Audio wire format, capability, capacity and diagnostic privacy contracts."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from app.core.llm.errors import LLMClientError, LLMContextCapacityError, LLMProviderHTTPError
from app.core.llm.models import LLMAudioBlock, LLMAudioInput, LLMMessage, LLMRequest, LLMTextBlock
from app.core.llm.openai_compatible import OpenAICompatibleLLMClient
from app.core.llm.registry import LLMClientRegistry
from app.core.llm.service import LLMService
from app.core.local_config import LLMClientConfig, LLMProviderConfig


def audio_request():
    return LLMRequest(prompt_summary="audio", model="audio", max_output_tokens=64, messages=[LLMMessage(role="user", content=[LLMTextBlock(text="Describe"), LLMAudioBlock(input_audio=LLMAudioInput(data="c2VjcmV0YXVkaW8=", format="wav"), duration_ms=1000)])])


def setup_audio(capacity=100000, enabled=True):
    c = OpenAICompatibleLLMClient(name="test", provider_name="test", base_url="https://invalid.example/v1", api_key="test", default_model="text", audio_model_overrides={"audio": enabled})
    registry = LLMClientRegistry()
    registry.register_client(c)
    config = LLMProviderConfig(default_client="test", audio_model="audio", clients=[LLMClientConfig(name="test", default_model="text", model_overrides={"audio": {"context_window_tokens": capacity}})])
    return c, LLMService(config=config, registry=registry)


def test_audio_wire_format_and_model_scoped_selection():
    c, service = setup_audio()
    payload = json.loads(c._build_request(audio_request(), stream=False).data)
    assert payload["model"] == "audio"
    assert payload["messages"][0]["content"][1] == {"type": "input_audio", "input_audio": {"data": "c2VjcmV0YXVkaW8=", "format": "wav"}}
    assert service.audio_identity() == ("test", "audio")
    assert not service.supports_audio(model="text")
    assert service._resolve_request_identity(LLMRequest(messages=[], prompt_summary="text")) == ("test", "text")
    count = service._count_request(audio_request(), None)
    req = audio_request()
    req.messages[0].content[1].input_audio.data *= 1000
    assert service._count_request(req, None) == count
    req.messages[0].content[1].duration_ms = 2000
    assert service._count_request(req, None) == count + 40


@pytest.mark.parametrize("role", ["system", "assistant", "tool"])
def test_audio_is_user_source_only(role):
    with pytest.raises(ValidationError):
        LLMMessage(role=role, content=audio_request().messages[0].content)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled,capacity,expected", [(False,100000,LLMClientError), (True,100,LLMContextCapacityError)])
def test_audio_rejects_unsupported_and_oversized_before_dispatch(stream, enabled, capacity, expected):
    c, service = setup_audio(capacity, enabled)
    async def run():
        if stream:
            return [event async for event in service.stream(audio_request())]
        return await service.complete(audio_request())
    with pytest.raises(expected):
        asyncio.run(run())
    if not enabled:
        with pytest.raises(LLMClientError, match="audio input"):
            c._build_request(audio_request(), stream=False)


@pytest.mark.parametrize("stream", [False, True])
def test_audio_provider_error_keeps_category_and_redacts_bytes(stream):
    c, service = setup_audio()
    encoded = audio_request().messages[0].content[1].input_audio.data
    async def complete(req):
        raise LLMProviderHTTPError(status_code=400, message="invalid " + encoded, error_body={"data": encoded}, error_category="bad_request")
    async def events(req):
        await complete(req)
        yield None
    c.complete = complete
    c.stream = events
    async def run():
        if stream:
            return [event async for event in service.stream(audio_request())]
        return await service.complete(audio_request())
    with pytest.raises(LLMProviderHTTPError) as caught:
        asyncio.run(run())
    assert encoded not in str(caught.value)
    assert encoded not in str(caught.value.error_body)
    assert caught.value.error_category == "bad_request"


def test_audio_inline_request_byte_limit():
    c, _ = setup_audio()
    req = audio_request()
    req.messages[0].content[1].input_audio.data = "A" * (20 * 1024 * 1024)
    with pytest.raises(LLMClientError, match="20 MiB"):
        c._build_request(req, stream=False)
