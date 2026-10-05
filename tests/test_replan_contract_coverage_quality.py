"""Proposed contract-coverage guard: intentionally red until production GO.

Replacement is an execution edge, not evidence that a different old contract
was satisfied. These cases use literal contracts, not objective similarity.
"""

import json
from hashlib import sha256

import pytest

from app.core.multi_agent import (
    Plan,
    PlanPatchOperation,
    PlanStatus,
    PlanStepStatus,
)
from app.core.multi_agent_replan import (
    PlanPatchRejected,
    apply_plan_patch,
    replay_plan_patch_history,
)
from tests.test_multi_agent_replan import _context, _patch, _plan, _step
from tests.test_partial_replan_attempt_quality import setup_recovery


def _schema_contract(field):
    schema = {
        "type": "object",
        "properties": {field: {"type": "string"}},
        "required": [field],
        "additionalProperties": False,
    }
    return "Return JSON.\n```json-schema\n" + json.dumps(schema) + "\n```"


@pytest.mark.parametrize(
    ("original_contract", "replacement_contract", "old_criteria", "new_criteria"),
    [
        ("Return observation A and its evidence.", "Return observation B.", (), ()),
        (_schema_contract("observation_a"), _schema_contract("observation_b"), (), ()),
        ("Return evidence.", "Return evidence.", ("Check source A.",), ()),
    ],
    ids=["literal-contract-change", "typed-schema-change", "verification-removed"],
)
def test_changed_contract_requires_explicit_degradation(
    original_contract, replacement_contract, old_criteria, new_criteria
):
    original = _step("a", status=PlanStepStatus.FAILED).model_copy(
        update={"output_contract": original_contract, "verification_criteria": old_criteria}
    )
    replacement = _step("replacement").model_copy(
        update={"output_contract": replacement_contract, "verification_criteria": new_criteria}
    )
    plan = _plan(original, _step("consumer", "a"))
    patch = _patch(
        PlanPatchOperation.ALTERNATIVE_STEP,
        target_step_id="a",
        alternative_step=replacement,
    )
    with pytest.raises(PlanPatchRejected, match="(?i)contract|coverage|degrad"):
        apply_plan_patch(plan, patch, _context())
    assert plan.patch_revision == 0 and plan.steps[1].depends_on == ("a",)


def test_same_contract_allows_different_execution_objective_and_replay():
    original = _step("a", status=PlanStepStatus.FAILED)
    replacement = _step("replacement").model_copy(
        update={"objective": "Use a completely different execution strategy."}
    )
    application = apply_plan_patch(
        _plan(original, _step("consumer", "a")),
        _patch(
            PlanPatchOperation.ALTERNATIVE_STEP,
            target_step_id="a",
            alternative_step=replacement,
        ),
        _context(),
    )
    assert application.plan.steps[1].depends_on == ("replacement",)
    assert application.plan.steps[0].output_contract == original.output_contract
    assert replay_plan_patch_history(application.plan.patch_history, _context())[0].steps == application.plan.steps


def test_explicit_contract_degradation_allows_replacement_and_survives_replay():
    note = "Original observation A remains unverified; deliver only observation B."
    original = _step("a", status=PlanStepStatus.FAILED).model_copy(
        update={"output_contract": "Return observation A."}
    )
    replacement = _step("replacement").model_copy(
        update={"output_contract": "Return observation B."}
    )
    # Proposed additive use of the EXISTING field; current schema rejects this.
    patch = _patch(
        PlanPatchOperation.ALTERNATIVE_STEP,
        target_step_id="a",
        alternative_step=replacement,
        degradation_note=note,
    )
    application = apply_plan_patch(
        _plan(original, _step("consumer", "a")), patch, _context()
    )
    assert application.record.patch.degradation_note == note
    assert application.plan.steps[0].output_contract == "Return observation A."
    assert application.plan.steps[1].degraded_dependency_notes == (f"a: {note}",)
    assert replay_plan_patch_history(application.plan.patch_history, _context())[0].steps == application.plan.steps


def test_explicit_skip_degradation_remains_available():
    application = apply_plan_patch(
        _plan(_step("a", status=PlanStepStatus.FAILED), _step("consumer", "a")),
        _patch(
            PlanPatchOperation.SKIP_AND_DEGRADE,
            target_step_id="a",
            degradation_note="Observation A is missing and must remain unknown.",
        ),
        _context(),
    )
    assert application.plan.steps[1].depends_on == ()
    assert "must remain unknown" in application.plan.steps[1].degraded_dependency_notes[0]


def test_actual_partial_replacement_cannot_clear_replan_with_unrelated_contract():
    manager, parent, _, executor, _, loop = setup_recovery()
    # The original attempt is already partial; the replacement really completes
    # its own different contract, so a second partial cannot mask the defect.
    executor.partial_attempts = 0
    plan = Plan.model_validate(manager.get_run(parent.run_id).metadata["multi_agent_plan"])
    original = next(step for step in plan.steps if step.step_id == "leaf")
    replacement = _step("replacement").model_copy(
        update={
            "correlation_id": plan.correlation_id,
            "allowed_packages": original.allowed_packages,
            "allowed_tools": original.allowed_tools,
            "output_contract": "Return a different observation, not the original contract.",
            "verification_criteria": original.verification_criteria,
        }
    )
    patch = _patch(
        PlanPatchOperation.ALTERNATIVE_STEP,
        target_step_id="leaf",
        alternative_step=replacement,
    ).model_copy(update={"plan_id": plan.plan_id})
    outcome = loop._handle_plan_patch_decision(
        run_id=parent.run_id, operation=patch.model_dump(mode="json")
    )
    # Either reject before dispatch, or retain uncovered original obligations.
    # Autogenerated "Replaced by canonical step ..." is not a coverage proof.
    recovery = loop._terminal_plan_recovery(parent.run_id)
    assert outcome["status"] == "rejected" or (
        recovery is None and loop._multi_agent_replan_pending(parent.run_id)
    ), (outcome["status"], recovery)


@pytest.mark.parametrize("note", ["", " \t\n"])
def test_alternative_degradation_note_cannot_be_blank(note):
    with pytest.raises(ValueError, match="(?i)nonempty|degrad"):
        _patch(
            PlanPatchOperation.ALTERNATIVE_STEP,
            target_step_id="a",
            alternative_step=_step("replacement"),
            degradation_note=note,
        )


def _completed_replacement(*, contract=None, note=None):
    manager, parent, _, executor, _, loop = setup_recovery()
    executor.partial_attempts = 0
    plan = Plan.model_validate(manager.get_run(parent.run_id).metadata["multi_agent_plan"])
    original = next(step for step in plan.steps if step.step_id == "leaf")
    replacement = _step("replacement").model_copy(update={
        "correlation_id": plan.correlation_id,
        "allowed_packages": original.allowed_packages,
        "allowed_tools": original.allowed_tools,
        "output_contract": contract or original.output_contract,
        "verification_criteria": original.verification_criteria,
    })
    patch = _patch(
        PlanPatchOperation.ALTERNATIVE_STEP,
        target_step_id="leaf",
        alternative_step=replacement,
        degradation_note=note,
    ).model_copy(update={"plan_id": plan.plan_id})
    outcome = loop._handle_plan_patch_decision(
        run_id=parent.run_id, operation=patch.model_dump(mode="json")
    )
    assert outcome["status"] == "applied"
    terminal = Plan.model_validate(manager.get_run(parent.run_id).metadata["multi_agent_plan"])
    assert next(step for step in terminal.steps if step.step_id == "replacement").status == PlanStepStatus.COMPLETED
    return manager, parent, loop, terminal


def test_same_contract_terminal_recovery_preserves_original_partial_and_verification():
    manager, parent, loop, _ = _completed_replacement()
    before = manager.get_run(parent.run_id).metadata["multi_agent_verification"]
    recovery = loop._terminal_plan_recovery(parent.run_id)
    assert recovery is not None
    assert recovery["verification"] == before
    assert "not established" in recovery["degradation_notes"]["leaf"]
    original_result = next(
        event.payload["task_result"] for event in manager.list_events(parent.run_id)
        if event.type == "subtask_result" and event.payload["task_result"]["step_id"] == "leaf"
    )
    assert original_result["status"] == "partial"
    assert original_result["missing_requirements"] == ["child_budget_finish"]


def test_explicit_terminal_degradation_is_not_replaced_by_generated_description():
    note = "Original observation remains unverified; alternative covers a different output."
    manager, parent, loop, _ = _completed_replacement(contract="Return observation B.", note=note)
    verification = manager.get_run(parent.run_id).metadata["multi_agent_verification"]
    recovery = loop._terminal_plan_recovery(parent.run_id)
    assert recovery is not None
    assert recovery["degradation_notes"]["leaf"] == note
    assert recovery["verification"] == verification


@pytest.mark.parametrize("change", ["literal", "schema", "criteria", "current-contract-erased"])
def test_legacy_changed_contract_journal_cannot_release_original_requirements(change):
    manager, parent, loop, terminal = _completed_replacement()
    record = terminal.patch_history[-1]
    alternative = record.patch.alternative_step
    update = {"output_contract": "Return observation B."}
    if change == "schema":
        update = {"output_contract": _schema_contract("observation_b")}
    elif change == "criteria":
        update = {"verification_criteria": ("A different check.",)}
    alternative = alternative.model_copy(update=update)
    changed_patch = record.patch.model_copy(update={"alternative_step": alternative})
    legacy = terminal.model_copy(update={
        "status": PlanStatus.REPLANNING,
        "steps": tuple(
            step.model_copy(update=update)
            if step.step_id == alternative.step_id
            or (change == "current-contract-erased" and step.step_id == "leaf")
            else step
            for step in terminal.steps
        ),
    })
    after_json = legacy.model_dump_json(exclude={"patch_revision", "patch_history"}, exclude_none=False)
    legacy_record = record.model_copy(update={
        "patch": changed_patch,
        "after_json": after_json,
        "after_hash": sha256(after_json.encode()).hexdigest(),
    })
    legacy = legacy.model_copy(update={"patch_history": (legacy_record,)})
    # A self-consistent historical transition, not a tampered hash, represents
    # the contract-changing replacements accepted before this guard existed.
    manager._update_run(parent.run_id, status=manager.get_run(parent.run_id).status,
                        metadata_patch={"multi_agent_plan": legacy.model_dump(mode="json"),
                                        "multi_agent_replan_required": True})
    prior = manager.get_run(parent.run_id).metadata["multi_agent_verification"]
    assert loop._terminal_plan_recovery(parent.run_id) is None
    assert loop._multi_agent_replan_pending(parent.run_id)
    assert manager.get_run(parent.run_id).metadata["multi_agent_verification"] == prior
    with pytest.raises(PlanPatchRejected, match="(?i)contract|criteria|degrad"):
        replay_plan_patch_history(legacy.patch_history, _context())
