from __future__ import annotations

import asyncio
import json

import pytest

from app.core.llm import LLMMessage, LLMRequest, build_llm_service
from app.core.llm.errors import LLMClientError
from app.core.llm.openai_compatible import OpenAICompatibleLLMClient
from app.core.local_config import LLMClientConfig, LLMProviderConfig, MemoryConfig


def client(control=None):
    return OpenAICompatibleLLMClient(
        name="test", provider_name="test", base_url="https://example.invalid/v1",
        api_key="test", default_model="test", thinking_control=control,
    )


@pytest.mark.parametrize("enabled,expected", [(True, "enabled"), (False, "disabled")])
@pytest.mark.parametrize("stream", [True, False])
def test_explicit_thinking_control_serializes_for_both_modes(enabled, expected, stream):
    request = LLMRequest(messages=[LLMMessage(role="user", content="test")],
                         prompt_summary="test", thinking_enabled=enabled)
    payload = json.loads(client("deepseek")._build_request(request, stream=stream).data)
    assert payload["thinking"] == {"type": expected}


def test_default_does_not_change_provider_reasoning_and_unsupported_fails_closed():
    request = LLMRequest(messages=[], prompt_summary="test")
    for control in (None, "deepseek"):
        assert "thinking" not in json.loads(client(control)._build_request(request, stream=False).data)
    with pytest.raises(LLMClientError, match="thinking control"):
        client()._build_request(request.model_copy(update={"thinking_enabled": False}), stream=False)


def test_legacy_config_carries_capability_and_service_passes_override(monkeypatch):
    monkeypatch.setenv("TEST_THINKING_KEY", "test")
    service = build_llm_service(LLMProviderConfig(
        provider="openai_compatible", api_key_env="TEST_THINKING_KEY",
        thinking_control="deepseek",
    ))
    assert service.supports_thinking_control()
    captured = []

    async def complete(request):
        captured.append(request)

    monkeypatch.setattr(service, "complete", complete)
    asyncio.run(service.complete_text(system_prompt="s", user_prompt="u", prompt_summary="t",
                                     thinking_enabled=False))
    assert captured[0].thinking_enabled is False


def test_named_clients_do_not_inherit_legacy_capability(monkeypatch):
    monkeypatch.setenv("TEST_THINKING_KEY", "test")
    service = build_llm_service(LLMProviderConfig(
        thinking_control="deepseek", default_client="basic", clients=[
            LLMClientConfig(name="basic", api_key_env="TEST_THINKING_KEY"),
            LLMClientConfig(name="thinking", api_key_env="TEST_THINKING_KEY", thinking_control="deepseek"),
        ],
    ))
    assert not service.supports_thinking_control()
    assert service.supports_thinking_control(client_name="thinking")


def test_exhausted_background_generation_has_visible_terminal_class():
    from app.core.background_jobs import _classify_worker_error
    from app.core.background_llm import IncompleteGenerationError

    assert _classify_worker_error(IncompleteGenerationError("incomplete")) == (
        "incomplete_generation", False,
    )


def test_generation_budgets_are_separate_configurable_positive_limits():
    from pydantic import ValidationError

    config = MemoryConfig(generation_output_tokens=6000, recovery_output_tokens=12000)
    assert config.generation_output_tokens == 6000
    assert config.recovery_output_tokens == 12000
    assert config.max_job_tokens == 32768
    with pytest.raises(ValidationError):
        MemoryConfig(generation_output_tokens=0)
    with pytest.raises(ValidationError):
        LLMClientConfig(name="bad", thinking_control="guess")
