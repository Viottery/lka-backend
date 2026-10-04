"""Offline co-running quality probes: real queue/admission, controlled provider.

No whole Agent/runtime bootstrap, credentials or network. A cancelled in-flight
provider retains its slot until return: this is measured, not claimed preemptive.
"""

import asyncio
import sqlite3
import threading
import time
from itertools import pairwise
from types import SimpleNamespace

import pytest

from app.core.background_jobs import BackgroundJobStore, BackgroundJobWorker
from app.core.background_llm import complete_text_in_worker
from app.core.llm import build_llm_service
from app.core.llm.errors import LLMTimeoutError
from app.core.llm_workloads import LLMWorkloadController, workload_scope
from app.core.local_config import BackgroundConfig, LLMClientConfig, LLMProviderConfig, MemoryConfig


class ControlledProvider:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.requests = []
        self.timeout = False

    async def complete(self, request):
        self.requests.append(request)
        if request.prompt_summary == "background":
            self.entered.set()
            started = time.monotonic()
            while not self.release.is_set():
                if self.timeout and time.monotonic() - started >= request.metadata["network_timeout_seconds"]:
                    raise LLMTimeoutError("synthetic provider timeout")
                if time.monotonic() - started > 3:
                    raise AssertionError("test failed to release provider")
                await asyncio.sleep(0.005)
        return SimpleNamespace(content="synthetic complete", finish_reason="stop",
                               usage={"prompt_tokens": 20, "completion_tokens": 5})


def harness(tmp_path):
    path = tmp_path / "isolated.sqlite3"
    store = BackgroundJobStore(path)
    store.ensure_schema()
    service = build_llm_service(LLMProviderConfig(default_client="mock", clients=[
        LLMClientConfig(name="mock", provider="mock", default_model="synthetic",
                        context_window_tokens=16384),
    ]))
    config = BackgroundConfig(max_llm_concurrency=2, interactive_reserved=1)
    service.workloads = LLMWorkloadController(
        path, max_concurrency=config.max_llm_concurrency,
        interactive_reserved=config.interactive_reserved,
    )
    provider = ControlledProvider()
    service.registry.get("mock").complete = provider.complete
    return path, store, service, provider


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


def start_job(store, service, *, checkpoint=False):
    item = store.enqueue("memory_extract", "synthetic-session", "answer-1",
                         {"message_id": "answer-1"})
    claims = []

    def handler(job):
        claims.append(job)
        if checkpoint:
            assert store.complete_input(job["job_id"], job["lease_owner"],
                                        job["lease_epoch"], "input-1")

        def dispatch_guard():
            if not store.heartbeat(job["job_id"], job["lease_owner"], job["lease_epoch"]):
                raise RuntimeError("lease no longer valid")

        with workload_scope("background_memory", task_id=job["job_id"], max_tokens=1000,
                            before_dispatch=dispatch_guard):
            complete_text_in_worker(service, system_prompt="synthetic", user_prompt="synthetic",
                                    prompt_summary="background", max_output_tokens=32)

    worker = BackgroundJobWorker(store, {"memory_extract": handler}, poll_seconds=0.01)
    worker.start()
    return item, worker, claims


def test_slow_background_keeps_foreground_heartbeat_tools_and_dispatch_live(tmp_path):
    _, store, service, provider = harness(tmp_path)
    item, worker, _ = start_job(store, service)

    async def scenario():
        await wait_until(provider.entered.is_set)
        ticks = []
        running = True

        async def heartbeat():
            while running:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.005)

        async def competing_background():
            with workload_scope("background_io", task_id="competing"):
                await service.complete_text(system_prompt="s", user_prompt="u",
                                            prompt_summary="competing", max_output_tokens=32)

        ticker = asyncio.create_task(heartbeat())
        competitor = asyncio.create_task(competing_background())
        try:
            await asyncio.sleep(0.03)
            assert not competitor.done()  # The background allowance is saturated.
            async with asyncio.timeout(1):
                for _ in range(5):
                    # Tool-style reads use the actual shared queue database.
                    status = await asyncio.to_thread(store.get, item["job_id"])
                    assert status["status"] == "running"
                    await service.complete_text(system_prompt="synthetic", user_prompt="synthetic",
                                                prompt_summary="foreground", max_output_tokens=32)
                    await asyncio.sleep(0.01)
            assert len(ticks) >= 5
            assert max(b - a for a, b in pairwise(ticks)) < 0.5
            assert not provider.release.is_set()
            assert service.workloads.health()["active_by_pool"]["background_memory"] == 1
        finally:
            competitor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await competitor
            running = False
            await ticker
            provider.release.set()
        await wait_until(lambda: store.get(item["job_id"])["status"] == "succeeded")

    try:
        asyncio.run(scenario())
    finally:
        provider.release.set()
        worker.stop(timeout=2)
    assert len(provider.requests) == 6
    assert not any(request.prompt_summary == "competing" for request in provider.requests)
    assert all(value == 0 for value in service.workloads.health()["active_by_pool"].values())


@pytest.mark.parametrize("terminal", ["cancelled", "deadline_exceeded"])
def test_terminal_job_fences_late_result_but_does_not_preempt_provider(tmp_path, terminal):
    path, store, service, provider = harness(tmp_path)
    item, worker, claims = start_job(store, service, checkpoint=True)

    async def scenario():
        await wait_until(provider.entered.is_set)
        job = claims[0]
        if terminal == "cancelled":
            assert store.cancel(item["job_id"])
        else:
            # Deterministically expire only this isolated fixture's deadline.
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE background_jobs SET deadline=? WHERE job_id=?",
                             ("2000-01-01T00:00:00+00:00", item["job_id"]))
            assert not store.heartbeat(item["job_id"], job["lease_owner"], job["lease_epoch"])
        assert service.workloads.health()["active_by_pool"]["background_memory"] == 1
        assert not store.complete_input(item["job_id"], job["lease_owner"], job["lease_epoch"], "late")
        # Cancellation is not preemption, but the reserved foreground slot works.
        async with asyncio.timeout(1):
            await service.complete_text(system_prompt="s", user_prompt="u",
                                        prompt_summary="foreground", max_output_tokens=32)
        provider.release.set()
        await wait_until(lambda: service.workloads.health()["active_by_pool"]["background_memory"] == 0)

    try:
        asyncio.run(scenario())
    finally:
        provider.release.set()
        worker.stop(timeout=2)
    restarted = BackgroundJobStore(path)
    restarted.ensure_schema()
    state = restarted.get(item["job_id"])
    assert state["status"] == ("cancelled" if terminal == "cancelled" else "failed")
    assert restarted.completed_inputs(item["job_id"]) == {"input-1"}
    assert state["attempts"] == 1
    assert not restarted.complete(item["job_id"], claims[0]["lease_owner"], claims[0]["lease_epoch"])


def test_provider_timeout_restart_preserves_checkpoint_usage_and_retry_identity(tmp_path):
    path, store, service, provider = harness(tmp_path)
    # Same configured timeout path, shortened solely in the isolated fixture.
    assert BackgroundConfig().request_timeout_seconds == 30
    assert MemoryConfig().background_worker_count == 2
    service.background_timeout_seconds = 0.05
    provider.timeout = True
    item, worker, claims = start_job(store, service, checkpoint=True)
    try:
        asyncio.run(wait_until(lambda: store.get(item["job_id"])["status"] == "retry_wait"))
    finally:
        provider.release.set()
        worker.stop(timeout=2)
    restarted = BackgroundJobStore(path)
    restarted.ensure_schema()
    state = restarted.get(item["job_id"])
    assert state["error_class"] == "provider_timeout" and state["attempts"] == 1
    assert restarted.completed_inputs(item["job_id"]) == {"input-1"}
    controller = LLMWorkloadController(path, max_concurrency=2, interactive_reserved=1)
    with sqlite3.connect(path) as conn:
        usage = conn.execute("SELECT status,count_method,input_tokens+output_tokens "
                             "FROM llm_workload_usage WHERE task_id=?", (item["job_id"],)).fetchone()
    assert usage[0:2] == ("failed", "conservative_estimate")
    assert usage[2] > 0
    assert all(value == 0 for value in controller.health()["active_by_pool"].values())
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE background_jobs SET available_at=? WHERE job_id=?",
                     ("2000-01-01T00:00:00+00:00", item["job_id"]))
    successor = restarted.claim("restarted", 60)
    assert successor["job_id"] == item["job_id"] and successor["attempts"] == 2
    assert successor["lease_epoch"] > claims[0]["lease_epoch"]
    assert not restarted.complete_input(item["job_id"], claims[0]["lease_owner"],
                                         claims[0]["lease_epoch"], "stale")
    assert restarted.complete_input(item["job_id"], "restarted", successor["lease_epoch"], "input-2")
    assert restarted.complete(item["job_id"], "restarted", successor["lease_epoch"])
