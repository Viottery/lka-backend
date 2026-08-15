"""Application-level LLM client registry, service, and compatibility exports."""

from __future__ import annotations

from app.core.llm.client import AsyncLLMClient, LLMClient, TextLLMClient
from app.core.llm.errors import (
    LLMAuthenticationError,
    LLMClientError,
    LLMNetworkError,
    LLMProviderHTTPError,
    LLMRateLimitError,
    LLMResponseParseError,
    LLMTimeoutError,
)
from app.core.llm.mock import MockLLMClient
from app.core.llm.models import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMResponseMode,
    LLMStreamEvent,
)
from app.core.llm.audit import LLMCallRecord
from app.core.llm.registry import LLMClientRegistry, build_llm_registry
from app.core.llm.service import LLMService
from app.core.local_config import LLMProviderConfig


def build_llm_service(config: LLMProviderConfig) -> LLMService | None:
    """Build the configured LLM service, or return None for mock-only defaults."""

    if config.is_disabled():
        return None
    registry = build_llm_registry(config)
    if not registry.list_clients():
        return None
    return LLMService(config=config, registry=registry)


def build_text_llm_client(config: LLMProviderConfig) -> LLMService | None:
    """Compatibility alias for existing runtime wiring."""

    return build_llm_service(config)


__all__ = [
    "AsyncLLMClient",
    "LLMAuthenticationError",
    "LLMClient",
    "LLMClientError",
    "LLMClientRegistry",
    "LLMCallRecord",
    "LLMMessage",
    "LLMNetworkError",
    "LLMProviderHTTPError",
    "LLMRateLimitError",
    "LLMRequest",
    "LLMResponse",
    "LLMResponseMode",
    "LLMResponseParseError",
    "LLMStreamEvent",
    "LLMService",
    "LLMTimeoutError",
    "MockLLMClient",
    "TextLLMClient",
    "build_llm_registry",
    "build_llm_service",
    "build_text_llm_client",
]
