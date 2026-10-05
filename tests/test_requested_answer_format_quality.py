"""User deliverable formats are not internal operation envelopes."""

import json

import pytest

from app.core import agent_turn as turn
from tests.test_answer_generation_recovery_quality import make_loop, response, scope


@pytest.mark.parametrize("phase", ["answer", "context_answer"])
@pytest.mark.parametrize("goal,content", [
    ('Answer in English. Return only JSON {"answer":"short answer"}.', '{"answer":"blue"}'),
    ("Return only CSV in English, without commentary.", "name,count\nalpha,2"),
])
def test_format_request_is_forwarded_without_a_contradicting_json_or_language_ban(
    tmp_path, phase, goal, content,
):
    loop, provider, manager = make_loop(tmp_path, [response(content)])
    with scope(manager):
        if phase == "answer":
            result = loop._answer_with_llm(user_input=goal, route={}, context_window={},
                                          observations=[], final_decision=None, llm_events=[])
        else:
            result = loop._answer_from_context_with_llm(user_input=goal, route={}, context_window={},
                                                       llm_events=[])
    assert result == content
    request = provider.requests[0]
    assert request.prompt_summary.startswith("agent_turn_" + phase)
    assert json.loads(request.messages[1].content)["user_input"] == goal
    system = request.messages[0].content
    assert "Do not wrap the answer in JSON" not in system
    assert "user-requested language and deliverable format" in system
    assert "Default to Chinese" in system
    assert "internal operation" in system


def test_explicit_task_schema_keeps_authority_over_ordinary_deliverable_defaults(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [response('{"ok":true}')])
    contract = 'Return JSON.\n```json-schema\n{"type":"object","required":["ok"],"properties":{"ok":{"type":"boolean"}}}\n```'
    monkeypatch.setattr(turn, "_current_output_contract", lambda: contract)
    with scope(manager):
        result = loop._answer_with_llm(user_input="Ordinary prose requested.", route={}, context_window={},
                                      observations=[], final_decision=None, llm_events=[])
    assert result == '{"ok":true}'
    assert "explicit structured output contract overrides" in provider.requests[0].messages[0].content


def test_default_prose_remains_a_text_answer_not_a_parsed_control_envelope(tmp_path):
    loop, _, manager = make_loop(tmp_path, [response("这是普通回答。")])
    with scope(manager):
        assert loop._answer_with_llm(user_input="你好", route={}, context_window={}, observations=[],
                                     final_decision=None, llm_events=[]) == "这是普通回答。"
