"""Scripted provider boundary, real scheduler/admission/tools/normalization."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from app.core.llm import LLMResponse, LLMService
from app.core.llm.models import LLMMessage, LLMRequest
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMClientConfig, LLMProviderConfig, LocalAppConfig
from app.core.prompt_tokens import PromptTokenCounter
from evals.lka_evals.live_budget import (
    LiveBudget,
    LiveBudgetExceeded,
    MeteredClient,
    ProtectedEvaluationContent,
)
from scripts.eval_runtime_watch_quality import (
    CALLS_PER_OCCURRENCE,
    MAX_CALLS,
    MODEL,
    RecordedClient,
    WatchBudget,
    run_probe,
)


class WatchProvider:
    name = "offline"
    default_model = MODEL
    provider_name = "offline"
    supports_function_calling = False
    supports_json_mode = True
    supports_stream = False
    available_models: ClassVar[list[str]] = [MODEL]

    def __init__(self, counter, exhaust=False, malformed=False, budget_partial=False):
        self.counter, self.exhaust, self.malformed = counter, exhaust, malformed
        self.budget_partial, self.occurrence = budget_partial, 0

    async def complete(self, request):
        prompt = json.loads(request.messages[1].content)
        stage = request.metadata.get("stage")
        if stage == "route":
            self.occurrence += 1
            result = {"selected_package": "mail", "reason": "Read permitted mail"}
        elif stage == "tool_result_check":
            result = {"status": "accepted", "message": "Recorded", "remaining_work": "Continue"}
        elif stage == "decision":
            if (not prompt["observations"] or self.exhaust
                    or self.budget_partial and self.occurrence <= 2):
                operation = {"type": "tool_call", "tool_name": "mail.search",
                    "tool_input": {"query": "", "limit": len(prompt["observations"]) + 1}, "final_answer": None,
                    "reason": "Read recent authorized messages", "confidence": "high"}
            else:
                operation = {"type": "final_answer", "final_answer": None,
                    "reason": "Sufficient excerpts", "confidence": "high"}
            result = {"operation": operation, "assistant_message": "Checking updates"}
        elif stage == "answer":
            messages = next(o["result"]["output"]["messages"]
                for o in reversed(prompt["observations"]) if o.get("tool_name") == "mail.search")
            latest = max(messages, key=lambda m: m["received_at"])
            claim = latest["snippet"].splitlines()[-1]
            result = {"summary": claim, "changes": [{"event_id": latest["message_id"],
                "subject_key": "RIVER-739-venue",
                "claim": claim, "current_observation": claim,
                "evidence_refs": [latest["message_id"]]}], "unchanged": [],
                "unconfirmed": [{"claim": "负责人尚未确定，邮件没有提供相关证据。",
                    "evidence_refs": []}], "decisions": []}
        else:
            raise AssertionError(f"unexpected stage {stage}")
        content = json.dumps(result, ensure_ascii=False)
        if self.malformed and stage == "answer":
            content = "Not a structured briefing."
        incoming = self.counter.count_request(request.messages[0].content,
            request.messages[1].content, request.tools).count
        return LLMResponse(provider="offline", model=MODEL, client_name=self.name,
            status="completed", finish_reason="stop", content=content,
            prompt_summary=request.prompt_summary, usage={"prompt_tokens": incoming,
                "completion_tokens": self.counter.count_text(content).count})


def fixture_service(tmp_path, exhaust=False, malformed=False, budget_partial=False):
    # Local offline fixture tokenizer, selected by actual config/service/loop.
    # No patched prompt counter or production child budget. Live uses local config.
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    config = LLMProviderConfig(model=MODEL, default_client="offline", clients=[
        LLMClientConfig(name="offline", provider="mock", default_model=MODEL, context_window_tokens=148000,
            tokenizer_json_path=path)])
    registry = LLMClientRegistry()
    registry.register_client(WatchProvider(PromptTokenCounter(path), exhaust, malformed, budget_partial))
    return LocalAppConfig(llm=config), LLMService(config=config, registry=registry)


def test_three_slots_real_pipeline_classification_scope_and_dedupe(tmp_path):
    config, service = fixture_service(tmp_path)
    # Legacy configuration has no explicit default_client; actual registry
    # selection must still resolve the model's real tokenizer/capacity.
    service.config = service.config.model_copy(update={"default_client": None})
    config = config.model_copy(update={"llm": service.config})
    report = run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
        config=config, injected_service=service)
    assert "error" not in report, report.get("error")
    assert report["mechanical_checks_pass"], report["mechanical_checks"]
    assert report["semantic_review"]["status"] == "pending_root_review"
    assert "semantic_pass" not in report
    assert report["inference_config"]["actual_client"] == "offline"
    assert report["inference_config"]["tokenizer_json_path"] == str(tmp_path / "tokenizer.json")
    assert len(report["occurrences"]) == 3
    assert report["calls"] == len(report["provider_records"]) == 12
    assert all(r["calls"] == 4 for r in report["occurrences"])
    assert len(report["runs"]) == 6
    assert all(r["status"] == "completed" for r in report["ledger_calls"])
    assert report["slot_mode"] == "compressed_fixture_replay_not_natural_days"
    assert (tmp_path / "probe" / "report.json").stat().st_mode & 0o777 == 0o600


def test_budget_partial_with_invalid_summary_is_not_published(tmp_path):
    config, service = fixture_service(tmp_path, exhaust=True, malformed=True)
    report = run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
        config=config, injected_service=service)
    assert not report["mechanical_checks_pass"]
    assert all(r["occurrence"]["status"] == "failed" and r["briefing"] is None
        for r in report["occurrences"])
    assert all(r["calls"] <= 6 for r in report["occurrences"])
    assert report["calls"] <= 18
    assert len(report["runs"]) == 6 and all(r["events"] for r in report["runs"])


def test_success_status_with_only_unconfirmed_is_not_semantic_success(tmp_path):
    config, service = fixture_service(tmp_path, malformed=True)
    report = run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
        config=config, injected_service=service)
    assert report["mechanical_pass"] and not report["mechanical_checks_pass"]
    assert all(r["briefing"]["unconfirmed"] and not r["briefing"]["changes"]
        for r in report["occurrences"])


def test_attempt_caps_are_locked_and_shared_across_retries(tmp_path):
    budget = WatchBudget(LiveBudget(tmp_path / "budget.sqlite3"), "bounded")

    def reserve(_):
        try:
            return budget.reserve(kind="llm", stage="retry", incoming=100, outgoing=10)
        except LiveBudgetExceeded:
            return None

    for index in range(3):
        budget.occurrence = index
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert sum(bool(v) for v in pool.map(reserve, range(10))) == CALLS_PER_OCCURRENCE
    assert len(budget.ids) == MAX_CALLS == 18
    budget.occurrence = None
    assert reserve(None) is None


@pytest.mark.parametrize("secret", [True, False], ids=["secret", "unpriced"])
def test_guard_before_recording_or_dispatch(tmp_path, secret):
    _, service = fixture_service(tmp_path)
    budget = WatchBudget(LiveBudget(tmp_path / "budget.sqlite3"), "guard")
    budget.occurrence = 0
    records = []
    provider = MeteredClient(RecordedClient(service.registry.list_clients()[0], records,
        threading.Lock()), budget, allowed_model=MODEL, protected_values=("SYNTHETIC_SECRET_739",))
    request = LLMRequest(messages=[LLMMessage(role="user",
        content="SYNTHETIC_SECRET_739" if secret else "safe")],
        model=MODEL if secret else "unpriced", prompt_summary="guard")
    with pytest.raises(ProtectedEvaluationContent if secret else LiveBudgetExceeded):
        asyncio.run(provider.complete(request))
    assert not records and not budget.ids
