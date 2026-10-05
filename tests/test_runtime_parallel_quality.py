"""Offline contract tests for the one-attempt parallel runtime diagnostic."""

import asyncio
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.config import Settings
from app.core.llm.models import LLMMessage, LLMRequest, LLMResponse
from app.core.local_config import LocalAppConfig
from evals.lka_evals.live_budget import (
    LiveBudget,
    LiveBudgetExceeded,
    MeteredClient,
    ProtectedEvaluationContent,
)
from scripts import eval_realworld
from scripts import eval_runtime_parallel_quality as harness


def test_parent_and_children_share_exactly_32_concurrent_reservations(tmp_path):
    ledger = LiveBudget(tmp_path / "shared.sqlite3")
    budget = harness.AttemptBudget(ledger, source_id="offline-parent-and-children")

    def reserve(index):
        try:
            return budget.reserve(kind="llm", stage=f"child-{index % 3}", incoming=1, outgoing=1)
        except LiveBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = [call for call in pool.map(reserve, range(64)) if call is not None]
    assert len(ids) == len(set(ids)) == 32
    assert budget.snapshot()["run_budget"]["dispatches"] == 32
    assert ledger.snapshot()["groups"]["llm"]["calls"] == 32
    for _ in range(2):
        with pytest.raises(LiveBudgetExceeded):
            budget.reserve(kind="llm", stage="retry", incoming=1, outgoing=1)


def test_no_search_or_shared_cost_refusal_consumes_a_local_slot(tmp_path):
    ledger = LiveBudget(tmp_path / "shared.sqlite3", usd_limit=.00001)
    budget = harness.AttemptBudget(ledger, source_id="offline")
    with pytest.raises(LiveBudgetExceeded):
        budget.reserve(kind="search", stage="web.search")
    with pytest.raises(LiveBudgetExceeded):
        budget.reserve(kind="llm", stage="too-costly", incoming=100, outgoing=100)
    assert budget.snapshot()["run_budget"]["dispatches"] == 0
    assert ledger.snapshot()["groups"] == {}


def test_attempt_usage_excludes_concurrent_shared_calls_and_marks_unknown(tmp_path):
    ledger = LiveBudget(tmp_path / "shared.sqlite3")
    budget = harness.AttemptBudget(ledger, source_id="offline")
    mine = budget.reserve(kind="llm", stage="parent", incoming=100, outgoing=50)
    budget.finish(mine, {"prompt_tokens": 12, "completion_tokens": 3,
                         "prompt_cache_hit_tokens": 2}, status="completed")
    failed = budget.reserve(kind="llm", stage="child", incoming=100, outgoing=50)
    budget.finish(failed, None, status="failed_or_cancelled")
    budget.reserve(kind="llm", stage="unfinished-child", incoming=100, outgoing=50)
    other = ledger.reserve(kind="llm", stage="other-worker", incoming=1000, outgoing=1000)
    ledger.finish(other, {"prompt_tokens": 999, "completion_tokens": 888}, status="completed")
    usage = budget.usage()
    assert usage["dispatches"] == 3 and usage["known_usage_calls"] == 1
    assert usage["known_input_tokens"] == 12 and usage["known_output_tokens"] == 3
    assert usage["known_cached_tokens"] == 2 and usage["unknown_usage_calls"] == 2
    assert usage["pending_reserved_calls"] == 1 and usage["accounting_final"] is False
    assert usage["known_usage_cost_usd"] == pytest.approx(LiveBudget.cost(12, 3, 2))
    assert usage["unknown_usage_charge_usd"] == pytest.approx(2 * LiveBudget.cost(100, 50))


class FakeClient:
    default_model = harness.MODEL

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return LLMResponse(content="answer", status="completed", provider="fake",
                           prompt_summary=request.prompt_summary,
                           usage={"prompt_tokens": 1, "completion_tokens": 1})


def test_metered_secret_model_guard_and_output_reserve_are_not_bypassed(tmp_path):
    ledger = LiveBudget(tmp_path / "shared.sqlite3")
    budget = harness.AttemptBudget(ledger, source_id="offline")
    provider = FakeClient()
    client = MeteredClient(provider, budget, allowed_model=harness.MODEL,
                           protected_values=("PRIVATE_API_TOKEN_123",))
    request = LLMRequest(model=harness.MODEL, prompt_summary="child", max_output_tokens=32768,
                         messages=[LLMMessage(role="user", content="local public evidence")])
    asyncio.run(client.complete(request))
    assert provider.requests[0].max_output_tokens == 32768
    with pytest.raises(ProtectedEvaluationContent):
        asyncio.run(client.complete(request.model_copy(update={"messages": [
            LLMMessage(role="user", content="PRIVATE_API_TOKEN_123")]})))
    with pytest.raises(LiveBudgetExceeded):
        asyncio.run(client.complete(request.model_copy(update={"model": "unpriced"})))
    assert len(provider.requests) == budget.snapshot()["run_budget"]["dispatches"] == 1


def test_isolation_keeps_child_and_provider_caps_without_mutating_config():
    configured = LocalAppConfig()
    before = configured.model_dump()
    isolated = harness.isolated_parallel_config(configured)
    assert configured.model_dump() == before
    assert isolated.agent.multi_agent_planning_enabled is True
    assert isolated.agent.model_dump(exclude={"multi_agent_planning_enabled", "mail_expert_enabled",
                                             "codex_expert_enabled", "orchestrator", "checkpoint_backend"}) == (
        configured.agent.model_dump(exclude={"multi_agent_planning_enabled", "mail_expert_enabled",
                                              "codex_expert_enabled", "orchestrator", "checkpoint_backend"}))
    assert isolated.llm.timeout_seconds == 30
    assert all(client.timeout_seconds == 30 for client in isolated.llm.client_configs())
    assert not isolated.memory.enabled and not isolated.memory.background_enabled
    assert not isolated.message_history.enabled and not isolated.message_history.background_enabled
    assert not isolated.mail.outlook.enabled and not isolated.mail.outlook.startup_sync_enabled
    assert not isolated.mail.outlook.background_sync_enabled and not isolated.mail.imap.enabled
    assert not isolated.embedding.enabled and not isolated.reranker.enabled


@pytest.mark.parametrize("remote,root_go", [(False, False), (False, True), (True, False)])
def test_run_requires_both_explicit_authorizations_before_runner(tmp_path, remote, root_go):
    async def forbidden(*args, **kwargs):
        pytest.fail("no dispatch authorized")

    with pytest.raises(ValueError, match="remote.*Root"):
        asyncio.run(harness.run_parallel_case(ledger=LiveBudget(tmp_path / "shared.sqlite3"),
                    output=tmp_path, runner=forbidden, remote=remote, root_go=root_go, baseline=None))


@pytest.mark.parametrize("changed", [False, True])
def test_fake_runner_preserves_goal_raw_states_and_sha_without_semantic_pass(
    tmp_path, monkeypatch, changed,
):
    monkeypatch.setattr(Settings, "load_local_config", lambda _: LocalAppConfig())
    fingerprints = iter([{"source": "before"}, {"source": "after" if changed else "before"}])
    monkeypatch.setattr(harness, "source_fingerprints", lambda: next(fingerprints))
    original = copy.deepcopy(eval_realworld.CASES["parallel_audit"])
    ledger = LiveBudget(tmp_path / "shared.sqlite3")
    raw_bytes = []
    child = {"run_id": "child-1", "status": "partial", "metadata": {
        "budget": {"max_tokens": 32768}, "verification": {"status": "inconclusive"}}}
    parent = {"run_id": "parent", "status": "blocked", "metadata": {
        "multi_agent_verification": {"status": "failed", "replan_required": True}}}

    async def runner(name, *, output, budget, planning, timeout, **kwargs):
        assert name == "parallel_audit" and planning is True and timeout == 240
        assert eval_realworld.CASES[name] == original
        assert kwargs == {"protocol": "configured", "mail_expert": False,
                          "full_retrieval": False, "response_mode": "text"}
        assert output.stat().st_mode & 0o777 == 0o700
        assert Settings().load_local_config().memory.background_enabled is False
        call = budget.reserve(kind="llm", stage="fake-child", incoming=100, outgoing=50)
        budget.finish(call, {"prompt_tokens": 12, "completion_tokens": 3}, status="completed")
        root = output / "parallel_audit_offline"
        root.mkdir()
        report = {"private_artifacts": str(root), "result": {
            "answer": " ".join(original["facts"]), "llm_events": [], "tool_events": []},
            "run_snapshot": parent, "child_runs": [child], "child_events": {"child-1": []},
            "run_events": [], "checks": {"files_unchanged": True}, "mechanical_pass": True,
            "task_wall_seconds": 1.5, "metrics": {"llm_calls": 1}}
        raw = json.dumps(report).encode()
        (root / "report.json").write_bytes(raw)
        raw_bytes.append(raw)
        return report

    analysis = asyncio.run(harness.run_parallel_case(ledger=ledger, output=tmp_path, runner=runner,
                                                    remote=True, root_go=True, baseline=None))
    raw = harness.Path(analysis["private_artifacts"]) / "report.json"
    assert raw.read_bytes() == raw_bytes[0]
    assert analysis["raw_report_sha256"] == hashlib.sha256(raw_bytes[0]).hexdigest()
    assert raw.stat().st_mode & 0o777 == 0o600
    derived = raw.parent / "parallel_analysis.json"
    assert derived.stat().st_mode & 0o777 == 0o600
    assert analysis["derived_report_sha256"] == hashlib.sha256(derived.read_bytes()).hexdigest()
    assert harness.Path(analysis["derived_report_path"]) == derived
    assert analysis["parent_run_snapshot"] == parent and analysis["child_runs"] == [child]
    assert analysis["literal_fact_coverage"]["matched_count"] == 6
    assert analysis["semantic_review"] == {"status": "pending_root_review"}
    assert analysis["source_files_changed_during_attempt"] is changed
    assert analysis["attempt_usage"]["dispatches"] == 1
    assert eval_realworld.CASES["parallel_audit"] == original
    summary = json.dumps(harness.stdout_summary(analysis))
    assert "Nora" not in summary and "BACKUP_419" not in summary
    assert "budget\"" not in summary and "inconclusive" not in summary
    assert "parent_run_snapshot" not in summary and "child_events" not in summary


@pytest.mark.parametrize("args", [[], ["--remote"], ["--root-go"]])
def test_cli_is_inert_without_remote_and_root_go(monkeypatch, args):
    monkeypatch.setattr(harness, "LiveBudget", lambda *a, **kw: pytest.fail("no ledger creation"))
    with pytest.raises(SystemExit) as error:
        harness.main(args)
    assert error.value.code == 2


def test_cli_refuses_missing_shared_ledger_without_creating_an_independent_one(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "LEDGER", tmp_path / "missing.sqlite3")
    monkeypatch.setattr(harness, "LiveBudget", lambda *a, **kw: pytest.fail("no new ledger allowed"))
    with pytest.raises(SystemExit) as error:
        harness.main(["--remote", "--root-go"])
    assert error.value.code == 2


def test_cli_stdout_is_metrics_only_and_dispatches_once(tmp_path, monkeypatch, capsys):
    shared = tmp_path / "shared.sqlite3"
    LiveBudget(shared)
    monkeypatch.setattr(harness, "LEDGER", shared)
    calls = []

    async def run(**kwargs):
        calls.append(kwargs)
        return {"case_id": "parallel_audit", "semantic_review": {"status": "pending_root_review"},
                "answer": "SECRET RAW ANSWER", "runtime_error": {"message": "PRIVATE_API_KEY"},
                "parent_run_snapshot": {"metadata": {"prompt": "PRIVATE PROMPT"}}}

    monkeypatch.setattr(harness, "run_parallel_case", run)
    harness.main(["--remote", "--root-go"])
    output = capsys.readouterr().out
    assert len(calls) == 1 and calls[0]["remote"] is True and calls[0]["root_go"] is True
    assert calls[0]["ledger"].path == shared
    assert "SECRET" not in output and "PRIVATE" not in output


def test_baseline_fact_hits_remain_literal_not_semantic():
    baseline = {"result": {"answer": "2026-10-10 16:00 Nora BACKUP_419"},
                "task_wall_seconds": 190.135, "total_agent_metrics": {"llm_calls": 29},
                "mechanical_pass": False}
    analysis = harness.analyze_report(baseline)
    assert analysis["literal_fact_coverage"]["matched_count"] == 4
    assert analysis["literal_fact_coverage"]["expected_count"] == 6
    assert analysis["semantic_review"]["status"] == "pending_root_review"


def test_stdout_nested_metrics_do_not_leak_new_raw_fields():
    analysis = {"metrics": {"llm_calls": 1, "prompt": "PRIVATE_PROMPT"},
                "child_metrics": {"input_tokens": 2, "child_output": "PRIVATE_OUTPUT"},
                "total_agent_metrics": {"llm_calls": 3, "raw": "PRIVATE_RAW"}}
    summary = harness.stdout_summary(analysis)
    assert "PRIVATE" not in json.dumps(summary)
    assert summary["metrics"] == {"llm_calls": 1}
    assert summary["child_metrics"] == {"input_tokens": 2}
    assert summary["total_agent_metrics"] == {"llm_calls": 3}


def test_metered_concurrent_requests_cannot_dispatch_33rd_fake_provider_call(tmp_path):
    ledger = LiveBudget(tmp_path / "shared.sqlite3")
    budget = harness.AttemptBudget(ledger, source_id="offline-concurrent-metered")
    provider = FakeClient()
    client = MeteredClient(provider, budget, allowed_model=harness.MODEL, protected_values=())
    request = LLMRequest(model=harness.MODEL, prompt_summary="child", max_output_tokens=32768,
                         messages=[LLMMessage(role="user", content="public")])

    def dispatch(_):
        try:
            return asyncio.run(client.complete(request))
        except LiveBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(dispatch, range(64)))
    assert sum(result is not None for result in results) == len(provider.requests) == 32
    assert budget.usage()["dispatches"] == budget.usage()["known_usage_calls"] == 32


def test_prepare_failure_is_private_explicit_without_fabricating_a_raw_report(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "load_local_config", lambda _: LocalAppConfig())

    async def runner(*args, **kwargs):
        raise RuntimeError("PRIVATE_SETUP_DETAIL")

    analysis = asyncio.run(harness.run_parallel_case(ledger=LiveBudget(tmp_path / "shared.sqlite3"),
        output=tmp_path, runner=runner, remote=True, root_go=True, baseline=None))
    assert analysis["raw_report_available"] is False and analysis["raw_report_sha256"] is None
    assert analysis["runtime_error"]["type"] == "RuntimeError"
    assert analysis["attempt_usage"]["dispatches"] == 0
    root = harness.Path(analysis["private_artifacts"])
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "parallel_analysis.json").stat().st_mode & 0o777 == 0o600
    assert "PRIVATE" not in json.dumps(harness.stdout_summary(analysis))


def test_fake_report_cannot_redirect_artifact_permission_changes_outside_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "load_local_config", lambda _: LocalAppConfig())
    outside = tmp_path / "parallel_audit_outside"
    outside.mkdir(mode=0o755)

    async def runner(*args, **kwargs):
        return {"private_artifacts": str(outside)}

    with pytest.raises(ValueError, match="escaped"):
        asyncio.run(harness.run_parallel_case(ledger=LiveBudget(tmp_path / "shared.sqlite3"),
            output=tmp_path, runner=runner, remote=True, root_go=True, baseline=None))
    assert outside.stat().st_mode & 0o777 == 0o755


def test_cli_has_no_attempt_limit_token_cap_or_timeout_override():
    with pytest.raises(SystemExit) as error:
        harness.main(["--remote", "--root-go", "--max-calls", "33"])
    assert error.value.code == 2
