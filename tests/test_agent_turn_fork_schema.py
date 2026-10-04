from __future__ import annotations

import json
from types import SimpleNamespace

from pydantic import ValidationError

from app.api.main import create_app
from app.core.agent_turn import (
    AgentTurnLoop,
    _fork_subtasks_function_schema,
    _fork_validation_error_summary,
)
from app.core.multi_agent import ForkPolicy, ForkSubtasksOperation, ScopeGrant


def test_fork_function_schema_documents_exact_nested_field_shapes():
    schema = _fork_subtasks_function_schema()
    subtask = schema["properties"]["subtasks"]["items"]
    properties = subtask["properties"]

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["operation_id", "parent_step_id", "subtasks"]
    assert subtask["additionalProperties"] is False
    assert subtask["required"] == ["step_id", "objective", "output_contract"]
    assert "requested_scope" not in subtask["required"]
    assert properties["objective"]["type"] == "string"
    assert properties["output_contract"]["type"] == "string"
    assert properties["verification_criteria"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert properties["requested_scope"]["properties"]["allowed_tools"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert properties["requested_scope"]["properties"]["side_effect_level"]["enum"] == [
        "none",
        "read",
        "write",
        "external",
    ]


def test_native_decision_uses_one_required_function_for_fork_and_finish() -> None:
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.fork_policy = ForkPolicy(
        max_depth=1,
        max_children=2,
        max_fork_size=2,
        allowed_scope=ScopeGrant(),
    )
    loop.run_manager = None
    loop.llm_client = SimpleNamespace(supports_required_tool_choice=True)
    loop.llm_generation_token_budget = 512
    loop._route_context = lambda route: route
    loop._context_window_for_llm = lambda context_window: context_window
    definitions, actions = loop._native_decision_tools(
        package_catalog=[],
        expanded_tools=[],
    )
    assert {definition.name for definition in definitions} == {
        "agent_fork_subtasks",
        "agent_finish_decision",
    }
    calls = iter(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        name="agent_fork_subtasks",
                        arguments={
                            "operation_id": "fork_1",
                            "parent_step_id": "root_coordinator",
                            "subtasks": [
                                {
                                    "step_id": "task_1",
                                    "objective": "Inspect one independent part.",
                                    "output_contract": "Return a finding.",
                                }
                            ],
                        },
                    )
                ],
            ),
            SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        name="agent_finish_decision",
                        arguments={"reason": "Child evidence is sufficient."},
                    )
                ],
            ),
        ]
    )
    choices: list[str] = []

    def complete(**kwargs):
        choices.append(kwargs["tool_choice"])
        return next(calls)

    loop._complete_text_with_retry = complete

    def decide():
        return loop._decide_next_action_with_native_tools(
            user_input="Delegate two independent checks.",
            route={},
            context_window={},
            package_catalog=[],
            expanded_package_names=[],
            expanded_tools=[],
            observations=[],
            completed_tool_calls=[],
            tools=definitions,
            actions=actions,
            llm_events=[],
        )

    fork = decide()
    finish = decide()
    assert choices == ["required", "required"]
    assert fork["action"] == "fork_subtasks"
    assert fork["operation"]["subtasks"][0]["step_id"] == "task_1"
    assert finish["action"] == "final_answer"
    assert finish["reason"] == "Child evidence is sufficient."


def test_native_decision_keeps_auto_choice_but_requires_explicit_control() -> None:
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.fork_policy = None
    loop.llm_client = SimpleNamespace(supports_required_tool_choice=False)
    loop.llm_generation_token_budget = 512
    loop._route_context = lambda route: route
    loop._context_window_for_llm = lambda context_window: context_window
    choices: list[str] = []

    def complete(**kwargs):
        choices.append(kwargs["tool_choice"])
        return SimpleNamespace(content="Evidence is sufficient.", tool_calls=[])

    loop._complete_text_with_retry = complete
    decision = loop._decide_next_action_with_native_tools(
        user_input="Answer the question.",
        route={},
        context_window={},
        package_catalog=[],
        expanded_package_names=[],
        expanded_tools=[],
        observations=[],
        completed_tool_calls=[],
        tools=[],
        actions={},
        llm_events=[],
    )
    assert choices == ["auto"]
    # Auto remains necessary for endpoints without required tool_choice, but
    # absence of a control call must enter the bounded JSON fallback, not finish.
    assert decision is None


def test_required_native_decision_without_a_function_falls_back_to_json() -> None:
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.fork_policy = None
    loop.llm_client = SimpleNamespace(supports_required_tool_choice=True)
    loop.llm_generation_token_budget = 512
    loop._route_context = lambda route: route
    loop._context_window_for_llm = lambda context_window: context_window
    loop._complete_text_with_retry = lambda **kwargs: SimpleNamespace(
        content="Unexpected plain text.",
        tool_calls=[],
    )
    assert (
        loop._decide_next_action_with_native_tools(
            user_input="Answer the question.",
            route={},
            context_window={},
            package_catalog=[],
            expanded_package_names=[],
            expanded_tools=[],
            observations=[],
            completed_tool_calls=[],
            tools=[],
            actions={},
            llm_events=[],
        )
        is None
    )


def test_first_json_decision_prompt_shows_a_minimal_fork_shape() -> None:
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.fork_policy = ForkPolicy(
        max_depth=1,
        max_children=2,
        max_fork_size=2,
        allowed_scope=ScopeGrant(),
    )
    loop.decision_format_max_attempts = 1
    loop.llm_generation_token_budget = 512
    loop._observations_within_prompt_budget = lambda observations: observations
    loop.llm_client = SimpleNamespace(supports_function_calling=False)
    loop.run_manager = None
    loop._route_context = lambda route: route
    loop._context_window_for_llm = lambda context_window: context_window
    prompts: list[str] = []

    def complete(**kwargs):
        prompts.append(kwargs["system_prompt"])

    loop._complete_text_with_retry = complete
    decision = loop._decide_next_action(
        user_input="Delegate two independent checks.",
        route={},
        context_window={},
        package_catalog=[],
        expanded_package_names=[],
        expanded_tools=[],
        observations=[],
        llm_events=[],
    )

    assert decision is None
    prompt = prompts[0]
    example = json.loads(
        prompt.split(" Minimal fork example: ", 1)[1].split(". Copy the shape", 1)[0]
    )
    assert set(example["operation"]) == {
        "type",
        "operation_id",
        "parent_step_id",
        "subtasks",
    }
    assert set(example["operation"]["subtasks"][0]) == {
        "step_id",
        "objective",
        "output_contract",
    }
    assert "Task requirements belong in objective or output_contract, not requested_scope" in prompt
    assert '"type":"tool_call|expand_package' not in prompt


def test_answer_prompt_distinguishes_execution_from_verification() -> None:
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.llm_client = object()
    loop._multi_agent_replan_pending = lambda: False
    loop._observations_within_prompt_budget = lambda observations: observations
    loop._route_context = lambda route: route
    loop._context_window_for_llm = lambda context_window: context_window
    prompts: list[str] = []

    def complete(**kwargs):
        prompts.append(kwargs["system_prompt"])
        return SimpleNamespace(content="done")

    loop._complete_text_with_retry = complete
    assert (
        loop._answer_with_llm(
            user_input="Summarize child results.",
            route={},
            context_window={},
            observations=[],
            final_decision=None,
            llm_events=[],
        )
        == "done"
    )
    assert "distinguish completed child execution" in prompts[0]
    assert "inconclusive verification check as passed" in prompts[0]
    assert "Do not wrap the answer in JSON." in prompts[0]


def test_generic_output_schema_validation_rejects_missing_required_fields():
    from app.core.agent_turn import _json_schema_error

    schema = {
        "type": "object",
        "required": ["summary", "items"],
        "properties": {
            "summary": {"type": "string", "minLength": 1},
            "items": {"type": "array"},
        },
        "additionalProperties": False,
    }
    assert _json_schema_error({"summary": "done", "items": []}, schema) is None
    assert "missing required keys" in _json_schema_error({"summary": "done"}, schema)


def test_json_schema_type_union_and_common_bounds_are_enforced():
    from app.core.agent_turn import _json_schema_error

    schema = {
        "type": "object",
        "required": ["label", "items", "score", "optional"],
        "properties": {
            "label": {"type": "string", "minLength": 2, "maxLength": 5},
            "items": {"type": "array", "minItems": 1, "maxItems": 2},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "optional": {"type": ["string", "null"]},
        },
    }
    valid = {"label": "good", "items": [1], "score": 0.5, "optional": None}
    assert _json_schema_error(valid, schema) is None
    assert "one of" in _json_schema_error({**valid, "optional": False}, schema)
    assert "maxLength" in _json_schema_error({**valid, "label": "too long"}, schema)
    assert "maxItems" in _json_schema_error({**valid, "items": [1, 2, 3]}, schema)
    assert "above maximum" in _json_schema_error({**valid, "score": 2}, schema)


def test_declared_schema_parse_and_unsupported_keywords_fail_closed():
    import pytest

    from app.core.agent_turn import _output_schema_from_contract

    with pytest.raises(ValueError, match="malformed"):
        _output_schema_from_contract("Return JSON.\n```json-schema\n{bad json}\n```")
    with pytest.raises(ValueError, match="Unsupported JSON Schema keyword.*\\$ref"):
        _output_schema_from_contract(
            'Return JSON.\n```json-schema\n{"$ref":"#/definitions/Thing"}\n```'
        )
    with pytest.raises(ValueError, match="Unsupported JSON Schema keyword.*pattern"):
        _output_schema_from_contract(
            'Return JSON.\n```json-schema\n{"type":"string","pattern":"x+"}\n```'
        )


def test_fork_validation_errors_are_actionable_without_echoing_invalid_values():
    try:
        ForkSubtasksOperation.model_validate(
            {
                "operation": "fork_subtasks",
                "operation_id": "bad-shape",
                "parent_step_id": "root_coordinator",
                "subtasks": [
                    {
                        "step_id": "task_1",
                        "objective": "Check the material",
                        "output_contract": {"secret_value": "must-not-be-echoed"},
                        "verification_criteria": "must be an array",
                    }
                ],
            }
        )
    except ValidationError as exc:
        summary = _fork_validation_error_summary(exc)
    else:
        raise AssertionError("The invalid fork shape unexpectedly passed validation.")

    assert "subtasks.0.output_contract" in summary
    assert "subtasks.0.verification_criteria" in summary
    assert "must-not-be-echoed" not in summary


def test_invalid_fork_gets_one_targeted_repair_opportunity(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text("[agent]\n", encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    from app.core.config import get_settings

    get_settings.cache_clear()
    loop = create_app().state.runtime.agent_turn_loop
    seen_observations: list[list[dict]] = []
    handled_forks: list[dict] = []
    decisions = iter(
        [
            {
                "action": "fork_subtasks_invalid",
                "operation": {"type": "fork_subtasks", "operation_id": "fork_1"},
                "reason": "subtasks.0.output_contract: expected string",
            },
            {
                "action": "fork_subtasks_invalid",
                "operation": {"type": "fork_subtasks", "operation_id": "fork_1"},
                "reason": "subtasks.0.output_contract: expected string",
            },
            {"action": "final_answer", "operation": {"type": "final_answer"}},
        ]
    )

    def decide(**kwargs):
        seen_observations.append(kwargs["observations"])
        return next(decisions)

    monkeypatch.setattr(loop, "_decide_next_action", decide)
    monkeypatch.setattr(
        loop,
        "_handle_fork_subtasks_decision",
        lambda **kwargs: handled_forks.append(kwargs) or {"status": "rejected"},
    )
    monkeypatch.setattr(loop, "_answer_with_llm", lambda **kwargs: "done")

    answer = loop._run_llm_decision_loop(
        user_input="Coordinate independent work.",
        route={},
        context_window={},
        context=SimpleNamespace(tool_view=None),
        tool_events=[],
        llm_events=[],
        decision_events=[],
        progress_events=[],
        expanded_tools=[],
        selected_package="",
    )

    assert answer == "done"
    assert len(seen_observations) == 3
    repair = next(
        observation
        for observation in seen_observations[1]
        if observation.get("action") == "fork_subtasks_schema_feedback"
    )
    assert repair["action"] == "fork_subtasks_schema_feedback"
    assert repair["status"] == "consumed"
    assert repair["required_shape_example"]["operation"]["subtasks"][0]["output_contract"]
    assert "type" not in repair["function_arguments_example"]
    assert len(handled_forks) == 1
