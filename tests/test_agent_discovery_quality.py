from types import SimpleNamespace

from app.core.agent_executors import AgentDefinition, AgentExecutorRegistry
from app.core.agent_turn import AgentTurnLoop, _fork_subtasks_function_schema


def test_agent_discovery_exposes_enabled_current_allowlist_without_private_executor():
    registry = AgentExecutorRegistry()
    for name, enabled in [("specialist", True), ("disabled", False), ("other", True)]:
        registry.register(AgentDefinition(agent_id=name, version="1", executor_kind="workflow",
                          description="Bounded deterministic record analysis", enabled=enabled), object())
    registry.register(AgentDefinition(agent_id="specialist", version="2", executor_kind="workflow",
                      description="New current capability"), object(), make_current=True)
    catalog = registry.discovery_catalog(("specialist", "disabled", "unknown", "specialist"))
    assert len(catalog) == 1
    assert catalog[0]["version"] == "2"
    assert catalog[0]["description"] == "New current capability"
    assert "executor" not in catalog[0]
    assert "other" not in str(catalog)


def test_loop_filters_discovery_against_current_child_policy_and_no_fork_has_no_catalog():
    loop = object.__new__(AgentTurnLoop)
    loop.fork_policy = SimpleNamespace(allowed_agent_ids=("allowed",))
    loop.agent_catalog_provider = lambda: [{"agent_id": "allowed", "description": "bounded"},
                                          {"agent_id": "denied", "description": "not authorized"}]
    assert loop._agent_catalog_for_prompt() == [{"agent_id": "allowed", "description": "bounded"}]
    loop.fork_policy = None
    assert loop._agent_catalog_for_prompt() == []


def test_model_fork_must_explicitly_select_executor_when_catalog_is_available():
    loop = object.__new__(AgentTurnLoop)
    loop.fork_policy = SimpleNamespace(allowed_agent_ids=("specialist",))
    loop.agent_catalog_provider = lambda: [{"agent_id": "specialist", "description": "bounded"}]
    proposal = {"operation": {"type": "fork_subtasks", "operation_id": "fork", "parent_step_id": "root",
                              "subtasks": [{"step_id": "part", "objective": "Inspect", "output_contract": "Evidence"}]}}
    rejected = loop._normalize_decision_output(proposal, raw_output="test")
    assert rejected["action"] == "fork_subtasks_invalid"
    assert "agent_id" in rejected["reason"]
    proposal["operation"]["subtasks"][0]["agent_id"] = "specialist"
    assert loop._normalize_decision_output(proposal, raw_output="test")["action"] == "fork_subtasks"
    assert "agent_id" in _fork_subtasks_function_schema(require_agent_id=True)["properties"]["subtasks"]["items"]["required"]


def test_legacy_fork_without_discovery_keeps_default_executor_compatibility():
    loop = object.__new__(AgentTurnLoop)
    loop.fork_policy = None
    proposal = {"operation": {"type": "fork_subtasks", "operation_id": "fork", "parent_step_id": "root",
                              "subtasks": [{"step_id": "part", "objective": "Inspect", "output_contract": "Evidence"}]}}
    accepted = loop._normalize_decision_output(proposal, raw_output="test")
    assert accepted["action"] == "fork_subtasks"
    assert "agent_id" not in _fork_subtasks_function_schema()["properties"]["subtasks"]["items"]["required"]
