"""Deterministic prompt fitting at the provider-request boundary.

The session-history budget is separate. This module never mutates the persisted
window, tool result cache, or user-owned instruction files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


class RequestTokenCounter(Protocol):
    def count_request(
        self, system_prompt: str, user_prompt: str, tools: list[Any] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class BudgetedPrompt:
    user_prompt: str
    input_tokens: int
    input_limit: int
    count_method: str
    conservative: bool
    omitted: dict[str, Any]
    output_reserve_tokens: int | None = None


class PromptBudgetExceeded(ValueError):
    def __init__(self, *, needed: int, limit: int, reason: str) -> None:
        super().__init__(
            f"Prompt requires {needed} estimated input tokens, but safe limit is "
            f"{limit}; {reason}. Reduce the current input or configure a larger "
            "verified model context window."
        )
        self.needed = needed
        self.limit = limit
        self.reason = reason


class PromptBudgeter:
    """Preserve mandatory contracts and trim only lower-priority, recoverable views."""

    def __init__(self, counter: RequestTokenCounter) -> None:
        self.counter = counter

    def fit(
        self, *, system_prompt: str, user_prompt: str, input_limit: int,
        tools: list[Any] | None = None, output_reserve_tokens: int | None = None,
    ) -> BudgetedPrompt:
        if input_limit < 1:
            raise ValueError("input_limit must be positive")
        counted = self.counter.count_request(system_prompt, user_prompt, tools)
        if counted.count <= input_limit:
            return BudgetedPrompt(
                user_prompt, counted.count, input_limit, counted.method,
                counted.conservative, {}, output_reserve_tokens,
            )
        try:
            payload = json.loads(user_prompt)
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            raise PromptBudgetExceeded(
                needed=counted.count, limit=input_limit,
                reason="the prompt has no safely trimmable structured sections",
            )

        omitted: dict[str, Any] = {}

        def recount() -> None:
            nonlocal user_prompt, counted
            if omitted:
                payload["_prompt_budget"] = omitted
            user_prompt = json.dumps(payload, ensure_ascii=False, indent=2)
            counted = self.counter.count_request(system_prompt, user_prompt, tools)

        observations = payload.get("observations")
        while counted.count > input_limit and isinstance(observations, list) and observations:
            first = observations[0]
            if isinstance(first, dict) and first.get("action") == "fork_subtasks":
                if not first.get("_prompt_budget_compacted"):
                    observations[0] = self._fork_status_summary(first)
                    omitted["compacted_fork_results"] = omitted.get("compacted_fork_results", 0) + 1
                    recount()
                    continue
                if len(observations) == 1:
                    # The Planner must retain a replan/failure signal. Fail
                    # explicitly rather than silently turn a child outcome
                    # into an apparent absence of evidence.
                    break
            old = observations.pop(0)
            omitted["older_observations"] = omitted.get("older_observations", 0) + 1
            if isinstance(old, dict):
                cache = old.get("_result_cache")
                if isinstance(cache, dict) and isinstance(cache.get("artifact_id"), str):
                    refs = omitted.setdefault("observation_artifact_ids", [])
                    if len(refs) < 20:
                        refs.append(cache["artifact_id"])
            recount()

        window = payload.get("session_context_window")
        if isinstance(window, dict):
            memories = window.get("recalled_memories")
            items = memories.get("items") if isinstance(memories, dict) else None
            while counted.count > input_limit and isinstance(items, list) and items:
                items.pop()
                omitted["lower_ranked_memories"] = omitted.get("lower_ranked_memories", 0) + 1
                recount()

            recent = window.get("recent_messages")
            while counted.count > input_limit and isinstance(recent, list) and len(recent) > 2:
                recent.pop(0)
                omitted["older_context_messages"] = omitted.get("older_context_messages", 0) + 1
                recount()

            instructions = window.get("agent_instructions")
            if isinstance(instructions, list):
                # Keep the global prelude and most-specific project preview as
                # long as possible. Every omitted preview retains its path,
                # version and byte-offset continuation metadata.
                order = [*range(1, max(1, len(instructions) - 1))]
                if len(instructions) > 1:
                    order.append(len(instructions) - 1)
                if instructions:
                    order.append(0)
                for index in order:
                    if counted.count <= input_limit:
                        break
                    item = instructions[index]
                    if not isinstance(item, dict) or not item.get("content"):
                        continue
                    item["content"] = ""
                    item["preview_omitted"] = True
                    item["truncated"] = True
                    item["next_offset"] = 0
                    kind = item.get("kind")
                    item["read_more"] = (
                        "instructions.read(kind='global', offset=0)"
                        if kind == "global" else
                        f"instructions.read_project(path={item.get('path')!r}, offset=0)"
                    )
                    omitted["instruction_previews"] = omitted.get("instruction_previews", 0) + 1
                    recount()

        if counted.count > input_limit:
            raise PromptBudgetExceeded(
                needed=counted.count, limit=input_limit,
                reason="mandatory system, current-task, schema or raw-tail content cannot be dropped",
            )
        return BudgetedPrompt(
            user_prompt, counted.count, input_limit, counted.method,
            counted.conservative, omitted, output_reserve_tokens,
        )

    @staticmethod
    def _fork_status_summary(observation: dict[str, Any]) -> dict[str, Any]:
        summary = {
            key: observation[key]
            for key in (
                "action", "status", "execution_status", "operation_id", "plan_id",
                "failed_step_ids", "replan_required", "waiting_child_run_ids",
            ) if key in observation
        }
        results = observation.get("task_results")
        if isinstance(results, list):
            summary["task_results"] = [
                {
                    **{key: row[key] for key in ("step_id", "child_run_id", "status") if key in row},
                    "summary": str(row.get("summary") or "")[:160],
                    "failure": {
                        key: row["failure"][key]
                        for key in ("category", "code", "retryable")
                        if isinstance(row.get("failure"), dict) and key in row["failure"]
                    },
                }
                for row in results[:20] if isinstance(row, dict)
            ]
            summary["omitted_task_result_count"] = max(0, len(results) - 20)
        summary["_prompt_budget_compacted"] = True
        return summary
