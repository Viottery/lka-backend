from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core import agent_turn as turn
from app.core.agent_runs import AgentRunCancelled, InMemoryAgentRunManager
from app.core.llm import (
    LLMClientError,
    LLMRateLimitError,
    LLMResponse,
    LLMResponseMode,
    LLMService,
    LLMStreamEvent,
)
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMClientConfig, LLMProviderConfig


def response(content="complete answer", *, reason="stop", partial=False, status="completed"):
    return LLMResponse(provider="test", status=status, content=content, prompt_summary="answer",
                       finish_reason=reason, partial=partial,
                       usage={"prompt_tokens": 500, "completion_tokens": 900, "total_tokens": 1400})


class Provider:
    def __init__(self, responses, *, name="selected", thinking_control=None, after_call=None):
        self.name = name
        self.default_model = "test-model"
        self.thinking_control = thinking_control
        self.responses = list(responses)
        self.requests = []
        self.after_call = after_call

    async def complete(self, request):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if self.after_call:
            self.after_call()
        return result.model_copy(update={"client_name": self.name, "model": request.model})

    async def stream(self, request):
        result = await self.complete(request)
        yield LLMStreamEvent(event_type="llm_delta", stage="answer", client_name=self.name,
                             provider="test", model=request.model, delta=result.content,
                             content_snapshot=result.content)
        yield LLMStreamEvent(event_type="llm_completed", stage="answer", client_name=self.name,
                             provider="test", model=request.model, content_snapshot=result.content,
                             metadata={"finish_reason": result.finish_reason, "usage": result.usage})


def make_loop(tmp_path, responses, *, selected_thinking=None, default_thinking=None):
    selected = Provider(responses, thinking_control=selected_thinking)
    default = Provider([], name="default", thinking_control=default_thinking)
    registry = LLMClientRegistry()
    registry.register_client(default)
    registry.register_client(selected)
    config = LLMProviderConfig(default_client="default", clients=[
        LLMClientConfig(name=name, default_model="test-model", context_window_tokens=100_000,
                        output_reserve_tokens=1024, thinking_control=control)
        for name, control in [("default", default_thinking), ("selected", selected_thinking)]
    ])
    manager = InMemoryAgentRunManager()
    loop = turn.AgentTurnLoop(session_service=None, tool_executor=None,
                              llm_client=LLMService(config=config, registry=registry),
                              log_dir=tmp_path, run_manager=manager)
    return loop, selected, manager


@contextmanager
def scope(manager, *, stream=False, child_budget=None):
    parent = manager.create_run(session_id="answer-test", user_input="test")
    run = parent
    if child_budget is not None:
        run = manager.create_child_run(parent_run_id=parent.run_id, plan_id="test-plan",
                                       step_id="answer", attempt=1, user_input="test")
        manager._update_run(run.run_id, status=run.status,
                            metadata_patch={"context_snapshot": {"budget": child_budget}})
    tokens = [(var, var.set(value)) for var, value in [
        (turn._turn_run_manager, manager), (turn._turn_run_id, run.run_id),
        (turn._turn_llm_client_name, "selected"),
        (turn._turn_llm_response_mode, LLMResponseMode.STREAM if stream else LLMResponseMode.TEXT),
    ]]
    try:
        yield run
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def answer(loop, events):
    return loop._answer_with_llm(user_input="Return the supported results.", route={},
                                 context_window={}, observations=[], final_decision=None,
                                 llm_events=events)


def test_complete_answer_needs_no_recovery(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response()])
    with scope(manager):
        assert answer(loop, []) == "complete answer"
    assert len(provider.requests) == 1
    assert provider.requests[0].thinking_enabled is None


@pytest.mark.parametrize("first", [
    response("truncated table |", reason="length"),
    response("", reason="length"), response("   "), response(partial=True),
    response(status="incomplete"),
])
def test_incomplete_answer_regenerates_once_with_same_cap_and_full_audit(tmp_path, first):
    loop, provider, manager = make_loop(tmp_path, [first, response("replacement answer")],
                                       selected_thinking="deepseek")
    events = []
    with scope(manager) as run:
        assert answer(loop, events) == "replacement answer"
        audit = [e for e in manager.list_events(run.run_id) if e.type == "llm_completed"]
    assert len(provider.requests) == len(events) == len(audit) == 2
    assert provider.requests[0].max_output_tokens == provider.requests[1].max_output_tokens == 1024
    assert provider.requests[0].thinking_enabled is None
    assert provider.requests[1].thinking_enabled is False
    assert [e.payload["budget_token_count"] for e in audit] == [1400, 1400]
    assert events[0].llm_call_id != events[1].llm_call_id
    assert provider.requests[0].messages == provider.requests[1].messages


def test_recovery_does_not_infer_thinking_from_other_client(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(reason="length"), response()],
                                       default_thinking="deepseek")
    with scope(manager):
        assert answer(loop, []) == "complete answer"
    assert provider.requests[1].thinking_enabled is None


def test_recovery_uses_frozen_child_selection_for_thinking_capability(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(reason="length"), response()],
                                       selected_thinking="deepseek")
    snapshot = SimpleNamespace(
        inference_selection_source="server_profile", inference_client_name="selected",
        inference_model="test-model", inference_reasoning_effort=None,
        inference_profile_id="test-profile", output_contract=None,
    )
    with scope(manager):
        client_token = turn._turn_llm_client_name.set(None)
        snapshot_token = turn._turn_inference_snapshot.set(snapshot)
        try:
            assert answer(loop, []) == "complete answer"
        finally:
            turn._turn_llm_client_name.reset(client_token)
            turn._turn_inference_snapshot.reset(snapshot_token)
    assert all(r.client_name == "selected" for r in provider.requests)
    assert provider.requests[1].thinking_enabled is False
    assert provider.requests[1].metadata["inference_profile_id"] == "test-profile"


def test_recovery_output_cap_is_clamped_to_child_tokens_remaining(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(reason="length"), response()])
    with scope(manager, child_budget={"max_tokens": 10000}) as run:
        def reduce_remaining():
            if len(provider.requests) != 1:
                return
            request = provider.requests[0]
            counted = loop._budget_llm_prompt(
                system_prompt=request.messages[0].content, user_prompt=request.messages[1].content,
                max_output_tokens=None, tools=None,
            )
            manager._update_run(run.run_id, status=run.status, metadata_patch={
                "context_snapshot": {"budget": {"max_tokens": 1400 + counted.input_tokens + 37}},
            })
        provider.after_call = reduce_remaining
        assert answer(loop, []) == "complete answer"
    assert provider.requests[0].max_output_tokens == 1024
    assert provider.requests[1].max_output_tokens == 37


@pytest.mark.parametrize("last", [response(reason="length"), response(""), response(partial=True)])
def test_twice_incomplete_is_explicit_failure_not_success(tmp_path, last):
    loop, provider, manager = make_loop(tmp_path, [response(reason="length"), last, response()])
    with scope(manager) as run:
        with pytest.raises(LLMClientError, match="incomplete"):
            answer(loop, [])
        assert not any(e.type in {"final_answer", "run_completed"}
                       for e in manager.list_events(run.run_id))
    assert len(provider.requests) == 2


def test_recovery_rate_limit_does_not_make_a_third_dispatch(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        response(reason="length"), LLMRateLimitError(message="limited", status_code=429, retry_after="0"),
        response(),
    ])
    with scope(manager), pytest.raises(LLMClientError):
        answer(loop, [])
    assert len(provider.requests) == 2


@pytest.mark.parametrize("budget", [{"max_llm_calls": 1}, {"max_tokens": 1500}])
def test_recovery_obeys_remaining_child_budget(tmp_path, budget, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [response(reason="length"), response()])
    original = loop._budget_llm_prompt
    # Match this fake provider's 500-token input, not the evolving writer
    # instruction's UTF-8 size. This unit tests remaining-budget recovery;
    # independent prompt-boundary tests cover real counters and long prompts.
    monkeypatch.setattr(loop, "_budget_llm_prompt", lambda **kwargs: replace(
        original(**kwargs), input_tokens=500,
    ))
    with scope(manager, child_budget=budget), pytest.raises(RuntimeError, match="budget"):
        answer(loop, [])
    assert len(provider.requests) == 1


def test_cancel_after_initial_generation_prevents_recovery(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(reason="length"), response()])
    with scope(manager) as run:
        provider.after_call = lambda: manager.request_cancel(run.run_id, "cancel recovery")
        with pytest.raises(AgentRunCancelled):
            answer(loop, [])
    assert len(provider.requests) == 1


def test_stream_recovery_is_nonstream_and_never_concatenates_two_delta_sequences(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        response("provisional truncated", reason="length"), response("full replacement"),
    ], selected_thinking="deepseek")
    with scope(manager, stream=True) as run:
        assert answer(loop, []) == "full replacement"
        deltas = [e for e in manager.list_events(run.run_id) if e.type == "llm_delta"]
        assert [e.payload["delta"] for e in deltas] == ["provisional truncated"]
        assert turn._turn_llm_response_mode.get() == LLMResponseMode.STREAM
    assert [r.response_mode for r in provider.requests] == [LLMResponseMode.STREAM, LLMResponseMode.TEXT]


def test_structured_answer_replaces_truncated_json_without_splicing(tmp_path, monkeypatch):
    contract = 'Return JSON.\n```json-schema\n{"type":"object","required":["value"],"properties":{"value":{"type":"integer"}}}\n```'
    monkeypatch.setattr(turn, "_current_output_contract", lambda: contract)
    loop, provider, manager = make_loop(tmp_path, [
        response('{"value":', reason="length"), response('{"value":42}'),
    ])
    with scope(manager, stream=True):
        assert answer(loop, []) == '{"value":42}'
    assert all(r.response_mode == LLMResponseMode.JSON for r in provider.requests)


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
@pytest.mark.parametrize("recovered", [True, False])
def test_orchestrator_marks_failed_recovery_failed_and_publishes_only_complete_final(
    tmp_path, monkeypatch, orchestrator, recovered,
):
    from app.api.main import create_app
    from app.core.config import get_settings

    config_path = tmp_path / "test.toml"
    config_path.write_text(f'[agent]\norchestrator = "{orchestrator}"\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    _, provider, _ = make_loop(tmp_path, [
        response("initial partial |", reason="length"),
        response("replacement complete", reason="stop" if recovered else "length"),
    ], selected_thinking="deepseek")
    original = provider.complete

    async def complete(request):
        stage = request.metadata["stage"]
        if stage == "route":
            return response(json.dumps({"selected_package": "filesystem", "reason": "test"}))
        if stage == "decision":
            return response(json.dumps({"operation": {"type": "final_answer", "reason": "test"}}))
        return await original(request)

    provider.complete = complete
    registry = LLMClientRegistry()
    registry.register_client(provider)
    service = LLMService(config=LLMProviderConfig(default_client="selected", clients=[
        LLMClientConfig(name="selected", default_model="test-model", context_window_tokens=100_000,
                        output_reserve_tokens=1024, thinking_control="deepseek"),
    ]), registry=registry)
    loop = runtime.agent_turn_loop
    loop.llm_client = service
    runner = runtime.agent_turn_runner
    run = runner.create_run_for_turn(session_id="integration-answer", user_input="Return supported results.")
    kwargs = {"session_id": run.session_id, "user_input": run.user_input,
              "existing_run_id": run.run_id, "llm_client_name": "selected",
              "llm_response_mode": LLMResponseMode.STREAM}
    try:
        if recovered:
            result = runner.run(**kwargs)
            assert result.answer == "replacement complete"
        else:
            with pytest.raises(LLMClientError, match="incomplete"):
                runner.run(**kwargs)
        events = runtime.agent_run_manager.list_events(run.run_id)
        current = runtime.agent_run_manager.get_run(run.run_id)
        assert current.status.value == ("completed" if recovered else "failed")
        final = [e for e in events if e.type == "final_answer"]
        assert len(final) == int(recovered)
        assert sum(e.type == "run_completed" for e in events) == int(recovered)
        if recovered:
            assert final[0].payload["metadata"]["answer"] == "replacement complete"
        answer_audit = [e for e in events if e.type == "llm_completed" and e.stage == "answer"]
        assert len(answer_audit) == 2
        assert sum(e.payload["budget_token_count"] for e in answer_audit) == 2800
        deltas = [e.payload["delta"] for e in events if e.type == "llm_delta" and e.stage == "answer"]
        assert deltas == ["initial partial |"]
        assert provider.requests[1].thinking_enabled is False
    finally:
        runtime.stop()
        get_settings.cache_clear()
