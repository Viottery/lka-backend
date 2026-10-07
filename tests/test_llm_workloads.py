import asyncio
import sqlite3

import pytest

from app.core.llm import build_llm_service
from app.core.llm.errors import LLMClientError
from app.core.llm_workloads import (
    BackgroundBudgetDeferred,
    BackgroundCircuitOpen,
    BackgroundTaskBudgetExceeded,
    BudgetQuota,
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
        with workload_scope("background_message", task_id="learn"):
            async with controller.admit(input_tokens=60, output_tokens=10) as ticket:
                ticket.dispatched = True
                ticket.usage = {"prompt_tokens": 50, "completion_tokens": 10}
    asyncio.run(first())
    restarted = LLMWorkloadController(path, hourly_tokens=100, daily_tokens=100)

    async def second():
        with workload_scope("background_message", task_id="next"), pytest.raises(BackgroundBudgetDeferred):
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


def test_memory_has_no_ordinary_quota_and_does_not_charge_message_pool(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db", hourly_tokens=10, daily_tokens=10, daily_cost_limit=1)

    async def run():
        with workload_scope("background_memory", task_id="compact"):
            async with controller.admit(input_tokens=200, output_tokens=50) as ticket:
                ticket.dispatched = True
        # No price means the message pool is still properly blocked by its cost
        # policy; it does not silently borrow the memory pool's exemption.
        with workload_scope("background_message"), pytest.raises(BackgroundBudgetDeferred, match="pricing"):
            async with controller.admit(input_tokens=1, output_tokens=1):
                pass
    asyncio.run(run())
    states = {row["pool"]: row for row in controller.health()["budget_pools"]}
    assert states["background_memory"]["daily"]["tokens"] == 250
    assert states["background_message"]["daily"]["tokens"] == 0


@pytest.mark.parametrize("threshold", ["task", "hourly", "daily"])
def test_actual_usage_opens_persistent_circuit_and_blocks_publication(tmp_path, threshold):
    path = tmp_path / "usage.db"
    limits = {f"memory_{threshold}_fuse_tokens": 100}
    controller = LLMWorkloadController(path, **limits)
    published = []

    async def run():
        with workload_scope("background_memory", task_id="extract"), pytest.raises(BackgroundCircuitOpen):
            async with controller.admit(input_tokens=10, output_tokens=10) as ticket:
                ticket.dispatched = True
                ticket.usage = {"prompt_tokens": 95, "completion_tokens": 10}
            published.append(True)
    asyncio.run(run())
    restarted = LLMWorkloadController(path, **limits)

    async def blocked():
        with workload_scope("background_memory", task_id="other"), pytest.raises(BackgroundCircuitOpen):
            async with restarted.admit(input_tokens=1, output_tokens=1):
                pass
        async with restarted.admit(input_tokens=1, output_tokens=1) as ticket:
            ticket.dispatched = True
    asyncio.run(blocked())
    assert not published
    assert len([event for event in restarted.health()["budget_events"] if event["kind"] == "circuit_open"]) == 1
    assert restarted.quota_usage("task:extract")["total_tokens"] == 105


def test_projected_usage_emergency_cancels_only_same_pool(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db", memory_concurrency=2, memory_hourly_fuse_tokens=100)

    async def run():
        entered = asyncio.Event()

        async def slow():
            with workload_scope("background_memory", task_id="slow"):
                async with controller.admit(input_tokens=50, output_tokens=10) as ticket:
                    ticket.dispatched = True
                    entered.set()
                    await asyncio.Event().wait()
        pending = asyncio.create_task(slow())
        await asyncio.wait_for(entered.wait(), 2)
        with workload_scope("background_memory", task_id="second"), pytest.raises(BackgroundCircuitOpen):
            async with controller.admit(input_tokens=50, output_tokens=10):
                pass
        with pytest.raises(BackgroundCircuitOpen):
            await asyncio.wait_for(pending, 2)
        with workload_scope("background_message"):
            async with controller.admit(input_tokens=1, output_tokens=1) as ticket:
                ticket.dispatched = True
        async with controller.admit(input_tokens=1, output_tokens=1):
            pass
    asyncio.run(run())
    assert sum(controller.health()["active_by_pool"].values()) == 0
    assert controller.health()["budget_pools"][2]["circuit_open"] is False  # message, alphabetically
    with sqlite3.connect(controller.db_path) as conn:
        assert conn.execute("SELECT status FROM llm_workload_usage WHERE task_id='slow'").fetchone()[0] == "failed"


def test_reset_reopens_circuit_preserves_history_work_caps_and_adoption(tmp_path):
    path = tmp_path / "usage.db"
    controller = LLMWorkloadController(path, hourly_tokens=100, daily_tokens=100, memory_task_fuse_tokens=100)

    async def run():
        with workload_scope("background_message", task_id="message", max_tokens=80, quotas=(BudgetQuota("message-service", hourly_tokens=100),)):
            async with controller.admit(input_tokens=50, output_tokens=10) as ticket:
                ticket.dispatched = True
        with workload_scope("background_memory", task_id="memory"), pytest.raises(BackgroundCircuitOpen):
            async with controller.admit(input_tokens=101, output_tokens=1):
                pass
    asyncio.run(run())
    with pytest.raises(RuntimeError, match="revision"):
        controller.reset_budgets({"background_memory": 0, "background_message": 0}, reason="stale")
    assert controller.health()["budget_pools"][2]["revision"] == 0  # atomic failure
    controller.reset_budgets({"background_memory": 1, "background_message": 0}, reason="user requested reset")
    assert controller.quota_usage("message-service")["hourly_tokens"] == 0
    assert controller.quota_usage("task:message")["total_tokens"] == 60
    restarted = LLMWorkloadController(path, hourly_tokens=100, daily_tokens=100)
    restarted.adopt_task_usage(("message",), ("message-service", "new-scope"))
    assert restarted.quota_usage("new-scope")["hourly_tokens"] == 0
    assert restarted.quota_usage("new-scope")["total_tokens"] == 60
    assert sum(row["calls"] for row in restarted.health()["last_24h"]) == 1

    async def resumed():
        with workload_scope("background_memory", task_id="memory"):
            async with restarted.admit(input_tokens=1, output_tokens=1) as ticket:
                ticket.dispatched = True
        with workload_scope("background_message", task_id="message", max_tokens=80), pytest.raises(BackgroundTaskBudgetExceeded):
            async with restarted.admit(input_tokens=25, output_tokens=1):
                pass
    asyncio.run(resumed())


def test_reset_refuses_inflight_and_nonbackground_targets(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db")
    with pytest.raises(ValueError):
        controller.reset_budgets({"interactive": 0}, reason="not permitted")

    async def run():
        with workload_scope("background_memory"):
            async with controller.admit(input_tokens=1, output_tokens=1):
                with pytest.raises(RuntimeError, match="active"):
                    await asyncio.to_thread(controller.reset_budgets, {"background_memory": 0}, reason="inflight")
    asyncio.run(run())


def test_legacy_attribution_migration_preserves_costs_and_quota_links(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db")

    async def run():
        for task_id in ("legacy-message", "memory-extract"):
            with workload_scope("background_memory", task_id=task_id):
                async with controller.admit(input_tokens=10, output_tokens=5) as ticket:
                    ticket.dispatched = True
    asyncio.run(run())
    controller.adopt_task_usage(("legacy-message",), ("message-service",))
    controller.reclassify_task_usage(("legacy-message",), "background_message")
    controller.reclassify_task_usage(("legacy-message",), "background_message")
    assert controller.quota_usage("message-service")["total_tokens"] == 15
    states = {row["pool"]: row for row in controller.health()["budget_pools"]}
    assert states["background_message"]["daily"]["tokens"] == 15
    assert states["background_memory"]["daily"]["tokens"] == 15
