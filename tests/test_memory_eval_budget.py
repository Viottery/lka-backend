from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.llm.errors import LLMClientError
from app.core.llm.models import LLMMessage, LLMRequest, LLMResponse
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMClientConfig, LLMProviderConfig
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded, instrument_service


class _Provider:
    def __init__(self, model, content):
        self.name = "eval-provider"
        self.default_model = model
        self.content = content
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        return LLMResponse(
            provider="offline-test-provider", status="completed", content=self.content,
            prompt_summary=request.prompt_summary, client_name=self.name,
            model=request.model, usage={"prompt_tokens": 10, "completion_tokens": 3},
        )


def _service(*, allowed_model="deepseek-flash", provider_model=None, content="{}"):
    provider_model = provider_model or allowed_model
    config = LLMProviderConfig(
        provider="eval-provider", model=allowed_model, default_client="eval-provider",
        clients=[LLMClientConfig(
            name="eval-provider", provider="openai_compatible", default_model=provider_model,
            context_window_tokens=100_000, output_reserve_tokens=1024,
        )],
    )
    provider = _Provider(provider_model, content)
    registry = LLMClientRegistry()
    registry.register_client(provider)

    class Service:
        def __init__(self):
            self.registry = registry
            self.background_timeout_seconds = None

        def complete_text(self, **kwargs):
            client = self.registry.get("eval-provider")
            request = LLMRequest(
                messages=[
                    LLMMessage(role="system", content=kwargs["system_prompt"]),
                    LLMMessage(role="user", content=kwargs["user_prompt"]),
                ],
                prompt_summary=kwargs["prompt_summary"],
                model=kwargs.get("model") or client.default_model,
                max_output_tokens=kwargs.get("max_output_tokens"),
            )
            return asyncio.run(client.complete(request))

        def supports_thinking_control(self):
            return False

    return config, Service(), provider


def _patch_config(monkeypatch, module, config, service):
    monkeypatch.setattr(module, "load_local_config", lambda _path: SimpleNamespace(llm=config))
    monkeypatch.setattr(module, "build_llm_service", lambda _config: service)


@pytest.mark.parametrize("evaluator", ["personal_memory", "background_compaction"])
def test_supplied_ledger_instruments_evaluator_provider_calls(monkeypatch, tmp_path, evaluator):
    if evaluator == "personal_memory":
        from scripts import eval_personal_assistant_memory as module

        config, service, provider = _service(content='{"candidates": []}')
        _patch_config(monkeypatch, module, config, service)
        fixture = tmp_path / "memory.jsonl"
        fixture.write_text(json.dumps({
            "id": "ledger-case", "category": "preference",
            "text": "Please keep answers concise and precise.", "expected_claims": [],
        }) + "\n", encoding="utf-8")
        ledger_path = tmp_path / "shared-budget.sqlite3"
        report = module.evaluate(
            fixture, force_model=True, config_path=tmp_path / "local.toml",
            budget_ledger=ledger_path,
        )
    else:
        from scripts import eval_background_compaction as module

        config, service, provider = _service(
            content="10月21日 不要自动付款 600 退票 SQLite 不能共享偏好 11月5日 不要删除旧数据库 断网恢复"
        )
        _patch_config(monkeypatch, module, config, service)
        ledger_path = tmp_path / "shared-budget.sqlite3"
        report = module.evaluate(
            remote=True, config_path=tmp_path / "local.toml", budget_ledger=ledger_path,
        )

    assert provider.calls > 0
    ledger = LiveBudget(ledger_path).snapshot()
    assert ledger["usd_limit"] == 50
    assert ledger["groups"]["llm"]["calls"] == provider.calls
    assert ledger["groups"]["llm"]["known_usage_calls"] == provider.calls
    assert report["budget_ledger"]["groups"]["llm"]["calls"] == provider.calls


def test_supplied_ledger_rejects_unpriced_model_before_provider_dispatch(tmp_path):
    _, service, provider = _service(provider_model="unpriced-model")
    budget = LiveBudget(tmp_path / "budget.sqlite3", usd_limit=50)
    instrument_service(service, budget, allowed_model="priced-model")
    with pytest.raises((LiveBudgetExceeded, LLMClientError), match="unpriced model"):
        asyncio.run(service.complete_text(
            system_prompt="test", user_prompt="test", prompt_summary="eval",
        ))
    assert provider.calls == 0


@pytest.mark.parametrize("evaluator", ["personal_memory", "background_compaction"])
def test_offline_evaluation_does_not_create_or_use_ledger(tmp_path, evaluator):
    ledger_path = tmp_path / "offline-budget.sqlite3"
    if evaluator == "personal_memory":
        from scripts.eval_personal_assistant_memory import evaluate

        report = evaluate(budget_ledger=ledger_path, max_cases=0)
    else:
        from scripts.eval_background_compaction import evaluate

        report = evaluate(budget_ledger=ledger_path)
    assert report["budget_ledger"] is None
    assert not ledger_path.exists()


@pytest.mark.parametrize("evaluator", ["personal_memory", "background_compaction"])
@pytest.mark.parametrize("failure", ["missing_ledger", "unpriced_model"])
def test_real_remote_mode_fails_closed_before_building_service(monkeypatch, tmp_path, evaluator, failure):
    if evaluator == "personal_memory":
        from scripts import eval_personal_assistant_memory as module
        kwargs = {"force_model": True, "max_cases": 1}
    else:
        from scripts import eval_background_compaction as module
        kwargs = {"remote": True}
    model = "unpriced-model" if failure == "unpriced_model" else "deepseek-flash"
    config, _, provider = _service(allowed_model=model)
    monkeypatch.setattr(module, "load_local_config", lambda _path: SimpleNamespace(llm=config))

    def unexpected_build(_config):
        raise AssertionError("invalid remote mode must be rejected before service construction")

    monkeypatch.setattr(module, "build_llm_service", unexpected_build)
    ledger = tmp_path / "budget.sqlite3"
    if failure == "unpriced_model":
        kwargs["budget_ledger"] = ledger
    with pytest.raises(ValueError, match="unpriced model|requires --budget-ledger"):
        module.evaluate(**kwargs)
    assert provider.calls == 0
    assert not ledger.exists()


@pytest.mark.parametrize("content,finish_reason,error", [
    ("", "length", "IncompleteGenerationError"),
    ("not JSON", "stop", "ValueError"),
])
@pytest.mark.parametrize("force_model", [True, False])
def test_invalid_model_response_is_not_scored_as_empty_extraction(
    monkeypatch, tmp_path, content, finish_reason, error, force_model,
):
    from scripts import eval_personal_assistant_memory as module

    config, service, provider = _service(content=content)
    original = provider.complete

    async def complete(request):
        response = await original(request)
        return response.model_copy(update={"finish_reason": finish_reason})

    provider.complete = complete
    _patch_config(monkeypatch, module, config, service)
    fixture = tmp_path / "remote.jsonl"
    fixture.write_text(json.dumps({
        "id": "remote", "category": "transient", "text": "What time is it?",
        "expected_claims": [],
    }) + "\n", encoding="utf-8")
    report = module.evaluate(fixture, remote=True, force_model=force_model, max_cases=1, max_calls=1,
                             budget_ledger=tmp_path / "budget.sqlite3")
    assert report["cases_scored"] == 0
    assert report["cases_failed"] == 1
    assert report["precision"] is report["recall"] is None
    assert report["failed_cases"][0]["error"].startswith(error)
    assert report["provider_responses"][0]["content_chars"] == len(content)
    assert report["budget_ledger"]["groups"]["llm"]["calls"] == 1


def test_provider_exact_accuracy_is_consistent_between_category_and_split(monkeypatch, tmp_path):
    from scripts import eval_personal_assistant_memory as module

    config, service, _ = _service(content='{"candidates": []}')
    _patch_config(monkeypatch, module, config, service)
    fixture = tmp_path / "negative.jsonl"
    fixture.write_text(json.dumps({
        "id": "negative", "category": "transient", "split": "holdout",
        "text": "What time is it?", "expected_claims": [],
    }) + "\n", encoding="utf-8")
    report = module.evaluate(fixture, force_model=True, budget_ledger=tmp_path / "budget.sqlite3")
    assert report["by_category"]["transient"]["provider_exact_case_accuracy"] == 1
    assert report["by_split"]["holdout"]["provider_exact_case_accuracy"] == 1
    assert report["provider_by_split"]["holdout"]["provider_exact_case_accuracy"] == 1
