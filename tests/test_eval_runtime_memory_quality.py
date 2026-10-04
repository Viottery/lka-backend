"""Offline provider boundary + real Runtime/Graph/worker; never publish setup memory."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import ClassVar

import pytest

from app.core.llm import LLMResponse, LLMService
from app.core.llm.models import LLMMessage, LLMRequest
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMClientConfig, LLMProviderConfig, LocalAppConfig
from evals.lka_evals.live_budget import (
    LiveBudget,
    LiveBudgetExceeded,
    MeteredClient,
    ProtectedEvaluationContent,
)
from scripts.eval_runtime_memory_quality import (
    MAX_CALLS,
    MODEL,
    RecordedClient,
    RunBudget,
    foreground_confounding_writes,
    run_probe,
)


class ScriptedProvider:
    name = "offline"
    default_model = MODEL
    provider_name = "offline"
    supports_function_calling = False
    supports_json_mode = True
    supports_stream = False
    available_models: ClassVar[list[str]] = [MODEL]

    async def complete(self, request):
        system, prompt = request.messages[0].content, json.loads(request.messages[1].content)
        if "Choose at most one tool package" in system:
            answer = json.dumps({"selected_package": None, "reason": "offline wiring"})
        elif "Answer the current user turn directly" in system or "Final Answer Writer" in system:
            items = prompt["session_context_window"]["recalled_memories"]["items"]
            answer = "你喜欢简洁回答。" if items else "没有可用的回答偏好记忆。"
        else:
            raise AssertionError("unexpected offline model stage")
        return LLMResponse(provider="offline", model=MODEL, client_name=self.name,
            status="completed", content=answer, prompt_summary=request.prompt_summary,
            finish_reason="stop", usage={"prompt_tokens": 100, "completion_tokens": 20})


def service():
    registry = LLMClientRegistry()
    registry.register_client(ScriptedProvider())
    config = LLMProviderConfig(model=MODEL, default_client="offline", clients=[
        LLMClientConfig(name="offline", default_model=MODEL, context_window_tokens=100_000)])
    return LLMService(config=config, registry=registry)


def test_six_turns_actual_worker_sources_and_late_publish_barrier(tmp_path):
    report = asyncio.run(run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
        config=LocalAppConfig(), injected_service=service()))
    assert report.get("error") is None, report.get("error")
    assert report["mechanical_pass"] and not report["confounded"]
    assert len(report["turns"]) == 6
    assert report["calls"] == 12 and len(report["provider_records"]) == 12
    assert report["call_limit"] == MAX_CALLS == 20
    assert all(report["checks"].values())
    assert all(row["status"] == "completed" for row in report["ledger_calls"])
    assert report["job_statuses"] == {"succeeded": 6}
    assert (tmp_path / "probe" / "report.json").is_file()


def test_call_cap_covers_concurrent_foreground_background_and_retries(tmp_path):
    ledger = LiveBudget(tmp_path / "budget.sqlite3")
    budget = RunBudget(ledger, "bounded")

    def reserve(index):
        try:
            return budget.reserve(kind="llm", stage=f"stage-{index}", incoming=100, outgoing=20)
        except LiveBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(25)))
    assert len([result for result in results if result]) == len(budget.ids) == MAX_CALLS == 20
    assert ledger.snapshot()["groups"]["llm"]["calls"] == 20
    assert ledger.snapshot()["groups"]["llm"]["known_usage_calls"] == 0


def test_secret_guard_precedes_private_recording_and_reservation(tmp_path):
    import threading

    records = []
    budget = RunBudget(LiveBudget(tmp_path / "budget.sqlite3"), "guard")
    provider = MeteredClient(RecordedClient(ScriptedProvider(), records, threading.Lock()),
        budget, allowed_model=MODEL, protected_values=("SYNTHETIC_SECRET_879",))
    request = LLMRequest(messages=[LLMMessage(role="user", content="SYNTHETIC_SECRET_879")],
                         model=MODEL, prompt_summary="guard")
    with pytest.raises(ProtectedEvaluationContent):
        asyncio.run(provider.complete(request))
    assert records == [] and budget.ids == []


def test_unpriced_model_refused_before_provider_or_ledger(tmp_path):
    budget = RunBudget(LiveBudget(tmp_path / "budget.sqlite3"), "guard")
    provider = MeteredClient(ScriptedProvider(), budget, allowed_model=MODEL, protected_values=())
    request = LLMRequest(messages=[], model="unpriced", prompt_summary="guard")
    with pytest.raises(LiveBudgetExceeded, match="unpriced"):
        asyncio.run(provider.complete(request))
    assert budget.ids == []


def test_foreground_memory_or_instruction_write_is_confounded_not_worker_promotion():
    registry = SimpleNamespace(get_tool=lambda name: SimpleNamespace(
        spec=SimpleNamespace(read_only=name in {"memory.search", "other.read"})))
    result = SimpleNamespace(tool_events=[SimpleNamespace(tool_name=name)
        for name in ("memory.search", "memory.create", "instructions.update", "other.read")])
    assert foreground_confounding_writes(SimpleNamespace(tool_registry=registry), result) == [
        "memory.create", "instructions.update"]
