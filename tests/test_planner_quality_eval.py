from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.lka_evals.planner_quality import evaluate_planner_outputs, main

ROOT = Path(__file__).resolve().parents[1]


def _suite() -> dict:
    return json.loads((ROOT / "evals/suites/planner_quality.yaml").read_text(encoding="utf-8"))


def _expected_candidates(suite: dict) -> dict:
    outputs = []
    for case in suite["cases"]:
        oracle = case["oracle"]
        action = oracle["action"]
        if action == "plan_patch":
            raw_plan = case["validation"]["plan"]
            expected_operation = oracle["operation"]
            outputs.append({
                "case_id": case["case_id"],
                "action": action,
                "operation": {
                    "patch_id": f"patch-{case['case_id']}",
                    "plan_id": raw_plan["plan_id"],
                    "expected_revision": 0,
                    "operation": expected_operation["operation"],
                    "reason": "Apply the bounded action selected for this case.",
                    **{key: value for key, value in expected_operation.items() if key != "operation"},
                },
            })
        elif action == "fork_subtasks":
            outputs.append({
                "case_id": case["case_id"],
                "action": action,
                "operation": {
                    "correlation_id": "planner_eval",
                    "operation": "fork_subtasks",
                    "operation_id": "fork-offline-eval",
                    "parent_step_id": "root",
                    "subtasks": [
                        {**item, "objective": item["objective"], "output_contract": item["output_contract"]}
                        for item in oracle["operation"]["subtasks"]
                    ],
                },
            })
        else:
            outputs.append({"case_id": case["case_id"], "action": action})
    return {"schema_version": 1, "candidate_kind": "scripted_fixture", "outputs": outputs}


def test_scripted_candidates_separate_structure_and_oracle_scores() -> None:
    suite = _suite()
    report = evaluate_planner_outputs(suite, _expected_candidates(suite))

    assert report["candidate_kind"] == "scripted_fixture"
    assert report["metrics"]["structure_policy_validity"] == {
        "numerator": 7, "denominator": 7, "rate": 1.0,
    }
    assert report["metrics"]["oracle_decision_match"] == {
        "numerator": 7, "denominator": 7, "rate": 1.0,
    }
    assert "not evidence of real LLM Planner capability" in report["interpretation"]
    assert report["candidate_provenance_claim"]["verified"] is False


def test_final_answer_does_not_pass_unresolved_failure_oracle() -> None:
    suite = _suite()
    candidates = _expected_candidates(suite)
    first = candidates["outputs"][0]
    first.update(action="final_answer")
    first.pop("operation")

    report = evaluate_planner_outputs(suite, candidates)

    assert report["cases"][0]["structurally_valid"] is True
    assert report["cases"][0]["oracle_decision_match"] is False


def test_omitted_required_step_is_structural_but_fails_case_oracle() -> None:
    suite = _suite()
    candidates = _expected_candidates(suite)
    output = candidates["outputs"][-1]
    output["operation"]["subtasks"] = output["operation"]["subtasks"][:1]

    report = evaluate_planner_outputs(suite, candidates)

    assert report["cases"][-1]["structurally_valid"] is True
    assert report["cases"][-1]["oracle_decision_match"] is False


def test_candidate_cannot_select_agent_outside_trusted_policy_fixture() -> None:
    suite = _suite()
    candidates = _expected_candidates(suite)
    fork = candidates["outputs"][-1]["operation"]
    fork["subtasks"][0]["agent_id"] = "unregistered_agent"

    report = evaluate_planner_outputs(suite, candidates)

    assert report["cases"][-1]["structurally_valid"] is False
    assert report["cases"][-1]["oracle_decision_match"] is False


def test_cyclic_fork_is_rejected_by_existing_fork_policy_validator() -> None:
    suite = _suite()
    candidates = _expected_candidates(suite)
    output = next(
        item for item in candidates["outputs"] if item["case_id"] == "invalid_dag_corrected_fork"
    )
    output.update({
        "action": "fork_subtasks",
        "operation": {
            "correlation_id": "planner_eval",
            "operation": "fork_subtasks",
            "operation_id": "cyclic-plan",
            "parent_step_id": "root",
            "subtasks": [
                {"step_id": "a", "objective": "Task A", "output_contract": "A", "depends_on": ["b"]},
                {"step_id": "b", "objective": "Task B", "output_contract": "B", "depends_on": ["a"]},
            ],
        },
    })

    report = evaluate_planner_outputs(suite, candidates)
    result = next(item for item in report["cases"] if item["case_id"] == "invalid_dag_corrected_fork")

    assert result["structurally_valid"] is False
    assert "cyclic" in result["structural_reason"]
    assert result["oracle_decision_match"] is False


def test_unknown_candidate_case_id_fails_closed() -> None:
    suite = _suite()
    candidates = _expected_candidates(suite)
    candidates["outputs"].append({"case_id": "not-in-suite", "action": "final_answer"})

    with pytest.raises(ValueError, match="unknown case IDs"):
        evaluate_planner_outputs(suite, candidates)


def test_missing_candidate_is_scored_as_not_valid_and_not_matched() -> None:
    suite = _suite()
    report = evaluate_planner_outputs(suite, {"schema_version": 1, "outputs": []})

    assert report["metrics"]["structure_policy_validity"]["rate"] == 0
    assert report["metrics"]["oracle_decision_match"]["rate"] == 0


def test_cli_reads_explicit_candidate_file_and_writes_requested_report(tmp_path) -> None:
    suite_path = ROOT / "evals/suites/planner_quality.yaml"
    candidate_path = tmp_path / "candidate.json"
    output_path = tmp_path / "report.json"
    candidate_path.write_text(
        json.dumps(_expected_candidates(_suite())), encoding="utf-8"
    )

    exit_code = main([
        str(suite_path), "--candidates", str(candidate_path), "--output", str(output_path),
    ])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["metrics"]["oracle_decision_match"]["rate"] == 1.0
