import json

from app.core.agent_turn import AgentTurnLoop


def test_route_catalog_contains_discovery_not_unexpanded_execution_details():
    original = [{"name": "example", "description": "Look up local records", "risk": "low",
        "routing_hints": ["Use for records"], "requires_expansion": True,
        "decision_hints": ["Execution rule " * 200], "tool_names": ["example.read"],
        "observation_cache": {"tool_names": ["example.read"]}}]
    result = AgentTurnLoop._catalog_for_prompt(original)
    assert result[0]["routing_hints"] == original[0]["routing_hints"]
    assert result[0]["risk"] == "low"
    assert not {"decision_hints", "tool_names", "observation_cache"}.intersection(result[0])
    assert "decision_hints" in original[0]
    assert len(json.dumps(result)) < len(json.dumps(original)) / 2


def test_expanded_package_execution_rules_remain_visible_and_catalog_not_mutated():
    catalog = [{"name": "active", "description": "active", "decision_hints": ["Rule A"],
                "tool_names": ["active.read"]},
               {"name": "other", "description": "other", "decision_hints": ["Rule B"]}]
    result = AgentTurnLoop._catalog_for_prompt(catalog, expanded_packages=["active"])
    assert result[0] == catalog[0]
    assert "decision_hints" not in result[1]
    assert catalog[1]["decision_hints"] == ["Rule B"]
