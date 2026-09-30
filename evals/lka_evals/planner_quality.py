"""Offline, deterministic scoring for explicit structured Planner outputs.

This module never calls a model or backend. Scores describe only the supplied
candidate file (often scripted fixtures), not general LLM Planner capability.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.multi_agent import (
    ForkCallerKind,
    ForkPolicy,
    ForkSubtasksOperation,
    ForkValidationContext,
    Plan,
    PlanPatch,
    PlanPatchContext,
    PlanStepStatus,
    ScopeGrant,
    validate_fork_subtasks,
)
from app.core.multi_agent_replan import apply_plan_patch

REPORT_SCHEMA_VERSION = 1
SUITE_SCHEMA_VERSION = 1
_ACTIONS = {"fork_subtasks", "plan_patch", "final_answer"}


def evaluate_planner_outputs(
    suite: dict[str, Any], candidates: dict[str, Any]
) -> dict[str, Any]:
    """Score supplied outputs for structure/policy and case-oracle agreement."""

    if suite.get("schema_version") != SUITE_SCHEMA_VERSION:
        raise ValueError(f"suite schema_version must be {SUITE_SCHEMA_VERSION}")
    if candidates.get("schema_version") != 1:
        raise ValueError("candidate schema_version must be 1")
    if not isinstance(suite.get("cases"), list) or not isinstance(candidates.get("outputs"), list):
        raise TypeError("suite.cases and candidates.outputs must be arrays")
    case_ids = [case.get("case_id") for case in suite["cases"]]
    if any(not isinstance(case_id, str) or not case_id for case_id in case_ids) or len(set(case_ids)) != len(case_ids):
        raise ValueError("case_id values must be non-empty and unique")
    output_map: dict[str, dict[str, Any]] = {}
    for output in candidates["outputs"]:
        if not isinstance(output, dict) or not isinstance(output.get("case_id"), str):
            raise TypeError("each output must be an object with case_id")
        if output["case_id"] in output_map:
            raise ValueError(f"duplicate candidate output for {output['case_id']}")
        output_map[output["case_id"]] = output
    unknown_case_ids = set(output_map) - set(case_ids)
    if unknown_case_ids:
        raise ValueError(f"candidate outputs contain unknown case IDs: {sorted(unknown_case_ids)}")

    details: list[dict[str, Any]] = []
    for case in suite["cases"]:
        candidate = output_map.get(case["case_id"])
        structural, structural_reason = _validate_candidate(case, candidate)
        decision_match = bool(
            structural
            and candidate is not None
            and _matches_oracle(case.get("oracle", {}), candidate)
        )
        details.append({
            "case_id": case["case_id"],
            "structurally_valid": structural,
            "structural_reason": structural_reason,
            "oracle_decision_match": decision_match,
            "candidate_action": candidate.get("action") if candidate else None,
            "expected_action": case.get("oracle", {}).get("action"),
        })
    count = len(details)
    structure_count = sum(item["structurally_valid"] for item in details)
    decision_count = sum(item["oracle_decision_match"] for item in details)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "planner_quality_offline",
        "suite_id": suite.get("suite_id"),
        "suite_version": suite.get("suite_version"),
        "candidate_kind": candidates.get("candidate_kind", "unspecified"),
        "candidate_provenance_claim": {
            "provider": candidates.get("provider"),
            "model": candidates.get("model"),
            "configuration_id": candidates.get("configuration_id"),
            "verified": False,
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "interpretation": (
            "Scores apply only to the explicitly supplied candidate outputs. "
            "Scripted fixture scores are not evidence of real LLM Planner capability."
        ),
        "metrics": {
            "structure_policy_validity": {
                "numerator": structure_count,
                "denominator": count,
                "rate": structure_count / count if count else None,
            },
            "oracle_decision_match": {
                "numerator": decision_count,
                "denominator": count,
                "rate": decision_count / count if count else None,
            },
        },
        "cases": details,
    }


def _validate_candidate(case: dict[str, Any], candidate: dict[str, Any] | None) -> tuple[bool, str | None]:
    if candidate is None:
        return False, "candidate output is missing"
    action = candidate.get("action")
    if action not in _ACTIONS:
        return False, "unsupported or missing action"
    operation = candidate.get("operation")
    if action in {"fork_subtasks", "plan_patch"} and not isinstance(operation, dict):
        return False, "operation object is required"
    if action not in {"fork_subtasks", "plan_patch"} and operation is not None:
        return False, "operation is not allowed for this action"
    try:
        if action == "fork_subtasks":
            _validate_fork(case, operation)
        elif action == "plan_patch":
            _validate_patch(case, operation)
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        return False, str(exc).splitlines()[0][:300]
    return True, None


def _scope(raw: dict[str, Any]) -> ScopeGrant:
    return ScopeGrant.model_validate(raw)


def _validate_fork(case: dict[str, Any], raw_operation: dict[str, Any]) -> None:
    context_data = case.get("validation", {})
    policy_data = context_data.get("policy", {})
    authorized_scope = _scope(policy_data.get("allowed_scope", {}))
    policy = ForkPolicy(
        max_depth=int(policy_data.get("max_depth", 1)),
        max_children=int(policy_data.get("max_children", 4)),
        max_fork_size=int(policy_data.get("max_fork_size", 4)),
        allowed_scope=authorized_scope,
        allowed_agent_ids=tuple(policy_data.get("allowed_agent_ids", ("general_agent",))),
        allowed_inference_profile_ids=tuple(policy_data.get("allowed_inference_profile_ids", ())),
    )
    known_step_ids = tuple(context_data.get("known_step_ids", ("root",)))
    validation_context = ForkValidationContext(
        parent_effective_scope=_scope(policy_data.get("parent_scope", policy_data.get("allowed_scope", {}))),
        session_scope=_scope(policy_data.get("session_scope", policy_data.get("allowed_scope", {}))),
        workspace_scope=_scope(policy_data.get("workspace_scope", policy_data.get("allowed_scope", {}))),
        parent_step_status=PlanStepStatus(context_data.get("parent_step_status", "running")),
        caller_kind=ForkCallerKind(context_data.get("caller_kind", "root_planner")),
        created_by_run_id="offline-eval-caller",
        current_depth=int(context_data.get("current_depth", 0)),
        existing_child_count=int(context_data.get("existing_child_count", 0)),
        known_step_ids=known_step_ids,
    )
    operation = ForkSubtasksOperation.model_validate(raw_operation)
    # Never accept candidate-provided limits or authority; limits come only from
    # this versioned suite case's explicit server-policy fixture.
    validate_fork_subtasks(operation, policy=policy, context=validation_context)


def _validate_patch(case: dict[str, Any], raw_operation: dict[str, Any]) -> None:
    validation = case.get("validation", {})
    raw_plan = validation.get("plan")
    if not isinstance(raw_plan, dict):
        raise TypeError("plan_patch case has no trusted validation.plan fixture")
    plan = Plan.model_validate(raw_plan)
    policy_data = validation.get("policy", {})
    allowed_scope = _scope(policy_data.get("allowed_scope", {}))
    policy = ForkPolicy(
        max_depth=int(policy_data.get("max_depth", 1)),
        max_children=int(policy_data.get("max_children", 4)),
        max_fork_size=int(policy_data.get("max_fork_size", 4)),
        allowed_scope=allowed_scope,
        allowed_agent_ids=tuple(policy_data.get("allowed_agent_ids", ("general_agent",))),
        allowed_inference_profile_ids=tuple(policy_data.get("allowed_inference_profile_ids", ())),
    )
    patch = PlanPatch.model_validate(raw_operation)
    apply_plan_patch(
        plan,
        patch,
        PlanPatchContext(
            fork_policy=policy,
            parent_effective_scope=_scope(policy_data.get("parent_scope", policy_data.get("allowed_scope", {}))),
            session_scope=_scope(policy_data.get("session_scope", policy_data.get("allowed_scope", {}))),
            workspace_scope=_scope(policy_data.get("workspace_scope", policy_data.get("allowed_scope", {}))),
            remaining_budget=validation.get("remaining_budget"),
            max_retries_per_step=int(validation.get("max_retries_per_step", 1)),
        ),
    )


def _matches_oracle(oracle: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if candidate.get("action") != oracle.get("action"):
        return False
    expected = oracle.get("operation")
    actual = candidate.get("operation")
    if expected is None:
        return actual is None
    if not isinstance(actual, dict):
        return False
    # Compare only the explicitly declared oracle fields, allowing harmless IDs
    # and explanatory text to differ between equivalent structured decisions.
    return _contains_expected(actual, expected)


def _contains_expected(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains_expected(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, (list, tuple))
            and len(actual) == len(expected)
            and all(_contains_expected(item, wanted) for item, wanted in zip(actual, expected))
        )
    return actual == expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score explicit offline Planner output candidates.")
    parser.add_argument("suite", type=Path)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    suite = json.loads(args.suite.read_text(encoding="utf-8"))
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    report = evaluate_planner_outputs(suite, candidates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    structure = report["metrics"]["structure_policy_validity"]
    decision = report["metrics"]["oracle_decision_match"]
    print(
        f"structure/policy={structure['numerator']}/{structure['denominator']} "
        f"oracle-match={decision['numerator']}/{decision['denominator']} output={args.output}"
    )
    return 0 if structure["rate"] == 1.0 and decision["rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
