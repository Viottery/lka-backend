from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.background_llm import (
    BatchedExtractionClient,
    IncompleteGenerationError,
    SelectedBackgroundClient,
    recover_generation,
    require_complete_response,
)


class SequenceClient:
    def __init__(self, responses, *, thinking=False, remaining=None):
        self.responses = list(responses)
        self.calls = []
        self._thinking = thinking
        self.remaining = remaining

    def complete_text(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def supports_thinking_control(self):
        return self._thinking

    def remaining_job_tokens(self):
        return self.remaining


def response(content, *, finish_reason=None, partial=False, usage=None):
    return SimpleNamespace(
        content=content, finish_reason=finish_reason, partial=partial,
        usage=usage or {}, metadata={},
    )


@pytest.mark.parametrize("first", [
    response('{"candidates":[]}'),
])
def test_complete_generation_succeeds_without_retry(first):
    client = SequenceClient([first])
    assert recover_generation(
        client, system_prompt="s", user_prompt="u", prompt_summary="test",
    ) is first
    assert len(client.calls) == 1


@pytest.mark.parametrize("first", [
    response('{"candidates":[]}', finish_reason="length"),
    response("", finish_reason="stop"),
    response('{"candidates":[]}', partial=True),
])
def test_incomplete_shapes_recover_even_when_first_json_parses(first):
    recovered = response('{"candidates":[]}')
    client = SequenceClient([first, recovered], thinking=True)
    result = recover_generation(
        client, system_prompt="s", user_prompt="u", prompt_summary="test",
        initial_output_tokens=128, recovery_output_tokens=1024,
    )
    assert result is recovered
    assert len(client.calls) == 2
    assert client.calls[0]["max_output_tokens"] == 128
    assert client.calls[1]["max_output_tokens"] == 1024
    assert client.calls[1]["thinking_enabled"] is False
    assert recovered.metadata["recovery_attempts"] == 2
    assert recovered.metadata["recovered"] is True
    assert recovered.metadata["generation_tokens"] >= 128 + 1024
    require_complete_response(result)


def test_unsupported_thinking_control_is_not_passed_to_selected_legacy_client():
    class LegacyClient:
        def complete_text(self, *, system_prompt, user_prompt, prompt_summary,
                          temperature=0, max_output_tokens=None):
            self.calls.append(max_output_tokens)
            return self.responses.pop(0)

        def __init__(self):
            self.calls = []
            self.responses = [
                response("", finish_reason="stop"),
                response('{"candidates":[]}'),
            ]

    wrapped = SelectedBackgroundClient(LegacyClient())
    recovered = recover_generation(
        wrapped, system_prompt="s", user_prompt="u", prompt_summary="test",
        initial_output_tokens=128, recovery_output_tokens=1024,
    )
    assert recovered.content == '{"candidates":[]}'
    assert wrapped.client.calls == [128, 1024]


def test_low_budget_suppresses_recovery_and_response_stays_incomplete():
    first = response("", finish_reason="length")
    client = SequenceClient([first], remaining=1)
    result = recover_generation(
        client, system_prompt="system", user_prompt="user", prompt_summary="test",
        initial_output_tokens=128, recovery_output_tokens=1024,
    )
    assert result is first
    assert len(client.calls) == 1
    with pytest.raises(IncompleteGenerationError):
        require_complete_response(result)


def test_batch_incomplete_generation_makes_exactly_two_provider_calls():
    good = response(json.dumps({"candidates": []}))
    client = SequenceClient([
        response('{"candidates":[]}', finish_reason="length"), good,
    ])
    batch = BatchedExtractionClient(client, ["ordinary text"])
    result = batch.complete_text(
        system_prompt="extract", user_prompt="ordinary text", prompt_summary="test",
    )
    # Invoke the async protocol synchronously for this direct focused unit.
    resolved = asyncio.run(result)
    assert json.loads(resolved.content) == {"candidates": []}
    assert len(client.calls) == 2


def test_recovery_records_provider_usage_for_both_attempts():
    client = SequenceClient([
        response("", finish_reason="length", usage={"prompt_tokens": 8, "completion_tokens": 3}),
        response("done", usage={"prompt_tokens": 9, "completion_tokens": 12}),
    ])
    result = recover_generation(
        client, system_prompt="system", user_prompt="input", prompt_summary="test",
    )
    assert result.metadata["generation_tokens"] == 32


def test_total_budget_suppresses_second_call_without_a_ledger():
    client = SequenceClient([response("", finish_reason="length")], thinking=True)
    result = recover_generation(client, system_prompt="s", user_prompt="u", prompt_summary="t",
                                initial_output_tokens=128, recovery_output_tokens=1024,
                                total_tokens_budget=500)
    assert len(client.calls) == 1
    assert result.metadata["recovery_attempts"] == 1


def test_initial_call_is_not_dispatched_when_total_budget_is_exhausted():
    from app.core.llm_workloads import BackgroundTaskBudgetExceeded

    client = SequenceClient([])
    with pytest.raises(BackgroundTaskBudgetExceeded):
        recover_generation(client, system_prompt="s", user_prompt="u", prompt_summary="t",
                           total_tokens_budget=1)
    assert client.calls == []


def test_persistent_incomplete_batch_does_not_cache_success():
    client = SequenceClient([response("", finish_reason="length"), response("", finish_reason="length")])
    batch = BatchedExtractionClient(SelectedBackgroundClient(client), ["ordinary text"])
    with pytest.raises(IncompleteGenerationError):
        asyncio.run(batch.complete_text(system_prompt="s", user_prompt="ordinary text", prompt_summary="t"))
    assert len(client.calls) == 2
    assert batch.cache == {}


def test_complete_but_malformed_batch_fails_closed_and_is_cached_empty():
    client = SequenceClient([response("not json")])
    batch = BatchedExtractionClient(client, ["ordinary text"])
    first = asyncio.run(batch.complete_text(
        system_prompt="extract", user_prompt="ordinary text", prompt_summary="test",
    ))
    again = asyncio.run(batch.complete_text(
        system_prompt="extract", user_prompt="ordinary text", prompt_summary="test",
    ))
    assert first.content == again.content == '{"candidates":[]}'
    assert len(client.calls) == 1


@pytest.mark.parametrize("failure", ["length", "budget", "invalid_json"])
def test_compaction_falls_back_preserving_constraints_and_old_summary(failure):
    from app.core.llm_workloads import BackgroundTaskBudgetExceeded
    from app.core.memory_background import MemoryBackgroundCoordinator
    from app.core.sessions import SessionRecentMessage, SessionService

    if failure == "budget":
        outputs = [BackgroundTaskBudgetExceeded("exhausted")]
    elif failure == "invalid_json":
        outputs = [response("not JSON")]
    else:
        outputs = [response('{"summary":"unsafe partial"}', finish_reason="length")] * 2
    client = SequenceClient(outputs, thinking=True)
    coordinator = MemoryBackgroundCoordinator(
        db_path=":memory:", memory=None, store=None,
        session_service=SessionService(lambda: None), llm_client=client,
    )
    summary = coordinator._summarize("旧目标：整理票务", [SessionRecentMessage(
        role="user", content="更正：10月21日，不要自动付款；预算上限600元。",
        created_at="2026-10-02T00:00:00Z", trace_id="source-1",
    )], 16384)
    assert "不要自动付款" in summary
    assert "10月21日" in summary
    assert "600" in summary
    assert "unsafe partial" not in summary
    assert "旧目标" in summary
    assert "source-1" in summary
    assert coordinator._compaction_state.used_local_fallback is True


def test_configured_selected_budget_applies_to_extraction():
    from app.core.memory_extraction import extract_user_memories

    client = SequenceClient([response('{"candidates":[]}')])
    selected = SelectedBackgroundClient(client, initial_output_tokens=6000, recovery_output_tokens=12000)
    assert extract_user_memories(source_id="s", content="我倾向在审阅合同时先看风险再看条款",
                                 llm_client=selected, allow_remote=True) == []
    assert client.calls[0]["max_output_tokens"] == 6000


def test_unlimited_compaction_does_not_fall_back_after_crossing_old_allowance():
    from app.core.memory_background import MemoryBackgroundCoordinator
    from app.core.sessions import SessionRecentMessage, SessionService

    client = SequenceClient([response('{"summary":"完整摘要","source_trace_ids":[]}') for _ in range(3)])
    coordinator = MemoryBackgroundCoordinator(db_path=":memory:", memory=None, store=None,
        session_service=SessionService(lambda: None), llm_client=client, max_job_tokens=0)
    messages = [SessionRecentMessage(role="user", content="保留用户限定" * 320,
                created_at="2026-10-07T00:00:00Z", trace_id=f"source-{i}") for i in range(3)]
    coordinator._summarize("", messages, 16384)
    assert len(client.calls) == 3
    assert coordinator._compaction_state.used_local_fallback is False


def test_zero_allowance_allows_bounded_incomplete_generation_recovery():
    from app.core.llm_workloads import workload_scope

    client = SequenceClient([response("partial", finish_reason="length"), response("complete")], thinking=True)
    selected = SelectedBackgroundClient(client)
    # A zero configured allowance must not be mistaken for zero remaining.
    with workload_scope("background_memory", task_id="compact", max_tokens=0):
        result = recover_generation(selected, system_prompt="s", user_prompt="u", prompt_summary="test")
    assert result.content == "complete"
    assert len(client.calls) == 2
