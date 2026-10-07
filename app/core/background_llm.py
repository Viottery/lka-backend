"""Call the async-capable LLM service from a dedicated background worker thread."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

from app.core.llm import LLMService
from app.core.llm.errors import LLMClientError


class SelectedBackgroundClient:
    """Bind configured background inference without changing foreground defaults."""

    def __init__(
        self, client: Any, client_name: str | None = None, model: str | None = None,
        *, initial_output_tokens: int = 4096, recovery_output_tokens: int = 8192,
    ):
        self.client, self.client_name, self.model = client, client_name, model
        self.initial_output_tokens = initial_output_tokens
        self.recovery_output_tokens = recovery_output_tokens

    def complete_text(self, **kwargs: Any) -> Any:
        if isinstance(self.client, LLMService):
            kwargs.update(client_name=self.client_name, model=self.model)
        return self.client.complete_text(**kwargs)

    def supports_thinking_control(self) -> bool:
        if isinstance(self.client, LLMService):
            return self.client.supports_thinking_control(client_name=self.client_name)
        capability = getattr(self.client, "supports_thinking_control", False)
        return bool(capability() if callable(capability) else capability)

    def remaining_job_tokens(self) -> int | None:
        from app.core.llm_workloads import current_workload
        workload = current_workload()
        controller = getattr(self.client, "workloads", None)
        if not workload.task_id or not workload.max_tokens or controller is None:
            return None
        with controller._connect() as conn:
            used = conn.execute(
                "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM llm_workload_usage WHERE task_id=?",
                (workload.task_id,),
            ).fetchone()[0]
        return max(0, workload.max_tokens - used)

    def supports_output_budget(self) -> bool:
        return isinstance(self.client, LLMService) or _accepts_keyword(self.client, "max_output_tokens")


class IncompleteGenerationError(LLMClientError):
    """A bounded generation remained incomplete and must not be accepted."""

    error_category = "incomplete_generation"


def _incomplete(response: Any) -> bool:
    content = getattr(response, "content", None)
    finish_reason = getattr(response, "finish_reason", None)
    metadata = getattr(response, "metadata", None)
    if finish_reason is None and isinstance(metadata, dict):
        finish_reason = metadata.get("finish_reason")
    return (
        not isinstance(content, str) or not content.strip()
        or getattr(response, "status", "completed") != "completed"
        or bool(getattr(response, "partial", False))
        or str(finish_reason or "").casefold() in {"length", "max_tokens", "partial"}
    )


def recover_generation(
    client: Any, *, initial_output_tokens: int | None = None,
    recovery_output_tokens: int | None = None, reserve_tokens: int | None = None,
    total_tokens_budget: int | None = None,
    **kwargs: Any,
) -> Any:
    """Retry one incomplete generation within the remaining per-job budget."""
    initial_output_tokens = (
        initial_output_tokens if initial_output_tokens is not None
        else getattr(client, "initial_output_tokens", 4096)
    )
    recovery_output_tokens = (
        recovery_output_tokens if recovery_output_tokens is not None
        else getattr(client, "recovery_output_tokens", 8192)
    )
    output_budget = getattr(client, "supports_output_budget", None)
    if ((callable(output_budget) and output_budget()) or
            (not callable(output_budget) and _accepts_keyword(client, "max_output_tokens"))):
        kwargs.setdefault("max_output_tokens", initial_output_tokens)
    recovery_output_tokens = reserve_tokens if reserve_tokens is not None else recovery_output_tokens
    prompt_estimate = _estimate_prompt_tokens(kwargs)
    if total_tokens_budget is not None and prompt_estimate + kwargs.get("max_output_tokens", initial_output_tokens) > total_tokens_budget:
        from app.core.llm_workloads import BackgroundTaskBudgetExceeded

        raise BackgroundTaskBudgetExceeded("background_generation_budget_exceeded")
    response = complete_text_in_worker(client, **kwargs)
    charged = response_usage_tokens(response)
    charged = charged if charged is not None else prompt_estimate + kwargs.get("max_output_tokens", initial_output_tokens)
    attempts = 1
    if not _incomplete(response) or recovery_output_tokens <= 0:
        _mark_recovery(response, attempts=attempts, recovered=False, tokens=charged)
        return response
    if total_tokens_budget is not None and total_tokens_budget - charged < prompt_estimate + recovery_output_tokens:
        _mark_recovery(response, attempts=attempts, recovered=False, tokens=charged)
        return response
    remaining = getattr(client, "remaining_job_tokens", None)
    if callable(remaining):
        available = remaining()
        # The first call has already been charged in the authoritative ledger.
        # Non-ledger wrappers are checked against the explicit call reserve below.
        if available is not None and available < prompt_estimate + recovery_output_tokens:
            _mark_recovery(response, attempts=attempts, recovered=False, tokens=charged)
            return response
    retry = dict(kwargs)
    supports = getattr(client, "supports_thinking_control", None)
    if callable(supports) and supports():
        retry["thinking_enabled"] = False
    if ((callable(output_budget) and output_budget()) or
            (not callable(output_budget) and _accepts_keyword(client, "max_output_tokens"))):
        retry["max_output_tokens"] = recovery_output_tokens
    recovered = complete_text_in_worker(client, **retry)
    retry_cost = response_usage_tokens(recovered)
    charged += retry_cost if retry_cost is not None else prompt_estimate + recovery_output_tokens
    _mark_recovery(recovered, attempts=2, recovered=not _incomplete(recovered), tokens=charged)
    return recovered


def _mark_recovery(response: Any, *, attempts: int, recovered: bool, tokens: int) -> None:
    try:
        metadata = getattr(response, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            response.metadata = metadata
        metadata.update(recovery_attempts=attempts, recovered=recovered, generation_tokens=tokens)
    except (AttributeError, TypeError):
        pass


def require_complete_response(response: Any) -> Any:
    if _incomplete(response):
        raise IncompleteGenerationError("background_generation_incomplete")
    return response


def _accepts_keyword(client: Any, key: str) -> bool:
    try:
        signature = inspect.signature(client.complete_text)
    except (TypeError, ValueError, AttributeError):
        return False
    return key in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def response_usage_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    output = usage.get("completion_tokens", usage.get("output_tokens"))
    if isinstance(prompt, int) and isinstance(output, int):
        return prompt + output
    return None


def _estimate_prompt_tokens(kwargs: dict[str, Any]) -> int:
    text = "\n".join(str(kwargs.get(key, "")) for key in ("system_prompt", "user_prompt"))
    return max(256, len(text.encode("utf-8")) // 3 + 64)


class BatchedExtractionClient:
    """One model call for a small burst; partition by exact original evidence.

    The ordinary extractor still validates each partition against its own user
    message. A batch never supplies new authority or a fabricated source ID.
    """

    def __init__(self, client: Any, contents: list[str]):
        self.client = client
        self.contents = list(dict.fromkeys(contents))
        self.cache: dict[str, str] = {}
        self.handles_generation_recovery = True
        self.initial_output_tokens = getattr(client, "initial_output_tokens", 4096)
        self.recovery_output_tokens = getattr(client, "recovery_output_tokens", 8192)

    def remaining_job_tokens(self) -> int | None:
        remaining = getattr(self.client, "remaining_job_tokens", None)
        return remaining() if callable(remaining) else None

    def supports_output_budget(self) -> bool:
        return _accepts_keyword(self.client, "max_output_tokens")

    def supports_thinking_control(self) -> bool:
        capability = getattr(self.client, "supports_thinking_control", False)
        return bool(capability() if callable(capability) else capability)

    async def complete_text(self, **kwargs: Any):
        message = kwargs["user_prompt"]
        if message not in self.cache:
            group = []
            eligible = [content for content in self.contents if content not in self.cache]
            start = eligible.index(message) if message in eligible else len(eligible)
            for content in eligible[start:]:
                if group and (len(group) >= 3 or sum(len(item) for item in group) + len(content) > 6000):
                    break
                group.append(content)
            if message not in group:
                group = [message]
            request = dict(kwargs)
            if len(group) > 1:
                request["user_prompt"] = json.dumps({"user_messages": group}, ensure_ascii=False)
                request["system_prompt"] += (
                    " The input contains separate USER messages; extract at most three candidates "
                    "PER message (at most nine total). Evidence must be a contiguous substring "
                    "of exactly one original message. Never merge different messages into a claim."
                )
                request.pop("max_output_tokens", None)
            # Recovery bridges async providers with asyncio.run(), so keep it
            # outside this adapter's running event loop. to_thread also carries
            # the current workload's token budget into the recovery worker.
            response = await asyncio.to_thread(recover_generation, self.client, **request)
            if _incomplete(response):
                raise IncompleteGenerationError("background_extraction_incomplete")
            try:
                value = json.loads(response.content)
                candidates = value["candidates"]
                if not isinstance(candidates, list):
                    raise TypeError("invalid candidate batch")
            except (AttributeError, KeyError, TypeError, ValueError):
                for content in group:
                    self.cache[content] = '{"candidates":[]}'
            else:
                for content in group:
                    selected = [candidate for candidate in candidates if isinstance(candidate, dict)
                                and isinstance(candidate.get("evidence"), str)
                                and candidate["evidence"]
                                and candidate["evidence"] in content
                                and sum(candidate["evidence"] in original for original in group) == 1]
                    self.cache[content] = json.dumps({"candidates": selected[:3]}, ensure_ascii=False)
        return SimpleNamespace(content=self.cache[message])


def complete_text_in_worker(client: Any, **kwargs: Any) -> Any:
    """Await provider calls without blocking the FastAPI event loop.

    BackgroundJobWorker runs its handlers in worker threads. Synchronous test
    clients and older provider adapters remain supported.
    """

    result = client.complete_text(**kwargs)
    if inspect.isawaitable(result):
        async def await_result() -> Any:
            return await result

        return asyncio.run(await_result())
    return result
