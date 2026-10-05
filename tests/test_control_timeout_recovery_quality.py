"""Retry typed control timeouts once, without mislabelling them as reasoning overflow."""

import pytest

from app.core.agent_runs import AgentRunCancelled
from app.core.llm import LLMClientError, LLMTimeoutError
from tests.test_answer_generation_recovery_quality import answer, make_loop, response, scope
from tests.test_control_generation_quality import _decide
from tests.test_control_recovery_budget_quality import FINISH, MALFORMED, calibrated


@pytest.mark.parametrize("stream", [False, True])
def test_typed_timeout_recovery_is_bounded_run_control_only_and_audited(tmp_path, stream):
    loop, provider, manager = make_loop(tmp_path, [
        LLMTimeoutError("provider request timed out"), response(FINISH),
        response(FINISH), response("answer"),
    ], selected_thinking="deepseek")
    with scope(manager, stream=stream) as run:
        assert _decide(loop, [])["action"] == "final_answer"
        assert _decide(loop, [])["action"] == "final_answer"
        assert answer(loop, []) == "answer"
        events = manager.list_events(run.run_id)
    assert [r.thinking_enabled for r in provider.requests] == [None, False, False, None]
    recovered = [e for e in events if e.type == "control_generation_timeout_recovery"]
    assert len(recovered) == 1
    assert recovered[0].payload["client_name"] == "selected"
    assert recovered[0].payload["model"] == "test-model"
    assert not any(e.type == "control_generation_overflow" for e in events)


def test_unsupported_provider_timeout_retries_without_thinking_flag(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [LLMTimeoutError("timeout"), response(FINISH)])
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
    assert [r.thinking_enabled for r in provider.requests] == [None, None]


def test_timeout_after_format_repair_cannot_add_third_dispatch(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [
        response(MALFORMED), LLMTimeoutError("timeout"), response(FINISH),
    ], selected_thinking="deepseek")
    with scope(manager):
        result = _decide(loop, [])
    assert result is None or result["action"] != "final_answer"
    assert len(provider.requests) == 2


def test_control_timeout_preserves_last_child_call_for_answer(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [LLMTimeoutError("timeout"), response("answer")])
    calibrated(loop, monkeypatch)
    with scope(manager, child_budget={"max_llm_calls": 2, "max_tokens": 100_000}):
        observations = []
        result = _decide(loop, observations)
        assert result["action"] == "final_answer"
        assert len(provider.requests) == 1
        assert answer(loop, []) == "answer"
    assert provider.requests[-1].metadata["stage"] == "answer"


def test_cancel_between_timeout_and_recovery_prevents_dispatch(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [], selected_thinking="deepseek")
    with scope(manager) as run:
        async def cancelled(request):
            provider.requests.append(request)
            manager.request_cancel(run.run_id, "cancel timed-out control")
            raise LLMTimeoutError("timeout")

        provider.complete = cancelled
        with pytest.raises(AgentRunCancelled):
            _decide(loop, [])
    assert len(provider.requests) == 1


def test_unclassified_client_error_is_not_a_timeout_retry(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [LLMClientError("unclassified"), response(FINISH)])
    with scope(manager):
        assert _decide(loop, []) is None
    assert len(provider.requests) == 1


def test_unknown_failed_usage_reservation_prevents_budget_free_retry(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [LLMTimeoutError("timeout"), response("answer")])
    calibrated(loop, monkeypatch)
    with scope(manager, child_budget={"max_llm_calls": 5, "max_tokens": 6200}) as run:
        result = _decide(loop, [])
        assert result["action"] == "final_answer"
        assert len(provider.requests) == 1
        reservation = next(e.payload["budget_token_reservation"] for e in manager.list_events(run.run_id)
                           if e.type == "llm_started")
        budget = loop._child_budget_for_prompt()
        assert budget["consumed_tokens"] == reservation
        assert budget["remaining_tokens"] == 6200 - reservation


@pytest.mark.parametrize("second", [LLMTimeoutError("second timeout"), response(MALFORMED)])
def test_timeout_recovery_cannot_trigger_a_third_control_call(tmp_path, second):
    loop, provider, manager = make_loop(tmp_path, [LLMTimeoutError("timeout"), second, response(FINISH)])
    with scope(manager):
        result = _decide(loop, [])
    assert len(provider.requests) == 2
    assert result is None or result["action"] != "call_tool"


def test_failed_default_control_audit_uses_actual_dispatch_identity(tmp_path):
    from app.core import agent_turn as turn

    loop, _provider, manager = make_loop(tmp_path, [LLMTimeoutError("timeout"), response(FINISH)])
    loop.llm_client.config.default_client = "selected"
    with scope(manager) as run:
        token = turn._turn_llm_client_name.set(None)
        try:
            assert _decide(loop, [])["action"] == "final_answer"
        finally:
            turn._turn_llm_client_name.reset(token)
        failed = next(e for e in manager.list_events(run.run_id) if e.type == "llm_failed")
    assert failed.payload["audit_record"]["client_name"] == "selected"
    assert failed.payload["audit_record"]["model"] == "test-model"
