"""Offline tests for bounded, model-declared task completion recovery."""

from app.core.task_completion import (
    TaskCompletionState,
    assess_finish,
    completion_feedback,
)


def check(requirement="Required finding", *, identifier="finding", status="pending", gap=""):
    return {
        "requirement_id": identifier,
        "requirement": requirement,
        "status": status,
        "gap": gap,
    }


def finish(*checks):
    return {"type": "final_answer", "answer_checks": list(checks)}


def observation(value, *, invocation="call-1", input_value=None, control=None):
    row = {
        "tool_name": "generic.read",
        "input": input_value or {"query": "record"},
        "result": {"status": "completed", "invocation_id": invocation, "output": value},
    }
    if control is not None:
        row["tool_feedback"] = control
    return row


def test_reason_only_finish_is_compatible_noop():
    state = TaskCompletionState()

    assert assess_finish(state, operation={"reason": "ready"}, observations=[], can_continue=True)
    assert state.requirements == []
    assert state.recovery_attempts == 0
    assert state.context() is None


def test_omitted_empty_and_invalid_notes_do_not_erase_known_requirement():
    state = TaskCompletionState()
    assert not assess_finish(state, operation=finish(check()), observations=[], can_continue=True)

    for operation in ({"reason": "omitted"}, finish(), finish({"requirement": "", "status": "bogus"})):
        assert assess_finish(state, operation=operation, observations=[], can_continue=True)
        assert [(item.requirement_id, item.requirement, item.status) for item in state.requirements] == [
            ("finding", "Required finding", "pending")
        ]


def test_ids_remain_stable_when_order_and_model_supplied_ids_change():
    state = TaskCompletionState()
    first = [check("Alpha", identifier="a-v1"), check("Beta", identifier="b-v1")]
    assert not assess_finish(state, operation=finish(*first), observations=[], can_continue=True)

    second = [check("Beta", identifier="renamed-beta"), check("Alpha", identifier="renamed-alpha")]
    assert assess_finish(state, operation=finish(*second), observations=[], can_continue=True)
    assert [(item.requirement_id, item.requirement) for item in state.requirements] == [
        ("a-v1", "Alpha"), ("b-v1", "Beta")
    ]


def test_same_id_cannot_replace_requirement_text():
    state = TaskCompletionState()
    assert not assess_finish(state, operation=finish(check("Original")), observations=[], can_continue=True)

    assert assess_finish(
        state,
        operation=finish(check("Different requirement", identifier="finding", status="supported")),
        observations=[], can_continue=True,
    )
    assert [(item.requirement_id, item.requirement, item.status) for item in state.requirements] == [
        ("finding", "Original", "pending")
    ]


def test_blocked_requirement_allows_partial_answer_with_reason():
    state = TaskCompletionState()
    assert assess_finish(
        state,
        operation=finish(check(status="blocked", gap="Required source is unavailable")),
        observations=[], can_continue=True,
    )
    assert state.requirements[0].status == "blocked"
    assert state.requirements[0].gap == "Required source is unavailable"
    assert state.stop_reason is None


def test_blocked_without_reason_and_supported_with_conflicting_gap_remain_pending():
    state = TaskCompletionState()
    assert not assess_finish(
        state,
        operation=finish(
            check("Blocked", identifier="blocked", status="blocked"),
            check("Conflicted", identifier="conflicted", status="supported", gap="Evidence missing"),
        ),
        observations=[], can_continue=True,
    )
    assert {item.requirement_id: item.status for item in state.requirements} == {
        "blocked": "pending", "conflicted": "pending"
    }
    assert state.requirements[0].gap


def test_no_progress_finish_stops_instead_of_repeating_recovery():
    state = TaskCompletionState()
    evidence = [observation({"value": "unchanged"})]
    assert not assess_finish(state, operation=finish(check()), observations=evidence, can_continue=True)
    assert assess_finish(state, operation=finish(), observations=evidence, can_continue=True)
    assert state.recovery_attempts == 1
    assert state.stop_reason == "no_new_evidence"


def test_new_completed_output_allows_second_recovery_but_not_a_third():
    state = TaskCompletionState()
    assert not assess_finish(
        state, operation=finish(check()), observations=[observation({"value": 1})], can_continue=True
    )
    assert not assess_finish(
        state,
        operation=finish(),
        observations=[observation({"value": 2}, invocation="call-2")],
        can_continue=True,
    )
    assert state.recovery_attempts == 2

    assert assess_finish(
        state,
        operation=finish(),
        observations=[observation({"value": 3}, invocation="call-3")],
        can_continue=True,
    )
    assert state.recovery_attempts == 2
    assert state.stop_reason == "completion_recovery_limit"


def test_step_budget_exhaustion_enters_partial_answer_and_records_stop_reason():
    state = TaskCompletionState()

    assert assess_finish(
        state,
        operation=finish(check()),
        observations=[],
        can_continue=False,
        stop_reason="decision_budget_exhausted",
    )
    assert state.requirements[0].status == "pending"
    assert state.stop_reason == "decision_budget_exhausted"
    assert completion_feedback(state)["requirements"][0]["status"] == "pending"


def test_state_schema_roundtrip_and_old_working_set_default():
    state = TaskCompletionState()
    assert TaskCompletionState.model_validate(state.model_dump()) == state

    from app.core.agent_turn import AgentTurnWorkingSet

    old_snapshot = {
        "run_id": "run",
        "session_id": "session",
        "trace_id": "trace",
        "user_input": "Summarize the available information.",
    }
    restored = AgentTurnWorkingSet.model_validate(old_snapshot)
    assert restored.completion_state == TaskCompletionState()


def test_control_feedback_and_invocation_ids_do_not_count_as_new_evidence():
    state = TaskCompletionState()
    initial = [observation({"value": "same"}, invocation="call-1", control={"message": "first"})]
    assert not assess_finish(state, operation=finish(check()), observations=initial, can_continue=True)

    replay = [observation({"value": "same"}, invocation="call-99", control={"message": "different"})]
    assert assess_finish(state, operation=finish(), observations=replay, can_continue=True)
    assert state.stop_reason == "no_new_evidence"


def test_observation_order_does_not_change_evidence_digest():
    state = TaskCompletionState()
    rows = [observation({"value": "one"}), observation({"value": "two"}, invocation="call-2")]
    assert not assess_finish(state, operation=finish(check()), observations=rows, can_continue=True)
    reordered = list(reversed(rows))
    assert assess_finish(state, operation=finish(), observations=reordered, can_continue=True)
    assert state.stop_reason == "no_new_evidence"


def test_requirement_and_check_capacity_are_bounded():
    state = TaskCompletionState()
    for batch in range(4):
        checks = [
            check(f"Requirement {batch * 8 + index}", identifier=f"requirement-{batch * 8 + index}")
            for index in range(8)
        ]
        assess_finish(
            state, operation=finish(*checks), observations=[], can_continue=False
        )

    assert len(state.requirements) == 32
    overflow = check("Additional requirement", identifier="additional")
    assert assess_finish(
        state, operation=finish(overflow), observations=[], can_continue=True
    )
    assert len(state.requirements) == 32
    assert state.stop_reason == "requirement_capacity_exhausted"


def test_completion_context_marks_supported_as_model_declared_not_verified():
    state = TaskCompletionState()
    assert assess_finish(
        state,
        operation=finish(check(status="supported")),
        observations=[], can_continue=True,
    )
    context = state.context()
    assert context["scope"] == "model_declared_requirements_not_semantic_verification"
    assert context["requirements"][0]["status"] == "supported"
