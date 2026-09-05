"""LLM client protocols."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.core.context import TaskContext
from app.core.llm.models import LLMRequest, LLMResponse, LLMStreamEvent


class LLMClient(Protocol):
    def complete(self, *, user_input: str, task_context: TaskContext) -> LLMResponse:
        ...


class TextLLMClient(Protocol):
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        ...


class AsyncLLMClient(Protocol):
    name: str
    provider_name: str
    default_model: str
    available_models: list[str]
    supports_stream: bool
    supports_json_mode: bool
    supports_function_calling: bool

    async def complete(self, request: LLMRequest) -> LLMResponse:
        ...

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        ...
