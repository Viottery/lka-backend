"""Run deterministic, offline checks for memory provenance and release risks.

This small synthetic suite is a regression foundation, not a production release
gate. It exercises the real local MemoryService and MemoryContextProvider.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from app.core.memory_context import MemoryContextProvider
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "evals/fixtures/memory_release_cases.jsonl"
PRODUCTION_MIN_CASES = 200
SECURITY_DIMENSIONS = {"scope", "retraction", "injection"}


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not cases:
        raise ValueError("fixture contains no cases")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("every fixture case must have a unique non-empty id")
    return cases


def _run_case(service: MemoryService, provider: MemoryContextProvider,
              case: dict[str, Any]) -> dict[str, Any]:
    kind = case["kind"]
    common = {"id": case["id"], "kind": kind, "passed": False, "checks": {}}
    if kind == "provenance":
        source = service.register_source(MemorySourceInput(
            source_type=case["source_type"], source_ref=f"fixture:{case['id']}",
            trusted_source=case["trusted_source"],
        ))
        record = service.create(MemoryInput(
            content=case["content"], source_id=source, sensitivity="normal",
        ))
        common["checks"] = {
            "source_recorded": source in record.source_ids,
            "untrusted_not_active": record.status == case["expected_status"],
        }
    elif kind == "scope":
        project_ids = {
            key: service.resolve_project(f"/synthetic/{key}")
            for key in case["projects"]
        }
        source = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=f"fixture:{case['id']}", trusted_source=True,
        ))
        for item in case["memories"]:
            service.create(MemoryInput(
                content=item["content"], memory_type="project_decision", scope="project",
                project_id=project_ids[item["project"]], source_id=source,
                sensitivity="normal",
            ))
        for session in case["sessions"]:
            view = provider(session["id"], f"/synthetic/{session['project']}", case["query"])
            actual = {entry["content"] for entry in view["items"]}
            expected = set(session["expected_contents"])
            common["checks"][session["id"]] = (
                expected <= actual
                and all(
                    content not in actual
                    for other in case["sessions"] if other["id"] != session["id"]
                    for content in other["expected_contents"]
                )
            )
    elif kind == "retraction":
        source = service.register_source(MemorySourceInput(
            source_type="user_message", source_ref=f"fixture:{case['id']}", trusted_source=True,
        ))
        record = service.create(MemoryInput(
            content=case["content"], source_id=source, sensitivity="normal",
        ))
        before = {entry["memory_id"] for entry in provider("before", None, case["query"])["items"]}
        service.retract(record.memory_id, expected_version=record.version)
        after = {entry["memory_id"] for entry in provider("after", None, case["query"])["items"]}
        common["checks"] = {
            "visible_before_retraction": record.memory_id in before,
            "absent_after_retraction": record.memory_id not in after,
            "status_retracted": service.get(record.memory_id).status == "retracted",
        }
    elif kind == "injection":
        # Simulate untrusted external text entering the extraction/publish boundary.
        source = service.register_source(MemorySourceInput(
            source_type=case["source_type"], source_ref=f"fixture:{case['id']}",
            trusted_source=False,
        ))
        record = service.create(MemoryInput(
            content=case["content"], source_id=source, sensitivity="normal",
        ))
        injected = {entry["memory_id"] for entry in provider("injection", None, case["query"])["items"]}
        common["checks"] = {
            "external_source_untrusted": record.status == "candidate",
            "candidate_not_injected": record.memory_id not in injected,
        }
    else:
        raise ValueError(f"unsupported case kind: {kind!r}")
    common["passed"] = bool(common["checks"]) and all(common["checks"].values())
    return common


def evaluate(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    cases = load_cases(path)
    with tempfile.TemporaryDirectory(prefix="lka-memory-release-") as temp_dir:
        service = MemoryService(Path(temp_dir) / "memory.sqlite3")
        service.ensure_schema()
        provider = MemoryContextProvider(service)
        results = [_run_case(service, provider, case) for case in cases]

    by_kind: dict[str, dict[str, int | float]] = {}
    for kind in sorted({result["kind"] for result in results}):
        selected = [result for result in results if result["kind"] == kind]
        passed = sum(result["passed"] for result in selected)
        by_kind[kind] = {
            "samples": len(selected), "passed": passed,
            "pass_rate": passed / len(selected),
        }
    security_rows = [row for row in results if row["kind"] in SECURITY_DIMENSIONS]
    security_failures = sum(not row["passed"] for row in security_rows)
    sufficient_sample_count = len(cases) >= PRODUCTION_MIN_CASES
    return {
        "suite": "memory_release_offline",
        "fixture": str(path.resolve()),
        "offline": True,
        "samples": len(cases),
        "passed": sum(row["passed"] for row in results),
        "failed": sum(not row["passed"] for row in results),
        "pass_rate": sum(row["passed"] for row in results) / len(results),
        "metrics": {
            "by_case_kind": by_kind,
            "provenance_source_accuracy": _rate(results, {"provenance"}, "source_recorded"),
            "scope_isolation_pass_rate": _rate(results, {"scope"}),
            "retraction_non_injection_pass_rate": _rate(results, {"retraction", "injection"}),
            "external_injection_promotion_count": sum(
                row["checks"].get("external_source_untrusted") is False for row in results
                if row["kind"] == "injection"
            ),
            "candidate_injection_count": sum(
                row["checks"].get("candidate_not_injected") is False for row in results
                if row["kind"] == "injection"
            ),
        },
        "release_gate": {
            # This suite uses synthetic offline cases. Sample count alone can
            # never establish real-model quality, privacy or latency SLOs.
            "production_gate_eligible": False,
            "status": "synthetic_regression_only",
            "minimum_samples_for_review": PRODUCTION_MIN_CASES,
            "minimum_sample_count_met": sufficient_sample_count,
            "required_security_dimensions_present": SECURITY_DIMENSIONS <= set(by_kind),
            "security_failures": security_failures,
            "note": "Synthetic offline regression only; not a production release decision or quality/SLO estimate.",
        },
        "cases": results,
    }


def _rate(results: list[dict[str, Any]], kinds: set[str], check: str | None = None) -> dict[str, int | float]:
    selected = [row for row in results if row["kind"] in kinds]
    denominator = sum(
        (1 if row["kind"] in {"scope", "retraction", "injection"} else len(row["checks"]))
        if check is None else 1
        for row in selected
    )
    numerator = (
        sum(all(row["checks"].values()) for row in selected) if check is None
        else sum(bool(row["checks"].get(check)) for row in selected)
    )
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else 0.0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.fixture)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
