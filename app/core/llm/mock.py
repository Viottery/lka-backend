"""Deterministic mock LLM client for tests and local fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.core.context import TaskContext
from app.core.llm.models import LLMRequest, LLMResponse, LLMResponseMode, LLMStreamEvent


class MockLLMClient:
    name = "mock"
    provider_name = "mock_llm"
    default_model = "mock"
    available_models = ["mock"]
    supports_stream = True
    supports_json_mode = True

    def complete(self, *, user_input: str, task_context: TaskContext) -> LLMResponse:
        return LLMResponse(
            provider=self.provider_name,
            client_name=self.name,
            model=self.default_model,
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
            client_name=self.name,
            model=self.default_model,
            status="completed",
            content='{"matters":[]}',
            prompt_summary=prompt_summary,
        )

    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        return _mock_response(
            request=request,
            client_name=request.client_name or self.name,
            provider_name=self.provider_name,
            default_model=self.default_model,
        )


class AsyncMockLLMClient:
    name = "mock"
    provider_name = "mock_llm"
    default_model = "mock"
    available_models = ["mock"]
    supports_stream = True
    supports_json_mode = True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return _mock_response(
            request=request,
            client_name=request.client_name or self.name,
            provider_name=self.provider_name,
            default_model=self.default_model,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        async for event in _mock_stream(
            request=request,
            client_name=request.client_name or self.name,
            provider_name=self.provider_name,
            default_model=self.default_model,
        ):
            yield event


def _mock_response(
    *,
    request: LLMRequest,
    client_name: str,
    provider_name: str,
    default_model: str,
) -> LLMResponse:
    content = '{"matters":[]}' if request.require_json else "Mock LLM response."
    return LLMResponse(
        provider=provider_name,
        client_name=client_name,
        model=request.model or default_model,
        status="completed",
        content=content,
        prompt_summary=request.prompt_summary,
        response_mode=request.response_mode,
    )


async def _mock_stream(
    *,
    request: LLMRequest,
    client_name: str,
    provider_name: str,
    default_model: str,
) -> AsyncIterator[LLMStreamEvent]:
    model = request.model or default_model
    yield LLMStreamEvent(
        event_type="llm_started",
        stage=str(request.metadata.get("stage") or "llm"),
        client_name=client_name,
        provider=provider_name,
        model=model,
    )
    content = "Mock LLM response."
    snapshot = ""
    for delta in content.split(" "):
        token = delta + " "
        snapshot += token
        yield LLMStreamEvent(
            event_type="llm_delta",
            stage=str(request.metadata.get("stage") or "llm"),
            client_name=client_name,
            provider=provider_name,
            model=model,
            delta=token,
            content_snapshot=snapshot,
        )
    yield LLMStreamEvent(
        event_type="llm_completed",
        stage=str(request.metadata.get("stage") or "llm"),
        client_name=client_name,
        provider=provider_name,
        model=model,
        content_snapshot=snapshot.strip(),
        metadata={"response_mode": LLMResponseMode.STREAM.value},
    )
