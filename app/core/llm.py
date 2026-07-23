"""LLM client protocol and deterministic mock provider."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from app.core.context import TaskContext


class LLMResponse(BaseModel):
    provider: str
    status: str
    content: str
    prompt_summary: str


class LLMClient(Protocol):
    def complete(self, *, user_input: str, task_context: TaskContext) -> LLMResponse:
        ...


class MockLLMClient:
    provider_name = "mock_llm"

    def complete(self, *, user_input: str, task_context: TaskContext) -> LLMResponse:
        return LLMResponse(
            provider=self.provider_name,
            status="completed",
            content=(
                "Mock LLM response: runtime debug chain is available. "
                "No external model was called."
            ),
            prompt_summary=f"goal={task_context.goal_summary[:120]}",
        )
