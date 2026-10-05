"""Filtering/listing precedes subject/provider creation; skips are not passes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.lka_evals import runner
from evals.lka_evals.case_loader import select_suite_cases
from evals.lka_evals.report import _suite_summary


def cases():
    return [{"case_id": "basic", "tags": ["foundation"]},
            {"case_id": "hard", "tags": ["challenge", "web"]},
            {"case_id": "other", "tags": ["challenge", "memory"]}]


def test_selected_order_is_suite_order_and_exclusion_is_not_a_success():
    selected = select_suite_cases(cases(), case_ids={"other", "basic", "hard"},
                                  exclude_case_ids={"basic"}, tags={"web", "memory"})
    assert [c["case_id"] for c in selected] == ["hard", "other"]


@pytest.mark.parametrize("kwargs", [{"case_ids": {"typo"}}, {"exclude_case_ids": {"typo"}},
                                    {"tags": {"typo"}}, {"case_ids": set()},
                                    {"exclude_case_ids": {"basic", "hard", "other"}}])
def test_typo_or_empty_selection_is_not_a_green_evaluation(kwargs):
    with pytest.raises(ValueError):
        select_suite_cases(cases(), **kwargs)


@pytest.mark.parametrize("bad", [[], [{"case_id": "a"}, {"case_id": "a"}],
                                 [{"case_id": "a", "tags": "web"}], [None]])
def test_invalid_suite_rejected_before_execution(bad):
    with pytest.raises(ValueError):
        select_suite_cases(bad)
    assert _suite_summary([])["passed"] is False


def test_list_with_real_mode_and_judge_has_zero_provider_or_subject_creation(tmp_path, monkeypatch, capsys):
    path = tmp_path / "suite.yaml"
    path.write_text(json.dumps({"suite_id": "test", "cases": cases()}))
    def forbidden(**kwargs):
        pytest.fail("Listing must not instantiate a subject, provider, or judge")
    monkeypatch.setattr(runner, "build_subject", forbidden)
    monkeypatch.setattr(runner, "build_text_llm_client", forbidden)
    assert runner.main([str(path), "--list", "--json", "--llm-mode", "real",
                        "--judge", "--tag", "challenge", "--exclude-case", "other"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"suite_id": "test", "total": 3, "selected": ["hard"], "executed": False}


def test_zero_selection_cannot_build_subject(tmp_path, monkeypatch):
    path = tmp_path / "suite.yaml"
    path.write_text(json.dumps({"suite_id": "test", "cases": cases()}))
    monkeypatch.setattr(runner, "build_subject", lambda **kw: pytest.fail("No subject allowed"))
    with pytest.raises(ValueError, match="Empty"):
        runner.run_suite(path, case_ids={"basic"}, exclude_case_ids={"basic"})


def test_execution_and_persisted_report_show_skipped_not_passed_cases(tmp_path, monkeypatch):
    path = tmp_path / "suite.yaml"
    path.write_text(json.dumps({"suite_id": "test", "cases": cases()}))
    seen = []
    class Subject:
        def run_case(self, *, suite_id, case):
            seen.append(case["case_id"])
            return SimpleNamespace(case_id=case["case_id"])
    monkeypatch.setattr(runner, "build_subject", lambda **kw: Subject())
    monkeypatch.setattr(runner, "evaluate_case", lambda *args: [])
    monkeypatch.setattr(runner, "summarize_metrics", lambda metrics: {"passed": True, "score": 1})
    monkeypatch.setattr(runner, "_case_result", lambda **kwargs: {
        "case_id": kwargs["case"]["case_id"], "passed": True, "score": 1, "metrics": []})
    result = runner.run_suite(path, case_ids={"hard"}, report_dir=tmp_path / "reports")
    assert seen == ["hard"] and result["case_count"] == 1
    assert result["selection"] == {"total": 3, "selected": 1, "skipped_case_ids": ["basic", "other"]}
    report = json.loads(Path(result["report_paths"]["json"]).read_text(encoding="utf-8"))
    assert report["selection"] == result["selection"]
    assert report["summary"]["passed_count"] == 1


def test_latency_only_failure_is_diagnostic_not_a_relaxed_pass():
    summary = _suite_summary([
        {"case_id": "slow", "passed": False, "metrics": [
            {"name": "wall_time_ms", "passed": False}, {"name": "schema_valid", "passed": True}]},
        {"case_id": "mixed", "passed": False, "metrics": [
            {"name": "wall_time_ms", "passed": False}, {"name": "schema_valid", "passed": False}]},
        {"case_id": "unknown", "passed": False, "metrics": []},
    ])
    assert summary["passed"] is False and summary["failed_count"] == 3
    assert summary["failure_kinds"] == {"latency_only": ["slow"], "other_or_unknown": ["mixed", "unknown"]}
