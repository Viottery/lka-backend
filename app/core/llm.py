"""LLM client protocol, deterministic mock provider, and OpenAI-compatible client."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol

from pydantic import BaseModel

from app.core.context import TaskContext
from app.core.local_config import LLMProviderConfig


class LLMResponse(BaseModel):
    provider: str
    status: str
    content: str
    prompt_summary: str


class LLMClientError(RuntimeError):
    """Raised when a configured LLM provider cannot complete a request."""


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

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            provider=self.provider_name,
            status="completed",
            content='{"matters":[]}',
            prompt_summary=prompt_summary,
        )


class OpenAICompatibleLLMClient:
    """Minimal OpenAI-compatible chat client for third-party providers."""

    def __init__(self, config: LLMProviderConfig) -> None:
        self.config = config
        self.provider_name = config.provider
        self.api_key = config.resolved_api_key()
        if not self.api_key:
            raise LLMClientError(f"Missing API key environment variable: {config.api_key_env}")

    def complete(self, *, user_input: str, task_context: TaskContext) -> LLMResponse:
        return self.complete_text(
            system_prompt=(
                "You are the Local Knowledge Agent OS runtime. Use the provided task "
                "context and answer with a concise operational summary."
            ),
            user_prompt=f"Task: {user_input}\n\nContext:\n{task_context.model_dump_json()}",
            prompt_summary=f"goal={task_context.goal_summary[:120]}",
        )

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens

        request = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise LLMClientError(f"LLM provider returned HTTP {exc.code}: {error_body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMClientError(f"LLM provider request failed: {exc}") from exc

        try:
            message = response_payload["choices"][0]["message"]
            content = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMClientError("LLM provider response did not include message content.") from exc

        return LLMResponse(
            provider=self.provider_name,
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


def build_text_llm_client(config: LLMProviderConfig) -> TextLLMClient | None:
    """Return a real text LLM client when configured, otherwise no-op."""

    if config.provider == "mock":
        return None
    if not config.resolved_api_key():
        return None
    return OpenAICompatibleLLMClient(config)
