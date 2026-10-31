"""Call the async-capable LLM service from a dedicated background worker thread."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

from app.core.llm import LLMService
from app.core.llm.errors import LLMClientError, LLMTimeoutError
from app.core.llm.models import (
    LLMMessage,
    LLMRequest,
    LLMTextBlock,
)


class SelectedBackgroundClient:
    """Bind configured background inference without changing foreground defaults."""

    def __init__(
        self, client: Any, client_name: str | None = None, model: str | None = None,
        *, initial_output_tokens: int = 4096, recovery_output_tokens: int = 8192,
        limit_output_tokens: bool = True, request_timeout_seconds: float | None = None,
    ):
        self.client, self.client_name, self.model = client, client_name, model
        self.initial_output_tokens = initial_output_tokens
        self.recovery_output_tokens = recovery_output_tokens
        self.limit_output_tokens = limit_output_tokens
        self.request_timeout_seconds = request_timeout_seconds

    def complete_text(self, **kwargs: Any) -> Any:
        preserve_budget_cap = bool(kwargs.pop("_background_budget_output_cap", False))
        if not self.limit_output_tokens and not preserve_budget_cap:
            kwargs.pop("max_output_tokens", None)
        if isinstance(self.client, LLMService):
            kwargs.update(client_name=self.client_name, model=self.model)
            kwargs.setdefault("reasoning_level", "low")
            kwargs["metadata"] = {
                **(kwargs.get("metadata") or {}),
                "reasoning_policy": "internal",
                "reasoning_source": (kwargs.get("metadata") or {}).get(
                    "reasoning_source", "background_internal",
                ),
            }
            if self.request_timeout_seconds is not None:
                kwargs["metadata"]["background_timeout_seconds"] = self.request_timeout_seconds
        elif not _accepts_keyword(self.client, "metadata"):
            # Keep the compatibility wrapper transparent to old narrow
            # complete_text signatures; reasoning metadata belongs to the
            # shared LLMService boundary.
            kwargs.pop("metadata", None)
        result = self.client.complete_text(**kwargs)
        if self.request_timeout_seconds is None or not inspect.isawaitable(result):
            return result

        async def bounded_result():
            try:
                return await asyncio.wait_for(result, timeout=self.request_timeout_seconds)
            except TimeoutError as exc:
                raise LLMTimeoutError("Background generation exceeded its fallback deadline.") from exc

        return bounded_result()

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
        return self.limit_output_tokens and (
            isinstance(self.client, LLMService) or _accepts_keyword(self.client, "max_output_tokens")
        )


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
    real_service = isinstance(client, LLMService) or (
        isinstance(client, SelectedBackgroundClient) and isinstance(client.client, LLMService)
    )
    output_budget = getattr(client, "supports_output_budget", None)
    can_set_output_budget = (
        real_service
        or (callable(output_budget) and output_budget())
        or (not callable(output_budget) and _accepts_keyword(client, "max_output_tokens"))
    )
    explicit_output_limit = isinstance(kwargs.get("max_output_tokens"), int) and not isinstance(
        kwargs.get("max_output_tokens"), bool
    )
    prompt_estimate = _estimate_prompt_tokens(kwargs)
    if real_service:
        prompt_estimate = max(prompt_estimate, _service_prompt_estimate(client, kwargs))
    available = _remaining_job_tokens(client) if real_service else None
    if total_tokens_budget is not None:
        available = min(available, total_tokens_budget) if available is not None else total_tokens_budget
    ignore_stage_limit = isinstance(client, SelectedBackgroundClient) and not client.limit_output_tokens
    if real_service and can_set_output_budget:
        output_cap = _service_output_cap(client, available, prompt_estimate)
        if ignore_stage_limit:
            if output_cap is not None:
                kwargs["max_output_tokens"] = output_cap
                kwargs["_background_budget_output_cap"] = True
            else:
                kwargs.pop("max_output_tokens", None)
                kwargs.pop("_background_budget_output_cap", None)
        elif explicit_output_limit:
            if output_cap is not None and output_cap < kwargs["max_output_tokens"]:
                kwargs["max_output_tokens"] = output_cap
                if isinstance(client, SelectedBackgroundClient):
                    kwargs["_background_budget_output_cap"] = True
        elif output_cap is not None:
            kwargs["max_output_tokens"] = output_cap
            if isinstance(client, SelectedBackgroundClient):
                kwargs["_background_budget_output_cap"] = True
    elif can_set_output_budget and not explicit_output_limit:
        if not real_service:
            kwargs.setdefault("max_output_tokens", initial_output_tokens)
    recovery_output_tokens = reserve_tokens if reserve_tokens is not None else recovery_output_tokens
    initial_reservation = _output_reservation(kwargs, initial_output_tokens)
    if (total_tokens_budget is not None and not real_service
            and prompt_estimate + initial_reservation > total_tokens_budget):
        from app.core.llm_workloads import BackgroundTaskBudgetExceeded

        raise BackgroundTaskBudgetExceeded("background_generation_budget_exceeded")
    response = complete_text_in_worker(client, **kwargs)
    charged = response_usage_tokens(response)
    charged = charged if charged is not None else prompt_estimate + initial_reservation
    attempts = 1
    if not _incomplete(response) or recovery_output_tokens <= 0:
        _mark_recovery(response, attempts=attempts, recovered=False, tokens=charged)
        return response
    remaining = getattr(client, "remaining_job_tokens", None)
    available = _remaining_job_tokens(client)
    if total_tokens_budget is not None:
        total_remaining = max(0, total_tokens_budget - charged)
        available = min(available, total_remaining) if available is not None else total_remaining
    if real_service:
        if available is not None:
            if available <= prompt_estimate:
                _mark_recovery(response, attempts=attempts, recovered=False, tokens=charged)
                return response
            recovery_cap = _service_output_cap(client, available, prompt_estimate)
        else:
            recovery_cap = None
    else:
        recovery_cap = recovery_output_tokens
        if total_tokens_budget is not None and total_tokens_budget - charged < prompt_estimate + recovery_output_tokens:
            _mark_recovery(response, attempts=attempts, recovered=False, tokens=charged)
            return response
    # The first call has already been charged in the authoritative ledger.
    # Non-ledger wrappers are checked against the explicit call reserve below.
    if (callable(remaining) and not real_service and available is not None
            and available < prompt_estimate + recovery_output_tokens):
        _mark_recovery(response, attempts=attempts, recovered=False, tokens=charged)
        return response
    retry = dict(kwargs)
    retry["metadata"] = {
        **(retry.get("metadata") or {}),
        "reasoning_source": "recovery",
    }
    supports = getattr(client, "supports_thinking_control", None)
    if callable(supports) and supports():
        retry["thinking_enabled"] = False
    if can_set_output_budget:
        if real_service:
            if ignore_stage_limit:
                if recovery_cap is not None:
                    retry["max_output_tokens"] = recovery_cap
                    retry["_background_budget_output_cap"] = True
                else:
                    retry.pop("max_output_tokens", None)
                    retry.pop("_background_budget_output_cap", None)
            elif explicit_output_limit:
                retry_cap = kwargs["max_output_tokens"]
                if recovery_cap is not None:
                    retry_cap = min(retry_cap, recovery_cap)
                retry["max_output_tokens"] = retry_cap
                if isinstance(client, SelectedBackgroundClient) and retry_cap < kwargs["max_output_tokens"]:
                    retry["_background_budget_output_cap"] = True
            elif recovery_cap is not None:
                retry["max_output_tokens"] = recovery_cap
                if isinstance(client, SelectedBackgroundClient):
                    retry["_background_budget_output_cap"] = True
            else:
                retry.pop("max_output_tokens", None)
                retry.pop("_background_budget_output_cap", None)
        else:
            retry["max_output_tokens"] = recovery_output_tokens
    recovered = complete_text_in_worker(client, **retry)
    retry_cost = response_usage_tokens(recovered)
    retry_reservation = _output_reservation(retry, recovery_output_tokens)
    charged += retry_cost if retry_cost is not None else prompt_estimate + retry_reservation
    _mark_recovery(recovered, attempts=2, recovered=not _incomplete(recovered), tokens=charged)
    return recovered


def _output_reservation(kwargs: dict[str, Any], fallback: int) -> int:
    value = kwargs.get("max_output_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else fallback


def _remaining_job_tokens(client: Any) -> int | None:
    remaining = getattr(client, "remaining_job_tokens", None)
    if callable(remaining):
        return remaining()
    service = client if isinstance(client, LLMService) else None
    if service is None or service.workloads is None:
        return None
    from app.core.llm_workloads import current_workload

    workload = current_workload()
    if not workload.task_id or not workload.max_tokens:
        return None
    with service.workloads._connect() as conn:
        used = conn.execute(
            "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM llm_workload_usage WHERE task_id=?",
            (workload.task_id,),
        ).fetchone()[0]
    return max(0, workload.max_tokens - used)


def _service_output_cap(client: Any, available: int | None, prompt_estimate: int) -> int | None:
    """Bound a service request by task and route capacity, never by estimates alone."""
    service = client if isinstance(client, LLMService) else client.client
    client_name, model = _service_identity(client, service)
    resolved = service.config.resolve_model_config(client_name, model)
    context = getattr(resolved, "context_window_tokens", None) if resolved is not None else None
    context_cap = None
    context_room = None
    if context is not None:
        context_room = context - prompt_estimate - 4096
        configured_reserve = (
            getattr(resolved, "output_reserve_tokens", None)
            or getattr(service.config, "output_reserve_tokens", None)
            or 8192
        )
        if context_room <= 0:
            from app.core.llm_workloads import BackgroundTaskBudgetExceeded

            raise BackgroundTaskBudgetExceeded("background_generation_context_capacity_exhausted")
        # Only add a capacity-derived cap when the service's default admission
        # reserve would otherwise exceed the route's remaining context.
        if context_room < configured_reserve:
            context_cap = context_room
    budget_cap = None
    if available is not None and (context is None or available < context):
        budget_cap = available - prompt_estimate
        if context_room is not None:
            budget_cap = min(budget_cap, context_room)
        if budget_cap <= 0:
            from app.core.llm_workloads import BackgroundTaskBudgetExceeded

            raise BackgroundTaskBudgetExceeded("background_generation_budget_exceeded")
    caps = [cap for cap in (budget_cap, context_cap) if cap is not None]
    return min(caps) if caps else None


def _service_identity(client: Any, service: LLMService) -> tuple[str | None, str]:
    client_name = getattr(client, "client_name", None) or service.config.default_client
    if not client_name:
        registered = service.registry.list_clients()
        client_name = registered[0].name if registered else None
    model = getattr(client, "model", None)
    if not model:
        provider = service.registry.get(client_name) if client_name else None
        model = getattr(provider, "default_model", None) or service.config.model
    return client_name, model


def _service_prompt_estimate(client: Any, kwargs: dict[str, Any]) -> int:
    service = client if isinstance(client, LLMService) else client.client
    client_name, model = _service_identity(client, service)
    resolved = service.config.resolve_model_config(client_name, model)
    user_content = kwargs.get("user_prompt", "")
    image_blocks = kwargs.get("image_blocks") or []
    audio_blocks = kwargs.get("audio_blocks") or []
    if image_blocks or audio_blocks:
        user_content = [LLMTextBlock(text=str(user_content)), *image_blocks, *audio_blocks]
    request = LLMRequest(
        client_name=client_name,
        model=model,
        messages=[
            LLMMessage(role="system", content=str(kwargs.get("system_prompt", ""))),
            LLMMessage(role="user", content=user_content),
        ],
        prompt_summary=str(kwargs.get("prompt_summary", "background_generation")),
        tools=kwargs.get("tools") or [],
    )
    path = getattr(resolved, "tokenizer_json_path", None) if resolved is not None else None
    return service._count_request(request, path)


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
