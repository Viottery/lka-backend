import copy
import json

from app.core.agent_turn import AgentTurnLoop


def test_model_tool_projection_preserves_contracts_not_server_lock_and_scope_plumbing():
    tool = {"name": "custom.lookup", "package": "custom", "description": "Read authorized records.",
            "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
            "output_schema": {"items": "array"}, "read_only": True, "risk": "low",
            "requires_confirmation": False, "side_effects": ["read_local_db"],
            "origin_constraints": [{"constraint_id": "origin-bound", "instruction": "Keep source boundaries."}],
            "scope_uses_sources": True, "scope_source_fields": [], "scope_uses_accounts": True,
            "resource_lock_fields": [], "resource_lock_group": None, "effect_domains": [],
            "future_metadata": {"instruction": "Retain unknown nonempty metadata."}}
    original = copy.deepcopy(tool)
    projected = AgentTurnLoop._tools_for_prompt([tool])[0]
    for key in ("name", "package", "description", "input_schema", "output_schema", "read_only", "risk",
                "requires_confirmation", "side_effects", "origin_constraints", "future_metadata"):
        assert projected[key] == tool[key]
    assert "scope_uses_sources" not in projected
    assert "resource_lock_group" not in projected
    assert "effect_domains" not in projected
    assert tool == original
    assert len(json.dumps(projected)) < len(json.dumps(tool))


def test_unknown_tool_defaults_do_not_hide_safety_classification_or_empty_schema():
    projected = AgentTurnLoop._tools_for_prompt([{
        "name": "external.custom", "input_schema": {}, "read_only": None,
        "requires_confirmation": True, "side_effects": ["external"], "empty_extension": [],
    }])[0]
    assert projected["input_schema"] == {}
    assert projected["read_only"] is None
    assert projected["requires_confirmation"] is True
    assert projected["side_effects"] == ["external"]
    assert "empty_extension" not in projected
