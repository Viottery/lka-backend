from app.core.llm.registry import build_llm_registry
from app.core.local_config import LLMProviderConfig


def test_legacy_client_does_not_infer_protocol_capabilities_from_model():
    config = LLMProviderConfig(provider="openai_compatible", model="deepseek-flash")
    client = config.client_configs()[0]
    assert not client.supports_json_mode
    assert not client.supports_function_calling


def test_explicit_legacy_json_capability_reaches_provider_without_enabling_tools(monkeypatch):
    monkeypatch.setenv("LKA_CAPABILITY_TEST_KEY", "unit-test-not-real")
    config = LLMProviderConfig(provider="openai_compatible", model="example-model",
                               api_key_env="LKA_CAPABILITY_TEST_KEY", supports_json_mode=True)
    client = config.client_configs()[0]
    assert client.supports_json_mode
    assert not client.supports_function_calling
    built = build_llm_registry(config).get(client.name)
    assert built.supports_json_mode
    assert not built.supports_function_calling


def test_legacy_function_capability_does_not_enable_required_tool_choice():
    config = LLMProviderConfig(provider="openai_compatible", supports_function_calling=True)
    client = config.client_configs()[0]
    assert client.supports_function_calling
    assert not client.supports_required_tool_choice
