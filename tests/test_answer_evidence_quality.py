"""Final delivery sees accumulated evidence, not only the decision working set."""

import json

from tests.test_answer_generation_recovery_quality import make_loop, response, scope


def observations():
    return [{"tool_name": "source.read", "result": {"output": {
        "items": [{"text": f"source-{source}-fragment-{index}-" + "x" * 350}
                  for index in range(10)], "marker": f"EVIDENCE_{source}",
    }}} for source in range(6)]


def _answer(loop, evidence):
    return loop._answer_with_llm(user_input="Compare all observed sources.", route={},
        context_window={}, observations=evidence, final_decision=None, llm_events=[])


def test_root_answer_retains_earlier_evidence_within_whole_prompt_gate(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response("Supported findings.")])
    with scope(manager):
        _answer(loop, observations())
    sent = provider.requests[0]
    payload = json.loads(sent.messages[1].content)
    markers = [item["result"]["output"]["marker"] for item in payload["observations"]
               if "result" in item]
    assert markers == [f"EVIDENCE_{source}" for source in range(6)]
    budgeted = loop._budget_llm_prompt(system_prompt=sent.messages[0].content,
        user_prompt=sent.messages[1].content, max_output_tokens=sent.max_output_tokens, tools=None)
    assert budgeted.input_tokens <= budgeted.input_limit


def test_child_delivery_keeps_existing_conservative_observation_limit(tmp_path):
    loop, _, manager = make_loop(tmp_path, [response("supported")])
    with scope(manager, child_budget={"max_tokens": 200000, "max_llm_calls": 10}):
        evidence = observations() * 3
        selected = loop._observations_for_answer_prompt(evidence)
        control = loop._observations_within_prompt_budget(evidence)
        assert selected == control
        assert len(selected) < len(evidence)


def test_larger_delivery_view_is_still_bounded_and_preserves_omitted_handles(tmp_path):
    loop, _, manager = make_loop(tmp_path, [])
    evidence = observations() * 10
    for index, item in enumerate(evidence):
        evidence[index] = dict(item, _result_cache={"artifact_id": f"artifact-{index}"})
    with scope(manager):
        selected = loop._observations_for_answer_prompt(evidence)
    assert len(json.dumps(selected, ensure_ascii=False)) < 70000
    assert selected[0]["omitted_result_artifacts"]
    assert len(evidence) == 60
