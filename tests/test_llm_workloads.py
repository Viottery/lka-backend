import asyncio
import sqlite3

import pytest

from app.core.llm import build_llm_service
from app.core.llm.errors import LLMClientError
from app.core.llm_workloads import (
    BackgroundBudgetDeferred,
    BackgroundTaskBudgetExceeded,
    LLMWorkloadController,
    workload_scope,
)
from app.core.local_config import LLMClientConfig, LLMProviderConfig


def test_foreground_has_reserved_capacity_when_background_provider_is_slow(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db", max_concurrency=2, interactive_reserved=1)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def background():
            with workload_scope("background_memory", task_id="summarize"):
                async with controller.admit(input_tokens=100, output_tokens=10) as ticket:
                    ticket.dispatched = True
                    entered.set()
                    await release.wait()

        task = asyncio.create_task(background())
        await asyncio.wait_for(entered.wait(), timeout=2)
        async with asyncio.timeout(1):
            async with controller.admit(input_tokens=100, output_tokens=10) as ticket:
                ticket.dispatched = True
                ticket.usage = {"prompt_tokens": 80, "completion_tokens": 5}
        release.set()
        await task

    asyncio.run(scenario())
    assert controller.health()["active_by_pool"].get("interactive") == 0
    assert sum(row["calls"] for row in controller.health()["last_24h"]) == 2


def test_budget_survives_restart_and_foreground_still_runs(tmp_path):
    path = tmp_path / "usage.db"
    controller = LLMWorkloadController(path, hourly_tokens=100, daily_tokens=100)

    async def first():
        with workload_scope("background_memory", task_id="learn"):
            async with controller.admit(input_tokens=60, output_tokens=10) as ticket:
                ticket.dispatched = True
                ticket.usage = {"prompt_tokens": 50, "completion_tokens": 10}
    asyncio.run(first())
    restarted = LLMWorkloadController(path, hourly_tokens=100, daily_tokens=100)

    async def second():
        with workload_scope("background_memory", task_id="next"), pytest.raises(BackgroundBudgetDeferred):
            async with restarted.admit(input_tokens=50, output_tokens=10):
                raise AssertionError("must not dispatch")
        async with restarted.admit(input_tokens=200, output_tokens=10) as ticket:
            ticket.dispatched = True
    asyncio.run(second())


def test_task_budget_and_actual_usage_not_output_reservation(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db")

    async def scenario():
        with workload_scope("background_io", task_id="daily", max_tokens=100):
            async with controller.admit(input_tokens=30, output_tokens=40) as ticket:
                ticket.dispatched = True
                ticket.usage = {"input_tokens": 20, "output_tokens": 5}
            with pytest.raises(BackgroundTaskBudgetExceeded):
                async with controller.admit(input_tokens=60, output_tokens=40):
                    raise AssertionError("must not dispatch")
    asyncio.run(scenario())
    with sqlite3.connect(controller.db_path) as conn:
        assert conn.execute("SELECT SUM(input_tokens+output_tokens) FROM llm_workload_usage").fetchone()[0] == 25


def test_cancelled_waiter_does_not_lose_foreground_slots(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db", max_concurrency=1, interactive_reserved=1)

    async def scenario():
        async with controller.admit(input_tokens=1, output_tokens=1):
            async def wait():
                async with controller.admit(input_tokens=1, output_tokens=1):
                    pass
            waiter = asyncio.create_task(wait())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        async with controller.admit(input_tokens=1, output_tokens=1):
            pass
    asyncio.run(scenario())
    assert controller.health()["interactive_waiters"] == 0


def test_service_background_capacity_and_timeout_are_separate_from_foreground(tmp_path):
    service = build_llm_service(LLMProviderConfig(default_client="mock", clients=[
        LLMClientConfig(name="mock", provider="mock", default_model="m", context_window_tokens=5000)
    ]))
    service.workloads = LLMWorkloadController(tmp_path / "usage.db")
    received = []
    provider = service.registry.get("mock")
    original = provider.complete

    async def capture(request):
        received.append(request)
        return await original(request)

    provider.complete = capture

    async def scenario():
        with workload_scope("background_memory", task_id="small"):
            await service.complete_text(system_prompt="s", user_prompt="u", prompt_summary="test", max_output_tokens=100)
            with pytest.raises(LLMClientError, match="exceeds"):
                await service.complete_text(system_prompt="s", user_prompt="u" * 2000, prompt_summary="test", max_output_tokens=100)
        await service.complete_text(system_prompt="s", user_prompt="u", prompt_summary="test")

    asyncio.run(scenario())
    assert len(received) == 2
    assert received[0].metadata["network_timeout_seconds"] == 30
    assert "network_timeout_seconds" not in received[1].metadata
