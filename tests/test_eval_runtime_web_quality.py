"""Offline guards and the real SQLite replay pipeline; no external dispatch."""

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from app.core.llm import LLMResponse, LLMService
from app.core.llm.models import LLMMessage, LLMRequest
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMClientConfig, LLMProviderConfig, LocalAppConfig
from app.core.prompt_tokens import PromptTokenCounter
from app.integrations.web_search import BraveSearchAdapter, PublicPageFetcher
from evals.lka_evals.live_budget import (
    LiveBudget,
    LiveBudgetExceeded,
    MeteredClient,
    ProtectedEvaluationContent,
)
from scripts import eval_runtime_web_quality as probe
from scripts.eval_realworld import CASES, _SearchReservation

PAGES = {
    "https://sqlite.org/wal.html": "WAL permits only one writer at a time. Long readers impede checkpoint progress.",
    "https://sqlite.org/lang_transaction.html": "A read transaction sees a historic snapshot of the database.",
}


class WebProvider:
    name = "offline"
    default_model = probe.MODEL
    provider_name = "offline"
    supports_function_calling = False
    supports_json_mode = True
    supports_stream = False
    available_models: ClassVar[list[str]] = [probe.MODEL]

    def __init__(self, counter):
        self.counter = counter

    async def complete(self, request):
        prompt = json.loads(request.messages[1].content)
        stage = request.metadata.get("stage")
        if stage == "route":
            result = {"selected_package": "web", "reason": "Consult official pages"}
        elif stage == "decision":
            calls = [o for o in prompt["observations"] if o.get("tool_name")]
            operations = [
                {"type": "tool_call", "tool_name": "web.search", "tool_input": {"query": "SQLite WAL concurrency"}},
                {"type": "tool_call", "tool_name": "web.find", "tool_input": {
                    "url": next(iter(PAGES)), "query": "single writer"}},
                {"type": "tool_call", "tool_name": "web.find", "tool_input": {
                    "url": list(PAGES)[1], "query": "CONCURRENT"}},
                {"type": "final_answer", "final_answer": None},
            ]
            result = {"operation": {**operations[min(len(calls), 3)], "reason": "Read then summarize",
                "confidence": "high"}, "assistant_message": "Checking official sources"}
        elif stage == "tool_result_check":
            result = {"status": "accepted", "message": "Excerpt received", "remaining_work": "Continue"}
        elif stage == "answer":
            result = "WAL: one writer; readers see their starting snapshot; long readers delay checkpoint. " + " ".join(PAGES)
        else:
            raise AssertionError(stage)
        content = result if isinstance(result, str) else json.dumps(result)
        return LLMResponse(provider="offline", model=self.default_model, client_name=self.name,
            status="completed", finish_reason="stop", content=content, prompt_summary=request.prompt_summary,
            usage={"prompt_tokens": self.counter.count_request(request.messages[0].content,
                request.messages[1].content, request.tools).count,
                "completion_tokens": self.counter.count_text(content).count})


def fixture_service(tmp_path):
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    config = LLMProviderConfig(model=probe.MODEL, default_client="offline", clients=[
        LLMClientConfig(name="offline", provider="mock", default_model=probe.MODEL,
            context_window_tokens=148000, tokenizer_json_path=path)])
    registry = LLMClientRegistry()
    registry.register_client(WebProvider(PromptTokenCounter(path)))
    return LocalAppConfig(llm=config), LLMService(config=config, registry=registry)


@pytest.mark.parametrize("argv", [[], ["--remote"], ["--root-go"]])
def test_cli_requires_both_approvals_before_opening_ledger(monkeypatch, argv):
    def forbidden(*args):
        pytest.fail("must not open ledger or runtime without both approvals")
    monkeypatch.setattr(probe, "existing_ledger", forbidden)
    monkeypatch.setattr(probe, "run_probe", forbidden)
    with pytest.raises(SystemExit) as exc:
        probe.main(argv)
    assert exc.value.code == 2


def test_existing_shared_ledger_only_no_bootstrap(tmp_path):
    path = tmp_path / "absent" / "budget.sqlite3"
    with pytest.raises(ValueError, match="existing shared ledger"):
        probe.existing_ledger(path)
    assert not path.parent.exists()
    path = tmp_path / "empty.sqlite3"
    path.touch()
    with pytest.raises(ValueError, match="compatible"):
        probe.existing_ledger(path)
    assert path.stat().st_size == 0
    ledger = LiveBudget(tmp_path / "shared.sqlite3")
    existing_id = ledger.reserve(kind="llm", stage="other_probe", incoming=30, outgoing=10)
    opened = probe.existing_ledger(ledger.path)
    assert opened.snapshot()["groups"]["llm"]["calls"] == 1
    with opened.connect() as conn:
        assert conn.execute("SELECT status FROM calls WHERE id=?", (existing_id,)).fetchone() == ("reserved",)


def test_approved_cli_dispatches_once_on_existing_shared_ledger(tmp_path, monkeypatch, capsys):
    ledger = LiveBudget(tmp_path / "budget.sqlite3")
    calls = []

    async def offline(root, opened):
        calls.append((root, opened.path))
        return {"private_artifacts": str(root), "calls": 0, "searches": 0, "charged_usd": 0,
            "semantic_review": {"status": "pending_root_review"}}

    monkeypatch.setattr(probe, "LEDGER", ledger.path)
    monkeypatch.setattr(probe, "OUTPUT", tmp_path)
    monkeypatch.setattr(probe, "run_probe", offline)
    probe.main(["--remote", "--root-go"])
    assert len(calls) == 1 and calls[0][1] == ledger.path
    assert calls[0][0].name.startswith("runtime_web_sqlite_")
    assert json.loads(capsys.readouterr().out)["semantic_review"]["status"] == "pending_root_review"


def test_approved_cli_still_refuses_absent_shared_ledger(tmp_path, monkeypatch):
    path = tmp_path / "missing" / "budget.sqlite3"
    monkeypatch.setattr(probe, "LEDGER", path)
    with pytest.raises(ValueError, match="existing shared ledger"):
        probe.main(["--remote", "--root-go"])
    assert not path.parent.exists()


def test_allowances_thread_safe_failures_still_consume_and_close_blocks_dispatch(tmp_path):
    ledger = LiveBudget(tmp_path / "budget.sqlite3")
    budget = probe.WebBudget(ledger, "one_probe")

    def reserve(kind):
        try:
            return budget.reserve(kind=kind, stage="parallel", incoming=100, outgoing=50)
        except LiveBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = list(pool.map(reserve, ["llm"] * 30 + ["search"] * 10))
    assert sum(x is not None for x in ids) == 23
    assert len(budget.ids) == 20 and len(budget.search_ids) == 3
    budget.finish(budget.ids[0], None, status="failed_or_cancelled")
    with pytest.raises(LiveBudgetExceeded):
        budget.reserve(kind="llm", stage="retry")
    with ledger.connect() as conn:
        assert conn.execute("SELECT usage_known,charged FROM calls WHERE id=?", (budget.ids[0],)).fetchone() == (
            0, ledger.cost(100, 50))
    fresh = probe.WebBudget(ledger, "closed")
    fresh.close()
    for kind in ("llm", "search"):
        with pytest.raises(LiveBudgetExceeded):
            fresh.reserve(kind=kind, stage="late")
    assert ledger.snapshot()["groups"]["llm"]["calls"] == 20


def test_real_search_boundary_allows_three_including_failed_dispatch_not_four(tmp_path):
    calls = []
    budget = probe.WebBudget(LiveBudget(tmp_path / "budget.sqlite3"), "search")
    adapter = BraveSearchAdapter("offline-test-key", quota=_SearchReservation(budget),
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(503)))
    from app.integrations.web_search import WebSearchError
    for _ in range(3):
        with pytest.raises(WebSearchError):
            adapter.search("SQLite WAL")
    with pytest.raises(LiveBudgetExceeded):
        adapter.search("SQLite WAL")
    assert len(calls) == len(budget.search_ids) == 3


def test_secret_guard_precedes_recording_and_ledger_reservation(tmp_path):
    _, service = fixture_service(tmp_path)
    budget = probe.WebBudget(LiveBudget(tmp_path / "budget.sqlite3"), "guard")
    records = []
    client = MeteredClient(probe.RecordedClient(service.registry.list_clients()[0], records, threading.Lock()),
        budget, allowed_model=probe.MODEL, protected_values=("private-test-token",))
    request = LLMRequest(messages=[LLMMessage(role="user", content="private-test-token")], prompt_summary="test")
    with pytest.raises(ProtectedEvaluationContent):
        asyncio.run(client.complete(request))
    assert records == budget.ids == []


def test_real_pipeline_reuses_goal_allows_search_and_retains_raw_excerpts_and_hashes(tmp_path, monkeypatch):
    config, service = fixture_service(tmp_path)
    fetched, searched, observed_configs = [], [], []

    def fetch(self, url):
        fetched.append(url)
        return 200, {"content-type": "text/plain"}, PAGES[url].encode()

    original_runtime = probe.eval_realworld.LocalKnowledgeAgentRuntime

    def create(settings):
        runtime = original_runtime(settings)
        observed_configs.append(runtime.local_app_config)
        adapter = runtime.tool_registry.get_tool("web.search").adapter
        adapter.api_key = "offline-test-key"
        adapter.transport = httpx.MockTransport(lambda request: searched.append(request) or httpx.Response(
            200, json={"web": {"results": [{"title": "WAL", "url": next(iter(PAGES)), "description": "Index"}]}}))
        return runtime

    monkeypatch.setattr(probe.eval_realworld, "LocalKnowledgeAgentRuntime", create)
    monkeypatch.setattr(PublicPageFetcher, "_fetch", fetch)
    ledger = LiveBudget(tmp_path / "budget.sqlite3")
    ledger.reserve(kind="llm", stage="another_probe", incoming=3, outgoing=1)
    report = asyncio.run(probe.run_probe(tmp_path / "probe", ledger, config=config, injected_service=service))
    assert "error" not in report, report.get("error")
    assert report["goal"] == CASES[probe.CASE]["goal"]
    assert report["protocol"] == "configured"
    assert report["mechanical_pass"], report["checks"]
    assert "semantic_pass" not in report and report["semantic_review"]["status"] == "pending_root_review"
    assert fetched == list(PAGES) and len(searched) == report["searches"] == 1
    assert 0 < report["calls"] == len(report["provider_records"]) <= 20
    assert len(report["ledger_calls"]) == report["calls"] + 1
    assert all(row["stage"].startswith("probe:") for row in report["ledger_calls"])
    assert all(row["usage_known"] == 1 for row in report["ledger_calls"] if row["kind"] == "llm")
    assert all("cached_tokens" in row for row in report["ledger_calls"])
    finds = [e["result"]["output"] for e in report["result"]["tool_events"] if e["tool_name"] == "web.find"]
    assert len(finds) == 2 and all(e["matches"] == [] and e["recovery_preview"] for e in finds)
    assert [e["text_sha256"] for e in finds] == [hashlib.sha256(text.encode()).hexdigest() for text in PAGES.values()]
    for actual in observed_configs:
        assert not actual.memory.enabled and not actual.memory.background_enabled
        assert not actual.message_history.enabled and not actual.message_history.background_enabled
        assert not actual.mail.outlook.enabled and not actual.mail.outlook.startup_sync_enabled
        assert not actual.mail.outlook.background_sync_enabled and not actual.mail.imap.enabled
        assert actual.llm.timeout_seconds == 30 and all(c.timeout_seconds == 30 for c in actual.llm.clients)
    assert report["source_sha256_before"] == report["source_sha256_after"]
    assert report["source_unchanged"] and report["scenario_unchanged"]
    path = Path(report["private_artifacts"]) / "report.json"
    raw = json.loads(path.read_text())
    assert all(report[key] == value for key, value in raw.items())
    assert "provider_records" not in raw and "raw_report_sha256" not in raw
    assert json.loads(path.with_name("web_analysis.json").read_text()) == report
    assert report["raw_report_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.with_suffix(".json.sha256").read_text().strip() == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("kind", ["outside", "wrong_name", "directory_symlink", "file_symlink"])
def test_private_raw_boundary_rejects_escape_before_chmod_or_write(tmp_path, monkeypatch, kind):
    config, service = fixture_service(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    target = foreign / "report.json"
    target.write_text("untouched raw content")
    target.chmod(0o644)

    async def fake_case(case, *, output, **kwargs):
        assert kwargs["protocol"] == "configured"
        expected = output / (probe.CASE + "_20261005T010203000000")
        if kind == "outside":
            expected = tmp_path / expected.name
            expected.mkdir()
            (expected / "report.json").write_text("outside")
        elif kind == "wrong_name":
            expected = output / "unexpected"
            expected.mkdir()
            (expected / "report.json").write_text("wrong name")
        elif kind == "directory_symlink":
            expected.symlink_to(foreign, target_is_directory=True)
        else:
            expected.mkdir()
            (expected / "report.json").symlink_to(target)
        return {"private_artifacts": str(expected)}

    monkeypatch.setattr(probe.eval_realworld, "run_case", fake_case)
    with pytest.raises(ValueError, match="boundary|non-symlink"):
        asyncio.run(probe.run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
            config=config, injected_service=service))
    assert target.read_text() == "untouched raw content" and target.stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.rglob("web_analysis.json")) and not list(tmp_path.rglob("report.json.sha256"))


def test_raw_report_bytes_are_not_reserialized(tmp_path, monkeypatch):
    config, service = fixture_service(tmp_path)
    raw_bytes = b'{"private_artifacts": "placeholder", "unusual_spacing":    [1, 2]}\n'
    written = []

    async def fake_case(case, *, output, **kwargs):
        raw_root = output / (probe.CASE + "_20261005T010203000000")
        raw_root.mkdir()
        content = raw_bytes.replace(b"placeholder", str(raw_root).encode())
        (raw_root / "report.json").write_bytes(content)
        written.append(content)
        return json.loads(content)

    monkeypatch.setattr(probe.eval_realworld, "run_case", fake_case)
    report = asyncio.run(probe.run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
        config=config, injected_service=service))
    assert Path(report["raw_report_path"]).read_bytes() == written[0]
    assert report["raw_report_sha256"] == hashlib.sha256(written[0]).hexdigest()


def test_timeout_persists_cancelled_trace_closes_allowance_and_retains_unknown_usage(tmp_path, monkeypatch):
    config, service = fixture_service(tmp_path)

    async def stalled(self, **kwargs):
        client = self.agent_llm_client.registry.list_clients()[0]
        # Unknown usage after provider failure remains charged, before turn timeout.
        request = LLMRequest(messages=[LLMMessage(role="user", content="offline")], prompt_summary="stall")
        _, call_id = client._reserve(request)
        client.budget.finish(call_id, None, status="failed_or_cancelled")
        await asyncio.sleep(1)

    monkeypatch.setattr(probe.eval_realworld.LocalKnowledgeAgentRuntime, "run_agent_turn_async", stalled)
    report = asyncio.run(probe.run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"),
        config=config, injected_service=service, timeout=.01))
    assert report["error"]["type"] == "TimeoutError"
    assert report["run_snapshot"]["status"] == "cancelled"
    assert any(e["type"] == "run_cancelled" for e in report["run_events"])
    assert report["calls"] == 1 and report["ledger_calls"][0]["usage_known"] == 0
    assert report["charged_usd"] > 0
    client = service.registry.list_clients()[0]
    with pytest.raises(LiveBudgetExceeded):
        client.budget.reserve(kind="llm", stage="late_thread")


def test_provider_timeout_is_30_seconds_and_failed_call_retains_reservation(tmp_path, monkeypatch):
    _, service = fixture_service(tmp_path)
    records, timeouts = [], []
    budget = probe.WebBudget(LiveBudget(tmp_path / "budget.sqlite3"), "provider")
    client = MeteredClient(probe.RecordedClient(service.registry.list_clients()[0], records, threading.Lock()),
        budget, allowed_model=probe.MODEL, protected_values=())

    async def timeout(coro, seconds):
        timeouts.append(seconds)
        coro.close()
        raise TimeoutError("offline simulated deadline")

    monkeypatch.setattr(asyncio, "wait_for", timeout)
    with pytest.raises(TimeoutError):
        asyncio.run(client.complete(LLMRequest(messages=[LLMMessage(role="user", content="offline")],
            prompt_summary="provider_timeout")))
    assert timeouts == [30] and records[0]["error_type"] == "TimeoutError"
    assert budget.snapshot()["groups"]["llm"]["known_usage_calls"] == 0
    assert budget.snapshot()["groups"]["llm"]["charged_usd"] > 0


def test_missing_remote_inference_refused_without_runtime_or_output(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled inference must not construct runtime")
    monkeypatch.setattr(probe.eval_realworld, "LocalKnowledgeAgentRuntime", forbidden)
    with pytest.raises(ValueError, match="configured, priced"):
        asyncio.run(probe.run_probe(tmp_path / "probe", LiveBudget(tmp_path / "budget.sqlite3"), config=LocalAppConfig()))
    assert not (tmp_path / "probe").exists()
