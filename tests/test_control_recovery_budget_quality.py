"""Provider-boundary control recovery, with real audit and child counters."""

from dataclasses import replace

import pytest

from app.core.agent_runs import AgentRunCancelled
from app.core.llm import LLMClientError, LLMRateLimitError
from tests.test_answer_generation_recovery_quality import answer, make_loop, response, scope
from tests.test_control_generation_quality import _decide

FINISH = '{"operation":{"type":"final_answer","reason":"evidence available"}}'
TOOL = '{"operation":{"type":"tool_call","tool_name":"custom.read","tool_input":{}}}'
MALFORMED = TOOL + "\n" + TOOL
INVALID_PATCH = '{"operation":{"type":"plan_patch","patch_id":"invalid-schema"}}'


def calibrated(loop, monkeypatch):
    original = loop._budget_llm_prompt
    monkeypatch.setattr(loop, "_budget_llm_prompt",
                        lambda **kw: replace(original(**kw), input_tokens=500))


@pytest.mark.parametrize("state", [
    {"reason": "length"}, {"partial": True}, {"status": "incomplete"},
])
def test_malformed_repair_never_authorizes_incomplete_json(tmp_path, state):
    loop, provider, manager = make_loop(tmp_path, [
        response(MALFORMED), response(TOOL, **state), response(FINISH),
    ])
    with scope(manager):
        decision = _decide(loop, [])
    assert decision["action"] != "call_tool"
    assert len(provider.requests) == 2


def test_repair_preserves_last_child_call_for_answer(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [response(MALFORMED), response("delivered")])
    calibrated(loop, monkeypatch)
    observations = []
    with scope(manager, child_budget={"max_llm_calls": 2, "max_tokens": 100_000}) as run:
        decision = loop._decide_next_action(user_input="Inspect evidence.", route={},
            context_window={}, package_catalog=[], expanded_package_names=[], expanded_tools=[],
            observations=observations, llm_events=[])
        assert len(provider.requests) == 1
        assert decision["action"] == "final_answer"
        assert observations[-1]["action"] == "child_budget_finish"
        assert answer(loop, []) == "delivered"
        assert len([e for e in manager.list_events(run.run_id) if e.type == "llm_completed"]) == 2
    assert provider.requests[-1].metadata["stage"] == "answer"


def test_repair_uses_same_child_control_cap(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [response(MALFORMED), response(TOOL)])
    loop.llm_generation_token_budget = 8192
    calibrated(loop, monkeypatch)
    with scope(manager, child_budget={"max_llm_calls": 3, "max_tokens": 100_000}):
        assert _decide(loop, [])["action"] == "call_tool"
    assert [r.max_output_tokens for r in provider.requests] == [1024, 1024]


def test_repair_preserves_child_token_delivery_reserve(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [response(MALFORMED), response(TOOL)])
    calibrated(loop, monkeypatch)
    with scope(manager, child_budget={"max_llm_calls": 5, "max_tokens": 6200}):
        decision = _decide(loop, [])
    assert decision["action"] == "final_answer"
    assert len(provider.requests) == 1


def test_overflow_then_malformed_cannot_add_third_control_dispatch(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        response("", reason="length"), response(MALFORMED), response(TOOL),
    ], selected_thinking="deepseek")
    with scope(manager):
        assert _decide(loop, [])["action"] != "call_tool"
    assert len(provider.requests) == 2


def test_overflow_policy_is_run_and_output_family_scoped(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        response("", reason="length"), response(FINISH), response(FINISH),
        response("answer"), response(FINISH),
    ], selected_thinking="deepseek")
    with scope(manager) as run:
        assert _decide(loop, [])["action"] == "final_answer"
        assert _decide(loop, [])["action"] == "final_answer"
        assert answer(loop, []) == "answer"
        events = manager.list_events(run.run_id)
        assert any(e.type == "control_generation_overflow" and
                   e.payload.get("reason") == "length" for e in events)
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
    assert [r.thinking_enabled for r in provider.requests] == [None, False, False, None, None]


def test_stream_control_recovery_really_disables_selected_thinking(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        response("", reason="length"), response(FINISH),
    ], selected_thinking="deepseek")
    with scope(manager, stream=True):
        assert _decide(loop, [])["action"] == "final_answer"
    assert [r.thinking_enabled for r in provider.requests] == [None, False]


def test_overflow_state_does_not_follow_a_different_model_in_same_run(tmp_path):
    from app.core import agent_turn as turn

    loop, provider, manager = make_loop(tmp_path, [
        response("", reason="length"), response(FINISH), response(FINISH),
    ], selected_thinking="deepseek")
    from app.core.local_config import LLMModelConfigOverride

    loop.llm_client.config.clients[1].model_overrides["another-model"] = LLMModelConfigOverride(
        context_window_tokens=100_000, output_reserve_tokens=1024,
    )
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
        token = turn._turn_llm_model.set("another-model")
        try:
            assert _decide(loop, [])["action"] == "final_answer"
        finally:
            turn._turn_llm_model.reset(token)
    assert [r.thinking_enabled for r in provider.requests] == [None, False, None]


def test_concurrent_runs_have_independent_control_recovery_allowances(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    loop, provider, manager = make_loop(tmp_path, [], selected_thinking="deepseek")
    barrier = threading.Barrier(2)
    counts = {}

    async def complete(request):
        identity = threading.get_ident()
        counts[identity] = counts.get(identity, 0) + 1
        provider.requests.append(request)
        if counts[identity] == 1:
            barrier.wait(timeout=5)
            return response("", reason="length")
        return response(FINISH)

    provider.complete = complete

    def decide():
        with scope(manager) as run:
            assert _decide(loop, [])["action"] == "final_answer"
            return manager.list_events(run.run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        events = list(pool.map(lambda _: decide(), range(2)))
    assert sorted(counts.values()) == [2, 2]
    assert len(provider.requests) == 4
    assert all(sum(e.type == "control_generation_overflow" for e in run_events) == 1
               for run_events in events)


def test_cancel_after_malformed_prevents_repair(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(MALFORMED), response(TOOL)])
    with scope(manager) as run:
        provider.after_call = lambda: manager.request_cancel(run.run_id, "stop control")
        with pytest.raises(AgentRunCancelled):
            _decide(loop, [])
    assert len(provider.requests) == 1


def test_recovery_provider_failure_cannot_retry_into_third_dispatch(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        response("", reason="length"),
        LLMRateLimitError(message="limited", status_code=429, retry_after="0"),
        response(FINISH),
    ], selected_thinking="deepseek")
    with scope(manager):
        try:
            decision = _decide(loop, [])
        except LLMClientError:
            decision = None
    assert len(provider.requests) == 2
    assert decision is None or decision["action"] != "final_answer"


def test_multiple_json_objects_are_not_partially_selected(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(MALFORMED), response(MALFORMED)])
    with scope(manager):
        assert _decide(loop, [])["action"] != "call_tool"
    assert len(provider.requests) == 2


@pytest.mark.parametrize("path", ["json", "repair", "native", "stream"])
def test_provider_content_filter_fails_without_mode_downgrade_or_retry(tmp_path, monkeypatch, path):
    responses = ([response(MALFORMED)] if path == "repair" else []) + [
        response(TOOL, reason="content_filter", partial=True), response(FINISH),
    ]
    loop, provider, manager = make_loop(tmp_path, responses, selected_thinking="deepseek")
    if path == "native":
        from app.core.llm import LLMToolDefinition

        monkeypatch.setattr(loop, "_native_decision_tools", lambda **kw: (
            [LLMToolDefinition(name="control", description="One operation")], {}))
        monkeypatch.setattr(loop, "_supports_function_calling", lambda: True)
        monkeypatch.setattr(loop, "_supports_required_tool_choice", lambda: False)
    with scope(manager, stream=path == "stream") as run:
        with pytest.raises(LLMClientError, match="content_filter"):
            _decide(loop, [])
        events = manager.list_events(run.run_id)
    assert len(provider.requests) == (2 if path == "repair" else 1)
    assert all(r.thinking_enabled is None for r in provider.requests)
    assert not any(e.type == "control_generation_overflow" for e in events)
    rejected = [e for e in events if e.type == "control_generation_rejected"]
    assert len(rejected) == 1 and rejected[0].payload["reason"] == "content_filter"
    assert sum(e.type == "llm_completed" for e in events) == len(provider.requests)


def test_native_overflow_and_json_repair_share_one_recovery(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [
        response("", reason="length"), response(MALFORMED), response(TOOL),
    ], selected_thinking="deepseek")
    from app.core.llm import LLMToolDefinition
    monkeypatch.setattr(loop, "_native_decision_tools", lambda **kw: (
        [LLMToolDefinition(name="control", description="One control operation",
                           parameters={"type": "object"})], {}))
    monkeypatch.setattr(loop, "_supports_function_calling", lambda: True)
    monkeypatch.setattr(loop, "_supports_required_tool_choice", lambda: False)
    with scope(manager):
        assert _decide(loop, [])["action"] != "call_tool"
    assert len(provider.requests) == 2


@pytest.mark.parametrize("prefix", ["overflow", "malformed", "native"])
def test_plan_patch_schema_feedback_cannot_reset_control_quota(tmp_path, monkeypatch, prefix):
    responses = ([response(INVALID_PATCH), response(MALFORMED), response(TOOL)]
                 if prefix == "malformed" else
                 [response("", reason="length"), response(INVALID_PATCH), response(TOOL), response(FINISH)])
    loop, provider, manager = make_loop(tmp_path, responses,
                                       selected_thinking="deepseek")
    if prefix == "native":
        from app.core.llm import LLMToolDefinition

        monkeypatch.setattr(loop, "_native_decision_tools", lambda **kw: (
            [LLMToolDefinition(name="control", description="One operation")], {}))
        monkeypatch.setattr(loop, "_supports_function_calling", lambda: True)
        monkeypatch.setattr(loop, "_supports_required_tool_choice", lambda: False)
    with scope(manager):
        result = _decide(loop, [])
    assert len(provider.requests) == 2
    assert result is None or result["action"] != "call_tool"


def test_default_provider_identity_is_actual_and_does_not_leak_after_switch(tmp_path):
    from app.core import agent_turn as turn

    loop, selected, manager = make_loop(tmp_path, [response("", reason="length"), response(FINISH)],
                                        selected_thinking="deepseek", default_thinking="deepseek")
    fallback = loop.llm_client.registry.get("default")
    fallback.responses = [response(FINISH), response("answer")]
    loop.llm_client.config.default_client = "selected"
    with scope(manager) as run:
        token = turn._turn_llm_client_name.set(None)
        try:
            assert _decide(loop, [])["action"] == "final_answer"
            overflow = next(e for e in manager.list_events(run.run_id)
                            if e.type == "control_generation_overflow")
            assert overflow.payload["client_name"] == "selected"
            assert overflow.payload["model"] == "test-model"
            loop.llm_client.config.default_client = "default"
            assert _decide(loop, [])["action"] == "final_answer"
            assert answer(loop, []) == "answer"
        finally:
            turn._turn_llm_client_name.reset(token)
    assert [r.thinking_enabled for r in selected.requests] == [None, False]
    assert [r.thinking_enabled for r in fallback.requests] == [None, None]


def test_default_model_change_does_not_reuse_previous_overflow(tmp_path):
    from app.core import agent_turn as turn
    from app.core.local_config import LLMModelConfigOverride

    loop, provider, manager = make_loop(tmp_path, [response("", reason="length"),
        response(FINISH), response(FINISH)], selected_thinking="deepseek")
    loop.llm_client.config.default_client = "selected"
    loop.llm_client.config.clients[1].model_overrides["new-default"] = LLMModelConfigOverride(
        context_window_tokens=100_000, output_reserve_tokens=1024)
    with scope(manager):
        token = turn._turn_llm_client_name.set(None)
        try:
            assert _decide(loop, [])["action"] == "final_answer"
            provider.default_model = "new-default"
            assert _decide(loop, [])["action"] == "final_answer"
        finally:
            turn._turn_llm_client_name.reset(token)
    assert [r.model for r in provider.requests] == ["test-model", "test-model", "new-default"]
    assert [r.thinking_enabled for r in provider.requests] == [None, False, None]
