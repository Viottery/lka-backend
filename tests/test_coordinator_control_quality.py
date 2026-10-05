"""Bounded coordinator worksets keep delegation and honest cumulative accounting."""

import json

from tests.test_child_budget_quality import child_loop, decide


def test_coordinator_compact_control_keeps_fork_and_schema_contract(tmp_path):
    with child_loop(tmp_path, max_depth=2) as (loop, _, _, calls):
        assert decide(loop)["action"] == "final_answer"
        compact = calls[-1]["system_prompt"]
        assert "fork_subtasks" in compact and "agent_catalog" in compact
        assert "output_contract" in compact and "inference_profile_id" in compact
        payload = json.loads(calls[-1]["user_prompt"])
        assert payload["agent_catalog"] and payload["child_budget"]["max_tokens"] == 32768
        definitions, _ = loop._native_decision_tools(package_catalog=[], expanded_tools=[])
        assert "agent_fork_subtasks" in [definition.name for definition in definitions]
        # The same execution capability with no child budget uses the root view.
        loop._child_budget_for_prompt = lambda: None
        decide(loop)
        assert len(compact.encode()) < len(calls[-1]["system_prompt"].encode()) * 0.65


def test_budget_breakdown_does_not_count_unknown_dispatch_as_free(tmp_path):
    with child_loop(tmp_path, max_depth=2) as (loop, manager, child, _):
        manager.append_event(child.run_id, "llm_started", "control", stage="decision",
                             payload={"llm_call_id": "settled", "input_token_estimate": 150})
        manager.append_event(child.run_id, "llm_completed", "control", stage="decision",
                             payload={"llm_call_id": "settled", "budget_token_count": 230,
                                      "audit_record": {"input_token_count": 200, "output_token_count": 30}})
        manager.append_event(child.run_id, "llm_completed", "legacy unknown", stage="answer",
                             payload={"budget_token_count": 100})
        manager.append_event(child.run_id, "llm_started", "pending", stage="decision",
                             payload={"llm_call_id": "pending", "budget_token_reservation": 400})
        view = loop._child_budget_for_prompt()
        assert view["consumed_tokens"] == 730 and view["remaining_tokens"] == 32768 - 730
        assert view["usage_breakdown"] == {
            "known_input_tokens": 200, "known_output_tokens": 30, "settled_calls": 2,
            "unknown_usage_calls": 1, "pending_reservation_tokens": 400,
            "calls_by_stage": {"decision": 2},
        }
