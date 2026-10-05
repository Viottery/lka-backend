"""Offline checks prove prompt delivery, not model semantic compliance."""

import copy
import json

import pytest

from app.core import agent_turn as turn
from tests.test_answer_generation_recovery_quality import make_loop, response, scope

CALIBRATION = (
    "Preserve evidence source scope, negation, qualifiers and known event time. "
    "Do not upgrade a reported result, unverified state or missing evidence into "
    "verified current state, impossibility or universal absence. Fetch time or tool "
    "completion does not establish the reported event's time or the object's current "
    "correctness. Attach each material unknown once to its affected conclusion, within "
    "the requested output contract; do not repeat audit details."
)


def _call(loop, phase, goal, *, window=None, observations=None):
    args = {"user_input": goal, "route": {}, "context_window": window or {}, "llm_events": []}
    if phase == "answer":
        return loop._answer_with_llm(
            **args, observations=observations or [], final_decision=None
        )
    return loop._answer_from_context_with_llm(**args)


@pytest.mark.parametrize("phase", ["answer", "context_answer"])
@pytest.mark.parametrize("goal,content", [
    ('Return only short JSON in English: {"finding":"text"}.', '{"finding":"unverified"}'),
    ("Return only short CSV in English, no commentary.", "finding\nunverified"),
])
def test_both_provider_prompts_calibrate_evidence_without_changing_format(
    tmp_path, phase, goal, content
):
    loop, provider, manager = make_loop(tmp_path, [response(content)])
    with scope(manager):
        assert _call(loop, phase, goal) == content
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.messages[0].content.count(CALIBRATION) == 1
    assert "user-requested language and deliverable format" in request.messages[0].content
    assert turn.USER_STATEMENT_POLICY in request.messages[0].content
    assert "Do not wrap the answer in JSON" not in request.messages[0].content
    assert json.loads(request.messages[1].content)["user_input"] == goal
    assert request.thinking_enabled is None


def test_assigned_schema_still_has_priority_and_no_extra_calibration_fields(tmp_path, monkeypatch):
    loop, provider, manager = make_loop(tmp_path, [response('{"finding":"unverified"}')])
    contract = (
        'Return JSON.\n```json-schema\n{"type":"object","required":["finding"],'
        '"properties":{"finding":{"type":"string"}},"additionalProperties":false}\n```'
    )
    monkeypatch.setattr(turn, "_current_output_contract", lambda: contract)
    with scope(manager):
        assert _call(loop, "answer", "Use ordinary prose.") == '{"finding":"unverified"}'
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert CALIBRATION in request.messages[0].content
    assert "explicit structured output contract overrides" in request.messages[0].content
    assert json.loads(request.messages[1].content)["output_contract"] == contract


@pytest.mark.parametrize("phase", ["answer", "context_answer"])
def test_historical_negation_and_partial_results_survive_actual_provider_fit(tmp_path, phase):
    loop, provider, manager = make_loop(tmp_path, [response("Recorded result; current state unknown.")])
    # Isolated fake-client capacity only: exercise real selected-model fitting.
    selected = next(client for client in loop.llm_client.config.clients if client.name == "selected")
    selected.context_window_tokens = 12_000
    record = {
        "tool_name": "records.read",
        "input": {"path": "record.txt"},
        "result": {"status": "completed", "output": {
            "path": "record.txt",
            "content": "At 2025-04-03 the check failed. Recovery is not verified. Current health was not measured.",
        }},
        "_cache": {"historical_only": True, "as_of": "2025-04-04T00:00:00Z"},
    }
    partial = {
        "action": "fork_subtasks", "status": "partial", "execution_status": "failed",
        "task_results": [{
            "step_id": "independent-check", "status": "partial",
            "summary": "Only the record was checked; present conditions are unknown.",
            "missing_requirements": ["child_budget_finish"],
        }],
        "verification": {"status": "inconclusive"},
    }
    noise = [{"tool_name": "records.read", "result": {"output": {"content": "x" * 6000}}}
             for _ in range(12)]
    observations = noise + [record, partial]
    window = {"recent_messages": [
        {"role": "assistant", "content": "x" * 6000} for _ in range(12)
    ] + [{"role": "user", "content": "Summarize only recorded evidence."},
         {"role": "assistant", "content": json.dumps([record, partial])}]}
    original = copy.deepcopy((observations, window))
    with scope(manager):
        assert _call(loop, phase, "Give one short evidence-bound conclusion.",
                     window=window if phase == "context_answer" else {},
                     observations=observations) == "Recorded result; current state unknown."
        request = provider.requests[0]
        fitted = loop._budget_llm_prompt(
            system_prompt=request.messages[0].content, user_prompt=request.messages[1].content,
            max_output_tokens=request.max_output_tokens, tools=None,
        )
    assert len(provider.requests) == 1
    payload = json.loads(request.messages[1].content)
    assert payload["_prompt_budget"].get(
        "older_observations" if phase == "answer" else "older_context_messages", 0
    ) > 0
    if phase == "answer":
        assert record in payload["observations"]
        assert partial in payload["observations"]
    else:
        assert json.loads(payload["session_context_window"]["recent_messages"][-1]["content"]) == [record, partial]
    assert CALIBRATION in request.messages[0].content
    assert fitted.input_tokens <= fitted.input_limit
    assert (observations, window) == original
