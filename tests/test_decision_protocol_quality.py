import json
from types import SimpleNamespace

import pytest

from app.core.agent_turn import AgentTurnLoop
from app.core.tool_result_gate import preview


def _loop(outputs):
    loop = object.__new__(AgentTurnLoop)
    loop.fork_policy = None
    loop.llm_generation_token_budget = 1024
    loop.decision_format_max_attempts = 2
    loop._observations_within_prompt_budget = lambda observations: observations
    loop._native_decision_tools = lambda **kwargs: ([], {})
    loop._route_context = lambda route: route
    loop._context_window_for_llm = lambda context: context
    calls = []
    queue = iter(outputs)
    def complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=next(queue))
    loop._complete_text_with_retry = complete
    return loop, calls


@pytest.mark.parametrize("invalid", [
    '<calls><invoke name="example.read"><parameter>{"path":"notes.md"}</parameter></invoke></calls>',
    '{"command":"read notes.md"}',
    '{"operation":{"type":"imaginary_operation"}}',
    '{"action":"imaginary_operation"}',
    '<calls><parameter>{"action":"final_answer"}</parameter></calls>',
    'provider metadata {"operation":{"type":"final_answer"}} trailing data',
])
def test_parameter_json_is_not_a_valid_decision_and_gets_bounded_retry(invalid):
    corrected = json.dumps({"operation": {"type": "tool_call", "tool_name": "example.read",
                                          "tool_input": {"path": "notes.md"}}})
    loop, calls = _loop([invalid, corrected])
    decision = loop._decide_next_action(user_input="read my notes", route={}, context_window={},
        package_catalog=[], expanded_package_names=[], expanded_tools=[], observations=[], llm_events=[])
    assert decision["action"] == "call_tool"
    assert decision["tool_input"] == {"path": "notes.md"}
    assert len(calls) == 2
    assert "decision_retry" in json.loads(calls[1]["user_prompt"])


def test_repeated_unrecognized_structure_is_rejected_not_executed():
    loop, calls = _loop(['{"parameter":42}', '{"parameter":43}'])
    decision = loop._decide_next_action(user_input="task", route={}, context_window={},
        package_catalog=[], expanded_package_names=[], expanded_tools=[], observations=[], llm_events=[])
    assert decision["action"] == "invalid_structured_decision"
    assert len(calls) == 2


def test_long_string_preview_exposes_tail_and_exact_omitted_range():
    value = "START_123\n" + "heartbeat\n" * 1000 + "FINAL_789\n"
    result = preview(value, path="/output/stdout")
    assert "START_123" in result["head"]
    assert "FINAL_789" in result["tail"]
    assert result["head_range"] == [0, len(result["head"])]
    assert result["tail_range"] == [len(value) - len(result["tail"]), len(value)]
    assert result["omitted_chars"] == len(value) - len(result["head"]) - len(result["tail"])
    assert result["_partial"] is True
    assert result["path"] == "/output/stdout"
