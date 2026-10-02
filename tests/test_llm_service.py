from __future__ import annotations

import asyncio

from app.core.llm import LLMMessage, LLMRequest, LLMResponseMode, build_llm_service
from app.core.local_config import LLMClientConfig, LLMProviderConfig


def test_llm_service_uses_request_model_override():
    service = build_llm_service(
        LLMProviderConfig(
            default_client="mock",
            clients=[
                LLMClientConfig(
                    name="mock",
                    provider="mock",
                    default_model="mock-default",
                    available_models=["mock-default", "mock-user-selected"],
                )
            ],
        )
    )

    assert service is not None
    response = asyncio.run(
        service.complete_text(
            system_prompt="system",
            user_prompt="user",
            prompt_summary="test",
            client_name="mock",
            model="mock-user-selected",
            require_json=True,
        )
    )

    assert response.client_name == "mock"
    assert response.model == "mock-user-selected"
    assert response.content == '{"matters":[]}'


def test_llm_service_exposes_function_choice_capability_per_client(monkeypatch):
    monkeypatch.setenv("TEST_FUNCTION_CLIENT_KEY", "test-key")
    service = build_llm_service(
        LLMProviderConfig(
            default_client="structured",
            clients=[
                LLMClientConfig(
                    name="structured",
                    api_key_env="TEST_FUNCTION_CLIENT_KEY",
                    supports_function_calling=True,
                    supports_required_tool_choice=True,
                ),
                LLMClientConfig(
                    name="basic",
                    api_key_env="TEST_FUNCTION_CLIENT_KEY",
                    supports_function_calling=False,
                ),
            ],
        )
    )

    assert service is not None
    assert service.supports_function_calling(client_name="structured")
    assert service.supports_required_tool_choice(client_name="structured")
    assert not service.supports_function_calling(client_name="basic")
    assert not service.supports_required_tool_choice(client_name="basic")


def test_llm_service_streams_standard_events():
    service = build_llm_service(
        LLMProviderConfig(
            default_client="mock",
            clients=[
                LLMClientConfig(
                    name="mock",
                    provider="mock",
                    default_model="mock",
                )
            ],
        )
    )
    assert service is not None

    async def collect():
        events = []
        async for event in service.stream(
            LLMRequest(
                client_name="mock",
                model="mock",
                response_mode=LLMResponseMode.STREAM,
                messages=[LLMMessage(role="user", content="hello")],
                prompt_summary="stream-test",
                metadata={"stage": "answer"},
            )
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())

    assert events[0].event_type == "llm_started"
    assert events[-1].event_type == "llm_completed"
    assert any(event.event_type == "llm_delta" for event in events)
    assert events[-1].content_snapshot == "Mock LLM response."
