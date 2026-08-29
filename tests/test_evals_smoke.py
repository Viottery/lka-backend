from __future__ import annotations

import json
from pathlib import Path

from evals.lka_evals.case_loader import load_suite
from evals.lka_evals.metrics import evaluate_case
from evals.lka_evals.runner import run_suite
from evals.lka_evals.subject import EvalRunArtifact


def test_eval_smoke_suite_runs_with_isolated_runtime(tmp_path):
    suite_path = Path("evals/suites/smoke.yaml")

    result = run_suite(
        suite_path,
        subject_name="runtime",
        report_dir=tmp_path / "reports",
    )

    assert result["suite_id"] == "smoke"
    assert result["subject"] == "runtime"
    assert result["passed"] is True
    assert result["case_count"] == 1

    case = result["cases"][0]
    assert case["passed"] is True
    assert case["result"]["selected_package"] == "mail"
    assert case["result"]["tool_events"][0]["tool_name"] == "mail.search"
    assert case["result"]["tool_events"][1]["tool_name"] == "mail.load_messages"
    assert case["log"]["exists"] is True
    assert case["log"]["missing_sections"] == []
    assert "eval_ntuso_audition_20260805" in case["fixture_index"]["mail"][
        "messages_by_external_id"
    ]

    json_report = Path(result["report_paths"]["json"])
    markdown_report = Path(result["report_paths"]["markdown"])
    assert json_report.exists()
    assert markdown_report.exists()
    report_payload = json.loads(json_report.read_text(encoding="utf-8"))
    assert report_payload["summary"]["passed"] is True
    metric_names = {
        metric["name"]
        for metric in report_payload["cases"][0]["metrics"]
    }
    assert {
        "selected_package_match",
        "tool_sequence_exact_match",
        "evidence_recall_at_k",
        "mail_search_recall_at_k",
        "mail_search_precision_at_k",
        "mail_search_mrr",
        "run_log_completeness_rate",
    }.issubset(metric_names)


def test_eval_suite_files_are_json_compatible_yaml():
    for suite_path in Path("evals/suites").rglob("*.yaml"):
        suite = load_suite(suite_path)
        assert suite["suite_id"]
        assert suite["cases"]


def test_mail_search_metrics_quantify_recall_precision_mrr_and_forbidden():
    artifact = EvalRunArtifact(
        suite_id="unit",
        case_id="mail_search_metrics",
        subject="runtime",
        request={},
        fixture_index={
            "mail": {
                "messages_by_external_id": {
                    "relevant_a": {"message_id": "msg_a"},
                    "relevant_b": {"message_id": "msg_b"},
                    "forbidden": {"message_id": "msg_x"},
                }
            }
        },
        result={
            "tool_events": [
                {
                    "tool_name": "mail.search",
                    "result": {
                        "status": "completed",
                        "output": {
                            "messages": [
                                {"message_id": "msg_x"},
                                {"message_id": "msg_a"},
                                {"message_id": "msg_noise"},
                            ]
                        },
                    },
                },
                {
                    "tool_name": "mail.search",
                    "result": {
                        "status": "completed",
                        "output": {
                            "messages": [
                                {"message_id": "msg_a"},
                                {"message_id": "msg_b"},
                            ]
                        },
                    },
                },
            ]
        },
    )
    case = {
        "expect": {
            "mail_search_k": 4,
            "mail_search_relevant_external_ids": ["relevant_a", "relevant_b"],
            "mail_search_forbidden_external_ids": ["forbidden"],
        }
    }

    metrics = {metric.name: metric for metric in evaluate_case(case, artifact)}

    assert metrics["mail_search_recall_at_k"].score == 1.0
    assert metrics["mail_search_precision_at_k"].score == 0.5
    assert metrics["mail_search_mrr"].score == 0.5
    assert metrics["mail_search_forbidden_at_k"].passed is False
    assert metrics["mail_search_forbidden_at_k"].details["violations"] == ["msg_x"]


def test_file_ops_eval_suites_run_with_isolated_runtime(tmp_path):
    suite_paths = [
        Path("evals/suites/file_ops/filesystem_tools.yaml"),
        Path("evals/suites/file_ops/bash_tools.yaml"),
        Path("evals/suites/file_ops/safety_review_tools.yaml"),
    ]

    for suite_path in suite_paths:
        result = run_suite(
            suite_path,
            subject_name="runtime",
            report_dir=tmp_path / "reports",
        )

        assert result["passed"] is True
        assert result["case_count"] >= 1
