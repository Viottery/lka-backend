"""Deterministic prompt fitting at the provider-request boundary.

The session-history budget is separate. This module never mutates the persisted
window, tool result cache, or user-owned instruction files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.observation_context import valid_resource_descriptor


def serialize_prompt_payload(payload: Any) -> str:
    """Encode a constructed payload without changing values or parsing raw text."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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
    approximate: bool = False


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
                getattr(counted, "approximate", False),
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

        omitted = self._existing_budget_omissions(payload.get("_prompt_budget"))

        def recount() -> None:
            nonlocal user_prompt, counted
            if omitted:
                payload["_prompt_budget"] = omitted
            user_prompt = serialize_prompt_payload(payload)
            counted = self.counter.count_request(system_prompt, user_prompt, tools)

        # Initial input previews can be recovered from their registered source
        # routes. Reduce them before evicting observations of later source pages.
        sources = payload.get("input_sources")
        while counted.count > input_limit and isinstance(sources, list):
            candidates = [source for source in sources if isinstance(source, dict)
                          and isinstance(source.get("text"), str) and len(source["text"]) > 256
                          and type(source.get("start_char")) is int
                          and type(source.get("total_chars")) is int
                          and valid_resource_descriptor(source.get("_resource"))]
            if not candidates:
                break
            source = max(candidates, key=lambda item: len(item["text"]))
            old_length = len(source["text"])
            source["text"] = source["text"][:max(256, old_length // 2)]
            source["end_char"] = source["start_char"] + len(source["text"])
            source["next_start_char"] = source["end_char"]
            source["truncated"] = True
            resource = source["_resource"]
            source["read_more"] = {
                "package": resource["read_package"], "tool": resource["read_tool"],
                "input": {**resource["read_input"], "start_char": source["end_char"]},
            }
            omitted["input_source_preview_chars"] = omitted.get("input_source_preview_chars", 0) + old_length - len(source["text"])
            recount()

        observations = payload.get("observations")
        # Shrink child reports before sacrificing context for them. Never
        # replace a small verdict with a larger synthetic envelope.
        if isinstance(observations, list):
            for index, item in enumerate(observations):
                if counted.count <= input_limit:
                    break
                if not isinstance(item, dict) or item.get("action") != "fork_subtasks" or item.get("_prompt_budget_compacted"):
                    continue
                compacted = self._fork_status_summary(item)
                if len(serialize_prompt_payload(compacted).encode("utf-8")) < len(serialize_prompt_payload(item).encode("utf-8")):
                    observations[index] = compacted
                    omitted["compacted_fork_results"] = omitted.get("compacted_fork_results", 0) + 1
                    recount()

        recent_view_limit = min(4096, input_limit // 2)
        recent_views = {id(item) for item in observations[-2:]
                        if len(serialize_prompt_payload(item).encode("utf-8")) <= recent_view_limit
                        } if isinstance(observations, list) else set()
        if isinstance(observations, list):
            for item in reversed(observations):
                if (isinstance(item, dict) and isinstance(item.get("tool_name"), str)
                        and isinstance(item.get("result"), dict)):
                    # Keep one small source observation even when recent
                    # control feedback follows it. Never search its prose for
                    # supposedly important facts or truncate its qualifiers.
                    if len(serialize_prompt_payload(item).encode("utf-8")) <= recent_view_limit:
                        recent_views.add(id(item))
                    break

        def trim_observations(*, keep_recent: bool) -> None:
            while counted.count > input_limit and isinstance(observations, list) and observations:
                index = next((i for i, item in enumerate(observations)
                              if not keep_recent or id(item) not in recent_views), None)
                if index is None:
                    break
                first = observations[index]
                if isinstance(first, dict) and first.get("action") == "fork_subtasks" and len(observations) == 1:
                    # Keep the failure/replan signal even when it cannot
                    # be compacted. Mandatory overflow remains explicit.
                    break
                old = observations.pop(index)
                projection = self._projection_omissions(old)
                old_count, old_refs = projection if projection is not None else (1, [])
                omitted["older_observations"] = omitted.get("older_observations", 0) + old_count
                if id(old) in recent_views:
                    omitted["recent_observations"] = omitted.get("recent_observations", 0) + 1
                if projection is None and isinstance(old, dict):
                    cache = old.get("_result_cache")
                    if isinstance(cache, dict) and isinstance(cache.get("artifact_id"), str):
                        old_refs = [cache["artifact_id"]]
                for artifact_id in old_refs:
                    refs = omitted.setdefault("observation_artifact_ids", [])
                    if len(refs) >= 20:
                        break
                    if artifact_id not in refs:
                        refs.append(artifact_id)
                resources = []
                if isinstance(old, dict):
                    if projection is not None:
                        resources = old.get("omitted_resources", [])
                    elif valid_resource_descriptor(old.get("_resource")):
                        resources = [old["_resource"]]
                for resource in resources:
                    kept = omitted.setdefault("observation_resources", [])
                    if len(kept) < 4 and not any(item["identity"] == resource["identity"] for item in kept):
                        kept.append(resource)
                recount()

        trim_observations(keep_recent=True)

        window = payload.get("session_context_window")
        if isinstance(window, dict):
            memories = window.get("recalled_memories")
            items = memories.get("items") if isinstance(memories, dict) else None
            while counted.count > input_limit and isinstance(items, list) and items:
                items.pop()
                omitted["lower_ranked_memories"] = omitted.get("lower_ranked_memories", 0) + 1
                recount()

            recent = window.get("recent_messages")
            intent_history = isinstance(window.get("summary_metadata"), dict) and (
                window["summary_metadata"].get("method") == "conversation_intent"
            )
            while counted.count > input_limit and isinstance(recent, list) and recent:
                first = recent[0]
                trace = first.get("trace_id") if isinstance(first, dict) else None
                if isinstance(trace, str) and trace:
                    indices = [i for i, item in enumerate(recent)
                               if isinstance(item, dict) and item.get("trace_id") == trace]
                elif isinstance(first, dict) and first.get("role") in {"user", "agent", "assistant", "tool"}:
                    # Old untraced history has no batch identity. Keep its
                    # user/answer prefix together up to the next user input.
                    boundary = next((i for i, item in enumerate(recent[1:], 1)
                                     if isinstance(item, dict) and item.get("role") == "user"), len(recent))
                    indices = list(range(boundary))
                else:
                    indices = [0]  # Compatibility with older untyped views.
                if not intent_history and len(recent) - len(indices) < 2:
                    break  # Existing profiles retain their mandatory raw tail.
                for index in reversed(indices):
                    recent.pop(index)
                omitted["older_context_messages"] = omitted.get("older_context_messages", 0) + len(indices)
                metadata = window.get("summary_metadata")
                if isinstance(metadata, dict):
                    # Source sequence bounds describe the pre-fit projection,
                    # since legacy message payloads do not carry individual seq.
                    metadata["source_range_before_prompt_fit"] = True
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

        # Recent evidence has priority over old context/memory previews, but
        # never enlarges the verified input budget. Preserve handles and mark
        # omissions when even this bounded working view cannot fit.
        trim_observations(keep_recent=False)

        if counted.count > input_limit:
            raise PromptBudgetExceeded(
                needed=counted.count, limit=input_limit,
                reason="mandatory system, current-task, schema or raw-tail content cannot be dropped",
            )
        return BudgetedPrompt(
            user_prompt, counted.count, input_limit, counted.method,
            counted.conservative, omitted, output_reserve_tokens,
            getattr(counted, "approximate", False),
        )

    @staticmethod
    def _existing_budget_omissions(value: Any) -> dict[str, Any]:
        """Carry bounded server counters forward only when another trim is needed."""
        counters = {
            "older_observations", "compacted_fork_results", "lower_ranked_memories",
            "older_context_messages", "instruction_previews", "input_source_preview_chars",
            "recent_observations",
        }
        if not isinstance(value, dict) or set(value) - (counters | {"observation_artifact_ids", "observation_resources"}):
            return {}
        if any(type(value[key]) is not int or value[key] <= 0 for key in counters & value.keys()):
            return {}
        refs = value.get("observation_artifact_ids", [])
        if (
            not isinstance(refs, list) or len(refs) > 20
            or any(not isinstance(ref, str) or not 1 <= len(ref) <= 200 for ref in refs)
        ):
            return {}
        resources = value.get("observation_resources", [])
        if not isinstance(resources, list) or len(resources) > 4 or not all(valid_resource_descriptor(item) for item in resources):
            return {}
        result = dict(value)
        if "observation_artifact_ids" in result:
            result["observation_artifact_ids"] = list(dict.fromkeys(refs))
        return result

    @staticmethod
    def _projection_omissions(observation: Any) -> tuple[int, list[str]] | None:
        """Read only the server projection envelope, never tool data or prose.

        Tool observations have tool_name/result and control observations have
        action; neither can impersonate this exact top-level envelope. Handles
        remain continuation hints, not permissions or evidence endorsements.
        """
        if not isinstance(observation, dict) or set(observation) - {"omitted_resources"} != {
            "_prompt_compacted", "summary", "omitted_observation_count", "omitted_result_artifacts",
        }:
            return None
        count = observation["omitted_observation_count"]
        refs = observation["omitted_result_artifacts"]
        if (
            observation["_prompt_compacted"] is not True
            or not isinstance(observation["summary"], str)
            or not isinstance(count, int) or isinstance(count, bool) or count <= 0
            or not isinstance(refs, list) or len(refs) > 20
            or any(not isinstance(ref, str) or not 1 <= len(ref) <= 200 for ref in refs)
            or not isinstance(observation.get("omitted_resources", []), list)
            or len(observation.get("omitted_resources", [])) > 4
            or not all(valid_resource_descriptor(item) for item in observation.get("omitted_resources", []))
        ):
            return None
        return count, refs

    @staticmethod
    def _fork_status_summary(observation: dict[str, Any]) -> dict[str, Any]:
        summary = {
            key: observation[key]
            for key in (
                "action", "status", "execution_status", "operation_id", "plan_id",
                "failed_step_ids", "replan_required", "waiting_child_run_ids",
                "skipped_step_ids",
            ) if key in observation
        }
        notes = observation.get("degradation_notes")
        if isinstance(notes, dict):
            summary["degradation_notes"] = {
                str(key)[:80]: str(value or "")[:300] for key, value in list(notes.items())[:8]
            }
            summary["omitted_degradation_note_count"] = max(0, len(notes) - 8)
        verification = observation.get("verification")
        if isinstance(verification, dict):
            # Keep the verdict together with its qualifications and failed
            # requirements. A status alone can overstate what was verified.
            summary["verification"] = verification
        remaining = 20
        for field in ("task_results", "historical_task_results"):
            results = observation.get(field)
            if not isinstance(results, list):
                continue
            selected = results[:remaining]
            remaining -= len(selected)
            summary[field] = [
                {
                    **{key: row[key] for key in ("step_id", "child_run_id", "status", "attempt", "result_id", "snapshot_id") if key in row},
                    "summary": str(row.get("summary") or "")[:160],
                    **({"historical_only": True} if field == "historical_task_results" else {}),
                    "missing_requirements": [str(value)[:160] for value in row.get("missing_requirements", [])[:6]]
                    if isinstance(row.get("missing_requirements"), list) else [],
                    "failure": {
                        key: row["failure"][key]
                        for key in ("category", "code", "retryable")
                        if isinstance(row.get("failure"), dict) and key in row["failure"]
                    },
                }
                for row in selected if isinstance(row, dict)
            ]
            summary[f"omitted_{field[:-1]}_count"] = max(0, len(results) - len(selected))
        summary["_prompt_budget_compacted"] = True
        return summary
