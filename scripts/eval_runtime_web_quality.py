"""One explicitly approved existing web goal; mechanical evidence, not semantic scoring.

Run from the repository root: python -m scripts.eval_runtime_web_quality --remote --root-go
Uses only the existing shared ledger. No automatic retry or configuration writes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded
from scripts import eval_realworld
from scripts.eval_runtime_memory_quality import (
    LEDGER,
    MODEL,
    OUTPUT,
    RecordedClient,
    RunBudget,
    isolated_config,
)

CASE = "heldout_web_sqlite"
APPROVED_CASES = (CASE, "heldout_web_multisource", "web_search_release")
MAX_CALLS, MAX_SEARCHES, TIMEOUT = 20, 3, 180
SOURCES = (__file__, eval_realworld.__file__,
    "scripts/eval_runtime_memory_quality.py", "evals/lka_evals/live_budget.py",
    "app/core/agent_turn.py", "app/core/tools.py", "app/core/llm/service.py",
    "app/core/tool_result_gate.py", "app/tool_packages/observation.py",
    "app/integrations/web_search.py", "app/tool_packages/web.py")


class WebBudget(RunBudget):
    """All attempts share fixed allowances; refused calls never dispatch."""

    def __init__(self, ledger, tag):
        super().__init__(ledger, tag, limit=MAX_CALLS)
        self.search_ids, self.closed = [], False

    def reserve(self, **kwargs):
        with self.lock:
            kind = kwargs["kind"]
            if kind not in {"llm", "search"}:
                raise ValueError("unsupported web probe reservation")
            ids = self.ids if kind == "llm" else self.search_ids
            limit = self.limit if kind == "llm" else MAX_SEARCHES
            if self.closed or len(ids) >= limit:
                raise LiveBudgetExceeded("single web probe allowance exhausted or closed")
            call_id = self.ledger.reserve(**{**kwargs, "stage": self.tag + ":" + kwargs["stage"]})
            ids.append(call_id)
            return call_id

    def close(self):
        with self.lock:
            self.closed = True

    def snapshot(self):
        return self.ledger.snapshot()


def existing_ledger(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("existing shared ledger required; this probe never creates one")
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.execute("SELECT id,kind,stage,status,charged,usage_known FROM calls LIMIT 0")
    except sqlite3.Error as exc:
        raise ValueError("existing shared ledger has no compatible calls table") from exc
    return LiveBudget(path)


def fingerprints():
    repo = Path(__file__).resolve().parents[1]
    return {str(Path(p).resolve().relative_to(repo)): hashlib.sha256(Path(p).read_bytes()).hexdigest()
            for p in SOURCES}


def raw_report_path(root, report, *, case_id=CASE):
    raw_root = Path(report["private_artifacts"])
    if (raw_root.parent != root or not re.fullmatch(re.escape(case_id) + r"_\d{8}T\d{12}", raw_root.name)
            or raw_root.is_symlink() or raw_root.resolve().parent != root):
        raise ValueError("raw report directory is outside the expected private probe boundary")
    path = raw_root / "report.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("raw report must be a regular non-symlink file")
    return path


async def run_probe(root, ledger, *, config=None, injected_service=None, timeout=TIMEOUT, case_id=CASE):
    """Service/timeout injection is offline-only, never exposed by the CLI."""
    if case_id not in APPROVED_CASES:
        raise ValueError("an approved web case from the existing realworld suite is required")
    root = Path(root).resolve()
    config = isolated_config(config or Settings().load_local_config())
    config = config.model_copy(update={"memory": config.memory.model_copy(
        update={"enabled": False, "background_enabled": False})})
    if injected_service is None and (config.llm.is_disabled() or config.llm.model != MODEL):
        raise ValueError("probe requires configured, priced deepseek-flash")
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    budget, records, lock = WebBudget(ledger, root.name), [], threading.Lock()
    before = fingerprints()
    scenario_before = hashlib.sha256(json.dumps(eval_realworld.CASES[case_id],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    runtime_class = eval_realworld.LocalKnowledgeAgentRuntime

    def create_runtime(settings):
        runtime = runtime_class(settings)
        if injected_service is not None:
            runtime.agent_llm_client = injected_service
            runtime.agent_turn_loop.llm_client = injected_service
            injected_service.workloads = runtime.llm_workloads
        service = runtime.agent_llm_client
        service.background_timeout_seconds = 30
        for provider in service.registry.list_clients():
            service.registry.register_client(RecordedClient(provider, records, lock))
        turn = runtime.run_agent_turn_async

        async def bounded_turn(**kwargs):
            try:
                return await turn(**kwargs)
            finally:
                budget.close()  # Also reject late worker dispatch after timeout cancellation.

        runtime.run_agent_turn_async = bounded_turn
        return runtime

    try:
        with patch.object(Settings, "load_local_config", return_value=config), patch.object(
                eval_realworld, "LocalKnowledgeAgentRuntime", side_effect=create_runtime):
            report = await eval_realworld.run_case(case_id, output=root, budget=budget,
                planning=False, protocol="configured", timeout=timeout)
    finally:
        budget.close()
    with ledger.connect() as conn:
        columns = ("id", "kind", "stage", "status", "charged", "usage_known",
            "input_tokens", "output_tokens", "cached_tokens")
        rows = [dict(zip(columns, row, strict=True)) for call_id in budget.ids + budget.search_ids
                for row in conn.execute(f"SELECT {','.join(columns)} FROM calls WHERE id=?", (call_id,))]
    report_path = raw_report_path(root, report, case_id=case_id)
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    after = fingerprints()
    scenario_after = hashlib.sha256(json.dumps(eval_realworld.CASES[case_id],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    report = {**report, "mode": "scripted" if injected_service else "real_foreground",
        "call_limit": MAX_CALLS, "search_limit": MAX_SEARCHES, "turn_limit_seconds": timeout,
        "provider_limit_seconds": 30, "calls": len(budget.ids), "searches": len(budget.search_ids),
        "ledger_calls": rows, "charged_usd": sum(row["charged"] for row in rows),
        "provider_records": records, "source_sha256_before": before, "source_sha256_after": after,
        "source_unchanged": before == after, "scenario_sha256_before": scenario_before,
        "scenario_sha256_after": scenario_after, "scenario_unchanged": scenario_before == scenario_after,
        "raw_report_path": str(report_path), "raw_report_sha256": digest,
        "semantic_review": {"status": "pending_root_review"}}
    report_path.parent.chmod(0o700)
    report_path.chmod(0o600)
    analysis_path = report_path.with_name("web_analysis.json")
    analysis_path.touch(mode=0o600, exist_ok=False)
    analysis_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    sha_path = report_path.with_suffix(".json.sha256")
    sha_path.touch(mode=0o600, exist_ok=False)
    sha_path.write_text(digest + "\n", encoding="ascii")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--root-go", action="store_true")
    parser.add_argument("--case", choices=APPROVED_CASES, default=CASE)
    args = parser.parse_args(argv)
    if not (args.remote and args.root_go):
        parser.error("both --remote and --root-go are required; no dispatch authorized")
    ledger = existing_ledger(LEDGER)
    stem = "runtime_web_sqlite_" if args.case == CASE else "runtime_" + args.case + "_"
    root = OUTPUT / (stem + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"))
    options = {} if args.case == CASE else {"case_id": args.case}
    report = asyncio.run(run_probe(root, ledger, **options))
    print(json.dumps({key: report[key] for key in
        ("private_artifacts", "calls", "searches", "charged_usd", "semantic_review")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
