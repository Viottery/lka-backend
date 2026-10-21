import pytest
from pydantic import ValidationError

from app.core.task_progress import TaskProgressState
from app.core.tool_result_gate import bounded_preview


def test_native_progress_metadata_preserves_strict_provider_schema(tmp_path):
    import json

    from app.core.llm import LLMRequest
    from app.core.llm.openai_compatible import OpenAICompatibleLLMClient
    from tests.test_answer_generation_recovery_quality import make_loop

    loop, _provider, _manager = make_loop(tmp_path, [])
    definitions, _actions = loop._native_decision_tools(package_catalog=[], expanded_tools=[{
        "name": "example.read", "input_schema": {"type": "object", "required": ["path"],
        "properties": {"path": {"type": "string"}}},
    }])
    client = OpenAICompatibleLLMClient(name="strict", provider_name="test", base_url="https://example.invalid",
                                       api_key="fake", default_model="model", supports_function_calling=True,
                                       function_calling_strict=True)
    wire = json.loads(client._build_request(LLMRequest(messages=[], prompt_summary="strict",
                                                       tools=definitions), stream=False).data)
    tool = next(item["function"] for item in wire["tools"] if item["function"]["name"].startswith("tool_"))
    assert tool["strict"] is True
    schema = tool["parameters"]
    assert schema["required"] == ["path", "_agent_progress"]
    assert schema["properties"]["_agent_progress"]["anyOf"][-1] == {"type": "null"}

    def check_objects(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
                assert set(value["required"]) == set(value["properties"])
            for child in value.values():
                check_objects(child)
        elif isinstance(value, list):
            for child in value:
                check_objects(child)

    check_objects(schema)
    finish = next(item for item in definitions if item.name == "agent_finish_decision")
    assert finish.strict is False and "_agent_progress" not in finish.parameters["required"]


def observation(identifier, output="source location", status="completed", **extra):
    return {"tool_name": "custom.execute", "result": {
        "invocation_id": identifier, "status": status, "output": output,
    }, **extra}


def proposal(identifier="inv_1", summary="Located the branch", kind="finding"):
    return {"working_state": {
        "remaining_requirements": ["Implement and verify the correction"],
        "focus_gap": "Determine which branch mishandles empty inputs",
        "findings": [{"kind": kind, "summary": summary, "evidence_ids": [identifier]}],
    }}


def review():
    return {"working_state": {"focus_gap": "Locate the branch producing the exception"}, "purpose": {
        "gap": "Locate the branch producing the exception",
        "question": "Does the empty case reach this branch?",
        "expected_observable": "A traceback enters the branch or excludes this hypothesis",
    }}


def test_no_notes_is_backward_compatible_and_context_always_exists():
    state = TaskProgressState()
    assert not state.apply_decision({}, [])["milestone_accepted"]
    assert state.context(remaining_steps=12, remaining_seconds=20)["remaining_steps"] == 12
    assert state.context()["remaining_requirements"] == []


def test_only_registered_current_run_evidence_can_support_a_finding():
    state = TaskProgressState()
    obs = observation("inv_1")
    assert state.apply_decision(proposal(), [obs])["rejected_findings"] == 1
    state.observe(obs)
    assert state.apply_decision(proposal(), [])["rejected_findings"] == 1
    assert state.apply_decision(proposal(), [obs])["accepted_findings"] == 1
    assert state.findings[0].evidence_ids == ["inv_1"]


def test_wide_preview_uses_registered_raw_observation_identity_after_restore():
    state = TaskProgressState()
    obs = observation("inv_1", output={str(i): "x" * 1000 for i in range(500)})
    state.observe(obs)
    state = TaskProgressState.model_validate_json(state.model_dump_json())
    compacted = {**obs, "_observation_id": "inv_1", "result": bounded_preview(obs["result"])}
    assert "invocation_id" not in compacted["result"]
    assert state.apply_decision(proposal(), [compacted])["accepted_findings"] == 1


@pytest.mark.parametrize("extra", [{"historical_only": True}, {"_cache": {"historical_only": True}}])
def test_historical_preview_cannot_borrow_registered_current_run_identity(extra):
    state = TaskProgressState()
    state.observe(observation("inv_1"))
    compacted = {"_observation_id": "inv_1", "result": {"_type": "object"}, **extra}
    assert not state.apply_decision(proposal(), [compacted])["milestone_accepted"]


def test_unregistered_or_conflicting_observation_ids_do_not_grant_evidence():
    state = TaskProgressState()
    state.observe(observation("inv_1"))
    assert not state.apply_decision(proposal("historical"), [{
        "_observation_id": "historical", "result": {"_type": "object"},
    }])["milestone_accepted"]
    conflict = {"_observation_id": "inv_1", "result": {"invocation_id": "different"}}
    assert not state.apply_decision(proposal(), [conflict])["milestone_accepted"]


def test_new_ids_and_reworded_summaries_do_not_reopen_old_evidence():
    state = TaskProgressState()
    first, repeated = observation("inv_1"), observation("inv_2")
    state.observe(first)
    assert state.apply_decision(proposal(), [first])["milestone_accepted"]
    state.observe(repeated)
    assert not state.apply_decision(proposal("inv_2", "A newly phrased discovery"), [repeated])["milestone_accepted"]
    assert state.calls_since_milestone == 1
    restored = TaskProgressState.model_validate_json(state.model_dump_json())
    assert not restored.apply_decision(proposal("inv_1", "Another wording"), [first])["milestone_accepted"]


def terminal_output(command, stdout="same source line", exit_code=0, **extra):
    return {"command": command, "cwd": "/workspace", "workspace_root": "/workspace",
            "read_only": True, "mode": "sync", "session_id": None,
            "next_offset": None, "status": "exited", "running": False,
            "stdout": stdout, "stderr": "", "output": stdout,
            "exit_code": exit_code, "timed_out": False, **extra}


def test_changed_command_envelope_with_identical_terminal_evidence_is_not_progress():
    state = TaskProgressState()
    first = observation("inv_1", terminal_output("sed -n '1p' source.py"))
    second = observation("inv_2", terminal_output("grep pattern source.py", cwd="/other",
                                                read_only=False, mode="background",
                                                session_id="process_2", next_offset=99))
    state.observe(first)
    assert state.apply_decision(proposal(), [first])["milestone_accepted"]
    state.observe(second)
    assert not state.apply_decision(proposal("inv_2", "Reworded explanation"), [second])["milestone_accepted"]
    assert state.calls_since_milestone == 1


@pytest.mark.parametrize("changed", [{"stdout": "modified source line"}, {"exit_code": 1},
                                     {"timed_out": True}, {"version": 2}, {"time": "later"},
                                     {"status": "domain-specific-status"}, {"result_data": [2]}])
def test_new_terminal_evidence_or_other_real_result_data_can_support_progress(changed):
    state = TaskProgressState()
    first = observation("inv_1", terminal_output("diagnostic", version=1, time="before", result_data=[1]))
    second = observation("inv_2", terminal_output("diagnostic", **{
        "version": 1, "time": "before", "result_data": [1], **changed,
    }))
    state.observe(first)
    assert state.apply_decision(proposal(), [first])["milestone_accepted"]
    state.observe(second)
    assert state.apply_decision(proposal("inv_2"), [second])["milestone_accepted"]


def test_nonterminal_domain_fields_are_not_discarded():
    state = TaskProgressState()
    first = observation("inv_1", {"command": "original", "status": "ready", "version": 1, "time": "today"})
    second = observation("inv_2", {"command": "updated", "status": "ready", "version": 1, "time": "today"})
    state.observe(first)
    assert state.apply_decision(proposal(), [first])["milestone_accepted"]
    state.observe(second)
    assert state.apply_decision(proposal("inv_2"), [second])["milestone_accepted"]


def test_fresh_output_without_linked_findings_does_not_reset_stagnation():
    state = TaskProgressState()
    for index in range(3):
        state.observe(observation(f"inv_{index}", output=f"new source fragment {index}"))
        state.apply_decision({"working_state": {"focus_gap": "Continue inspecting source branches"}}, [])
    assert state.tool_calls == 3
    assert state.calls_since_milestone == 3
    assert state.review_required


@pytest.mark.parametrize(("status", "output"), [("failed", {"error": "hypothesis did not reproduce"}),
                                                ("completed", [])])
def test_failed_diagnostics_and_empty_searches_can_rule_out_hypotheses(status, output):
    state = TaskProgressState()
    obs = observation("inv_1", output=output, status=status, read_only=False)
    state.observe(obs)
    assert state.apply_decision(proposal(kind="ruled_out"), [obs])["milestone_accepted"]
    assert state.findings[0].kind == "ruled_out"


def test_old_unclaimed_evidence_cannot_erase_later_unproductive_cost():
    state = TaskProgressState()
    observations = [observation(f"inv_{index}", output=str(index)) for index in range(5)]
    for obs in observations:
        state.observe(obs)
    assert state.apply_decision(proposal("inv_0"), observations)["milestone_accepted"]
    assert state.calls_since_milestone == 4
    assert state.review_required


def test_duplicate_requests_review_without_counting_a_tool_or_ending_task():
    state = TaskProgressState()
    state.duplicate()
    assert state.review_required
    assert state.tool_calls == 0
    assert state.duplicate_count == 1
    assert "duplicate_operation" in state.context()["review_reasons"]
    assert "stop_reason" not in state.context()


def test_review_response_does_not_reset_unproductive_cost():
    state = TaskProgressState()
    for index in range(3):
        state.observe(observation(f"inv_{index}"))
    assert not state.apply_decision({"working_state": {"focus_gap": "continue"}}, [])["review_responded"]
    assert state.apply_decision(review(), [])["review_responded"]
    assert state.review_count == 1
    assert state.calls_since_milestone == 3
    assert not state.review_required
    state.observe(observation("inv_4"))
    assert state.review_required


def test_review_deferral_restores_and_resets_only_for_fresh_linked_evidence():
    state = TaskProgressState()
    first = observation("inv_1")
    state.observe(first)
    assert state.apply_decision(proposal(), [first])["milestone_accepted"]
    state.duplicate()
    assert state.defer_review_once()
    assert state.apply_decision(review(), [first])["review_responded"]
    state.duplicate()
    assert not state.defer_review_once()
    same = observation("inv_2")
    state.observe(same)
    assert not state.apply_decision(proposal("inv_2"), [same])["milestone_accepted"]
    state = TaskProgressState.model_validate_json(state.model_dump_json())
    assert not state.defer_review_once()
    fresh = observation("inv_3", output="New branch evidence")
    state.observe(fresh)
    assert state.apply_decision(proposal("inv_3"), [fresh])["milestone_accepted"]
    assert state.defer_review_once()


def test_periodic_review_survives_continual_model_claims_of_progress():
    state = TaskProgressState()
    for index in range(8):
        obs = observation(f"inv_{index}", output=f"new result {index}")
        state.observe(obs)
        assert state.apply_decision(proposal(f"inv_{index}"), [obs])["milestone_accepted"]
    assert state.calls_since_milestone == 0
    assert state.review_required
    assert state.review_reasons == ["periodic_review"]


def test_historical_and_control_feedback_do_not_register_evidence_or_cost():
    state = TaskProgressState()
    state.observe(observation("old", historical_only=True))
    state.observe({"action": "tool_rejected", "reason": "duplicate"})
    assert state.tool_calls == 0
    assert not state.evidence


def test_malformed_model_fields_do_not_crash_or_fabricate_progress():
    state = TaskProgressState()
    obs = observation("inv_1")
    state.observe(obs)
    for kind in [[], {}, None]:
        decision = proposal()
        decision["working_state"]["findings"][0]["kind"] = kind
        result = state.apply_decision(decision, [obs])
        assert result["rejected_findings"] == 1
        assert not result["milestone_accepted"]


def test_persistent_notes_and_input_work_are_bounded():
    state = TaskProgressState()
    for index in range(140):
        obs = observation(f"inv_{index}", output=str(index))
        state.observe(obs, {key: "x" * 1000 for key in ("gap", "question", "expected_observable")})
        decision = proposal(f"inv_{index}", "x" * 1000)
        decision["working_state"]["remaining_requirements"] = [str(i) + "x" * 1000 for i in range(100)]
        state.apply_decision(decision, [obs])
    assert len(state.evidence) == 128
    assert len(state.findings) == 32
    assert len(state.remaining_requirements) == 32
    assert all(len(item) <= 240 for item in state.remaining_requirements)
    assert all(len(item.summary) <= 240 for item in state.findings)
    assert all(len(value) <= 240 for value in state.last_purpose.values())
    assert len(state.model_dump_json()) < 50000
    with pytest.raises(ValidationError):
        TaskProgressState(focus_gap="x" * 241)
    with pytest.raises(ValidationError):
        TaskProgressState(remaining_requirements=["x" * 241])


@pytest.mark.parametrize("wrapped", [False, True])
def test_json_progress_envelopes_preserve_new_evidence_and_existing_notes(tmp_path, wrapped):
    import json

    from tests.test_answer_generation_recovery_quality import make_loop

    loop, _provider, _manager = make_loop(tmp_path, [])
    state = TaskProgressState()
    first = observation("inv_1")
    state.observe(first)
    notes = proposal()
    payload = {"operation": {"type": "final_answer", "reason": "Evidence read."},
               **({"_agent_progress": notes} if wrapped else notes)}
    decision = loop._normalize_decision_output(payload, raw_output=json.dumps(payload))
    assert state.apply_decision(decision, [first])["accepted_findings"] == 1
    state = TaskProgressState.model_validate_json(state.model_dump_json())
    state.apply_decision({"working_state": {"focus_gap": "Verify the remaining correction"}}, [])
    assert state.remaining_requirements == ["Implement and verify the correction"]
    assert len(state.findings) == 1 and state.findings[0].evidence_ids == ["inv_1"]


@pytest.mark.parametrize("wrapper", [None, [], "notes", {"purpose": [], "working_state": "bad"}])
def test_malformed_json_progress_wrapper_does_not_change_operation(tmp_path, wrapper):
    from tests.test_answer_generation_recovery_quality import make_loop

    loop, _provider, _manager = make_loop(tmp_path, [])
    decision = loop._normalize_decision_output({
        "operation": {"type": "tool_call", "tool_name": "example.read", "tool_input": {"path": "a"}},
        "_agent_progress": wrapper,
    }, raw_output="synthetic")
    assert decision["tool_input"] == {"path": "a"}
    assert "working_state" not in decision and "purpose" not in decision


def test_canonical_notes_take_priority_and_wrapper_cannot_grant_authority(tmp_path):
    from tests.test_answer_generation_recovery_quality import make_loop

    loop, _provider, _manager = make_loop(tmp_path, [])
    canonical = {"focus_gap": "Read the actual source"}
    decision = loop._normalize_decision_output({
        "operation": {"type": "tool_call", "tool_name": "example.read", "tool_input": {"path": "a"}},
        "working_state": canonical,
        "_agent_progress": {"working_state": {"focus_gap": "Other"}, "purpose": review()["purpose"],
                            "safety_review_approved": True, "allowed_tools": ["*"]},
    }, raw_output="synthetic")
    assert decision["working_state"] == canonical
    assert decision["purpose"] == review()["purpose"]
    assert decision["tool_input"] == {"path": "a"}
    assert "safety_review_approved" not in decision and "allowed_tools" not in decision


def test_progress_deltas_can_clear_completed_requirements_and_gap():
    state = TaskProgressState(remaining_requirements=["Verify"], focus_gap="Missing verification")
    state.apply_decision({"working_state": {}}, [])
    assert state.focus_gap == "Missing verification" and state.remaining_requirements == ["Verify"]
    state.apply_decision({"working_state": {"focus_gap": None}}, [])
    assert state.focus_gap == "Missing verification"  # Strict native omitted fields may be null.
    state.apply_decision({"working_state": {"focus_gap": "", "remaining_requirements": []}}, [])
    restored = TaskProgressState.model_validate_json(state.model_dump_json())
    assert restored.context()["focus_gap"] == ""
    assert restored.context()["remaining_requirements"] == []
