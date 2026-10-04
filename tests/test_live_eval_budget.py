import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.llm.models import LLMMessage, LLMRequest, LLMResponse
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded, MeteredClient


def test_unknown_failures_and_restart_keep_reservations(tmp_path):
    path = tmp_path / "budget.sqlite3"
    budget = LiveBudget(path, usd_limit=.1)
    first = budget.reserve(kind="llm", stage="decision", incoming=10000, outgoing=10000)
    budget.finish(first, None, status="timeout")
    budget.finish(first, {"prompt_tokens": 1, "completion_tokens": 1}, status="completed")
    # Finishing twice cannot discount an uncertain failed dispatch.
    assert budget.snapshot()["groups"]["llm"]["charged_usd"] == pytest.approx(.04)
    resumed = LiveBudget(path, usd_limit=.1)
    resumed.reserve(kind="llm", stage="answer", incoming=10000, outgoing=10000)
    with pytest.raises(LiveBudgetExceeded):
        resumed.reserve(kind="llm", stage="retry", incoming=10000, outgoing=10000)


def test_actual_usage_and_cache_discount(tmp_path):
    budget = LiveBudget(tmp_path / "budget.sqlite3")
    call = budget.reserve(kind="llm", stage="answer", incoming=1000, outgoing=1000)
    budget.finish(call, {"prompt_tokens": 1000, "completion_tokens": 100,
                         "prompt_tokens_details": {"cached_tokens": 900}}, status="completed")
    row = budget.snapshot()["groups"]["llm"]
    assert row["charged_usd"] == pytest.approx((100*.8 + 900*.016 + 100*3.2)/1e6)
    assert row["known_usage_calls"] == 1
    assert row["cached_tokens"] == 900


def test_concurrent_calls_cannot_bypass_limit(tmp_path):
    budget = LiveBudget(tmp_path / "budget.sqlite3", call_limit=3, search_limit=2)
    def reserve(_):
        try:
            return budget.reserve(kind="llm", stage="parallel", incoming=100, outgoing=100)
        except LiveBudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(16)))
    assert sum(result is not None for result in results) == 3
    budget.reserve(kind="search", stage="web")
    budget.reserve(kind="search", stage="web")
    with pytest.raises(LiveBudgetExceeded):
        budget.reserve(kind="search", stage="web")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1])
def test_invalid_limits_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        LiveBudget(tmp_path / "budget.sqlite3", usd_limit=value)


def test_metered_provider_refuses_unpriced_model_without_dispatch(tmp_path):
    class Client:
        default_model = "priced"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            return LLMResponse(provider="fake", status="completed", content="ok",
                               prompt_summary="test", usage={"prompt_tokens": 10, "completion_tokens": 2})

    budget = LiveBudget(tmp_path / "budget.sqlite3")
    client = Client()
    wrapped = MeteredClient(client, budget, allowed_model="priced")
    request = LLMRequest(messages=[LLMMessage(role="user", content="hello")],
                         prompt_summary="test", model="unpriced")
    with pytest.raises(LiveBudgetExceeded):
        asyncio.run(wrapped.complete(request))
    assert client.calls == 0
    asyncio.run(wrapped.complete(request.model_copy(update={"model": "priced"})))
    assert client.calls == 1
    assert budget.snapshot()["groups"]["llm"]["known_usage_calls"] == 1
