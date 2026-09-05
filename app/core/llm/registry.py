"""LLM client registry and builders."""

from __future__ import annotations

from collections.abc import Callable

from app.core.llm.client import AsyncLLMClient
from app.core.llm.errors import LLMClientError
from app.core.llm.mock import AsyncMockLLMClient
from app.core.llm.openai_compatible import OpenAICompatibleLLMClient
from app.core.local_config import LLMClientConfig, LLMProviderConfig


ClientFactory = Callable[[LLMClientConfig], AsyncLLMClient]


class LLMClientRegistry:
    """Named client registry for configured LLM providers."""

    def __init__(self) -> None:
        self._clients: dict[str, AsyncLLMClient] = {}
        self._factories: dict[str, ClientFactory] = {}

    def register_factory(self, provider: str, factory: ClientFactory) -> None:
        self._factories[provider] = factory

    def register_client(self, client: AsyncLLMClient) -> None:
        self._clients[client.name] = client

    def build_client(self, config: LLMClientConfig) -> AsyncLLMClient | None:
        factory = self._factories.get(config.provider)
        if factory is None:
            raise LLMClientError(f"Unsupported LLM provider: {config.provider}")
        client = factory(config)
        self.register_client(client)
        return client

    def get(self, name: str) -> AsyncLLMClient | None:
        return self._clients.get(name)

    def list_clients(self) -> list[AsyncLLMClient]:
        return list(self._clients.values())


def build_llm_registry(config: LLMProviderConfig) -> LLMClientRegistry:
    registry = LLMClientRegistry()
    registry.register_factory("mock", _build_mock_client)
    registry.register_factory("openai_compatible", _build_openai_compatible_client)

    for client_config in config.client_configs():
        try:
            registry.build_client(client_config)
        except LLMClientError:
            if client_config.name == config.default_client:
                raise
            continue
    return registry


def _build_mock_client(config: LLMClientConfig) -> AsyncLLMClient:
    client = AsyncMockLLMClient()
    client.name = config.name
    client.default_model = config.default_model
    client.available_models = config.available_models or [config.default_model]
    return client


def _build_openai_compatible_client(config: LLMClientConfig) -> AsyncLLMClient:
    return OpenAICompatibleLLMClient(
        name=config.name,
        provider_name=config.name,
        base_url=config.base_url,
        api_key=config.resolved_api_key(),
        default_model=config.default_model,
        available_models=config.available_models,
        timeout_seconds=config.timeout_seconds,
        supports_stream=config.supports_stream,
        supports_json_mode=config.supports_json_mode,
        supports_function_calling=config.supports_function_calling,
        function_calling_strict=config.function_calling_strict,
    )
