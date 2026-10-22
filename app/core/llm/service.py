"""Application-level LLM service."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.core.llm.errors import LLMClientError, LLMContextCapacityError
from app.core.llm.models import (
    IMAGE_INPUT_TOKEN_RESERVATION,
    LLMAudioBlock,
    LLMImageBlock,
    LLMMessage,
    LLMReasoningEffort,
    LLMRequest,
    LLMResponse,
    LLMResponseMode,
    LLMStreamEvent,
    LLMTextBlock,
    LLMToolDefinition,
)
from app.core.llm.reasoning import (
    reasoning_capability,
    resolve_reasoning_request,
    validate_reasoning_level,
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
        if any(message.has_audio for message in request.messages) and not self.supports_audio(client_name=client_name, model=model):
            raise LLMClientError(f"LLM client {client_name} does not support audio input.")
        if any(message.has_images for message in request.messages) and not self.supports_vision(client_name=client_name, model=model):
            raise LLMClientError(f"LLM client {client_name} does not support image input.")
        request = request.model_copy(
            update={
                "client_name": client_name,
                "model": model,
            }
        )
        request, reasoning = resolve_reasoning_request(self.config, request)
        request = self._bounded_background_request(request)
        await self._preflight_multimodal_capacity(request)
        async with self._admission(request) as ticket:
            from app.core.llm_workloads import current_workload

            await current_workload().check_dispatch()
            if ticket is not None:
                ticket.dispatched = True
            try:
                response = await client.complete(request)
                if reasoning is not None:
                    response = response.model_copy(update={
                        "metadata": {**response.metadata, "reasoning": reasoning},
                    })
                if ticket is not None:
                    ticket.usage = response.usage
                if any(message.has_images or message.has_audio for message in request.messages):
                    response = response.model_copy(update={
                        "metadata": self._redact_image_diagnostics(response.metadata, request),
                        "provider_request_id": self._redact_image_diagnostics(response.provider_request_id, request),
                    })
                return response
            except Exception as exc:
                if reasoning is not None:
                    self._record_reasoning_error(exc, reasoning)
                self._sanitize_image_error(exc, request)
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
        reasoning_level=None,
        image_blocks: list[LLMImageBlock] | None = None,
        audio_blocks: list[LLMAudioBlock] | None = None,
    ) -> LLMResponse:
        return await self.complete(
            LLMRequest(
                client_name=client_name,
                model=model,
                response_mode=response_mode,
                messages=[
                    LLMMessage(role="system", content=system_prompt),
                    LLMMessage(role="user", content=[LLMTextBlock(text=user_prompt), *(image_blocks or []), *(audio_blocks or [])] if image_blocks or audio_blocks else user_prompt),
                ],
                prompt_summary=prompt_summary,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                reasoning_level=reasoning_level,
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
        if any(message.has_audio for message in request.messages) and not self.supports_audio(client_name=client_name, model=model):
            raise LLMClientError(f"LLM client {client_name} does not support audio input.")
        if any(message.has_images for message in request.messages) and not self.supports_vision(client_name=client_name, model=model):
            raise LLMClientError(f"LLM client {client_name} does not support image input.")
        request = request.model_copy(
            update={
                "client_name": client_name,
                "model": model,
            }
        )
        request, reasoning = resolve_reasoning_request(self.config, request)
        request = self._bounded_background_request(request)
        await self._preflight_multimodal_capacity(request)
        async with self._admission(request) as ticket:
            from app.core.llm_workloads import current_workload

            await current_workload().check_dispatch()
            if ticket is not None:
                ticket.dispatched = True
            try:
                async for event in client.stream(request):
                    if reasoning is not None:
                        event = event.model_copy(update={
                            "metadata": {**event.metadata, "reasoning": reasoning},
                        })
                    if any(message.has_images or message.has_audio for message in request.messages):
                        event = event.model_copy(update={
                            "error": self._redact_image_diagnostics(event.error, request),
                            "metadata": self._redact_image_diagnostics(event.metadata, request),
                        })
                    if ticket is not None:
                        if isinstance(event.metadata.get("usage"), dict):
                            ticket.usage = event.metadata["usage"]
                        if event.event_type == "llm_failed":
                            ticket.failed = True
                    yield event
            except Exception as exc:
                if reasoning is not None:
                    self._record_reasoning_error(exc, reasoning)
                self._sanitize_image_error(exc, request)
                self._record_workload_error(ticket, exc)
                raise

    @staticmethod
    def _redact_image_diagnostics(value, request: LLMRequest):
        tokens = []
        for message in request.messages:
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, LLMImageBlock):
                        tokens.append(block.image_url.url)
                        if ";base64," in block.image_url.url:
                            tokens.append(block.image_url.url.split(";base64,", 1)[1])
                    elif isinstance(block, LLMAudioBlock):
                        tokens.append(block.input_audio.data)
        def redact(item):
            if isinstance(item, str):
                for token in tokens:
                    if token:
                        item = item.replace(token, "[media payload omitted]")
                return item
            if isinstance(item, dict):
                return {key: redact(entry) for key, entry in item.items()}
            if isinstance(item, list):
                return [redact(entry) for entry in item]
            return item
        return redact(value)

    def _sanitize_image_error(self, exc: Exception, request: LLMRequest) -> None:
        if not any(message.has_images or message.has_audio for message in request.messages):
            return
        exc.args = tuple(self._redact_image_diagnostics(item, request) for item in exc.args)
        for name in ("error_body", "error_event", "partial_content", "headers", "provider_error_param"):
            if hasattr(exc, name):
                setattr(exc, name, self._redact_image_diagnostics(getattr(exc, name), request))

    def _record_workload_error(self, ticket, exc: Exception) -> None:
        if ticket is not None:
            ticket.failed = True
        if self.workloads is not None and getattr(exc, "status_code", None) == 429:
            self.workloads.provider_limited()

    @staticmethod
    def _record_reasoning_error(exc: Exception, reasoning: dict) -> None:
        metadata = getattr(exc, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            exc.metadata = metadata
        metadata["reasoning"] = reasoning

    def _bounded_background_request(self, request: LLMRequest) -> LLMRequest:
        from app.core.llm_workloads import current_workload

        if current_workload().pool in {"interactive", "background_agent"}:
            # Independent Agent tasks use the configured provider deadline;
            # short maintenance jobs retain their separate background timeout.
            return request
        timeout = request.metadata.get("background_timeout_seconds")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 3600:
            timeout = self.background_timeout_seconds
        return request.model_copy(update={"metadata": {**request.metadata,
                                                       "network_timeout_seconds": timeout}})

    async def _preflight_multimodal_capacity(self, request: LLMRequest) -> None:
        """Check image reservations even without a workload admission controller."""
        if not any(message.has_images or message.has_audio for message in request.messages):
            return
        resolved = self.config.resolve_model_config(request.client_name, request.model)
        if resolved is None or resolved.context_window_tokens is None:
            # Unknown capacity retains existing foreground compatibility; configured
            # background capacity requirements still apply through admission.
            return
        input_tokens = await asyncio.to_thread(self._count_request, request, resolved.tokenizer_json_path)
        output_tokens = request.max_output_tokens or resolved.output_reserve_tokens or 8192
        if input_tokens + output_tokens > resolved.context_window_tokens:
            raise LLMContextCapacityError(
                "Multimodal request exceeds configured model context capacity.",
                error_category="context_capacity_exceeded",
            )

    @asynccontextmanager
    async def _admission(self, request: LLMRequest):
        from app.core.llm_workloads import BackgroundBudgetDeferred, current_workload

        pricing = current_workload().pricing
        if pricing is not None and (pricing.client_name != request.client_name or pricing.model != request.model):
            exc = BackgroundBudgetDeferred("request model does not match budget pricing")
            exc.category = exc.error_category = "pricing_unknown"
            raise exc
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
        count = 0
        for message in request.messages:
            count += 8
            if isinstance(message.content, str):
                count += counter.count_text(message.content).count
            else:
                for block in message.content:
                    # Fixed conservative reservation for bounded local image tools;
                    # never tokenize base64 as text. Provider actual usage settles budgets.
                    if isinstance(block, LLMImageBlock):
                        count += IMAGE_INPUT_TOKEN_RESERVATION
                    elif isinstance(block, LLMAudioBlock):
                        count += block.token_reservation
                    else:
                        count += counter.count_text(block.text).count
        if request.tools:
            count += counter.count_request("", "", request.tools).count
        return count

    def audio_identity(self) -> tuple[str, str]:
        """Resolve the configured per-turn audio route without changing defaults."""
        if not self.config.audio_model:
            raise LLMClientError("Audio routing requires llm.audio_model configuration.")
        name = self.config.audio_client or self._client_name_for(LLMRequest(messages=[], prompt_summary="audio route"))
        model = self.config.audio_model
        if not self.supports_audio(client_name=name, model=model):
            raise LLMClientError("Configured audio model has not enabled audio input (supports_audio).")
        return name, model

    def supports_audio(self, *, client_name: str | None = None, model: str | None = None) -> bool:
        name = client_name or self.config.default_client
        client = self.registry.get(name) if name else next(iter(self.registry.list_clients()), None)
        if client is None:
            return False
        checker = getattr(client, "supports_audio_input", None)
        return bool(checker(model or client.default_model) if checker else getattr(client, "supports_audio", False))

    def supports_vision(self, *, client_name: str | None = None, model: str | None = None) -> bool:
        name = client_name or self.config.default_client
        client = self.registry.get(name) if name else next(iter(self.registry.list_clients()), None)
        if client is None:
            return False
        checker = getattr(client, "supports_image_input", None)
        return bool(checker(model or client.default_model) if checker else getattr(client, "supports_vision", False))

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

    def reasoning_capability(self, *, client_name: str | None = None, model: str | None = None) -> dict:
        name = client_name or self.config.default_client
        client = self.registry.get(name) if name else next(iter(self.registry.list_clients()), None)
        resolved_model = model or (client.default_model if client is not None else "")
        return reasoning_capability(self.config, name or (client.name if client else None), resolved_model)

    def validate_reasoning_level(self, *, client_name: str | None = None, model: str | None = None, level) -> dict:
        name = client_name or self.config.default_client
        client = self.registry.get(name) if name else next(iter(self.registry.list_clients()), None)
        resolved_model = model or (client.default_model if client is not None else "")
        return validate_reasoning_level(
            self.config, name or (client.name if client else None), resolved_model, level,
        )

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
