"""Private synthetic Runtime/Graph + real worker remote-memory/compaction probe.

Offline injection is test-only. CLI requires --remote --root-go, never retries
the probe, and uses the existing shared ledger for every provider attempt.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

from app.core.agent_turn import _turn_run_id
from app.core.config import Settings
from app.core.context_driver import ToolView
from app.core.llm_workloads import current_workload
from app.core.memory_extraction import extract_user_memories, memory_candidate_rejection_reasons
from app.core.multi_agent import SideEffectLevel
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.tools import effective_tool_read_only
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded, instrument_service
from scripts.eval_runtime_memory_quality import (
    LEDGER,
    MODEL,
    OUTPUT,
    RecordedClient,
    RunBudget,
    isolated_config,
)

CALL_LIMIT = 24
PROBE_SECONDS = 120
SOURCE_FILES = (
    "scripts/eval_runtime_background_quality.py", "scripts/eval_runtime_memory_quality.py",
    "evals/lka_evals/live_budget.py", "app/core/runtime.py", "app/core/agent_turn.py",
    "app/core/agent_graph.py", "app/core/sessions.py", "app/core/memory_background.py",
    "app/core/memory_extraction.py", "app/core/memory_context.py", "app/core/memory_files.py",
    "app/domains/memory.py", "app/core/background_jobs.py", "app/core/background_llm.py",
    "app/core/llm/service.py", "app/core/llm/openai_compatible.py", "app/core/llm_workloads.py",
    "app/core/prompt_budget.py", "app/core/prompt_tokens.py", "app/core/tools.py",
)
PREFERENCE = "我倾向在比较方案时先看到取舍理由再看结论。"
NOTE = "核对码 CHECK-BG-741；当前尚未批准上线。\n"
READ_GOAL = "请读取工作区 workspace_probe.txt，只报告里面的核对码和审批状态。"
HISTORY = "请整理这些模拟比较记录，不推断批准结果。SIM-742评审日期为2026-11-06，尚未批准上线。\n" + "\n".join(
    f"模拟记录{i:02d}：方案甲准备时间较少，方案乙便于检查；此次仅比较取舍，仍未选择执行方案。"
    for i in range(24))


def fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_fingerprints():
    base = Path(__file__).resolve().parents[1]
    return {name: hashlib.sha256((base / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def accounting(rows):
    known = [r for r in rows if r["usage_known"]]
    unknown = [r for r in rows if not r["usage_known"]]
    pending = [r for r in rows if r["status"] == "reserved"]
    totals = {key: sum(r[key] for r in known) for key in ("input_tokens", "output_tokens", "cached_tokens")}
    return {"calls": len(rows), "known_usage_calls": len(known), "unknown_usage_calls": len(unknown),
        "pending_calls": len(pending), "accounting_final": not unknown and not pending,
        "settlement_complete": not pending,
        "known_usage_charged_usd": sum(r["charged"] for r in known),
        "unknown_usage_reserved_usd": sum(r["reserved"] for r in unknown if r["status"] != "reserved"),
        "pending_reserved_usd": sum(r["reserved"] for r in pending),
        "conservative_charged_or_reserved_usd": sum(r["charged"] for r in rows),
        **{key: value if not unknown else None for key, value in totals.items()},
        "known_usage_tokens": totals}


def support_reasons(record, origin, sources, messages):
    reasons = []
    if origin != "configured_llm_v1":
        reasons.append("not_remote_origin")
    expected = {m["message_id"]: m for m in messages}
    if len(expected) != 2 or {s["source_ref"] for s in sources} != set(expected) or len(sources) != 2:
        reasons.append("independent_source_refs_mismatch")
    item = {"claim": record.content, "evidence": record.metadata.get("evidence"),
            "kind": record.memory_type, "confidence": record.confidence}
    for source in sources:
        message = expected.get(source["source_ref"])
        if not message or source["status"] != "active" or source["checksum"] != message["sha256"]:
            reasons.append("source_status_or_checksum_mismatch")
    for message in messages:
        reasons.extend(memory_candidate_rejection_reasons(item, message["content"]))
    return list(dict.fromkeys(reasons))


class PoolBudget(RunBudget):
    def __init__(self, ledger, tag):
        super().__init__(ledger, tag, limit=CALL_LIMIT)
        self.pools = {}
        self.closed = False

    def reserve(self, **kwargs):
        with self.lock:
            if self.closed or kwargs["kind"] != "llm" or len(self.ids) >= self.limit:
                raise LiveBudgetExceeded("background probe closed or call allowance exhausted")
            identity = self.ledger.reserve(**{**kwargs, "stage": self.tag + ":" + kwargs["stage"]})
            self.ids.append(identity)
            self.pools[identity] = current_workload().pool
        return identity

    def check_open(self):
        with self.lock:
            if self.closed:
                raise LiveBudgetExceeded("background probe dispatch is closed")

    def close(self):
        with self.lock:
            self.closed = True


class DispatchObserver:
    """Inside RecordedClient/MeteredClient; observe actual provider invocation."""

    def __init__(self, client, runtime, dispatches, entered, lock, inflight, budget=None):
        self.client, self.runtime, self.dispatches = client, runtime, dispatches
        self.entered, self.lock = entered, lock
        self.inflight = inflight
        self.budget = budget

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def complete(self, request):
        if self.budget is not None:
            self.budget.check_open()
        start = time.monotonic()
        run_id = _turn_run_id.get()
        audit = self.runtime.agent_run_manager.list_events(run_id) if run_id else []
        last = next((e for e in reversed(audit) if e.type == "llm_started"), None)
        now = datetime.now(UTC)
        row = {"pool": current_workload().pool, "task_id": current_workload().task_id,
               "run_id": run_id, "stage": request.prompt_summary,
               "client_name": request.client_name, "model": request.model,
               "provider": self.client.provider_name,
               "request_sha256": fingerprint(request.model_dump_json()),
               "started_at": now.isoformat(), "started_monotonic": start,
               "dispatch_wait_from_audit_ms": max(0, (now - datetime.fromisoformat(last.created_at)).total_seconds() * 1000)
               if last else None}
        compact = request.prompt_summary == "background_context_compact"
        with self.lock:
            self.dispatches.append(row)
            if compact:
                self.inflight["compaction"] += 1
            row["compaction_inflight_at_dispatch"] = self.inflight["compaction"] > 0
        async def dispatch():
            if self.budget is not None:
                self.budget.check_open()  # Also fence a reservation queued before close().
            return await self.client.complete(request)

        task = asyncio.create_task(dispatch())
        try:
            await asyncio.sleep(0)  # Underlying provider has been invoked, not merely enqueued.
            if compact and not task.done():
                self.entered.set()
            result = await task
            row["finish_reason"] = result.finish_reason
            return result
        except BaseException as exc:
            row["error_type"] = type(exc).__name__
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            row["seconds"] = time.monotonic() - start
            row["returned_monotonic"] = time.monotonic()
            if compact:
                with self.lock:
                    self.inflight["compaction"] -= 1


async def wait_for(predicate, seconds=PROBE_SECONDS):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


async def run_probe(root, ledger, *, config=None, injected_service=None):
    source_before = source_fingerprints()
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "workspace_probe.txt").write_text(NOTE, encoding="utf-8")
    settings = Settings(LKA_DATA_DIR=root / "data", LKA_WORKSPACE_ROOTS=str(workspace))
    config = isolated_config(config or settings.load_local_config())
    config = config.model_copy(update={"memory": config.memory.model_copy(update={
        "allow_remote_extraction": True, "background_worker_count": 1})})
    if injected_service is not None:
        config = config.model_copy(update={"llm": injected_service.config})
    if injected_service is None and config.llm.is_disabled():
        raise ValueError("configured real provider required")
    injection = patch("app.core.runtime.build_text_llm_client", return_value=injected_service) if injected_service else nullcontext()
    with patch.object(Settings, "load_local_config", return_value=config), injection:
        runtime = LocalKnowledgeAgentRuntime(settings)
    if injected_service is not None:
        runtime.agent_llm_client = runtime.agent_turn_loop.llm_client = injected_service
        runtime.memory_background.llm_client = injected_service
        injected_service.workloads = runtime.llm_workloads
    service = runtime.agent_llm_client
    service.background_timeout_seconds = 30
    budget = PoolBudget(ledger, root.name)
    records, dispatches, waits, lock, entered = [], [], [], threading.Lock(), threading.Event()
    inflight = {"compaction": 0}
    admit = runtime.llm_workloads.admit

    @asynccontextmanager
    async def observed_admit(**kwargs):
        start = time.monotonic()
        row = {"pool": current_workload().pool, "task_id": current_workload().task_id,
               "run_id": _turn_run_id.get(), "requested_tokens": kwargs}
        with lock:
            waits.append(row)
        try:
            async with admit(**kwargs) as ticket:
                row["admission_wait_ms"] = (time.monotonic() - start) * 1000
                yield ticket
        except BaseException as exc:
            row["error_type"] = type(exc).__name__
            raise

    runtime.llm_workloads.admit = observed_admit
    for provider in service.registry.list_clients():
        observed = DispatchObserver(provider, runtime, dispatches, entered, lock, inflight, budget)
        service.registry.register_client(RecordedClient(observed, records, lock))
    instrument_service(service, budget, allowed_model=MODEL)
    worker = runtime.memory_background.worker
    worker.poll_seconds = .02
    view = ToolView(snapshot_id="isolated-background-read", allowed_packages=("filesystem", "memory", "observation"),
                    allowed_paths=(str(workspace),), side_effect_level=SideEffectLevel.READ,
                    full_data_authority=True)
    execute = runtime.tool_executor.execute

    def scoped_execute(**kwargs):
        kwargs["context"] = kwargs["context"].model_copy(update={"tool_view": view})
        return execute(**kwargs)

    runtime.tool_executor.execute = scoped_execute
    report = {"mode": "scripted" if injected_service else "remote", "call_limit": CALL_LIMIT,
              "search_limit": 0, "provider_seconds": 30, "per_probe_seconds": PROBE_SECONDS,
              "semantic_review": "required_independent_not_scored", "private_artifacts": str(root),
              "fixture_fingerprints": {"preference": fingerprint(PREFERENCE), "note": fingerprint(NOTE),
                                       "history": fingerprint(HISTORY)},
              "fixture_controls": {"compaction_context_budget": 512, "worker_count": 1,
                                   "remote_extraction_disabled_during_compaction_phase": True,
                                   "provider_delay": "none in remote mode"},
              "turns": [], "scenarios": {}, "checks": {}, "confounded": False}
    attempted, source_messages = [], []

    def context_state(identity):
        with sqlite3.connect(runtime.db_path) as conn:
            row = conn.execute("SELECT revision,covered_seq,summary_revision FROM agent_session_context_state WHERE session_id=?",
                               (identity,)).fetchone()
        return dict(zip(("revision", "covered_seq", "summary_revision"), row, strict=True)) if row else {}

    def session(title):
        identity = runtime.create_session(title=title).session.session_id
        runtime.set_session_workspace(session_id=identity, path=str(workspace), platform="linux")
        return identity

    async def turn(identity, text):
        run = runtime.create_agent_run(session_id=identity, user_input=text)
        attempted.append(run.run_id)
        start = time.monotonic()
        try:
            result = await runtime.run_agent_turn_async(session_id=identity, user_input=text,
                                                         existing_run_id=run.run_id)
        except BaseException:
            runtime.agent_run_manager.cancel_run(run.run_id, reason="isolated background probe interrupted")
            raise
        row = {"seconds": time.monotonic() - start, "result": result.model_dump(mode="json")}
        report["turns"].append(row)
        writes = [e.tool_name for e in result.tool_events
                  if e.result.get("execution_started") is True
                  and effective_tool_read_only(runtime.tool_registry.get_tool(e.tool_name), e.input) is not True]
        if writes:
            report["confounded"] = True
            raise RuntimeError("foreground writes confound background publication")
        if runtime.agent_run_manager.get_run(run.run_id).status.value != "completed" or not result.answer.strip():
            raise RuntimeError("foreground did not complete")
        return result

    async def drain(identity, kind):
        def finished():
            jobs = runtime.background_job_store.list(scope_id=identity, kind=kind)
            if any(j["status"] in {"failed", "cancelled"} for j in jobs):
                raise RuntimeError("background job failed")
            return jobs and all(j["status"] == "succeeded" for j in jobs)
        await wait_for(finished)

    started = time.monotonic()
    runtime.memory_background.start()  # No runtime.start: mail/watch/server remain off.
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            tick = time.monotonic()
            if extract_user_memories(source_id="fixture", content=PREFERENCE):
                raise ValueError("fixture has local candidates; remote path would be confounded")
            for index in range(2):
                identity = session(f"remote-preference-{index}")
                result = await turn(identity, PREFERENCE)
                user = next(m for m in runtime.session_service.get_session(session_id=identity).messages
                            if m.role == "user" and m.payload.get("trace_id") == result.trace_id)
                source_messages.append({"message_id": user.message_id, "content": user.content,
                                        "sha256": fingerprint(user.content), "session_id": identity})
                await drain(identity, "memory_extract")
            active = runtime.memory_service.list(scope="global", statuses=("active",))
            entries = runtime.memory_service.list(scope="global", statuses=("active", "candidate"))
            with sqlite3.connect(runtime.db_path) as conn:
                origins = dict(conn.execute("SELECT memory_id,extraction_model FROM memory_entries"))
            entry_reasons = {m.memory_id: support_reasons(m, origins.get(m.memory_id),
                runtime.memory_service.sources_for(m.memory_id), source_messages) for m in entries}
            supported = [m for m in active if not entry_reasons[m.memory_id]]
            report["checks"].update(remote_extraction_dispatched=sum(
                d["stage"] == "background_memory_extract" for d in dispatches) >= 2,
                remote_nonempty_supported_active=bool(supported),
                independent_sources=len({x["message_id"] for x in source_messages}) == 2)
            report["scenarios"]["extraction"] = {"seconds": time.monotonic() - tick,
                "sources": source_messages, "records": [dict(m.model_dump(mode="json"),
                    extraction_model=origins.get(m.memory_id),
                    mechanical_support_reasons=entry_reasons[m.memory_id],
                    sources=runtime.memory_service.sources_for(m.memory_id)) for m in entries]}
        async with asyncio.timeout(PROBE_SECONDS):
            tick = time.monotonic()
            # This phase measures remote compaction, not additional extraction calls.
            runtime.memory_background.allow_remote_extraction = False
            baseline = await turn(session("read-baseline"), READ_GOAL)
            baseline_row = report["turns"][-1]
            runtime.session_service.default_context_token_budget = 512  # Fixture only; model caps unchanged.
            runtime.agent_turn_loop.session_context_token_budget = 512
            history_session = session("worker-compaction")
            await turn(history_session, HISTORY)
            before = runtime.session_service.get_context_window(session_id=history_session).model_dump(mode="json")
            state_before = context_state(history_session)
            entered.clear()
            await turn(history_session, HISTORY + "\n补充：没有新的批准决定。")
            await wait_for(entered.is_set)
            health_before = runtime.llm_workloads.health()
            ticks = []

            async def heartbeat():
                while True:
                    ticks.append(time.monotonic())
                    await asyncio.sleep(.01)

            ticker = asyncio.create_task(heartbeat())
            try:
                foreground = await turn(session("read-overlap"), READ_GOAL)
                foreground_row = report["turns"][-1]
            finally:
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)
            await drain(history_session, "context_compact")
            after = runtime.session_service.get_context_window(session_id=history_session).model_dump(mode="json")
            foreground_run = foreground.run_id
            overlap = [d for d in dispatches if d["run_id"] == foreground_run
                       and d["compaction_inflight_at_dispatch"]]
            read_tools = lambda r: [e.tool_name for e in r.tool_events if e.result.get("status") == "completed"
                                   and effective_tool_read_only(runtime.tool_registry.get_tool(e.tool_name), e.input) is True]
            tool_overlap = [e.tool_name for e in foreground.tool_events
                if e.tool_name in read_tools(foreground) and any(
                    datetime.fromisoformat(d["started_at"]) <= datetime.fromisoformat(e.selected_at)
                    and datetime.fromisoformat(e.completed_at).timestamp() <=
                    datetime.fromisoformat(d["started_at"]).timestamp() + d["seconds"]
                    for d in dispatches if d["stage"] == "background_context_compact")]
            state_after = context_state(history_session)
            report["checks"].update(provider_overlap_observed=bool(overlap),
                readonly_tool_during_provider=bool(tool_overlap),
                foreground_read_tool=bool(read_tools(foreground)), baseline_read_tool=bool(read_tools(baseline)),
                worker_model_summary=after["summary_metadata"].get("method") == "model",
                published_summary_revision=state_after["summary_revision"] > state_before["summary_revision"],
                worker_published_watermark=after["summary_metadata"].get("covered_seq", 0) > before["summary_metadata"].get("covered_seq", 0),
                fixture_file_unchanged=fingerprint((workspace / "workspace_probe.txt").read_text()) == fingerprint(NOTE))
            report["scenarios"]["compaction"] = {"seconds": time.monotonic() - tick,
                "baseline_seconds": baseline_row["seconds"], "foreground_seconds": foreground_row["seconds"],
                "foreground_minus_baseline_seconds": foreground_row["seconds"] - baseline_row["seconds"],
                "heartbeat_max_gap_ms": max((b-a for a, b in pairwise(ticks)), default=0) * 1000,
                "heartbeat_ticks": len(ticks), "health_before_foreground": health_before,
                "state_before": state_before, "state_after": state_after,
                "readonly_tools_during_provider": tool_overlap,
                "before": before, "after": after, "overlap_provider_calls": len(overlap),
                "interpretation": "observed overlap" if overlap else "inconclusive: no actual overlap"}
    except Exception as exc:  # noqa: BLE001 - preserve private failure trace; never retry the probe.
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        budget.close()  # Refuse new reservations/late dispatch before stopping the actual worker.
        runtime.stop()
        report["seconds"] = time.monotonic() - started
        report["provider_records"], report["dispatches"] = records, dispatches
        report["admission_waits"] = waits
        report["resolved_profiles"] = [{"client_name": name, "model": model,
            "context_window_tokens": profile.context_window_tokens if profile else None,
            "output_reserve_tokens": profile.output_reserve_tokens if profile else None,
            "tokenizer_json_path": str(profile.tokenizer_json_path) if profile and profile.tokenizer_json_path else None}
            for name, model in sorted({(d["client_name"], d["model"]) for d in dispatches})
            for profile in [service.config.resolve_model_config(name, model)]]
        report["calls"] = len(budget.ids)
        report["mechanical_checks_pass"] = bool(report["checks"]) and all(report["checks"].values()) and not report.get("error") and not report["confounded"]
        report["jobs"] = runtime.background_job_store.list(limit=100)
        report["health_final"] = runtime.llm_workloads.health()
        report["runs"] = [{"run": runtime.agent_run_manager.get_run(i).model_dump(mode="json"),
                           "events": [e.model_dump(mode="json") for e in runtime.agent_run_manager.list_events(i)]} for i in attempted]
        with ledger.connect() as conn:
            columns = ("id", "stage", "status", "reserved", "charged", "usage_known", "input_tokens", "output_tokens", "cached_tokens")
            report["ledger_calls"] = [dict(zip(columns, row, strict=True),
                pool=budget.pools[i]) for i in budget.ids for row in conn.execute(
                "SELECT " + ",".join(columns) + " FROM calls WHERE id=?", (i,))]
        for row in report["ledger_calls"]:
            row["pending"] = row["status"] == "reserved"
            row["accounting_final"] = bool(row["usage_known"]) and not row["pending"]
        report["budget_closed"] = budget.closed
        report["accounting"] = accounting(report["ledger_calls"])
        report["accounting_final"] = report["accounting"]["accounting_final"]
        report["pool_usage"] = {pool: accounting(rows)
            for pool in {r["pool"] for r in report["ledger_calls"]}
            for rows in [[r for r in report["ledger_calls"] if r["pool"] == pool]]}
        report["extraction_diagnostics"] = []
        valid_identities = []
        for record in records:
            if record["request"]["prompt_summary"] != "background_memory_extract":
                continue
            try:
                candidates = json.loads(record.get("response", {}).get("content", ""))["candidates"]
                batch = []
                for c in candidates:
                    reasons = memory_candidate_rejection_reasons(c, PREFERENCE)
                    normalized = c["claim"].strip().casefold() if not reasons else None
                    identity = fingerprint(f"global|None|{normalized}") if normalized else None
                    batch.append({"candidate": c, "validation_reasons": reasons,
                                  "normalized_claim": normalized, "publication_identity": identity})
                    if identity:
                        valid_identities.append(identity)
                report["extraction_diagnostics"].append(batch)
            except (ValueError, KeyError, TypeError):
                report["extraction_diagnostics"].append({"invalid_model_json": True})
        extraction = report["scenarios"].get("extraction")
        if extraction is not None:
            extraction["identity_diagnostics"] = {"valid_candidate_count": len(valid_identities),
                "same_publication_identity": len(valid_identities) >= 2 and len(set(valid_identities)) == 1,
                "unique_publication_identities": sorted(set(valid_identities)),
                "active_count": sum(m["status"] == "active" for m in extraction["records"]),
                "observed_not_promoted_reason": "no_valid_candidates" if not valid_identities else
                    "different_candidate_identities" if len(set(valid_identities)) > 1 else
                    "no_active_record_observed; inspect records/jobs" if not any(m["status"] == "active" for m in extraction["records"]) else None,
                "semantic_review": "Root manual pending"}
        source_after = source_fingerprints()
        report["source_fingerprints_before"] = source_before
        report["source_fingerprints_after"] = source_after
        report["source_changed_files"] = [name for name in source_before if source_before[name] != source_after[name]]
        report["source_changed"] = bool(report["source_changed_files"])
        with sqlite3.connect(runtime.db_path) as conn:
            report["context_state"] = [dict(zip(("session_id", "revision", "covered_seq", "summary_revision"), r, strict=True))
                for r in conn.execute("SELECT session_id,revision,covered_seq,summary_revision FROM agent_session_context_state")]
        raw = json.dumps(report, ensure_ascii=False, indent=2)
        (root / "report.json").write_text(raw, encoding="utf-8")
        (root / "report.json").chmod(0o600)
        (root / "report.sha256").write_text(fingerprint(raw) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--root-go", action="store_true")
    args = parser.parse_args(argv)
    if not args.remote or not args.root_go or not LEDGER.is_file():
        parser.error("requires --remote --root-go and existing shared ledger; no implicit paid run")
    root = OUTPUT / ("runtime_background_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"))
    report = asyncio.run(run_probe(root, LiveBudget(LEDGER, usd_limit=50, search_limit=0)))
    print(json.dumps({k: report[k] for k in ("mode", "seconds", "calls", "checks", "mechanical_checks_pass",
        "pool_usage", "private_artifacts", "semantic_review")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
