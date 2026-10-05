"""Explicitly authorized, seeded 4096-medium worker/follow-up probe (not 65536 coverage).

No semantic pass: private actual provider requests/answers require independent review.
Only this isolated runtime gets the context override; production triggers stay intact.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.context_driver import ToolView
from app.core.llm import LLMRequest
from app.core.multi_agent import SideEffectLevel
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.tools import effective_tool_read_only
from evals.lka_evals.live_budget import LiveBudget, instrument_service
from scripts.eval_runtime_background_quality import (
    SOURCE_FILES,
    DispatchObserver,
    PoolBudget,
    accounting,
    fingerprint,
    wait_for,
)
from scripts.eval_runtime_memory_quality import (
    LEDGER,
    MODEL,
    OUTPUT,
    RecordedClient,
    isolated_config,
)

CALL_LIMIT, PROBE_SECONDS, CONTEXT_WINDOW = 32, 180, 4096
OLD_FACTS = "模拟项目 SIM-FOLLOWUP-903：评审日期为2026-11-06；尚未批准上线；未完成事项是安全复核。"
CORRECTION = "更正：SIM-FOLLOWUP-903评审日期改为2026-11-09；仍未批准上线；安全复核仍未完成。"
RECALL = "请仅依据本会话，简短报告 SIM-FOLLOWUP-903 的日期、审批状态和未完成事项；缺失信息请明说。"


class FollowupBudget(PoolBudget):
    def __init__(self, ledger, tag):
        super().__init__(ledger, tag)
        self.limit = CALL_LIMIT


def source_fingerprints():
    base = Path(__file__).resolve().parents[1]
    return {name: hashlib.sha256((base / name).read_bytes()).hexdigest()
            for name in (*SOURCE_FILES, "scripts/eval_runtime_background_followup_quality.py")}


def private_json(path, value):
    # Fresh exclusive files beneath the freshly created private probe directory.
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        stream.write(json.dumps(value, ensure_ascii=False, indent=2))


def summary_delivery(records, published_summary):
    """Compare the committed version with decoded actual answer payloads, not facts."""
    delivered = []
    for record in records:
        for message in record["request"]["messages"]:
            if message["role"] != "user":
                continue
            try:
                payload = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                window = payload.get("session_context_window")
                if isinstance(window, dict) and isinstance(window.get("summary"), str):
                    delivered.append(window["summary"])
    status = "complete" if published_summary and published_summary in delivered else (
        "partial_or_unknown" if delivered else "unobserved")
    return {"status": status, "published_sha256": fingerprint(published_summary),
            "delivered_sha256": [fingerprint(s) for s in delivered],
            "delivered_chars": [len(s) for s in delivered],
            "meaning": "delivery identity only; factual retention requires manual semantic review"}


async def run_probe(root, ledger, *, config=None, injected_service=None):
    source_before = source_fingerprints()
    root = Path(root).absolute()
    if root.is_symlink() or root.exists() or root.parent.resolve() != root.parent:
        raise ValueError("fresh probe root under a non-symlink parent required")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    settings = Settings(LKA_DATA_DIR=root / "data", LKA_WORKSPACE_ROOTS=str(workspace))
    config = isolated_config(config or settings.load_local_config())
    if injected_service is not None:
        config = config.model_copy(update={"llm": injected_service.config})
    if injected_service is None and (config.llm.is_disabled() or config.llm.model != MODEL):
        raise ValueError("configured priced provider required")
    injection = patch("app.core.runtime.build_text_llm_client", return_value=injected_service) if injected_service else nullcontext()
    with patch.object(Settings, "load_local_config", return_value=config), injection:
        runtime = LocalKnowledgeAgentRuntime(settings)
    service = runtime.agent_llm_client
    if injected_service is not None:
        runtime.agent_turn_loop.llm_client = runtime.memory_background.llm_client = service = injected_service
        injected_service.workloads = runtime.llm_workloads
    service.background_timeout_seconds = 30
    budget = FollowupBudget(ledger, root.name)
    records, dispatches, publications, lock = [], [], [], threading.Lock()
    for provider in service.registry.list_clients():
        observed = DispatchObserver(provider, runtime, dispatches, threading.Event(), lock,
                                    {"compaction": 0}, budget)
        service.registry.register_client(RecordedClient(observed, records, lock))
    # Actual boundary order: Metered (protected .env guard) -> Recorded -> provider.
    instrument_service(service, budget, allowed_model=MODEL)
    production_window = runtime.session_service.default_context_token_budget
    runtime.session_service.default_context_token_budget = CONTEXT_WINDOW
    runtime.agent_turn_loop.session_context_token_budget = CONTEXT_WINDOW
    runtime.memory_background.worker.poll_seconds = .02
    session_id = runtime.create_session(title="seeded-medium-followup").session.session_id
    runtime.set_session_workspace(session_id=session_id, path=str(workspace), platform="linux")
    view = ToolView(snapshot_id="followup-read-only", allowed_packages=("filesystem", "observation", "memory"),
                    allowed_paths=(str(workspace),), side_effect_level=SideEffectLevel.READ, full_data_authority=True)
    execute = runtime.tool_executor.execute

    def readonly_execute(**kwargs):
        kwargs["context"] = kwargs["context"].model_copy(update={"tool_view": view})
        return execute(**kwargs)

    runtime.tool_executor.execute = readonly_execute
    publish = runtime.session_service.publish_context_summary

    def observed_publish(**kwargs):
        committed = publish(**kwargs)
        if committed:
            window = runtime.session_service.get_context_window(session_id=session_id)
            with lock:
                publications.append({"covered_seq": kwargs["target_seq"], "summary": window.summary,
                                     "summary_metadata": window.summary_metadata})
        return committed

    runtime.session_service.publish_context_summary = observed_publish
    report = {"mode": "scripted" if injected_service else "remote", "seeded_history": True,
        "seed_label": "synthetic append_message/record_context_exchange; not full user execution",
        "context_window": CONTEXT_WINDOW, "production_context_window": production_window,
        "window_coverage": "medium_override_only_not_production_65536",
        "call_limit": CALL_LIMIT, "search_limit": 0, "provider_seconds": 30, "probe_seconds": PROBE_SECONDS,
        "feasibility": {"production_trigger_tokens": int(production_window * .70),
            "session_fallback_chars_per_token": 4,
            "worker_prefix_byte_limit": max(1024, runtime.memory_background.max_job_tokens // 3),
            "worker_chunk_byte_target": 8000,
            "production_warning": "~183500 ASCII chars at threshold; whole-exchange prefixes plus chunking can exceed 32 calls/180s. Not tested as production SLA.",
            "medium_seed_plan": "3 x ~4100 chars plus small raw tail; await two prefixes, then append another such batch for >=3 publications; recovery still shares 32 cap"},
        "semantic_review": "required_independent_not_scored", "private_artifacts": str(root),
        "foreground_scope": "execution-only isolated READ view; not discovery authorization coverage",
        "turns": [], "checks": {}, "confounded": False}
    attempted = []
    started = time.monotonic()

    async def turn(text):
        run = runtime.create_agent_run(session_id=session_id, user_input=text)
        attempted.append(run.run_id)
        turn_started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        try:
            result = await runtime.run_agent_turn_async(session_id=session_id, user_input=text,
                                                         existing_run_id=run.run_id)
        except BaseException:
            runtime.agent_run_manager.cancel_run(run.run_id, reason="followup probe interrupted")
            raise
        report["turns"].append({"session_id": session_id, "result": result.model_dump(mode="json"),
            "started_at": started_at, "completed_at": datetime.now(UTC).isoformat(),
            "seconds": time.monotonic() - turn_started})
        report["confounded"] |= any(e.result.get("execution_started") is True
            and effective_tool_read_only(runtime.tool_registry.get_tool(e.tool_name), e.input) is not True
            for e in result.tool_events)
        if report["confounded"]:
            raise RuntimeError("foreground write confounds publication")
        if runtime.agent_run_manager.get_run(run.run_id).status.value != "completed" or not result.answer.strip():
            raise RuntimeError("foreground incomplete")

    try:
        async with asyncio.timeout(PROBE_SECONDS):
            seeds = []
            # Three large exchanges trigger two prefixes; one small newest pair
            # stays raw without forcing the 4096 prompt into emergency projection.
            # The real threshold/prefix/chunk code determines jobs, never this script.
            for index in range(4):
                text = (OLD_FACTS + "\n" if index == 0 else "") + (f"seeded comparison {index}; no decision. " * 130)[:4100 if index < 3 else 80]
                trace = f"seeded_followup_{index}"
                answer = "模拟记录已收录，没有新的审批结论。"
                for role, content in (("user", text), ("agent", answer)):
                    runtime.session_service.append_message(session_id=session_id, role=role, content=content,
                        payload={"trace_id": trace, "synthetic_seed": True})
                window = runtime.session_service.record_context_exchange(session_id=session_id,
                    user_input=text, agent_answer=answer, trace_id=trace,
                    background_enqueue=runtime.memory_background.enqueue_compaction)
                seeds.append({"trace_id": trace, "user_sha256": fingerprint(text), "chars": len(text),
                              "context_token_estimate": window.token_estimate})
            report["seeds"] = seeds
            report["seeded_target_seq"] = 8
            report["outbox_before_worker"] = runtime.background_job_store.list(scope_id=session_id)
            runtime.memory_background.start()  # no mail/watch/server startup
            await wait_for(lambda: bool(publications) and publications[-1]["covered_seq"] >= 6,
                           seconds=PROBE_SECONDS)
            # Append only after the first prefixes publish. Seeding everything
            # at once would test emergency foreground fallback, not continuous
            # background publication. The real threshold still selects work.
            report["first_prefixes"] = [dict(p) for p in publications]
            for index in range(4, 8):
                text = (f"seeded follow-up comparison {index}; no new approval. " * 100)[:4100 if index < 7 else 80]
                trace = f"seeded_followup_{index}"
                answer = "模拟比较记录已收录，没有新的审批结论。"
                for role, content in (("user", text), ("agent", answer)):
                    runtime.session_service.append_message(session_id=session_id, role=role, content=content,
                        payload={"trace_id": trace, "synthetic_seed": True})
                window = runtime.session_service.record_context_exchange(session_id=session_id,
                    user_input=text, agent_answer=answer, trace_id=trace,
                    background_enqueue=runtime.memory_background.enqueue_compaction)
                seeds.append({"trace_id": trace, "user_sha256": fingerprint(text), "chars": len(text),
                              "context_token_estimate": window.token_estimate})
            report["seeded_target_seq"] = 16
            await wait_for(lambda: bool(publications) and publications[-1]["covered_seq"] >= 14,
                           seconds=PROBE_SECONDS)
            report["published_before_followup"] = runtime.session_service.get_context_window(
                session_id=session_id).model_dump(mode="json")
            await turn(RECALL)
            await turn(CORRECTION + "\n" + RECALL)
    except Exception as exc:  # noqa: BLE001 - retain failure, never same-level retry.
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        budget.close()  # Before worker shutdown: queued reservations/dispatch cannot escape.
        runtime.stop()
        report.update(seconds=time.monotonic() - started, calls=len(budget.ids), budget_closed=budget.closed,
                      provider_records=records, dispatches=dispatches, publications=publications)
        report["jobs"] = runtime.background_job_store.list(scope_id=session_id, limit=100)
        report["health_final"] = runtime.llm_workloads.health()
        report["runs"] = [{"run": runtime.agent_run_manager.get_run(i).model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in runtime.agent_run_manager.list_events(i)]} for i in attempted]
        with ledger.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for identity in budget.ids for r in conn.execute("SELECT * FROM calls WHERE id=?", (identity,))]
        report["ledger_calls"] = rows
        report["accounting"] = accounting(rows)
        for turn_index, label in enumerate(("recall", "correction")):
            run_id = attempted[turn_index] if turn_index < len(attempted) else None
            hashes = {d["request_sha256"] for d in dispatches if d["run_id"] == run_id and run_id}
            actual = [r for r in records if fingerprint(LLMRequest.model_validate(r["request"]).model_dump_json()) in hashes]
            answer_requests = [r for r in actual if r["request"]["metadata"].get("stage") in {"answer", "context_answer"}]
            report[label + "_answer_requests"] = answer_requests
            report["checks"][label + "_provider_answer_observed"] = bool(answer_requests)
        published = report.get("published_before_followup", {})
        report["recall_summary_delivery"] = summary_delivery(report["recall_answer_requests"], published.get("summary", ""))
        correction_inputs = []
        for record in report["correction_answer_requests"]:
            for message in record["request"]["messages"]:
                if message["role"] == "user":
                    try:
                        payload = json.loads(message["content"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(payload, dict) and isinstance(payload.get("user_input"), str):
                        correction_inputs.append(payload["user_input"])
        report["checks"].update(multiple_prefix_watermarks=len({p["covered_seq"] for p in publications}) >= 3,
            model_publications=bool(publications) and all(p["summary_metadata"].get("method") == "model" for p in publications),
            same_session_two_turns=len(report["turns"]) == 2,
            recall_summary_delivered=report["recall_summary_delivery"]["status"] == "complete",
            old_fact_source_outside_raw_tail=bool(published) and all(OLD_FACTS not in m["content"] for m in published.get("recent_messages", [])),
            tail_correction_delivered=any(CORRECTION in text for text in correction_inputs),
            foreground_no_writes=not report["confounded"])
        report["mechanical_checks_pass"] = all(report["checks"].values()) and not report.get("error")
        report["source_fingerprints_before"] = source_before
        report["source_fingerprints_after"] = source_fingerprints()
        report["source_changed"] = source_before != report["source_fingerprints_after"]
        report["fixture_fingerprints"] = {"old_facts": fingerprint(OLD_FACTS),
            "correction": fingerprint(CORRECTION), "recall": fingerprint(RECALL)}
        private_json(root / "report.json", report)
        digest = hashlib.sha256((root / "report.json").read_bytes()).hexdigest()
        with (root / "report.sha256").open("x", encoding="utf-8") as stream:
            (root / "report.sha256").chmod(0o600)
            stream.write(digest + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--root-go", action="store_true")
    args = parser.parse_args(argv)
    if not args.remote or not args.root_go or not LEDGER.is_file():
        parser.error("requires --remote --root-go and existing shared ledger")
    root = OUTPUT / ("runtime_background_followup_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"))
    report = asyncio.run(run_probe(root, LiveBudget(LEDGER, usd_limit=50, search_limit=0)))
    print(json.dumps({k: report[k] for k in ("calls", "checks", "mechanical_checks_pass", "private_artifacts")}), flush=True)


if __name__ == "__main__":
    main()
