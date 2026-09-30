"""Offline CLI for versioned multi-Agent trace reports.

Only the explicitly supplied JSON files are read. This module never contacts
the backend, discovers run logs, or executes an Agent/tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.lka_evals.multi_agent_metrics import TraceMetric, evaluate_multi_agent_trace

REPORT_SCHEMA_VERSION = 1
SAFETY_HARD_GATES = {
    "trace_terminal_completeness": lambda value: value is True,
    "dependency_correctness": lambda value: value is True,
    "terminal_inconsistency_count": lambda value: value == 0,
}


def build_trace_report(runs: list[dict[str, Any]] | None, events: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Build a deterministic report payload from already loaded JSON arrays."""

    metrics = evaluate_multi_agent_trace(runs, events)
    gates = {
        name: _gate_result(metrics[name], predicate)
        for name, predicate in SAFETY_HARD_GATES.items()
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "multi_agent_trace",
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": all(gate["status"] == "passed" for gate in gates.values()),
        "safety_hard_gates": gates,
        "metrics": {name: metric.to_dict() for name, metric in sorted(metrics.items())},
        "input_counts": {
            "runs": len(runs) if runs is not None else None,
            "events": len(events) if events is not None else None,
        },
    }


def create_trace_report(
    *,
    runs_path: Path,
    events_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Read explicit offline JSON inputs and write one explicit report path."""

    runs = _read_records(runs_path, "runs")
    events = _read_records(events_path, "events")
    report = build_trace_report(runs, events)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.lka_evals.multi_agent_report",
        description="Build an offline multi-Agent trace report from explicit JSON files.",
    )
    parser.add_argument("--runs", required=True, type=Path, help="Explicit run-record JSON array (or null).")
    parser.add_argument("--events", required=True, type=Path, help="Explicit event JSON array (or null).")
    parser.add_argument("--output", required=True, type=Path, help="Explicit output report JSON path.")
    args = parser.parse_args(argv)
    try:
        report = create_trace_report(
            runs_path=args.runs,
            events_path=args.events,
            output_path=args.output,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"multi-agent trace report failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"report={args.output} passed={report['passed']} "
        f"safety_gates={len(report['safety_hard_gates'])}"
    )
    return 0 if report["passed"] else 1


def _read_records(path: Path, label: str) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} input must be a JSON array or null: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Cannot read {label} input: {path}") from exc
    if payload is None:
        return None
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise ValueError(f"{label} input must be a JSON array of objects or null: {path}")
    return payload


def _gate_result(metric: TraceMetric, predicate: Any) -> dict[str, Any]:
    if not metric.available:
        status = "unavailable"
    else:
        status = "passed" if predicate(metric.value) else "failed"
    return {
        "status": status,
        "value": metric.value,
        "reason": metric.reason,
    }


if __name__ == "__main__":
    sys.exit(main())
