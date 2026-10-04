import pytest

from tests.test_answer_generation_recovery_quality import make_loop, response, scope


def _decide(loop, events):
    return loop._decide_next_action(
        user_input="Inspect the evidence.", route={}, context_window={}, package_catalog=[],
        expanded_package_names=[], expanded_tools=[], observations=[], llm_events=events,
    )


def test_empty_control_retry_disables_only_supported_selected_thinking(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(" " * 200), response(
        '{"operation":{"type":"final_answer","reason":"supported"}}',
    )], selected_thinking="deepseek")
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
    assert len(provider.requests) == 2
    assert provider.requests[0].thinking_enabled is None
    assert provider.requests[1].thinking_enabled is False


def test_length_terminated_control_object_is_not_executed_as_complete(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(
        '{"operation":{"type":"tool_call","tool_name":"custom.read","tool_input":{}}}', reason="length",
    ), response('{"operation":{"type":"final_answer","reason":"supported"}}')], selected_thinking="deepseek")
    with scope(manager):
        assert _decide(loop, [])["action"] == "final_answer"
    assert len(provider.requests) == 2
    assert provider.requests[1].thinking_enabled is False


def test_unknown_thinking_capability_stays_unspecified_and_retry_is_bounded(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response(""), response("", reason="length")])
    with scope(manager):
        assert _decide(loop, [])["action"] == "invalid_empty_decision"
    assert len(provider.requests) == 2
    assert all(request.thinking_enabled is None for request in provider.requests)


@pytest.mark.parametrize("state", [
    {"partial": True}, {"status": "incomplete"}, {"finish_reason": "length"},
])
def test_incomplete_native_call_cannot_authorize_execution(state):
    from types import SimpleNamespace

    from tests.test_decision_protocol_quality import _loop

    loop, calls = _loop([])
    loop._native_decision_tools = lambda **kwargs: ([object()], {"custom_read": {
        "action": "call_tool", "tool_name": "custom.read",
    }})
    loop._supports_function_calling = lambda: True
    loop._supports_required_tool_choice = lambda: False
    responses = iter([
        SimpleNamespace(content="", tool_calls=[SimpleNamespace(name="custom_read", arguments={})],
                        **state),
        SimpleNamespace(content='{"operation":{"type":"final_answer","reason":"no execution"}}'),
    ])

    def complete(**kwargs):
        calls.append(kwargs)
        return next(responses)

    loop._complete_text_with_retry = complete
    assert _decide(loop, [])["action"] == "final_answer"
    assert len(calls) == 2
