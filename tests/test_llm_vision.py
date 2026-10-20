import asyncio
import json

import pytest
from pydantic import ValidationError

from app.core.llm.errors import LLMClientError
from app.core.llm.models import LLMImageBlock, LLMImageURL, LLMMessage, LLMRequest, LLMTextBlock
from app.core.llm.openai_compatible import OpenAICompatibleLLMClient
from app.core.llm.registry import LLMClientRegistry
from app.core.llm.service import LLMService
from app.core.local_config import LLMProviderConfig


def client(**kwargs):
    return OpenAICompatibleLLMClient(name="test", provider_name="test", base_url="https://invalid.example/v1", api_key="test", default_model="visual", **kwargs)


def request():
    return LLMRequest(prompt_summary="image", messages=[LLMMessage(role="user", content=[LLMTextBlock(text="Describe"), LLMImageBlock(image_url=LLMImageURL(url="data:image/png;base64,AAAA"))])])


def test_payload_text_compatibility_and_multimodal():
    c = client(supports_vision=True)
    payload = json.loads(c._build_request(request(), stream=False).data)
    assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/")
    text = request().model_copy(update={"messages": [LLMMessage(role="user", content="hello")]})
    assert json.loads(c._build_request(text, stream=False).data)["messages"] == [{"role": "user", "content": "hello"}]


def test_transport_and_service_reject_unsupported_before_http():
    c = client()
    with pytest.raises(LLMClientError, match="image input"):
        c._build_request(request(), stream=False)
    registry = LLMClientRegistry()
    registry.register_client(c)
    service = LLMService(config=LLMProviderConfig(default_client="test"), registry=registry)
    with pytest.raises(LLMClientError, match="image input"):
        asyncio.run(service.complete(request()))
    async def consume():
        return [event async for event in service.stream(request())]
    with pytest.raises(LLMClientError, match="image input"):
        asyncio.run(consume())


def test_exact_model_capability_and_counting():
    c = client(supports_vision=True, vision_model_overrides={"text": False})
    registry = LLMClientRegistry()
    registry.register_client(c)
    service = LLMService(config=LLMProviderConfig(default_client="test"), registry=registry)
    assert service.supports_vision(model="visual")
    assert not service.supports_vision(model="text")
    first = service._count_request(request(), None)
    larger = request()
    larger.messages[0].content[1].image_url.url += "A" * 100000
    assert service._count_request(larger, None) == first
    assert first >= 32768


def test_images_only_allowed_on_user_role():
    with pytest.raises(ValidationError):
        LLMMessage(role="system", content=request().messages[0].content)


@pytest.mark.parametrize("stream", [False, True])
def test_image_context_capacity_checked_without_workload_controller(stream):
    from app.core.llm.errors import LLMContextCapacityError
    from app.core.local_config import LLMClientConfig

    c = client(supports_vision=True)
    registry = LLMClientRegistry()
    registry.register_client(c)
    config = LLMProviderConfig(default_client="test", clients=[LLMClientConfig(name="test", default_model="visual", context_window_tokens=1024)])
    service = LLMService(config=config, registry=registry)
    dispatched = False

    async def complete(_request):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("must not dispatch")

    async def events(_request):
        nonlocal dispatched
        dispatched = True
        yield None

    c.complete = complete
    c.stream = events

    async def run():
        if stream:
            return [event async for event in service.stream(request())]
        return await service.complete(request())

    with pytest.raises(LLMContextCapacityError) as error:
        asyncio.run(run())
    assert error.value.error_category == "context_capacity_exceeded"
    assert not dispatched


@pytest.mark.parametrize("capacity", [None, 100000])
def test_image_known_large_and_unknown_capacities_keep_dispatch(capacity):
    from app.core.llm.models import LLMResponse
    from app.core.local_config import LLMClientConfig

    c = client(supports_vision=True)
    registry = LLMClientRegistry()
    registry.register_client(c)
    config = LLMProviderConfig(default_client="test", clients=[LLMClientConfig(name="test", default_model="visual", context_window_tokens=capacity)])
    service = LLMService(config=config, registry=registry)

    async def complete(_request):
        return LLMResponse(provider="test", status="completed", content="okay", prompt_summary="image")

    c.complete = complete
    assert asyncio.run(service.complete(request())).content == "okay"
