"""Application-level LLM service."""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.core.llm.errors import LLMClientError
from app.core.llm.models import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMResponseMode,
    LLMStreamEvent,
)
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMProviderConfig


class LLMService:
    """Select configured clients and execute async LLM requests."""

    def __init__(self, *, config: LLMProviderConfig, registry: LLMClientRegistry) -> None:
        self.config = config
        self.registry = registry

    async def complete(self, request: LLMRequest) -> LLMResponse:
        client_name = self._client_name_for(request)
        client = self.registry.get(client_name)
        if client is None:
            raise LLMClientError(f"LLM client is not registered: {client_name}")
        request = request.model_copy(
            update={
                "client_name": client_name,
                "model": request.model or client.default_model,
            }
        )
        return await client.complete(request)

    async def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        client_name: str | None = None,
        model: str | None = None,
        response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        require_json: bool = False,
        metadata: dict | None = None,
    ) -> LLMResponse:
        return await self.complete(
            LLMRequest(
                client_name=client_name,
                model=model,
                response_mode=response_mode,
                messages=[
                    LLMMessage(role="system", content=system_prompt),
                    LLMMessage(role="user", content=user_prompt),
                ],
                prompt_summary=prompt_summary,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                require_json=require_json,
                metadata=metadata or {},
            )
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        client_name = self._client_name_for(request)
        client = self.registry.get(client_name)
        if client is None:
            raise LLMClientError(f"LLM client is not registered: {client_name}")
        request = request.model_copy(
            update={
                "client_name": client_name,
                "model": request.model or client.default_model,
            }
        )
        async for event in client.stream(request):
            yield event

    def supports_function_calling(self, *, client_name: str | None = None) -> bool:
        """Return the configured capability for the selected provider client."""

        resolved_name = client_name or self.config.default_client
        client = self.registry.get(resolved_name) if resolved_name else None
        if client is None and not resolved_name:
            clients = self.registry.list_clients()
            client = clients[0] if clients else None
        return bool(client and getattr(client, "supports_function_calling", False))

    def _client_name_for(self, request: LLMRequest) -> str:
        if request.client_name:
            return request.client_name
        if self.config.default_client:
            return self.config.default_client
        clients = self.registry.list_clients()
        if clients:
            return clients[0].name
        raise LLMClientError("No LLM clients are configured.")
