"""One Root-authorized parallel_audit attempt; shared ledger, private artifacts.

No new goal, fixture, judge, retry, provider capability, or child-token override.
Run only after Root review/GO with both --remote and --root-go.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.core.config import Settings
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded
from scripts import eval_realworld
from scripts.eval_runtime_memory_quality import LEDGER, MODEL, OUTPUT, isolated_config

MAX_CALLS = 32
CASE_ID = "parallel_audit"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_REPORT = PROJECT_ROOT / (
    "data/quality_runs/linux_20261005/parallel_audit_20261004T221256861941/report.json"
)
SOURCE_FILES = (
    "app/core/agent_turn.py", "app/core/agent_graph.py", "app/core/agent_runs.py",
    "app/core/child_agent.py", "app/core/multi_agent.py", "app/core/prompt_budget.py",
    "app/core/multi_agent_replan.py", "app/core/tool_result_gate.py",
    "app/tool_packages/observation.py",
    "app/core/tools.py", "app/core/llm/service.py", "app/tool_packages/bash.py",
    "scripts/eval_realworld.py", "scripts/eval_runtime_parallel_quality.py",
)


class AttemptBudget:
    """One locked 32-dispatch allowance shared by parent, children and repairs."""

    def __init__(self, ledger: LiveBudget, *, source_id: str) -> None:
        if ledger.usd_limit > 50 or not 1 <= len(source_id) <= 200:
            raise ValueError("An authoritative USD-50 ledger and bounded source ID are required")
        self.ledger, self.source_id = ledger, source_id
        self.ids: list[str] = []
        self.lock = threading.Lock()

    def reserve(self, **kwargs) -> str:
        with self.lock:
            if kwargs["kind"] != "llm" or len(self.ids) >= MAX_CALLS:
                raise LiveBudgetExceeded("parallel attempt 32-call/search-zero allowance exhausted")
            call = self.ledger.reserve(**{**kwargs, "stage": self.source_id + ":" + kwargs["stage"]})
            self.ids.append(call)
            return call

    def finish(self, *args, **kwargs) -> None:
        self.ledger.finish(*args, **kwargs)

    def snapshot(self) -> dict:
        with self.lock:
            local = {"source_id": self.source_id, "call_limit": MAX_CALLS,
                     "search_limit": 0, "dispatches": len(self.ids), "call_ids": list(self.ids)}
        return {**self.ledger.snapshot(), "ledger_path": str(self.ledger.path.resolve()),
                "run_budget": local, "scope": "shared cumulative ledger; NOT attempt-only totals"}

    def usage(self) -> dict:
        with self.lock:
            ids = tuple(self.ids)
        with self.ledger.connect() as conn:
            # Attribute by exact reservation IDs, not a shared snapshot delta:
            # other Root workers may reserve/settle on this same ledger.
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id,status,charged,input_tokens,output_tokens,cached_tokens,usage_known "
                "FROM calls WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids,
            ).fetchall() if ids else []
        known = [row for row in rows if row["usage_known"] == 1]
        unknown = [row for row in rows if row["usage_known"] != 1]
        pending = sum(row["status"] == "reserved" for row in rows)
        return {"scope": "this attempt's reservation IDs only", "dispatches": len(ids),
                "ledger_row_count": len(rows), "known_usage_calls": len(known),
                "unknown_usage_calls": len(unknown), "pending_reserved_calls": pending,
                "accounting_final": not pending and len(rows) == len(ids),
                "known_input_tokens": sum(row["input_tokens"] or 0 for row in known),
                "known_output_tokens": sum(row["output_tokens"] or 0 for row in known),
                "known_cached_tokens": sum(row["cached_tokens"] or 0 for row in known),
                "known_usage_cost_usd": sum(row["charged"] for row in known),
                "unknown_usage_charge_usd": sum(row["charged"] for row in unknown),
                "charged_usd_including_unknown_reservations": sum(row["charged"] for row in rows),
                "statuses": dict(Counter(row["status"] for row in rows)),
                "note": "Unknown usage retains conservative reservations; not an actual provider fee claim."}


def isolated_parallel_config(config):
    isolated = isolated_config(config)
    return isolated.model_copy(update={
        "agent": isolated.agent.model_copy(update={"multi_agent_planning_enabled": True}),
        "memory": isolated.memory.model_copy(update={"enabled": False, "background_enabled": False}),
        "message_history": isolated.message_history.model_copy(update={
            "enabled": False, "background_enabled": False}),
    })


def source_fingerprints() -> dict[str, str]:
    return {name: hashlib.sha256((PROJECT_ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def analyze_report(report: dict) -> dict:
    answer = str((report.get("result") or {}).get("answer") or "")
    facts = eval_realworld.CASES[CASE_ID]["facts"]
    matched = [fact for fact in facts if fact.casefold() in answer.casefold()]
    return {"case_id": CASE_ID, "label": "actual parallel Agent diagnostic; NOT semantic accuracy",
            "semantic_review": {"status": "pending_root_review"},
            "literal_fact_coverage": {"matched_count": len(matched), "expected_count": len(facts),
                                      "matched": matched, "label": "literal strings only; NOT semantics"},
            "mechanical_pass_reported": report.get("mechanical_pass"), "checks": report.get("checks"),
            "task_wall_seconds": report.get("task_wall_seconds"), "metrics": report.get("metrics"),
            "child_metrics": report.get("child_metrics"), "total_agent_metrics": report.get("total_agent_metrics"),
            "runtime_error": report.get("error"), "parent_run_snapshot": report.get("run_snapshot"),
            "child_runs": report.get("child_runs", []), "child_events": report.get("child_events", {}),
            "run_events": report.get("run_events", [])}


def baseline_comparison(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    raw = path.read_bytes()
    report = json.loads(raw)
    analysis = analyze_report(report)
    return {"path": str(path.resolve()), "raw_report_sha256": hashlib.sha256(raw).hexdigest(),
            "task_wall_seconds": analysis["task_wall_seconds"],
            "total_agent_metrics": analysis["total_agent_metrics"],
            "literal_fact_coverage": analysis["literal_fact_coverage"],
            "mechanical_pass_reported": analysis["mechanical_pass_reported"],
            "semantic_review": {"status": "pending_root_review"}}


async def run_parallel_case(*, ledger: LiveBudget, output: Path = OUTPUT, runner=None,
                            remote: bool = False, root_go: bool = False,
                            baseline: Path | None = BASELINE_REPORT) -> dict:
    if not remote or not root_go:
        raise ValueError("Explicit remote and Root GO are required before any runtime dispatch")
    if ledger.usd_limit > 50:
        raise ValueError("The authoritative shared budget must not exceed USD 50")
    configured = Settings().load_local_config()
    if runner is None and (configured.llm.is_disabled() or configured.llm.model != MODEL):
        raise ValueError("Only configured, priced deepseek-flash is authorized")
    config = isolated_parallel_config(configured)
    before = source_fingerprints()
    case_before = hashlib.sha256(json.dumps(eval_realworld.CASES[CASE_ID], sort_keys=True).encode()).hexdigest()
    tag = "parallel-quality-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8]
    budget = AttemptBudget(ledger, source_id=tag)
    shared_before = ledger.snapshot()
    output.mkdir(parents=True, exist_ok=True)
    attempt_root = output / tag
    # Protect logs throughout execution, not just after report.json is written.
    attempt_root.mkdir(mode=0o700)
    started = time.perf_counter()
    try:
        with patch.object(Settings, "load_local_config", return_value=config):
            report = await (runner or eval_realworld.run_case)(
                CASE_ID, output=attempt_root, budget=budget, planning=True, timeout=240,
                protocol="configured", mail_expert=False, full_retrieval=False, response_mode="text",
            )
    except Exception as exc:  # noqa: BLE001 - private failure artifact, never retry
        report = {"error": {"type": type(exc).__name__, "message": str(exc)}}
    analysis = analyze_report(report)
    after = source_fingerprints()
    analysis.update({"source_fingerprints_before": before, "source_fingerprints_after": after,
                     "source_files_changed_during_attempt": before != after,
                     "case_changed_during_attempt": case_before != hashlib.sha256(
                         json.dumps(eval_realworld.CASES[CASE_ID], sort_keys=True).encode()).hexdigest(),
                     "attempt_wall_seconds_including_preparation": time.perf_counter() - started,
                     "run_budget": budget.snapshot()["run_budget"], "attempt_usage": budget.usage(),
                     "shared_budget_before": shared_before, "shared_budget_after": ledger.snapshot(),
                     "comparison_baseline": baseline_comparison(baseline),
                     "raw_report_available": False, "raw_report_sha256": None})
    root = attempt_root
    if report.get("private_artifacts"):
        root = Path(report["private_artifacts"]).resolve()
        if root.parent != attempt_root.resolve() or not root.name.startswith(CASE_ID + "_"):
            raise ValueError("Raw report path escaped the private attempt directory")
        root.chmod(0o700)
        raw = root / "report.json"
        if raw.is_symlink():
            raise ValueError("Raw report must not be a symlink")
        raw.chmod(0o600)
        analysis["raw_report_available"] = True
        analysis["raw_report_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    analysis["private_artifacts"] = str(root.resolve())
    path = root / "parallel_analysis.json"
    analysis["derived_report_path"] = str(path.resolve())
    path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    # External digest of the saved bytes; embedding it in that file would self-reference.
    analysis["derived_report_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return analysis


def stdout_summary(analysis: dict) -> dict:
    # Never serialize answers, prompts, error messages, child artifacts or trace metadata.
    summary = {key: analysis.get(key) for key in (
        "case_id", "task_wall_seconds", "attempt_wall_seconds_including_preparation",
        "attempt_usage", "semantic_review",
        "source_files_changed_during_attempt", "case_changed_during_attempt",
        "raw_report_available", "raw_report_sha256", "private_artifacts",
        "derived_report_path", "derived_report_sha256",
    )}
    numeric_metrics = {"first_agent_progress_seconds", "first_final_token_seconds", "llm_calls",
                       "llm_total_duration_ms", "tool_calls", "failed_tools", "nonzero_command_exits",
                       "input_tokens", "output_tokens"}
    for key in ("metrics", "child_metrics", "total_agent_metrics"):
        summary[key] = {name: value for name, value in (analysis.get(key) or {}).items()
                        if name in numeric_metrics and (value is None or type(value) in {int, float})}
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--root-go", action="store_true", help="Root authorized this single attempt")
    args = parser.parse_args(argv)
    if not args.remote or not args.root_go:
        parser.error("both --remote and --root-go are required; no implicit paid calls")
    if not LEDGER.is_file():
        parser.error("the existing authoritative shared ledger is required; no independent ledger creation")
    analysis = asyncio.run(run_parallel_case(ledger=LiveBudget(LEDGER, usd_limit=50),
                                            remote=True, root_go=True))
    print(json.dumps(stdout_summary(analysis), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
