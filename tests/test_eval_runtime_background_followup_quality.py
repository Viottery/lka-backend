"""Offline provider, actual session/outbox/worker and same-session Graph turns."""

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_eval_runtime_background_quality import Provider, service

from app.core.llm import LLMMessage, LLMRequest, LLMResponse
from app.core.local_config import LocalAppConfig
from app.core.sessions import SessionService
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded, ProtectedEvaluationContent
from scripts import eval_runtime_background_followup_quality as probe


@pytest.fixture(autouse=True)
def offline_deadline(monkeypatch):
    monkeypatch.setattr(probe, "PROBE_SECONDS", 10)


class FollowupProvider(Provider):
    def __init__(self, *, invalid=False, paraphrased=False):
        super().__init__(overlap=False)
        self.invalid = invalid
        self.paraphrased = paraphrased

    async def complete(self, request):
        payload = json.loads(request.messages[-1].content)
        if request.prompt_summary == "background_context_compact":
            summary = 'SIM-FOLLOWUP-903: Review on 2026-11-06. Release is NOT approved. Open item: safety review. "Pending".' if self.paraphrased else probe.OLD_FACTS
            content = json.dumps({"summary": summary,
                "source_trace_ids": [m["trace_id"] for m in payload["messages"]]}, ensure_ascii=False)
            if self.invalid:
                content = '{"summary":"fake","source_trace_ids":["foreign"]}'
        else:
            stage = request.metadata["stage"]
            if stage == "route":
                content = '{"selected_package":null,"reason":"Use session context."}'
            elif stage == "decision":
                content = '{"operation":{"type":"final_answer","reason":"Use context."}}'
            elif stage in {"answer", "context_answer"}:
                rendered = json.dumps(payload, ensure_ascii=False)
                content = probe.CORRECTION if probe.CORRECTION in rendered else (
                    "Review: 2026-11-06; not approved; safety review pending." if self.paraphrased else probe.OLD_FACTS)
            else:
                raise AssertionError(stage)
        return LLMResponse(provider="offline", client_name=self.name, model=probe.MODEL,
            status="completed", content=content, finish_reason="stop",
            usage={"prompt_tokens": 100, "completion_tokens": 40}, prompt_summary=request.prompt_summary)


def test_real_prefix_publications_then_same_session_recall_and_tail_correction(tmp_path):
    report = asyncio.run(probe.run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
        config=LocalAppConfig(), injected_service=service(FollowupProvider())))
    assert not report.get("error"), report.get("error")
    assert report["mechanical_checks_pass"], report["checks"]
    assert report["seeded_history"] and report["context_window"] == 4096
    assert report["production_context_window"] == 65536
    assert SessionService.default_context_token_budget == 65536
    assert report["semantic_review"] == "required_independent_not_scored"
    assert report["calls"] <= 32 and report["search_limit"] == 0
    assert report["budget_closed"] and report["accounting"]["accounting_final"]
    assert len(report["publications"]) >= 3
    assert report["first_prefixes"][-1]["covered_seq"] == 6
    assert report["publications"][-1]["covered_seq"] >= 12
    assert len({p["covered_seq"] for p in report["publications"]}) == len(report["publications"])
    assert all(p["summary_metadata"]["method"] == "model" for p in report["publications"])
    assert "seeded_followup_0" in report["publications"][-1]["summary_metadata"]["input_trace_ids"]
    assert report["turns"][0]["result"]["answer"] == probe.OLD_FACTS
    assert report["turns"][1]["result"]["answer"] == probe.CORRECTION
    assert len({t["session_id"] for t in report["turns"]}) == 1
    assert all(t["seconds"] > 0 and t["completed_at"] >= t["started_at"] for t in report["turns"])
    root = tmp_path / "probe"
    assert hashlib.sha256((root / "report.json").read_bytes()).hexdigest() == (root / "report.sha256").read_text().strip()
    assert (root / "report.json").stat().st_mode & 0o777 == 0o600
    assert not report["source_changed"]


def test_local_fallback_is_not_model_publication_pass(tmp_path):
    report = asyncio.run(probe.run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
        config=LocalAppConfig(), injected_service=service(FollowupProvider(invalid=True))))
    assert not report["mechanical_checks_pass"]
    assert not report["checks"]["model_publications"]


def test_legally_paraphrased_published_summary_delivery_is_not_literal_fact_match(tmp_path):
    report = asyncio.run(probe.run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
        config=LocalAppConfig(), injected_service=service(FollowupProvider(paraphrased=True))))
    assert not report.get("error"), report.get("error")
    assert probe.OLD_FACTS not in report["published_before_followup"]["summary"]
    assert report["recall_summary_delivery"]["status"] == "complete"
    assert report["checks"]["recall_summary_delivered"]
    assert report["turns"][0]["result"]["answer"] != probe.OLD_FACTS


def test_delivery_compares_decoded_published_version_and_discloses_projection():
    summary = 'Date 2026-11-06; NOT approved; open item: safety review. "待定"'
    def record(value):
        return {"request": {"messages": [{"role": "user", "content": json.dumps(
            {"session_context_window": {"summary": value}}, ensure_ascii=True)}]}}
    assert probe.summary_delivery([record(summary)], summary)["status"] == "complete"
    assert probe.summary_delivery([record(summary[:10])], summary)["status"] == "partial_or_unknown"
    assert probe.summary_delivery([], summary)["status"] == "unobserved"


def test_shared_cap_threadsafe_search_and_late_reservations_denied(tmp_path):
    budget = probe.FollowupBudget(LiveBudget(tmp_path / "budget.sqlite3"), "offline")
    def reserve(index):
        try:
            return budget.reserve(kind="llm", stage=str(index), incoming=1, outgoing=1)
        except LiveBudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(bool(v) for v in pool.map(reserve, range(40))) == 32
    with pytest.raises(LiveBudgetExceeded):
        budget.reserve(kind="search", stage="not-authorized")
    budget.close()
    with pytest.raises(LiveBudgetExceeded):
        budget.check_open()
    with pytest.raises(LiveBudgetExceeded):
        budget.reserve(kind="llm", stage="late")


def test_cli_refuses_implicit_paid_run():
    with pytest.raises(SystemExit):
        probe.main([])


def test_env_guard_precedes_recording_and_provider_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr("evals.lka_evals.live_budget._protected_credential_values",
                        lambda: ("synthetic-protected-secret-999",))
    provider_service = service(FollowupProvider())
    budget = probe.FollowupBudget(LiveBudget(tmp_path / "budget.sqlite3"), "guard")
    records = []
    for provider in provider_service.registry.list_clients():
        provider_service.registry.register_client(probe.RecordedClient(provider, records, budget.lock))
    probe.instrument_service(provider_service, budget, allowed_model=probe.MODEL)
    request = LLMRequest(prompt_summary="offline-guard",
                         messages=[LLMMessage(role="user", content="synthetic-protected-secret-999")])
    with pytest.raises(ProtectedEvaluationContent):
        asyncio.run(provider_service.complete(request))
    assert records == [] and budget.ids == []


def test_queued_provider_dispatch_is_fenced_after_budget_close(tmp_path):
    budget = probe.FollowupBudget(LiveBudget(tmp_path / "budget.sqlite3"), "close")
    budget.reserve(kind="llm", stage="reserved-before-close", incoming=1, outgoing=1)
    budget.close()
    dispatches = []
    observer = probe.DispatchObserver(FollowupProvider(), None, dispatches, threading.Event(),
                                     threading.Lock(), {"compaction": 0}, budget)
    with pytest.raises(LiveBudgetExceeded):
        asyncio.run(observer.complete(LLMRequest(messages=[], prompt_summary="offline-closed")))
    assert dispatches == []


def test_probe_rejects_symlink_artifact_root_before_runtime(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "probe"
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        asyncio.run(probe.run_probe(root, LiveBudget(tmp_path / "budget.sqlite3")))
    assert list(target.iterdir()) == []


def test_remote_cli_also_requires_existing_shared_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "LEDGER", tmp_path / "not-created.sqlite3")
    with pytest.raises(SystemExit):
        probe.main(["--remote", "--root-go"])
    assert not probe.LEDGER.exists()
