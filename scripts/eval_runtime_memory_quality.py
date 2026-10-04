"""Six-turn real foreground/worker probe; private artifacts, shared paid ledger.

The late-job barrier is isolated evaluation control, not production scheduling.
This measures deterministic extraction/publication policy, not free-form extraction.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.runtime import LocalKnowledgeAgentRuntime
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded, instrument_service

OUTPUT = Path("data/quality_runs/linux_20261005")
LEDGER = OUTPUT / "budget.sqlite3"
MODEL = "deepseek-flash"
MAX_CALLS = 20
TURNS = (
    "我喜欢简洁回答。", "我喜欢简洁回答。", "我之前的回答偏好是什么？",
    "我比较喜欢简洁回答。", "我不再喜欢简洁回答，请忘记简洁回答这个偏好。",
    "我之前的回答偏好是什么？",
)


class RunBudget:
    """One locked allowance shared by foreground, background and all attempts."""

    def __init__(self, ledger, tag, limit=MAX_CALLS):
        self.ledger, self.tag, self.limit = ledger, tag, limit
        self.ids = []
        self.lock = threading.Lock()

    def reserve(self, **kwargs):
        with self.lock:
            if kwargs["kind"] != "llm" or len(self.ids) >= self.limit:
                raise LiveBudgetExceeded("isolated memory probe call allowance exhausted")
            call_id = self.ledger.reserve(**{**kwargs, "stage": self.tag + ":" + kwargs["stage"]})
            self.ids.append(call_id)
            return call_id

    def finish(self, *args, **kwargs):
        self.ledger.finish(*args, **kwargs)


class RecordedClient:
    """Inside MeteredClient: secret guard runs before any request is recorded."""

    def __init__(self, client, records, lock):
        self.client, self.records, self.lock = client, records, lock

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def complete(self, request):
        row = {"request": request.model_dump(mode="json")}
        with self.lock:
            self.records.append(row)
        try:
            result = await asyncio.wait_for(self.client.complete(request), 30)
            row["response"] = result.model_dump(mode="json")
            return result
        except BaseException as exc:
            row["error_type"] = type(exc).__name__
            raise

    async def stream(self, request):
        # The probe uses text mode, but any unexpected stream remains bounded.
        async with asyncio.timeout(30):
            async for event in self.client.stream(request):
                yield event


def isolated_config(config):
    llm = config.llm.model_copy(update={"timeout_seconds": 30, "clients": [
        c.model_copy(update={"timeout_seconds": 30}) for c in config.llm.clients]})
    return config.model_copy(update={
        "llm": llm,
        "agent": config.agent.model_copy(update={"orchestrator": "langgraph",
            "checkpoint_backend": "sqlite", "multi_agent_planning_enabled": False,
            "mail_expert_enabled": False, "codex_expert_enabled": False}),
        "mail": config.mail.model_copy(update={
            "outlook": config.mail.outlook.model_copy(update={"enabled": False,
                "startup_sync_enabled": False, "background_sync_enabled": False}),
            "imap": config.mail.imap.model_copy(update={"enabled": False})}),
        "memory": config.memory.model_copy(update={"enabled": True, "background_enabled": True,
            "background_worker_count": 1, "extraction_debounce_seconds": 0,
            "allow_remote_extraction": False}),
        "background": config.background.model_copy(update={"request_timeout_seconds": 30}),
        "message_history": config.message_history.model_copy(update={"enabled": False,
            "background_enabled": False}),
        "embedding": config.embedding.model_copy(update={"enabled": False}),
        "reranker": config.reranker.model_copy(update={"enabled": False}),
    })


async def wait_jobs(runtime, session_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = [j for j in runtime.background_job_store.list(kind="memory_extract")
                if j["scope_id"] == session_id]
        if jobs and all(j["status"] == "succeeded" for j in jobs):
            return
        if any(j["status"] in {"failed", "cancelled"} for j in jobs):
            raise RuntimeError("memory job did not succeed")
        await asyncio.sleep(.02)
    raise TimeoutError("memory jobs did not drain")


def recall_items(result):
    return result.session_context_window.get("recalled_memories", {}).get("items", [])


def source_refs(runtime, record):
    return {source["source_ref"] for source in runtime.memory_service.sources_for(record.memory_id)}


def foreground_confounding_writes(runtime, result):
    # Ordinary preference turns need no foreground writes. Instruction/file
    # writes can also persist the preference and contaminate next-session recall.
    return [event.tool_name for event in result.tool_events
            if runtime.tool_registry.get_tool(event.tool_name).spec.read_only is not True]


async def run_probe(root, ledger, *, config=None, injected_service=None):
    root = Path(root).resolve()
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    settings = Settings(LKA_DATA_DIR=root / "data", LKA_WORKSPACE_ROOTS=str(workspace))
    config = isolated_config(config or settings.load_local_config())
    if injected_service is None and (config.llm.is_disabled() or config.llm.model != MODEL):
        raise ValueError("probe requires configured, priced deepseek-flash")
    with patch.object(Settings, "load_local_config", return_value=config):
        runtime = LocalKnowledgeAgentRuntime(settings)
    if injected_service is not None:  # offline tests only; CLI cannot inject inference
        runtime.agent_llm_client = injected_service
        runtime.agent_turn_loop.llm_client = injected_service
        runtime.memory_background.llm_client = injected_service
        injected_service.workloads = runtime.llm_workloads
    service = runtime.agent_llm_client
    service.background_timeout_seconds = 30
    budget = RunBudget(ledger, root.name)
    records, lock = [], threading.Lock()
    for provider in service.registry.list_clients():
        service.registry.register_client(RecordedClient(provider, records, lock))
    instrument_service(service, budget, allowed_model=MODEL)
    worker = runtime.memory_background.worker
    worker.poll_seconds = .02
    entered, release = threading.Event(), threading.Event()
    late_session = None
    original_handler = worker.handlers["memory_extract"]

    def controlled_handler(job):
        if job["scope_id"] == late_session:
            entered.set()
            if not release.wait(110):
                raise TimeoutError("isolated late-publication barrier expired")
        original_handler(job)

    worker.handlers["memory_extract"] = controlled_handler
    report = {"mode": "scripted" if injected_service else "real_foreground",
        "model": MODEL, "turn_limit_seconds": 90, "provider_limit_seconds": 30,
        "call_limit": budget.limit, "local_extraction_only": True,
        "late_publication_control": "active worker handler paused before extraction, lease unchanged",
        "turns": [], "attempted_run_ids": [], "checks": {}, "confounded": False,
        "semantic_review_required": True, "private_artifacts": str(root)}
    started = time.monotonic()
    runtime.memory_background.start()  # never runtime.start: watch/mail remain off
    try:
        active_id = None
        first_source = None
        for index, text in enumerate(TURNS):
            session_id = runtime.create_session(title=f"memory-quality-{index}").session.session_id
            runtime.set_session_workspace(session_id=session_id, path=str(workspace), platform="linux")
            if index == 3:
                late_session = session_id
            run = runtime.create_agent_run(session_id=session_id, user_input=text)
            report["attempted_run_ids"].append(run.run_id)
            turn_started = time.monotonic()
            try:
                result = await asyncio.wait_for(runtime.run_agent_turn_async(
                    session_id=session_id, user_input=text, existing_run_id=run.run_id), 90)
            except BaseException:
                runtime.agent_run_manager.cancel_run(run.run_id, reason="isolated memory probe interrupted")
                raise
            detail = runtime.session_service.get_session(session_id=session_id)
            user = next(m for m in detail.messages if m.role == "user" and m.payload.get("trace_id") == result.trace_id)
            row = {"source_message_id": user.message_id, "seconds": time.monotonic() - turn_started,
                   "result": result.model_dump(mode="json")}
            report["turns"].append(row)
            writes = foreground_confounding_writes(runtime, result)
            if writes:
                report["confounded"] = True
                row["confounding_write_tools"] = writes
                raise RuntimeError("foreground writes confound worker-policy measurement")
            persisted = runtime.agent_run_manager.get_run(result.run_id)
            if persisted.status.value != "completed" or not result.answer.strip():
                raise RuntimeError("foreground answer was not complete")
            if index == 3:
                deadline = time.monotonic() + 5
                while not entered.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(.02)
                if not entered.is_set():
                    raise TimeoutError("late job never reached isolated barrier")
                report["checks"]["late_source_not_published_before_retraction"] = all(
                    user.message_id not in source_refs(runtime, m)
                    for m in runtime.memory_service.list(scope="global", statuses=("active", "candidate")))
                continue
            if index == 4:
                release.set()
                await wait_jobs(runtime, late_session)
            await wait_jobs(runtime, session_id)
            active = runtime.memory_service.list(scope="global")
            candidates = runtime.memory_service.list(scope="global", statuses=("candidate",))
            row["memories"] = [m.model_dump(mode="json") for m in active + candidates]
            checks = report["checks"]
            if index == 0:
                first_source = user.message_id
                checks["ordinary_candidate_only"] = len(candidates) == 1 and not active
                checks["first_source_exact"] = len(candidates) == 1 and source_refs(runtime, candidates[0]) == {first_source}
            elif index == 1:
                active_id = active[0].memory_id if len(active) == 1 else None
                checks["two_sources_promote"] = len(active) == 1 and source_refs(runtime, active[0]) == {first_source, user.message_id}
            elif index == 2:
                checks["fresh_session_recall"] = active_id is not None and any(
                    item["memory_id"] == active_id for item in recall_items(result))
                checks["recall_answer_mentions_preference"] = "简洁" in result.answer or "简短" in result.answer
                checks["recall_has_no_old_session_history"] = not result.session_context_window.get("recent_messages")
            elif index == 4:
                checks["retracted_and_late_source_suppressed"] = not active and not candidates
                checks["original_record_retracted"] = active_id is not None and runtime.memory_service.get(active_id).status == "retracted"
            elif index == 5:
                checks["post_withdrawal_recall_empty"] = not recall_items(result)
        report["mechanical_pass"] = bool(report["checks"]) and all(report["checks"].values()) and not report["confounded"]
    except Exception as exc:  # noqa: BLE001 - private failure evidence, no retries of the probe
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["mechanical_pass"] = False
    finally:
        release.set()
        runtime.stop()
        report["seconds"] = time.monotonic() - started
        report["provider_records"] = records
        report["runs"] = [{"run": runtime.agent_run_manager.get_run(run_id).model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in runtime.agent_run_manager.list_events(run_id)]}
            for run_id in report["attempted_run_ids"]]
        report["calls"] = len(budget.ids)
        with ledger.connect() as conn:
            report["ledger_calls"] = [dict(zip(("id", "stage", "status", "charged", "input_tokens", "output_tokens"), row, strict=True))
                for call_id in budget.ids for row in conn.execute(
                    "SELECT id,stage,status,charged,input_tokens,output_tokens FROM calls WHERE id=?", (call_id,))]
        report["jobs"] = runtime.background_job_store.list()
        report["job_statuses"] = dict(Counter(j["status"] for j in report["jobs"]))
        report["final_memory_records"] = [dict(m.model_dump(mode="json"),
            source_refs=sorted(source_refs(runtime, m))) for m in runtime.memory_service.list(
                scope="global", statuses=("active", "candidate", "retracted"))]
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()
    if not args.remote:
        parser.error("explicit --remote required; no implicit paid call")
    root = OUTPUT / ("runtime_memory_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"))
    report = asyncio.run(run_probe(root, LiveBudget(LEDGER, usd_limit=50)))
    metrics = {k: report[k] for k in ("mode", "model", "seconds", "calls", "checks",
                                     "confounded", "mechanical_pass", "job_statuses", "private_artifacts")}
    metrics["input_tokens"] = sum(row["input_tokens"] or 0 for row in report["ledger_calls"])
    metrics["output_tokens"] = sum(row["output_tokens"] or 0 for row in report["ledger_calls"])
    metrics["charged_usd"] = sum(row["charged"] for row in report["ledger_calls"])
    metrics["call_stages"] = dict(Counter(row["stage"].split(":", 1)[1].split()[0]
                                          for row in report["ledger_calls"]))
    metrics["error_type"] = report.get("error", {}).get("type")
    print(json.dumps(metrics, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
