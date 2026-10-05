"""Application-level LLM service."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.core.llm.errors import LLMClientError, LLMContextCapacityError
from app.core.llm.models import (
    LLMMessage,
    LLMReasoningEffort,
    LLMRequest,
    LLMResponse,
    LLMResponseMode,
    LLMStreamEvent,
    LLMToolDefinition,
)
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMProviderConfig
from app.core.prompt_tokens import PromptTokenCounter

if TYPE_CHECKING:
    from app.core.llm_workloads import LLMWorkloadController


class LLMService:
    """Select configured clients and execute async LLM requests."""

    def __init__(self, *, config: LLMProviderConfig, registry: LLMClientRegistry) -> None:
        self.config = config
        self.registry = registry
        self.workloads: LLMWorkloadController | None = None
        self.background_timeout_seconds: float = 30
        self._token_counters: dict[str | None, PromptTokenCounter] = {}
        self._counter_lock = threading.Lock()

    def _resolve_request_identity(self, request: LLMRequest) -> tuple[str, str]:
        """Resolve the same configured dispatch identity for control and transport."""
        client_name = self._client_name_for(request)
        client = self.registry.get(client_name)
        if client is None:
            raise LLMClientError(f"LLM client is not registered: {client_name}")
        return client_name, request.model or client.default_model

    async def complete(self, request: LLMRequest) -> LLMResponse:
        client_name, model = self._resolve_request_identity(request)
        client = self.registry.get(client_name)
        request = request.model_copy(
            update={
                "client_name": client_name,
                "model": model,
            }
        )
        request = self._bounded_background_request(request)
        async with self._admission(request) as ticket:
            if ticket is not None:
                ticket.dispatched = True
            try:
                response = await client.complete(request)
                if ticket is not None:
                    ticket.usage = response.usage
                return response
            except Exception as exc:
                self._record_workload_error(ticket, exc)
                raise

    async def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        reasoning_effort: LLMReasoningEffort | None = None,
        thinking_enabled: bool | None = None,
        max_output_tokens: int | None = None,
        client_name: str | None = None,
        model: str | None = None,
        response_mode: LLMResponseMode = LLMResponseMode.TEXT,
        require_json: bool = False,
        tools: list[LLMToolDefinition] | None = None,
        tool_choice: str | dict | None = None,
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
                reasoning_effort=reasoning_effort,
                thinking_enabled=thinking_enabled,
                max_output_tokens=max_output_tokens,
                require_json=require_json,
                tools=tools or [],
                tool_choice=tool_choice,
                metadata=metadata or {},
            )
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        client_name, model = self._resolve_request_identity(request)
        client = self.registry.get(client_name)
        request = request.model_copy(
            update={
                "client_name": client_name,
                "model": model,
            }
        )
        request = self._bounded_background_request(request)
        async with self._admission(request) as ticket:
            if ticket is not None:
                ticket.dispatched = True
            try:
                async for event in client.stream(request):
                    if ticket is not None:
                        if isinstance(event.metadata.get("usage"), dict):
                            ticket.usage = event.metadata["usage"]
                        if event.event_type == "llm_failed":
                            ticket.failed = True
                    yield event
            except Exception as exc:
                self._record_workload_error(ticket, exc)
                raise

    def _record_workload_error(self, ticket, exc: Exception) -> None:
        if ticket is not None:
            ticket.failed = True
        if self.workloads is not None and getattr(exc, "status_code", None) == 429:
            self.workloads.provider_limited()

    def _bounded_background_request(self, request: LLMRequest) -> LLMRequest:
        from app.core.llm_workloads import current_workload

        if current_workload().pool == "interactive":
            return request
        return request.model_copy(update={"metadata": {**request.metadata,
                                                       "network_timeout_seconds": self.background_timeout_seconds}})

    @asynccontextmanager
    async def _admission(self, request: LLMRequest):
        if self.workloads is None:
            yield None
            return
        resolved = self.config.resolve_model_config(request.client_name, request.model) if request.model else None
        path = resolved.tokenizer_json_path if resolved is not None else None
        input_tokens = await asyncio.to_thread(self._count_request, request, path)
        output_tokens = request.max_output_tokens or (resolved.output_reserve_tokens if resolved else None) or 8192
        # Agent turns have their own richer preflight. Background calls do not
        # use that assembler, so enforce route capacity here as well.
        from app.core.llm_workloads import current_workload

        if current_workload().pool != "interactive":
            capacity = resolved.context_window_tokens if resolved is not None else None
            if capacity is None:
                raise LLMContextCapacityError("Background model context capacity is not configured.", error_category="context_capacity_missing")
            if input_tokens + output_tokens + 4096 > capacity:
                raise LLMContextCapacityError("Background prompt exceeds configured model context capacity.", error_category="context_capacity_exceeded")
        async with self.workloads.admit(input_tokens=input_tokens, output_tokens=output_tokens) as ticket:
            yield ticket

    def _count_request(self, request: LLMRequest, path) -> int:
        key = str(path) if path is not None else None
        with self._counter_lock:
            counter = self._token_counters.get(key)
            if counter is None:
                try:
                    counter = PromptTokenCounter(path)
                except (FileNotFoundError, ValueError, RuntimeError):
                    counter = PromptTokenCounter()
                self._token_counters[key] = counter
        count = sum(counter.count_text(message.content).count + 8 for message in request.messages)
        if request.tools:
            count += counter.count_request("", "", request.tools).count
        return count

    def supports_function_calling(self, *, client_name: str | None = None) -> bool:
        """Return the configured capability for the selected provider client."""

        resolved_name = client_name or self.config.default_client
        client = self.registry.get(resolved_name) if resolved_name else None
        if client is None and not resolved_name:
            clients = self.registry.list_clients()
            client = clients[0] if clients else None
        return bool(client and getattr(client, "supports_function_calling", False))

    def supports_required_tool_choice(self, *, client_name: str | None = None) -> bool:
        """Return whether the selected client supports forcing one function call."""

        resolved_name = client_name or self.config.default_client
        client = self.registry.get(resolved_name) if resolved_name else None
        if client is None and not resolved_name:
            clients = self.registry.list_clients()
            client = clients[0] if clients else None
        return bool(client and getattr(client, "supports_required_tool_choice", False))

    def supports_reasoning_effort(self, *, client_name: str | None = None) -> bool:
        """Return whether the selected configured client accepts the effort field."""
        resolved_name = client_name or self.config.default_client
        client = self.registry.get(resolved_name) if resolved_name else None
        if client is None and not resolved_name:
            clients = self.registry.list_clients()
            client = clients[0] if clients else None
        return bool(client and getattr(client, "supports_reasoning_effort", False))

    def supports_thinking_control(self, *, client_name: str | None = None) -> bool:
        """Only explicitly configured providers receive vendor thinking fields."""
        name = client_name or self.config.default_client
        client = self.registry.get(name) if name else None
        if client is None and not name:
            clients = self.registry.list_clients()
            client = clients[0] if clients else None
        return bool(client and getattr(client, "thinking_control", None) == "deepseek")

    def _client_name_for(self, request: LLMRequest) -> str:
        if request.client_name:
            return request.client_name
        if self.config.default_client:
            return self.config.default_client
        clients = self.registry.list_clients()
        if clients:
            return clients[0].name
        raise LLMClientError("No LLM clients are configured.")
