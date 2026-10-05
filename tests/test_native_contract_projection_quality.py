"""Native contracts appear once; authority and non-equivalent schemas remain."""

import copy
import json

from app.core.agent_turn import AgentTurnLoop
from app.core.llm import LLMToolDefinition
from tests.test_child_budget_quality import child_loop


def contract():
    return {"name": "renamed.inspect", "package": "renamed", "description": "Contract rule. " * 200,
            "input_schema": {"type": "object", "additionalProperties": False,
                             "required": ["path"], "properties": {"path": {"type": "string", "minLength": 1}}},
            "output_schema": {"type": "object"}, "read_only": True, "requires_confirmation": True,
            "effect_domains": ["local"], "origin_constraints": ["frozen-origin"],
            "unrestricted_execution": False}


def test_native_exact_contract_reference_keeps_authority_and_does_not_mutate():
    spec = contract()
    original = copy.deepcopy(spec)
    definition = LLMToolDefinition(name="tool_0_renamed_inspect", description=spec["description"],
                                   parameters=spec["input_schema"])
    actions = {definition.name: {"action": "call_tool", "tool_name": spec["name"]}}
    projected = AgentTurnLoop._native_tools_for_prompt([spec], [definition], actions)[0]
    assert spec == original
    assert projected["native_function"] == definition.name
    assert projected["input_schema_from_native_function"] is True
    assert "input_schema" not in projected and "description" not in projected
    for key in ("output_schema", "read_only", "requires_confirmation", "origin_constraints", "effect_domains", "unrestricted_execution"):
        assert projected[key] == spec[key]
    assert len(json.dumps(projected)) < len(json.dumps(spec)) / 3


def test_non_equivalent_or_ambiguous_mapping_keeps_full_input_contract():
    spec = contract()
    definition = LLMToolDefinition(name="native", description="Different transport description",
                                   parameters={"type": "object", "properties": {}})
    actions = {"native": {"action": "call_tool", "tool_name": spec["name"]}}
    result = AgentTurnLoop._native_tools_for_prompt([spec], [definition], actions)[0]
    assert result["input_schema"] == spec["input_schema"] and result["description"] == spec["description"]
    ambiguous = definition.model_copy(update={"name": "second"})
    actions["second"] = actions["native"]
    result = AgentTurnLoop._native_tools_for_prompt([spec], [definition, ambiguous], actions)[0]
    assert result == AgentTurnLoop._tools_for_prompt([spec])[0]


def test_schema_equivalence_never_conflates_boolean_and_numeric_values():
    spec = contract()
    spec["input_schema"]["properties"]["path"]["default"] = True
    parameters = copy.deepcopy(spec["input_schema"])
    parameters["properties"]["path"]["default"] = 1
    assert parameters == spec["input_schema"]  # Python equality is unsafe here.
    definition = LLMToolDefinition(name="native", description=spec["description"], parameters=parameters)
    result = AgentTurnLoop._native_tools_for_prompt([spec], [definition],
        {"native": {"action": "call_tool", "tool_name": spec["name"]}})[0]
    assert "input_schema_from_native_function" not in result
    assert result["input_schema"]["properties"]["path"]["default"] is True


def test_actual_native_decision_references_sent_contract_and_keeps_fork(tmp_path):
    with child_loop(tmp_path, max_depth=2) as (loop, _, _, calls):
        spec = contract()
        definitions, actions = loop._native_decision_tools(package_catalog=[], expanded_tools=[spec])
        loop._complete_control_generation = lambda **kwargs: (calls.append(kwargs), None)
        loop._decide_next_action_with_native_tools(
            user_input="Inspect assigned evidence.", route={}, context_window={},
            package_catalog=[], expanded_package_names=["renamed"], expanded_tools=[spec],
            observations=[], completed_tool_calls=[], tools=definitions, actions=actions, llm_events=[])
        payload = json.loads(calls[-1]["user_prompt"])
        projected = payload["expanded_tools"][0]
        matching = next(d for d in calls[-1]["tools"] if d.name == projected["native_function"])
        assert matching.parameters == spec["input_schema"]
        assert projected["input_schema_from_native_function"]
        assert payload["child_budget"]["max_tokens"] == 32768
        assert "agent_fork_subtasks" in [d.name for d in calls[-1]["tools"]]
        # JSON fallback must still carry its entire original contract.
        assert loop._tools_for_prompt([spec])[0]["input_schema"] == spec["input_schema"]
