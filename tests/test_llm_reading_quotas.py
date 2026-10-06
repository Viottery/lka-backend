"""Offline quota admission tests; no provider requests or message payloads."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.llm import build_llm_service
from app.core.llm.errors import LLMTimeoutError
from app.core.llm_workloads import (
    BackgroundBudgetDeferred,
    BudgetPricing,
    BudgetQuota,
    LLMWorkloadController,
    UsageTicket,
    WorkBudgetExceeded,
    Workload,
    current_workload,
    workload_scope,
)
from app.core.local_config import LLMClientConfig, LLMProviderConfig


def ticket(*quotas, incoming=10, outgoing=10, **kwargs):
    return UsageTicket(uuid4().hex, Workload("background_memory", quotas=quotas, **kwargs), incoming, outgoing)


def test_hierarchy_reserves_atomically_and_releases_every_scope(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db")
    rejected = ticket(BudgetQuota("service", hourly_calls=3), BudgetQuota("work", max_calls=0))
    with pytest.raises(WorkBudgetExceeded):
        controller._reserve(rejected)
    assert controller.quota_usage("service")["total_calls"] == 0
    admitted = ticket(BudgetQuota("service"), BudgetQuota("conversation"), BudgetQuota("work", max_calls=1))
    controller._reserve(admitted)
    with sqlite3.connect(controller.db_path) as conn:
        assert conn.execute("SELECT COUNT(DISTINCT call_id),COUNT(*) FROM llm_workload_quota_usage").fetchone() == (1, 3)
    controller._finish(admitted)
    controller._finish(admitted)  # release is idempotent
    for scope in ("service", "conversation", "work"):
        assert controller.quota_usage(scope)["total_calls"] == 0


def test_concurrent_controllers_cannot_oversell_any_scope(tmp_path):
    path = tmp_path / "usage.db"
    controllers = [LLMWorkloadController(path), LLMWorkloadController(path)]

    def reserve(index):
        call = ticket(BudgetQuota("service", hourly_calls=1), BudgetQuota(f"work-{index}", max_calls=1))
        try:
            controllers[index % 2]._reserve(call)
            return True
        except BackgroundBudgetDeferred:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(results) == 1
    assert controllers[0].quota_usage("service")["total_calls"] == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_workload_usage").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM llm_workload_quota_usage").fetchone()[0] == 2


def test_shared_limit_rolls_back_subquota_and_lifetime_is_permanent(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db", hourly_tokens=25)
    first = ticket(BudgetQuota("work", max_calls=1))
    controller._reserve(first)
    first.dispatched = True
    controller._finish(first)
    with pytest.raises(BackgroundBudgetDeferred):
        controller._reserve(ticket(BudgetQuota("fresh")))
    assert controller.quota_usage("fresh")["total_calls"] == 0
    with pytest.raises(WorkBudgetExceeded) as error:
        controller._reserve(ticket(BudgetQuota("work", max_calls=1)))
    assert error.value.error_category == "work_budget_exhausted"


def test_rolling_window_expires_but_retention_and_restart_keep_totals(tmp_path):
    path = tmp_path / "usage.db"
    controller = LLMWorkloadController(path)
    first = ticket(BudgetQuota("service", hourly_calls=1), BudgetQuota("work", max_calls=1))
    controller._reserve(first)
    first.dispatched = True
    controller._finish(first)
    with pytest.raises(BackgroundBudgetDeferred):
        controller._reserve(ticket(BudgetQuota("service", hourly_calls=1)))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_workload_usage SET created_at=?", ((datetime.now(UTC) - timedelta(days=91)).isoformat(),))
    restarted = LLMWorkloadController(path)
    second = ticket(BudgetQuota("service", hourly_calls=1))
    restarted._reserve(second)  # compacts old row and its associations
    assert restarted.quota_usage("service")["total_calls"] == 2
    assert restarted.quota_usage("service")["hourly_calls"] == 1
    assert restarted.work_remaining("work", max_tokens=30, max_calls=1) == {"tokens": 10, "calls": 0}
    with pytest.raises(WorkBudgetExceeded):
        restarted._reserve(ticket(BudgetQuota("work", max_calls=1)))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_workload_usage").fetchone()[0] == 1


def test_settlement_and_timeout_estimates_charge_original_work(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db")
    quotas = (BudgetQuota("work", max_tokens=45, max_calls=2),)
    first = ticket(*quotas)
    controller._reserve(first)
    first.dispatched = True
    first.usage = {"input_tokens": 7, "output_tokens": 3}
    controller._finish(first)
    controller._finish(first)  # settlement is idempotent
    retry = ticket(*quotas)
    controller._reserve(retry)
    retry.dispatched = retry.failed = True
    controller._finish(retry)
    assert controller.quota_usage("work")["total_tokens"] == 30
    assert controller.quota_usage("work")["total_calls"] == 2
    with pytest.raises(WorkBudgetExceeded):
        controller._reserve(ticket(*quotas))


def test_crashed_reservation_stays_charged_after_expiry(tmp_path):
    path = tmp_path / "usage.db"
    controller = LLMWorkloadController(path)
    first = ticket(BudgetQuota("work", max_calls=1))
    controller._reserve(first)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_workload_usage SET expires_at=?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
    restarted = LLMWorkloadController(path)
    other = ticket(BudgetQuota("other"))
    restarted._reserve(other)
    with pytest.raises(WorkBudgetExceeded):
        restarted._reserve(ticket(BudgetQuota("work", max_calls=1)))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM llm_workload_usage WHERE call_id=?", (first.call_id,)).fetchone()[0] == "estimated"
    assert restarted.quota_usage("work")["total_tokens"] == 20


def test_unknown_and_revisioned_model_prices(tmp_path):
    unknown = LLMWorkloadController(tmp_path / "unknown.db", daily_cost_limit=1, input_cost_per_million=1)
    with pytest.raises(BackgroundBudgetDeferred) as error:
        unknown._reserve(ticket(BudgetQuota("service")))
    assert error.value.error_category == "pricing_unknown"
    unlimited = LLMWorkloadController(tmp_path / "free.db")
    call = ticket(BudgetQuota("service"))
    unlimited._reserve(call)
    assert unlimited.health()["last_24h"][0]["estimated_cost"] is None
    pricing = BudgetPricing("provider", "model", "r2", 2, 4)
    priced = ticket(BudgetQuota("service"), pricing=pricing)
    unknown._reserve(priced)
    priced.dispatched = True
    priced.usage = {"prompt_tokens": 5, "completion_tokens": 3}
    unknown._finish(priced)
    with sqlite3.connect(unknown.db_path) as conn:
        row = conn.execute("SELECT cost,cost_known,pricing_client,pricing_model,pricing_revision FROM llm_workload_usage").fetchone()
    assert row == (22 / 1_000_000, 1, "provider", "model", "r2")


def test_adopt_failed_legacy_task_usage_is_idempotent_per_scope(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db")
    legacy = ticket(task_id="old-job")
    controller._reserve(legacy)
    legacy.dispatched = legacy.failed = True
    controller._finish(legacy)
    existing = ticket(BudgetQuota("service"), task_id="replacement-job")
    controller._reserve(existing)
    existing.dispatched = True
    existing.usage = {"input_tokens": 5, "output_tokens": 3}
    controller._finish(existing)
    task_before = controller.quota_usage("task:old-job")
    controller.adopt_task_usage(("old-job", "replacement-job"), ("service", "family"))
    controller.adopt_task_usage(("old-job", "replacement-job", "old-job"), ("family", "service", "family"))
    for scope in ("service", "family"):
        usage = controller.quota_usage(scope)
        assert usage["total_calls"] == usage["daily_calls"] == 2
        assert usage["total_tokens"] == usage["daily_tokens"] == 28
    assert controller.quota_usage("task:old-job") == task_before
    with sqlite3.connect(controller.db_path) as conn:
        assert conn.execute("SELECT status FROM llm_workload_usage WHERE call_id=?", (legacy.call_id,)).fetchone()[0] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM llm_workload_usage").fetchone()[0] == 2
    with pytest.raises(WorkBudgetExceeded):
        controller._reserve(ticket(BudgetQuota("family", max_calls=2)))


def test_adopt_compacted_legacy_lifetime_usage_and_later_compaction(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.db")
    legacy = ticket(task_id="old-job")
    controller._reserve(legacy)
    legacy.dispatched = legacy.failed = True
    controller._finish(legacy)
    cutoff = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    with sqlite3.connect(controller.db_path) as conn:
        conn.execute("UPDATE llm_workload_usage SET created_at=? WHERE call_id=?", (cutoff, legacy.call_id))
    controller._reserve(ticket(BudgetQuota("unrelated")))  # compact old detail
    for _ in range(2):
        controller.adopt_task_usage(("old-job",), ("family", "service"))
    for scope in ("family", "service"):
        assert controller.quota_usage(scope)["total_tokens"] == 20
        assert controller.quota_usage(scope)["total_calls"] == 1
        assert controller.quota_usage(scope)["daily_calls"] == 0
    assert controller.quota_usage("task:old-job")["total_tokens"] == 20

    # A retained new call is adopted once, then becomes compacted itself.
    newer = ticket(task_id="old-job", incoming=5, outgoing=3)
    controller._reserve(newer)
    newer.dispatched = True
    controller._finish(newer)
    controller.adopt_task_usage(("old-job",), ("family",))
    with sqlite3.connect(controller.db_path) as conn:
        conn.execute("UPDATE llm_workload_usage SET created_at=? WHERE call_id=?", (cutoff, newer.call_id))
    controller._reserve(ticket(BudgetQuota("unrelated")))
    for _ in range(2):
        controller.adopt_task_usage(("old-job",), ("family", "service"))
    for scope in ("family", "service"):
        assert controller.quota_usage(scope)["total_tokens"] == 28
        assert controller.quota_usage(scope)["total_calls"] == 2
    with pytest.raises(WorkBudgetExceeded):
        controller._reserve(ticket(BudgetQuota("family", max_calls=2)))


def test_dispatch_fence_releases_reservation_and_service_retries_are_counted(tmp_path, monkeypatch):
    service = build_llm_service(LLMProviderConfig(default_client="mock", clients=[
        LLMClientConfig(name="mock", provider="mock", default_model="m")
    ]))
    controller = LLMWorkloadController(tmp_path / "usage.db")
    calls = []

    @asynccontextmanager
    async def admission(request):
        call = UsageTicket(uuid4().hex, current_workload(), 10, 10)
        controller._reserve(call)
        try:
            yield call
        finally:
            controller._finish(call)

    async def provider(request):
        calls.append(request)
        raise LLMTimeoutError("offline timeout")

    async def fence():
        raise BackgroundBudgetDeferred("paused")

    monkeypatch.setattr(service, "_admission", admission)
    monkeypatch.setattr(service.registry.get("mock"), "complete", provider)

    async def scenario():
        quota = BudgetQuota("work", max_calls=2)
        with workload_scope("background_memory", quotas=(quota,), before_dispatch=fence), pytest.raises(BackgroundBudgetDeferred):
            await service.complete_text(system_prompt="s", user_prompt="u", prompt_summary="offline")
        assert controller.quota_usage("work")["total_calls"] == 0
        with workload_scope("background_memory", quotas=(quota,)):
            for _ in range(2):
                with pytest.raises(LLMTimeoutError):
                    await service.complete_text(system_prompt="s", user_prompt="u", prompt_summary="offline")
            with pytest.raises(WorkBudgetExceeded):
                await service.complete_text(system_prompt="s", user_prompt="u", prompt_summary="offline")

    asyncio.run(scenario())
    assert len(calls) == 2
    assert controller.quota_usage("work")["total_calls"] == 2
