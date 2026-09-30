import json

from evals.lka_evals.multi_agent_report import main


def _safe_inputs():
    runs = [
        {
            "run_id": "parent",
            "status": "completed",
            "created_at": "2026-01-01T00:00:00Z",
            "child_run_ids": [],
            "metadata": {
                "multi_agent_plan": {
                    "plan_id": "plan-1",
                    "steps": [{"step_id": "only-step", "depends_on": []}],
                }
            },
        }
    ]
    events = []
    return runs, events


def test_offline_report_writes_versioned_passed_report_to_explicit_path(tmp_path):
    runs, events = _safe_inputs()
    runs_path = tmp_path / "input-runs.json"
    events_path = tmp_path / "input-events.json"
    output_path = tmp_path / "reports" / "report.json"
    runs_path.write_text(json.dumps(runs), encoding="utf-8")
    events_path.write_text(json.dumps(events), encoding="utf-8")

    exit_code = main(
        [
            "--runs",
            str(runs_path),
            "--events",
            str(events_path),
            "--output",
            str(output_path),
        ]
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["schema_version"] == 1
    assert report["report_type"] == "multi_agent_trace"
    assert report["passed"] is True
    assert report["safety_hard_gates"]["dependency_correctness"]["status"] == "passed"
    assert report["safety_hard_gates"]["terminal_inconsistency_count"]["status"] == "passed"
    assert report["safety_hard_gates"]["trace_terminal_completeness"]["status"] == "passed"


def test_nonterminal_trace_cannot_pass_safety_gates():
    from evals.lka_evals.multi_agent_report import build_trace_report

    runs, events = _safe_inputs()
    runs[0]["status"] = "running"
    report = build_trace_report(runs, events)

    assert report["passed"] is False
    assert report["safety_hard_gates"]["trace_terminal_completeness"]["status"] == "failed"


def test_unavailable_safety_gate_fails_report_instead_of_passing(tmp_path):
    runs_path = tmp_path / "runs.json"
    events_path = tmp_path / "events.json"
    output_path = tmp_path / "report.json"
    runs_path.write_text("null", encoding="utf-8")
    events_path.write_text("null", encoding="utf-8")

    exit_code = main(
        ["--runs", str(runs_path), "--events", str(events_path), "--output", str(output_path)]
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["passed"] is False
    assert report["safety_hard_gates"]["dependency_correctness"]["status"] == "unavailable"
    assert report["metrics"]["dependency_correctness"]["value"] is None
    assert report["metrics"]["dependency_correctness"]["available"] is False


def test_report_cli_rejects_non_array_input(tmp_path, capsys):
    runs_path = tmp_path / "runs.json"
    events_path = tmp_path / "events.json"
    output_path = tmp_path / "report.json"
    runs_path.write_text("{}", encoding="utf-8")
    events_path.write_text("[]", encoding="utf-8")

    exit_code = main(
        ["--runs", str(runs_path), "--events", str(events_path), "--output", str(output_path)]
    )

    assert exit_code == 2
    assert "JSON array of objects or null" in capsys.readouterr().err
    assert not output_path.exists()
