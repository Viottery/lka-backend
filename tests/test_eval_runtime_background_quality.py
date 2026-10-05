"""Offline actual Graph/outbox/worker/provider path, not manual memory setup."""

import asyncio
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

import pytest

from app.core.llm import LLMRequest, LLMResponse, LLMService
from app.core.llm.errors import LLMTimeoutError
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMClientConfig, LLMProviderConfig, LocalAppConfig
from app.core.memory_extraction import extract_user_memories
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded
from scripts import eval_runtime_background_quality as probe


class Provider:
    name = "offline"
    default_model = probe.MODEL
    provider_name = "offline"
    supports_function_calling = False
    supports_json_mode = True
    supports_stream = False
    available_models: ClassVar[list[str]] = [probe.MODEL]

    def __init__(self, *, extraction="valid", compaction="valid", overlap=True):
        self.extraction, self.compaction, self.overlap = extraction, compaction, overlap
        self.release = threading.Event()
        self.compacting = threading.Event()
        self.extract_calls = 0

    async def complete(self, request):
        stage = request.prompt_summary
        if stage == "background_memory_extract":
            self.extract_calls += 1
            candidates = [{"claim": probe.PREFERENCE, "evidence": probe.PREFERENCE,
                           "kind": "preference", "explicit": False, "confidence": .95}]
            if self.extraction == "empty":
                candidates = []
            elif self.extraction == "invalid":
                candidates[0]["claim"] = "用户偏好直接执行所有方案"
            elif self.extraction in {"excerpt", "different"}:
                candidates[0]["claim"] = "在比较方案时先看到取舍理由再看结论"
                candidates[0]["evidence"] = probe.PREFERENCE[:-1]
                if self.extraction == "different" and self.extract_calls == 2:
                    candidates[0]["claim"] = "偏好" + candidates[0]["claim"]
            content = json.dumps({"candidates": candidates}, ensure_ascii=False)
        elif stage == "background_context_compact":
            payload = json.loads(request.messages[-1].content)
            if self.overlap:
                self.compacting.set()
                while not self.release.is_set():
                    await asyncio.sleep(.01)
            content = json.dumps({"summary": "SIM-742评审日期为2026-11-06，尚未批准上线。",
                "goals": [], "decisions": [], "constraints": [], "corrections": [],
                "open_questions": [], "source_trace_ids": list(dict.fromkeys(
                    m["trace_id"] for m in payload["messages"] if m.get("trace_id")))}, ensure_ascii=False)
            if self.compaction == "invalid":
                content = '{"summary":"invalid","source_trace_ids":["invented"]}'
        else:
            payload = json.loads(request.messages[-1].content)
            kind = request.metadata["stage"]
            read = "workspace_probe.txt" in payload.get("user_input", "")
            if kind == "route":
                content = json.dumps({"selected_package": "filesystem" if read else None,
                                      "reason": "Use the available local evidence."})
            elif kind == "decision":
                observed = any(o.get("tool_name") == "filesystem.read_file"
                               for o in payload["observations"])
                operation = {"type": "final_answer", "reason": "Evidence collected."} if observed else {
                    "type": "tool_call", "tool_name": "filesystem.read_file",
                    "tool_input": {"path": "workspace_probe.txt"}}
                content = json.dumps({"operation": operation})
            elif kind == "tool_result_check":
                content = '{"status":"accepted","message":"Read succeeded.","remaining_work":"Finish."}'
            elif kind in {"answer", "context_answer"}:
                if read and self.compacting.is_set():
                    self.release.set()  # Actual read tool already ran while compaction awaited its response.
                content = probe.NOTE if read else "已了解；没有新的批准决定。"
            else:
                raise AssertionError(f"unexpected model stage: {kind}")
        return LLMResponse(provider="offline", model=probe.MODEL, client_name=self.name,
            status="completed", content=content, prompt_summary=stage, finish_reason="stop",
            usage={"prompt_tokens": 100, "completion_tokens": 40})


def service(provider):
    registry = LLMClientRegistry()
    registry.register_client(provider)
    return LLMService(config=LLMProviderConfig(model=probe.MODEL, default_client="offline", clients=[
        LLMClientConfig(name="offline", default_model=probe.MODEL, context_window_tokens=100_000)]),
        registry=registry)


def run(tmp_path, provider):
    try:
        return asyncio.run(probe.run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
            config=LocalAppConfig(), injected_service=service(provider)))
    finally:
        provider.release.set()


def test_real_remote_extraction_and_worker_compaction_overlap(tmp_path):
    report = run(tmp_path, Provider())
    assert not report.get("error"), report.get("error")
    assert report["mechanical_checks_pass"] and not report["confounded"]
    assert all(report["checks"].values())
    assert report["calls"] <= 24 and report["search_limit"] == 0
    assert report["semantic_review"] == "required_independent_not_scored"
    assert report["budget_closed"] and report["accounting_final"]
    assert all(row["accounting_final"] and not row["pending"] for row in report["ledger_calls"])
    assert not report["source_changed"]
    assert "app/core/prompt_budget.py" in report["source_fingerprints_before"]
    assert "scripts/eval_runtime_memory_quality.py" in report["source_fingerprints_before"]
    extraction = report["scenarios"]["extraction"]
    assert len(extraction["sources"]) == 2
    assert len({s["message_id"] for s in extraction["sources"]}) == 2
    assert extraction["records"][0]["status"] == "active"
    assert report["scenarios"]["compaction"]["overlap_provider_calls"] >= 1
    assert report["scenarios"]["compaction"]["heartbeat_ticks"] >= 1
    assert report["scenarios"]["compaction"]["readonly_tools_during_provider"]
    assert report["admission_waits"] and report["resolved_profiles"][0]["model"] == probe.MODEL
    assert report["health_final"]["active_by_pool"]["background_memory"] == 0
    assert {"interactive", "background_memory"} <= set(report["pool_usage"])
    assert sum(p["calls"] for p in report["pool_usage"].values()) == report["calls"]
    raw = (tmp_path / "probe" / "report.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (tmp_path / "probe" / "report.sha256").read_text().strip()


def test_supported_short_excerpt_and_legal_claim_not_full_source_pass_mechanical(tmp_path, monkeypatch):
    before = probe.source_fingerprints()
    calls = 0

    def snapshots():
        nonlocal calls
        calls += 1
        return before if calls == 1 else {**before, "app/core/prompt_budget.py": "simulated_changed_source"}

    monkeypatch.setattr(probe, "source_fingerprints", snapshots)
    report = run(tmp_path, Provider(extraction="excerpt"))
    assert not report.get("error") and report["mechanical_checks_pass"]
    entry = report["scenarios"]["extraction"]["records"][0]
    assert entry["content"] != probe.PREFERENCE and entry["metadata"]["evidence"] != probe.PREFERENCE
    assert entry["mechanical_support_reasons"] == []
    assert report["scenarios"]["extraction"]["identity_diagnostics"]["same_publication_identity"]
    assert report["source_changed_files"] == ["app/core/prompt_budget.py"]


def test_different_legal_claim_identities_zero_active_is_honest_failure(tmp_path):
    report = run(tmp_path, Provider(extraction="different"))
    assert not report.get("error") and not report["mechanical_checks_pass"]
    diagnostics = report["scenarios"]["extraction"]["identity_diagnostics"]
    assert diagnostics["valid_candidate_count"] == 2 and diagnostics["active_count"] == 0
    assert not diagnostics["same_publication_identity"]
    assert diagnostics["observed_not_promoted_reason"] == "different_candidate_identities"


@pytest.mark.parametrize("extraction", ["empty", "invalid"])
def test_succeeded_jobs_with_no_accepted_candidates_are_not_learning_pass(tmp_path, extraction):
    report = run(tmp_path, Provider(extraction=extraction))
    assert not report.get("error"), report.get("error")
    assert not report["checks"]["remote_nonempty_supported_active"] and not report["mechanical_checks_pass"]
    source_sessions = {s["session_id"] for s in report["scenarios"]["extraction"]["sources"]}
    extraction_jobs = [j for j in report["jobs"] if j["kind"] == "memory_extract" and j["scope_id"] in source_sessions]
    assert len(extraction_jobs) == 2 and all(j["status"] == "succeeded" for j in extraction_jobs)
    if extraction == "invalid":
        assert any(c["validation_reasons"] for batch in report["extraction_diagnostics"] for c in batch)


def test_local_summary_fallback_does_not_claim_remote_compaction_success(tmp_path):
    report = run(tmp_path, Provider(compaction="invalid"))
    assert not report.get("error"), report.get("error")
    assert not report["checks"]["worker_model_summary"] and not report["mechanical_checks_pass"]
    assert report["scenarios"]["compaction"]["after"]["summary_metadata"]["method"] == "local_fallback"


def test_fast_provider_without_overlap_is_inconclusive_not_pass(tmp_path, monkeypatch):
    original = probe.wait_for

    async def short_wait(predicate, seconds=1):
        return await original(predicate, seconds=1)

    monkeypatch.setattr(probe, "wait_for", short_wait)
    report = run(tmp_path, Provider(overlap=False))
    assert report.get("error", {}).get("type") == "TimeoutError"
    assert not report["mechanical_checks_pass"]


def test_overlap_observation_shared_between_distinct_provider_clients():
    async def scenario():
        release = asyncio.Event()

        class MinimalProvider:
            provider_name = "offline"

            async def complete(self, request):
                if request.prompt_summary == "background_context_compact":
                    await release.wait()
                return LLMResponse(provider="offline", model=probe.MODEL, status="completed",
                                   prompt_summary=request.prompt_summary, content="complete", finish_reason="stop")

        rows, entered, lock, inflight = [], threading.Event(), threading.Lock(), {"compaction": 0}
        background = probe.DispatchObserver(MinimalProvider(), None, rows, entered, lock, inflight)
        foreground = probe.DispatchObserver(MinimalProvider(), None, rows, entered, lock, inflight)
        task = asyncio.create_task(background.complete(LLMRequest(messages=[], prompt_summary="background_context_compact")))
        await probe.wait_for(entered.is_set, seconds=1)
        await foreground.complete(LLMRequest(messages=[], prompt_summary="foreground"))
        assert rows[-1]["compaction_inflight_at_dispatch"]
        release.set()
        await task
        assert inflight["compaction"] == 0

    asyncio.run(scenario())


def test_fixture_requires_remote_and_call_cap_covers_all_pools(tmp_path):
    assert extract_user_memories(source_id="fixture", content=probe.PREFERENCE) == []
    ledger = LiveBudget(tmp_path / "budget.sqlite3")
    budget = probe.PoolBudget(ledger, "probe")

    def reserve(index):
        try:
            return budget.reserve(kind="llm", stage=str(index), incoming=10, outgoing=10)
        except LiveBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(bool(x) for x in pool.map(reserve, range(30))) == 24
    with pytest.raises(LiveBudgetExceeded):
        budget.reserve(kind="search", stage="forbidden")
    assert ledger.snapshot()["groups"]["llm"]["calls"] == 24


def test_close_blocks_late_dispatch_but_allows_settlement_unknown_and_pending_not_zero(tmp_path):
    ledger = LiveBudget(tmp_path / "budget.sqlite3")
    budget = probe.PoolBudget(ledger, "closed-probe")
    ids = [budget.reserve(kind="llm", stage=str(i), incoming=10, outgoing=20) for i in range(3)]
    budget.close()
    budget.close()
    with pytest.raises(LiveBudgetExceeded):
        budget.reserve(kind="llm", stage="late")
    budget.finish(ids[0], {"prompt_tokens": 10, "completion_tokens": 20, "prompt_cache_hit_tokens": 2}, status="completed")
    budget.finish(ids[1], None, status="failed_or_cancelled")
    with ledger.connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM calls")]
    total = probe.accounting(rows)
    assert total["calls"] == 3 and total["pending_calls"] == 1 and not total["accounting_final"]
    assert total["unknown_usage_calls"] == 2 and total["input_tokens"] is None and total["cached_tokens"] is None
    assert total["known_usage_tokens"] == {"input_tokens": 10, "output_tokens": 20, "cached_tokens": 2}
    assert total["unknown_usage_reserved_usd"] > 0 and total["pending_reserved_usd"] > 0
    provider = Provider()
    observer = probe.DispatchObserver(provider, None, [], threading.Event(), threading.Lock(), {"compaction": 0}, budget)
    with pytest.raises(LiveBudgetExceeded):
        asyncio.run(observer.complete(LLMRequest(messages=[], prompt_summary="background_memory_extract")))
    assert provider.extract_calls == 0
    queued = probe.PoolBudget(ledger, "queued-before-close")
    queued.reserve(kind="llm", stage="queued", incoming=10, outgoing=20)
    observed = probe.DispatchObserver(provider, None, [], threading.Event(), threading.Lock(), {"compaction": 0}, queued)

    async def queued_dispatch():
        asyncio.get_running_loop().call_soon(queued.close)
        with pytest.raises(LiveBudgetExceeded):
            await observed.complete(LLMRequest(messages=[], prompt_summary="background_memory_extract"))

    asyncio.run(queued_dispatch())
    assert provider.extract_calls == 0


@pytest.mark.parametrize("cancelled", [False, True])
def test_recorded_deadline_is_typed_but_other_cancellation_is_preserved(monkeypatch, cancelled):
    original_wait_for = asyncio.wait_for

    async def shortened_deadline(awaitable, timeout):
        assert timeout == 30
        return await original_wait_for(awaitable, .01)

    monkeypatch.setattr(asyncio, "wait_for", shortened_deadline)
    records, dispatches, inflight = [], [], {"compaction": 0}

    class PendingProvider:
        provider_name = "offline"

        async def complete(self, request):
            if cancelled:
                raise asyncio.CancelledError()
            await asyncio.Future()

    lock = threading.Lock()
    observed = probe.DispatchObserver(PendingProvider(), None, dispatches, threading.Event(), lock, inflight)
    client = probe.RecordedClient(observed, records, lock)
    with pytest.raises(asyncio.CancelledError if cancelled else LLMTimeoutError):
        asyncio.run(client.complete(LLMRequest(messages=[], prompt_summary="background_context_compact")))
    assert inflight["compaction"] == 0
    assert records[0]["error_type"] == ("CancelledError" if cancelled else "LLMTimeoutError")
    assert records[0].get("timeout_source") == (None if cancelled else "evaluation_provider_request_deadline")


@pytest.mark.parametrize("args", [[], ["--remote"], ["--root-go"]])
def test_cli_requires_explicit_paid_go(args):
    with pytest.raises(SystemExit):
        probe.main(args)
