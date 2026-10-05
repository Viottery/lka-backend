"""Run-scoped control timeout cooldown; fake providers only, no live dispatches."""

import pytest

from app.core import agent_turn as turn
from app.core.agent_runs import AgentRunCancelled
from app.core.background_llm import SelectedBackgroundClient, complete_text_in_worker
from app.core.llm import (
    LLMClientError,
    LLMProviderHTTPError,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolDefinition,
)
from app.core.local_config import LLMModelConfigOverride
from tests.test_answer_generation_recovery_quality import answer, make_loop, response, scope
from tests.test_control_generation_quality import _decide
from tests.test_control_recovery_budget_quality import FINISH, INVALID_PATCH, MALFORMED, calibrated


@pytest.mark.parametrize("stream", [False, True])
def test_successful_timeout_recovery_reused_by_next_control_but_not_answer(tmp_path, stream):
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("classified request timeout; cause unknown"), response(FINISH),
        response(FINISH), response("delivered"),
    ], selected_thinking="deepseek")
    with scope(manager, stream=stream) as run:
        before = len(provider.requests)
        assert _decide(loop, [])["action"] == "final_answer"
        assert len(provider.requests) - before == 2
        before = len(provider.requests)
        assert _decide(loop, [])["action"] == "final_answer"
        assert len(provider.requests) - before == 1
        assert answer(loop, []) == "delivered"
        events = manager.list_events(run.run_id)
    assert [request.thinking_enabled for request in provider.requests] == [None, False, False, None]
    recovered = [event for event in events if event.type == "control_generation_timeout_recovery"]
    assert len(recovered) == 1
    assert recovered[0].payload["client_name"] == "selected"
    assert recovered[0].payload["model"] == "test-model"
    assert recovered[0].payload["error_category"] == "timeout"
    assert not any(event.type == "control_generation_overflow" for event in events)
    confirmed = [event for event in events if event.type == "control_generation_timeout_cooldown_confirmed"]
    assert len(confirmed) == 1
    assert confirmed[0].payload["cause"] == "unknown"
    assert confirmed[0].payload["recovery_scope"] == "current_run_control_only"
    assert confirmed[0].payload["thinking_enabled"] is False
    assert confirmed[0].payload["client_name"] == "selected"
    assert confirmed[0].payload["model"] == "test-model"
    completed = next(event for event in events if event.type == "llm_completed"
                     and event.payload["llm_call_id"] == confirmed[0].payload["llm_call_id"])
    assert events.index(completed) < events.index(confirmed[0])


@pytest.mark.parametrize("boundary", ["run", "model", "client"])
def test_cooldown_key_is_current_run_and_actual_client_model_not_global(tmp_path, boundary):
    loop, selected, manager = make_loop(tmp_path, [
        LLMTimeoutError("timeout"), response(FINISH), response(FINISH), response(FINISH),
    ], selected_thinking="deepseek", default_thinking="deepseek")
    other = loop.llm_client.registry.get("default")
    other.responses = [response(FINISH)]
    loop.llm_client.config.clients[1].model_overrides["other-model"] = LLMModelConfigOverride(
        context_window_tokens=100_000, output_reserve_tokens=1024,
    )
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
        if boundary == "run":
            with scope(manager):
                assert _decide(loop, [])["action"] == "final_answer"
        else:
            variable, value = ((turn._turn_llm_model, "other-model") if boundary == "model"
                               else (turn._turn_llm_client_name, "default"))
            token = variable.set(value)
            try:
                assert _decide(loop, [])["action"] == "final_answer"
            finally:
                variable.reset(token)
        # Returning to the original identity still uses its own cooldown.
        assert _decide(loop, [])["action"] == "final_answer"
    if boundary == "client":
        assert [request.thinking_enabled for request in other.requests] == [None]
        assert [request.thinking_enabled for request in selected.requests] == [None, False, False]
    else:
        assert [request.thinking_enabled for request in selected.requests] == [None, False, None, False]
        if boundary == "model":
            assert [request.model for request in selected.requests] == [
                "test-model", "test-model", "other-model", "test-model",
            ]


def test_default_selection_uses_resolved_dispatch_identity_not_none_keys(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("timeout"), response(FINISH), response(FINISH), response(FINISH),
    ], selected_thinking="deepseek")
    loop.llm_client.config.default_client = "selected"
    with scope(manager):
        token = turn._turn_llm_client_name.set(None)
        try:
            assert _decide(loop, [])["action"] == "final_answer"
            assert _decide(loop, [])["action"] == "final_answer"
        finally:
            turn._turn_llm_client_name.reset(token)
        assert _decide(loop, [])["action"] == "final_answer"
    assert [request.client_name for request in provider.requests] == ["selected"] * 4
    assert [request.model for request in provider.requests] == ["test-model"] * 4
    assert [request.thinking_enabled for request in provider.requests] == [None, False, False, False]


def test_context_answer_does_not_inherit_control_cooldown(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("timeout"), response(FINISH), response("context answer"), response(FINISH),
    ], selected_thinking="deepseek")
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
        assert loop._answer_from_context_with_llm(
            user_input="Answer from available context.", route={}, context_window={}, llm_events=[],
        ) == "context answer"
        assert _decide(loop, [])["action"] == "final_answer"
    assert provider.requests[2].metadata["stage"] == "context_answer"
    assert [request.thinking_enabled for request in provider.requests] == [None, False, None, False]


def test_unknown_selected_capability_cannot_borrow_another_clients_support(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("timeout"), response(FINISH), response(FINISH),
    ], default_thinking="deepseek")
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
        assert _decide(loop, [])["action"] == "final_answer"
    assert [request.thinking_enabled for request in provider.requests] == [None, None, None]


@pytest.mark.parametrize("failure", [
    LLMClientError("unclassified error containing the word timeout"),
    LLMProviderHTTPError(status_code=408, message="non-retriable classified timeout",
                         error_category="timeout", is_retriable=False),
])
def test_unknown_or_explicitly_nonretriable_error_does_not_arm_cooldown(tmp_path, failure):
    loop, provider, manager = make_loop(tmp_path, [failure, response(FINISH)],
                                       selected_thinking="deepseek")
    with scope(manager) as run:
        assert _decide(loop, []) is None
        assert len(provider.requests) == 1
        assert _decide(loop, [])["action"] == "final_answer"
        assert not any(event.type == "control_generation_timeout_recovery"
                       for event in manager.list_events(run.run_id))
    assert [request.thinking_enabled for request in provider.requests] == [None, None]


@pytest.mark.parametrize("during_recovery", [False, True])
def test_content_filter_never_arms_cooldown_or_retries_filtered_control(tmp_path, during_recovery):
    responses = ([LLMTimeoutError("timeout")] if during_recovery else []) + [
        response(FINISH, reason="content_filter"), response(FINISH),
    ]
    loop, provider, manager = make_loop(tmp_path, responses, selected_thinking="deepseek")
    with scope(manager):
        with pytest.raises(LLMClientError, match="content_filter"):
            _decide(loop, [])
        assert len(provider.requests) == (2 if during_recovery else 1)
        assert _decide(loop, [])["action"] == "final_answer"
    assert [request.thinking_enabled for request in provider.requests] == (
        [None, False, None] if during_recovery else [None, None]
    )


@pytest.mark.parametrize("failure", [LLMTimeoutError("recovery timeout"), LLMClientError("unknown")])
def test_failed_recovery_is_not_a_successful_cooldown(tmp_path, failure):
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("initial timeout"), failure, response(FINISH),
    ], selected_thinking="deepseek")
    with scope(manager):
        assert _decide(loop, []) is None
        assert len(provider.requests) == 2
        assert _decide(loop, [])["action"] == "final_answer"
    assert [request.thinking_enabled for request in provider.requests] == [None, False, None]


@pytest.mark.parametrize("when", ["timeout", "successful_recovery"])
def test_cancel_during_timeout_or_recovery_cannot_publish_a_cooldown(tmp_path, when):
    loop, provider, manager = make_loop(tmp_path, [LLMTimeoutError("timeout"), response(FINISH)],
                                       selected_thinking="deepseek")
    with scope(manager) as run:
        if when == "timeout":
            async def cancel_timeout(request):
                provider.requests.append(request)
                manager.request_cancel(run.run_id, "cancel before recovery")
                raise LLMTimeoutError("timeout")

            provider.complete = cancel_timeout
        else:
            provider.after_call = lambda: manager.request_cancel(run.run_id, "cancel recovery result")
        try:
            _decide(loop, [])
        except AgentRunCancelled:
            pass
        assert manager.is_cancel_requested(run.run_id)
        assert len(provider.requests) == (1 if when == "timeout" else 2)
        assert loop._control_run_thinking_flag(("selected", "test-model")) is None
        with pytest.raises(AgentRunCancelled):
            _decide(loop, [])
        assert len(provider.requests) == (1 if when == "timeout" else 2)


@pytest.mark.parametrize("path", ["json", "native", "schema_feedback"])
def test_cooldown_does_not_reset_two_dispatch_limit_for_repair_or_native_fallback(
    tmp_path, monkeypatch, path,
):
    first = INVALID_PATCH if path == "schema_feedback" else MALFORMED
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("seed timeout"), response(FINISH),
        response(first), LLMTimeoutError("second control dispatch timed out"), response(FINISH),
    ], selected_thinking="deepseek")
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
        if path == "native":
            monkeypatch.setattr(loop, "_native_decision_tools", lambda **kwargs: (
                [LLMToolDefinition(name="control", description="One operation")], {}))
            monkeypatch.setattr(loop, "_supports_function_calling", lambda: True)
            monkeypatch.setattr(loop, "_supports_required_tool_choice", lambda: False)
        result = _decide(loop, [])
    assert result is None or result["action"] != "final_answer"
    assert len(provider.requests) == 4  # Two decisions, each at most two actual dispatches.
    assert len(provider.responses) == 1
    assert [request.thinking_enabled for request in provider.requests] == [None, False, False, False]


def test_cooldown_preserves_unknown_reservation_child_call_cap_and_answer_reserve(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("timeout without usage"), response(FINISH),
        response(FINISH), response("delivered"),
    ], selected_thinking="deepseek")
    calibrated(loop, monkeypatch)
    with scope(manager, child_budget={"max_llm_calls": 4, "max_tokens": 100_000}) as run:
        assert _decide(loop, [])["action"] == "final_answer"
        assert _decide(loop, [])["action"] == "final_answer"
        budget = loop._child_budget_for_prompt()
        reservation = next(event.payload["budget_token_reservation"]
                           for event in manager.list_events(run.run_id) if event.type == "llm_started")
        assert budget["consumed_tokens"] == reservation + 2 * 1400
        assert budget["remaining_llm_calls"] == 1
        assert answer(loop, []) == "delivered"
        assert loop._child_budget_for_prompt()["remaining_llm_calls"] == 0
    assert len(provider.requests) == 4
    assert provider.requests[-1].metadata["stage"] == "answer"
    assert [request.thinking_enabled for request in provider.requests] == [None, False, False, None]


def test_timeout_recovery_attempt_event_alone_cannot_activate_cooldown(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(FINISH)], selected_thinking="deepseek")
    with scope(manager) as run:
        manager.append_event(run.run_id, "control_generation_timeout_recovery", "Recovery attempted.",
            stage="decision", payload={"client_name": "selected", "model": "test-model",
                "thinking_enabled": False, "error_category": "timeout"})
        assert _decide(loop, [])["action"] == "final_answer"
    assert provider.requests[0].thinking_enabled is None


@pytest.mark.parametrize("state", [
    {"reason": "length"}, {"partial": True}, {"status": "incomplete"}, {"content": ""},
])
def test_incomplete_timeout_recovery_cannot_confirm_timeout_cooldown(tmp_path, state):
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("timeout"), response(**state) if "content" in state else response(FINISH, **state),
    ], selected_thinking="deepseek")
    with scope(manager) as run:
        result = _decide(loop, [])
        events = manager.list_events(run.run_id)
    assert result is None or result["action"] != "final_answer"
    assert len(provider.requests) == 2
    assert not any(event.type == "control_generation_timeout_cooldown_confirmed" for event in events)
    # Existing incomplete-output policy is separate; never rename it a timeout benefit.
    assert any(event.type == "control_generation_overflow" for event in events)


def test_complete_native_function_recovery_can_confirm_cooldown_without_prose(tmp_path, monkeypatch):
    native = response("").model_copy(update={"tool_calls": [
        LLMToolCall(name="agent_finish_decision", arguments={"reason": "evidence available"}),
    ]})
    loop, provider, manager = make_loop(tmp_path, [LLMTimeoutError("timeout"), native, native],
                                       selected_thinking="deepseek")
    monkeypatch.setattr(loop, "_native_decision_tools", lambda **kwargs: (
        [LLMToolDefinition(name="agent_finish_decision", description="Finish decision")],
        {"agent_finish_decision": {"action": "final_answer"}},
    ))
    monkeypatch.setattr(loop, "_supports_function_calling", lambda: True)
    monkeypatch.setattr(loop, "_supports_required_tool_choice", lambda: False)
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
        assert _decide(loop, [])["action"] == "final_answer"
    assert [request.thinking_enabled for request in provider.requests] == [None, False, False]


def test_background_adapter_does_not_inherit_foreground_control_cooldown(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("timeout"), response(FINISH), response("background result"), response(FINISH),
    ], selected_thinking="deepseek")
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
        background = SelectedBackgroundClient(loop.llm_client, "selected", "test-model")
        assert complete_text_in_worker(background, system_prompt="Background processing.",
            user_prompt="Authorized input.", prompt_summary="background", max_output_tokens=1024,
        ).content == "background result"
        assert _decide(loop, [])["action"] == "final_answer"
    assert [request.thinking_enabled for request in provider.requests] == [None, False, None, False]


def test_identity_change_between_timeout_and_recovery_cannot_confirm_wrong_mode_or_identity(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("timeout"), response(FINISH), response(FINISH),
    ], selected_thinking="deepseek")
    loop.llm_client.config.clients[1].model_overrides["other-model"] = LLMModelConfigOverride(
        context_window_tokens=100_000, output_reserve_tokens=1024,
    )
    complete = provider.complete

    async def switch_model(request):
        try:
            return await complete(request)
        except LLMTimeoutError:
            provider.default_model = "other-model"
            raise

    provider.complete = switch_model
    with scope(manager) as run:
        assert _decide(loop, [])["action"] == "final_answer"
        assert _decide(loop, [])["action"] == "final_answer"
        assert not any(event.type == "control_generation_timeout_cooldown_confirmed"
                       for event in manager.list_events(run.run_id))
        assert loop._control_run_thinking_flag(("selected", "test-model")) is None
    assert [request.model for request in provider.requests] == ["test-model", "other-model", "other-model"]
    assert [request.thinking_enabled for request in provider.requests] == [None, None, None]
