"""Offline independent-dispatch regressions with actual graph and resource gates."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier, Event, Lock, Thread, get_ident

import pytest

from app.core.agent_dispatch import AgentDispatchLeaseLost
from app.core.agent_runs import AgentRunStatus
from app.core.config import Settings
from app.core.llm_workloads import WorkBudgetExceeded, current_workload
from app.core.local_config import LocalAppConfig, ManagementConfig
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.session_admission import SessionBusyError


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = LocalAppConfig()
    config.management.enabled = True
    config.agent.orchestrator = "langgraph"
    config.agent.unified_entry_enabled = True
    config.agent.checkpoint_backend = "sqlite"
    config.memory.enabled = False
    config.message_history.enabled = False
    monkeypatch.setattr(Settings, "load_local_config", lambda _: config)
    instance = LocalKnowledgeAgentRuntime(Settings(
        LKA_DATA_DIR=tmp_path / "data", LKA_WORKSPACE_ROOTS=str(tmp_path),
    ))
    instance.agent_dispatch.worker.poll_seconds = 0.01
    yield instance
    instance.stop()


def roots(runtime, count=2):
    return [runtime.create_agent_run(
        session_id=runtime.session_service.create_session(title=f"Parallel task {i}").session.session_id,
        user_input=f"Offline task {i}", resumable=True,
    ) for i in range(count)]


def test_management_defaults_do_not_cap_dispatch_or_widen_global_resources(runtime):
    defaults = ManagementConfig()
    assert not defaults.enabled
    assert not defaults.autonomous_enabled
    assert not defaults.internal_items_enabled
    assert defaults.max_daily_rounds is None
    for name in ("manager_max_calls", "manager_max_tokens", "goal_max_calls", "goal_max_tokens",
                 "execution_max_calls", "execution_max_tokens"):
        assert getattr(defaults, name) is None
    resources = runtime.local_app_config.background
    assert runtime.llm_workloads.max_concurrency == resources.max_llm_concurrency == 4
    assert runtime.llm_workloads.interactive_reserved == resources.interactive_reserved == 2
    assert "background_agent" not in runtime.llm_workloads.pool_limits
    assert runtime.llm_workloads.pool_limits["background_io"] == resources.io_concurrency == 1
    # Legacy configuration remains readable, with no public or runtime pool setting.
    assert "dispatch_workers" not in ManagementConfig(dispatch_workers=8).model_dump()
    from app.core.agent_dispatch import AgentDispatcher

    legacy = AgentDispatcher(runtime.background_job_store, runtime.agent_run_manager,
        execute=lambda *_: None, resume=lambda *_: None, has_checkpoint=lambda _: False, worker_count=8)
    legacy.stop()
    assert runtime.llm_workloads.max_concurrency == 4
    assert runtime.llm_workloads.interactive_reserved == 2


def test_independent_dispatches_enter_independent_async_execution_before_either_completes(runtime, monkeypatch):
    runs = roots(runtime)
    entered = Barrier(3)
    release = Event()
    observations = []
    lock = Lock()

    async def execute(**kwargs):
        run_id = kwargs["existing_run_id"]
        runtime.agent_run_manager.mark_running(run_id)
        with lock:
            observations.append((run_id, kwargs["session_id"], get_ident(), id(asyncio.get_running_loop())))
        await asyncio.to_thread(entered.wait, 5)
        assert await asyncio.to_thread(release.wait, 5)
        runtime.agent_dispatch.check_run(run_id)
        runtime.agent_run_manager.complete_run(run_id)

    monkeypatch.setattr(runtime, "run_agent_turn", lambda **kwargs: asyncio.run(execute(**kwargs)))
    jobs = [runtime.agent_dispatch.enqueue(run.run_id) for run in runs]
    # Retrying a persisted command never creates another dispatch opportunity.
    assert runtime.agent_dispatch.enqueue(runs[0].run_id)["job_id"] == jobs[0]["job_id"]
    runtime.agent_dispatch.start()
    try:
        entered.wait(5)
        assert len(observations) == 2
        assert len({row[1] for row in observations}) == 2
        assert len({row[2] for row in observations}) == 2
        assert len({row[3] for row in observations}) == 2
        assert all(runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.RUNNING for run in runs)
        assert all(runtime.background_job_store.get(job["job_id"])["status"] == "running" for job in jobs)
    finally:
        release.set()
        runtime.agent_dispatch.stop()
    assert {row[0] for row in observations} == {run.run_id for run in runs}
    assert all(runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED for run in runs)
    assert all(runtime.background_job_store.get(job["job_id"])["status"] == "succeeded" for job in jobs)
    assert runtime.agent_dispatch.guards == {}


def test_concurrent_same_session_admission_has_one_root_and_one_dispatch(runtime):
    sid = runtime.session_service.create_session().session.session_id
    gate = Barrier(2)

    def admit(index):
        gate.wait(5)
        try:
            run = runtime.create_agent_run(session_id=sid, user_input=f"Concurrent input {index}", resumable=True)
            return runtime.agent_dispatch.enqueue(run.run_id)
        except SessionBusyError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, range(2)))
    jobs = [result for result in results if isinstance(result, dict)]
    refusals = [result for result in results if isinstance(result, SessionBusyError)]
    assert len(jobs) == len(refusals) == 1
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM agent_runs WHERE session_id=?", (sid,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch' AND scope_id=?", (sid,)).fetchone()[0] == 1
        assert conn.execute("SELECT run_id FROM session_admissions WHERE session_id=?", (sid,)).fetchone()[0] == refusals[0].run_id


def test_parallel_dispatch_preserves_background_pool_and_shared_persistent_quota(runtime, monkeypatch):
    runs = roots(runtime)
    quota = {"scope_id": "parallel-shared-budget", "max_calls": 1, "max_tokens": 100}
    for run in runs:
        runtime.agent_run_manager._update_run(run.run_id, status=run.status, metadata={**run.metadata, "work_budget_scopes": [quota]})
    gate = Barrier(3)
    done = Event()
    lock = Lock()
    seen, admitted, refused = [], [], []
    finished = []

    async def execute(**kwargs):
        run_id = kwargs["existing_run_id"]
        runtime.agent_run_manager.mark_running(run_id)
        scope = current_workload()
        with lock:
            seen.append((run_id, scope.pool, [(q.scope_id, q.max_calls, q.max_tokens) for q in scope.quotas]))
        await asyncio.to_thread(gate.wait, 5)
        try:
            async with runtime.llm_workloads.admit(input_tokens=1, output_tokens=1) as ticket:
                ticket.dispatched = True
                ticket.usage = {"prompt_tokens": 1, "completion_tokens": 1}
                with lock:
                    admitted.append(run_id)
            runtime.agent_run_manager.complete_run(run_id)
        except WorkBudgetExceeded:
            with lock:
                refused.append(run_id)
            runtime.agent_run_manager.fail_run(run_id, error_type="work_budget_exhausted", error="Offline shared quota exhausted")
        finally:
            with lock:
                finished.append(run_id)
                if len(finished) == 2:
                    done.set()

    monkeypatch.setattr(runtime, "run_agent_turn", lambda **kwargs: asyncio.run(execute(**kwargs)))
    jobs = [runtime.agent_dispatch.enqueue(run.run_id) for run in runs]
    runtime.agent_dispatch.start()
    try:
        gate.wait(5)
        assert done.wait(5)
    finally:
        runtime.agent_dispatch.stop()
    assert len(seen) == 2
    assert all(pool == "background_agent" and quotas == [(quota["scope_id"], 1, 100)] for _, pool, quotas in seen)
    assert len(admitted) == len(refused) == 1
    assert runtime.llm_workloads.quota_usage(quota["scope_id"])["total_calls"] == 1
    assert runtime.llm_workloads.pool_limits["background_io"] == 1
    assert all(runtime.background_job_store.get(job["job_id"])["status"] == "succeeded" for job in jobs)


def test_stop_drains_both_dispatches_and_preserves_independent_lease_fences(runtime, monkeypatch):
    runs = roots(runtime)
    entered = Barrier(3)
    release = Event()
    both_pause_requests = Event()
    stopped = Event()
    paused, effects = [], []
    lock = Lock()

    async def execute(**kwargs):
        run_id = kwargs["existing_run_id"]
        runtime.agent_run_manager.mark_running(run_id)
        await asyncio.to_thread(entered.wait, 5)
        assert await asyncio.to_thread(release.wait, 5)
        try:
            runtime.agent_dispatch.check_run(run_id)
        except AgentDispatchLeaseLost:
            runtime.agent_run_manager.mark_cancelled(run_id, reason="dispatch_lease_lost")
            return
        effects.append(run_id)
        runtime.agent_run_manager.complete_run(run_id)

    def request_pause(run):
        with lock:
            paused.append(run.run_id)
            if len(paused) == 2:
                both_pause_requests.set()

    monkeypatch.setattr(runtime, "run_agent_turn", lambda **kwargs: asyncio.run(execute(**kwargs)))
    runtime.agent_dispatch.request_pause = request_pause
    jobs = [runtime.agent_dispatch.enqueue(run.run_id) for run in runs]
    runtime.agent_dispatch.start()
    stopper = None
    try:
        entered.wait(5)
        stopper = Thread(target=lambda: (runtime.agent_dispatch.stop(), stopped.set()))
        stopper.start()
        assert both_pause_requests.wait(5)
        assert not stopped.is_set(), "stop must drain both handlers before returning"
        assert not any(runtime.agent_run_manager.is_cancel_requested(run.run_id) for run in runs)
        runtime.background_job_store.cancel(jobs[0]["job_id"])
    finally:
        release.set()
        if stopper is not None:
            stopper.join(5)
        runtime.agent_dispatch.stop()
    assert stopped.is_set()
    assert set(paused) == {run.run_id for run in runs}
    assert effects == [runs[1].run_id]
    assert runtime.agent_run_manager.get_run(runs[0].run_id).status == AgentRunStatus.CANCELLED
    assert runtime.agent_run_manager.get_run(runs[1].run_id).status == AgentRunStatus.COMPLETED
    assert not runtime.agent_run_manager.is_cancel_requested(runs[1].run_id)
    assert runtime.background_job_store.get(jobs[0]["job_id"])["status"] == "cancelled"
    assert runtime.background_job_store.get(jobs[1]["job_id"])["status"] == "succeeded"
    assert runtime.agent_dispatch.guards == {}
    assert runtime.agent_dispatch.run_one() is False



def test_real_graph_runs_overlap_async_decisions_and_keep_checkpoints_and_publication_separate(runtime, monkeypatch):
    from app.core.agent_graph import AgentGraphRunner
    from app.core.llm import LLMResponse
    from tests.test_unified_entry_quality import operation

    assert isinstance(runtime.agent_turn_runner, AgentGraphRunner)
    assert runtime.agent_turn_runner.checkpoint_runtime is not None
    runs = roots(runtime)
    entered = Barrier(3)
    release, completed = Event(), Event()
    lock = Lock()
    observations, finished_jobs = [], []

    class AsyncScriptedClient:
        async def complete_text(self, **kwargs):
            scope = current_workload()
            run_id = scope.task_id
            assert run_id in {run.run_id for run in runs}
            assert scope.pool == "background_agent"
            stage = kwargs["metadata"]["stage"]
            async with runtime.llm_workloads.admit(input_tokens=1, output_tokens=1) as ticket:
                ticket.dispatched = True
                ticket.usage = {"prompt_tokens": 1, "completion_tokens": 1}
                if stage == "decision":
                    with lock:
                        observations.append((run_id, get_ident(), id(asyncio.get_running_loop())))
                    await asyncio.to_thread(entered.wait, 5)
                    assert await asyncio.to_thread(release.wait, 5)
                    content = operation("final_answer", reason="Offline graph concurrency proof")
                else:
                    assert stage in {"answer", "context_answer"}
                    content = "Answer for " + run_id
                return LLMResponse(provider="isolated", status="completed", content=content,
                                   prompt_summary=kwargs["prompt_summary"])

    runtime.agent_turn_loop.llm_client = AsyncScriptedClient()
    original_complete = runtime.background_job_store.complete

    def capture_completion(job_id, *args, **kwargs):
        result = original_complete(job_id, *args, **kwargs)
        with lock:
            finished_jobs.append(job_id)
            if len(finished_jobs) == 2:
                completed.set()
        return result

    monkeypatch.setattr(runtime.background_job_store, "complete", capture_completion)
    jobs = [runtime.agent_dispatch.enqueue(run.run_id) for run in runs]
    runtime.agent_dispatch.start()
    try:
        entered.wait(5)
        assert {item[0] for item in observations} == {run.run_id for run in runs}
        assert len({item[1] for item in observations}) == 2
        assert len({item[2] for item in observations}) == 2
        assert runtime.llm_workloads.health()["active_by_pool"]["background_agent"] == 2
        assert all(runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.RUNNING for run in runs)
        assert all(not any(message.role == "agent" for message in runtime.session_service.get_session(
            session_id=run.session_id).messages) for run in runs)
        release.set()
        assert completed.wait(10)
    finally:
        release.set()
        runtime.agent_dispatch.stop()
    assert set(finished_jobs) == {job["job_id"] for job in jobs}
    for run, job in zip(runs, jobs, strict=True):
        assert runtime.agent_run_manager.get_run(run.run_id).status == AgentRunStatus.COMPLETED
        assert runtime.background_job_store.get(job["job_id"])["status"] == "succeeded"
        messages = runtime.session_service.get_session(session_id=run.session_id).messages
        assert [message.content for message in messages if message.role == "agent"] == ["Answer for " + run.run_id]
        checkpoint = runtime.agent_turn_runner.get_state(run.run_id)
        assert checkpoint.values["run_id"] == run.run_id
        assert checkpoint.config["configurable"]["thread_id"] == run.run_id
        recovered = runtime.agent_turn_runner._completed_result_from_checkpoint(run.run_id)
        assert recovered.run_id == run.run_id and recovered.answer == "Answer for " + run.run_id
