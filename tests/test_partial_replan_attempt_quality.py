"""A validated recovery patch must not replay a terminal partial attempt."""

import asyncio
import sqlite3

import pytest

from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_turn import AgentTurnLoop
from app.core.multi_agent import (
    ForkPolicy,
    Plan,
    PlanPatch,
    PlanPatchOperation,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    TaskResultStatus,
)
from app.core.multi_agent_scheduler import MultiAgentScheduler
from tests.test_multi_agent_scheduler import FakeChildExecutor, _active_parent, _step


class PartialExecutor(FakeChildExecutor):
    def __init__(self, manager, partial_attempts=1):
        super().__init__(manager)
        self.partial_attempts = partial_attempts

    async def execute(self, **kwargs):
        result = await super().execute(**kwargs)
        child = self.manager.get_run(kwargs["child_run_id"])
        result = result.model_copy(update={"attempt": child.attempt})
        if result.attempt <= self.partial_attempts:
            return result.model_copy(update={
                "status": TaskResultStatus.PARTIAL,
                "missing_requirements": ("child_budget_finish",),
            })
        return result


def setup_recovery(partial_attempts=1, store=None):
    step = _step("leaf").model_copy(update={
        "fork_operation_id": "fork-main", "fork_parent_step_id": "root_coordinator",
        "fork_depth": 1, "created_by_run_id": "parent",
        "budget": RuntimeBudget(
            max_tokens=16_384, max_llm_calls=8, max_tool_calls=3, max_wall_time_seconds=180,
        ),
    })
    manager, parent, _, context = _active_parent(step)
    if store is not None:
        saved_parent = manager.get_run(parent.run_id)
        manager = InMemoryAgentRunManager(durable_store=store)
        parent = manager.restore_run(saved_parent)
    executor = PartialExecutor(manager, partial_attempts)
    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=executor, max_retries=2)
    first = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))
    assert first.status == "failed"
    assert first.task_results[0].status == TaskResultStatus.PARTIAL
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.run_manager = manager
    loop.fork_policy = ForkPolicy(
        max_depth=2, max_children=5, max_fork_size=3, allowed_scope=context.policy_scope,
    )
    loop.fork_scope_resolver = lambda _: (
        context.parent_effective_scope, context.session_scope, context.workspace_scope,
    )
    loop.multi_agent_max_retries = 2
    loop.fork_execution = lambda run_id: asyncio.run(
        scheduler.execute_plan_async(run_id, context=context)
    )
    return manager, parent, context, executor, scheduler, loop


def patch_for(manager, parent, operation):
    plan = Plan.model_validate(manager.get_run(parent.run_id).metadata["multi_agent_plan"])
    return PlanPatch(
        patch_id=f"recover:{plan.patch_revision}", plan_id=plan.plan_id,
        expected_revision=plan.patch_revision, operation=operation,
        target_step_id="leaf", reason="Recover explicit partial termination.",
        reduced_scope=ScopeGrant(
            allowed_packages=("knowledge",), side_effect_level=SideEffectLevel.READ,
        ) if operation == PlanPatchOperation.REDUCED_SCOPE else None,
    ).model_dump(mode="json")


@pytest.mark.parametrize("operation", [
    PlanPatchOperation.RETRY_STEP, PlanPatchOperation.REDUCED_SCOPE,
])
def test_patch_creates_new_partial_attempt_context_and_replay_is_idempotent(operation):
    manager, parent, _, executor, _, loop = setup_recovery()
    old = manager.child_tree(parent.run_id)[0]
    patch = patch_for(manager, parent, operation)
    result = loop._handle_plan_patch_decision(run_id=parent.run_id, operation=patch)
    assert result["status"] == "applied"
    assert result["execution_status"] == "completed"
    children = manager.child_tree(parent.run_id)
    assert len(children) == 2
    new = children[-1]
    assert new.attempt == 2 and new.run_id != old.run_id and new.session_id != old.session_id
    assert new.metadata["context_snapshot"]["snapshot_id"] != old.metadata["context_snapshot"]["snapshot_id"]
    assert new.metadata["context_snapshot"]["budget"] == old.metadata["context_snapshot"]["budget"]
    if operation == PlanPatchOperation.REDUCED_SCOPE:
        assert new.metadata["context_snapshot"]["effective_scope"]["allowed_tools"] == []
        assert old.metadata["context_snapshot"]["effective_scope"]["allowed_tools"] == ["knowledge.search"]
    calls = list(executor.calls)
    replay = loop._handle_plan_patch_decision(run_id=parent.run_id, operation=patch)
    assert replay["execution_status"] == "completed"
    assert len(manager.child_tree(parent.run_id)) == 2 and executor.calls == calls


def test_each_new_patch_consumes_one_attempt_even_after_scheduler_restart():
    manager, parent, context, executor, _, loop = setup_recovery(partial_attempts=2)
    first_patch = patch_for(manager, parent, PlanPatchOperation.RETRY_STEP)
    first = loop._handle_plan_patch_decision(run_id=parent.run_id, operation=first_patch)
    assert first["execution_status"] == "failed"
    assert [child.attempt for child in manager.child_tree(parent.run_id)] == [1, 2]
    # Restart without a new recovery patch: preserve the terminal partial result.
    restarted = MultiAgentScheduler(run_manager=manager, child_executor=executor, max_retries=2)
    asyncio.run(restarted.execute_plan_async(parent.run_id, context=context))
    assert [child.attempt for child in manager.child_tree(parent.run_id)] == [1, 2]
    loop.fork_execution = lambda run_id: asyncio.run(
        restarted.execute_plan_async(run_id, context=context)
    )
    second = loop._handle_plan_patch_decision(
        run_id=parent.run_id,
        operation=patch_for(manager, parent, PlanPatchOperation.REDUCED_SCOPE),
    )
    assert second["execution_status"] == "completed"
    assert [child.attempt for child in manager.child_tree(parent.run_id)] == [1, 2, 3]


def test_new_queued_attempt_is_reused_after_crash_before_context_attachment():
    manager, parent, context, _, scheduler, loop = setup_recovery()
    loop.fork_execution = lambda _: {"status": "running"}
    loop._handle_plan_patch_decision(
        run_id=parent.run_id, operation=patch_for(manager, parent, PlanPatchOperation.RETRY_STEP),
    )
    parent = manager.get_run(parent.run_id)
    plan = Plan.model_validate(parent.metadata["multi_agent_plan"])
    step = next(step for step in plan.steps if step.step_id == "leaf")
    first = asyncio.run(scheduler._prepare_attempt(parent.run_id, parent, plan, step, context, {}))
    assert first[0] == 2
    parent = manager.get_run(parent.run_id)
    second = asyncio.run(scheduler._prepare_attempt(parent.run_id, parent, plan, step, context, {}))
    assert first[1].run_id == second[1].run_id
    assert len(manager.child_tree(parent.run_id)) == 2


def test_durable_restart_consumes_recovery_patch_once(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "recovery.sqlite3")
    manager, parent, context, _, scheduler, loop = setup_recovery(store=store)
    patch = patch_for(manager, parent, PlanPatchOperation.RETRY_STEP)
    loop.fork_execution = lambda _: {"status": "running"}
    loop._handle_plan_patch_decision(run_id=parent.run_id, operation=patch)
    parent = manager.get_run(parent.run_id)
    plan = Plan.model_validate(parent.metadata["multi_agent_plan"])
    step = next(step for step in plan.steps if step.step_id == "leaf")
    prepared = asyncio.run(scheduler._prepare_attempt(parent.run_id, parent, plan, step, context, {}))
    assert prepared[0] == 2
    manager.close()

    restored = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    try:
        executor = PartialExecutor(restored)
        scheduler = MultiAgentScheduler(run_manager=restored, child_executor=executor, max_retries=2)
        result = asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))
        assert result.status == "completed"
        assert [child.attempt for child in restored.child_tree(parent.run_id)] == [1, 2]
        assert result.task_results[0].child_run_id == prepared[1].run_id
        assert executor.calls == [("leaf", 2)]
        loop.run_manager = restored
        loop.fork_execution = lambda run_id: asyncio.run(
            scheduler.execute_plan_async(run_id, context=context)
        )
        replay = loop._handle_plan_patch_decision(run_id=parent.run_id, operation=patch)
        assert replay["execution_status"] == "completed"
        assert len(restored.child_tree(parent.run_id)) == 2
        assert executor.calls == [("leaf", 2)]
    finally:
        restored.close()


def test_rejected_or_unvalidated_patch_event_does_not_create_attempt():
    manager, parent, context, _, scheduler, loop = setup_recovery()
    patch = patch_for(manager, parent, PlanPatchOperation.RETRY_STEP)
    patch["expected_revision"] = 999
    assert loop._handle_plan_patch_decision(run_id=parent.run_id, operation=patch)["status"] == "rejected"
    # A journal payload without validated patch history is not authority.
    manager.append_event(parent.run_id, "multi_agent_plan_patched", "Unvalidated event.",
                         payload={"plan_id": patch["plan_id"], "patch": patch})
    parent = manager.get_run(parent.run_id)
    plan = Plan.model_validate(parent.metadata["multi_agent_plan"])
    step = next(step for step in plan.steps if step.step_id == "leaf")
    prepared = asyncio.run(scheduler._prepare_attempt(parent.run_id, parent, plan, step, context, {}))
    assert prepared[0] == 1 and len(manager.child_tree(parent.run_id)) == 1


@pytest.mark.parametrize("operation", [
    PlanPatchOperation.RETRY_STEP, PlanPatchOperation.REDUCED_SCOPE,
])
def test_sqlite_patch_journal_failure_rolls_back_history_and_cache(tmp_path, operation):
    store = SqliteAgentRunStore(tmp_path / "interrupted-patch.sqlite3")
    manager, parent, context, _, _, loop = setup_recovery(store=store)
    patch = patch_for(manager, parent, operation)
    before = manager.get_run(parent.run_id)
    before_events = manager.list_events(parent.run_id)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("""CREATE TRIGGER interrupt_patch_journal
            BEFORE INSERT ON agent_run_events
            WHEN json_extract(NEW.event_payload, '$.type') = 'multi_agent_plan_patched'
            BEGIN SELECT RAISE(ABORT, 'patch journal write interrupted'); END""")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="patch journal write interrupted"):
            loop._handle_plan_patch_decision(run_id=parent.run_id, operation=patch)
        persisted = store.load_run(parent.run_id)
        # Previously this was history=1, patch_events=0: restart replayed the
        # old partial child because its recovery event was never committed.
        assert persisted["metadata"]["multi_agent_plan"]["patch_revision"] == 0
        assert manager.get_run(parent.run_id) == before
        assert manager.list_events(parent.run_id) == before_events
        assert not any(event["type"] == "multi_agent_plan_patched"
                       for event in store.list_events(parent.run_id))
    finally:
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DROP TRIGGER interrupt_patch_journal")
        manager.close()

    restored = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    try:
        executor = PartialExecutor(restored)
        scheduler = MultiAgentScheduler(run_manager=restored, child_executor=executor, max_retries=2)
        loop.run_manager = restored
        loop.fork_execution = lambda run_id: asyncio.run(
            scheduler.execute_plan_async(run_id, context=context)
        )
        result = loop._handle_plan_patch_decision(run_id=parent.run_id, operation=patch)
        assert result["execution_status"] == "completed"
        assert [child.attempt for child in restored.child_tree(parent.run_id)] == [1, 2]
        assert executor.calls == [("leaf", 2)]
        assert sum(event.type == "multi_agent_plan_patched"
                   for event in restored.list_events(parent.run_id)) == 1
        replay = loop._handle_plan_patch_decision(run_id=parent.run_id, operation=patch)
        assert replay["execution_status"] == "completed"
        assert executor.calls == [("leaf", 2)]
    finally:
        restored.close()


@pytest.mark.parametrize("operation", [
    PlanPatchOperation.RETRY_STEP, PlanPatchOperation.REDUCED_SCOPE,
])
def test_crash_after_atomic_patch_commit_restores_new_attempt_once(tmp_path, monkeypatch, operation):
    store = SqliteAgentRunStore(tmp_path / "committed-patch.sqlite3")
    manager, parent, context, _, _, loop = setup_recovery(store=store)
    patch = patch_for(manager, parent, operation)
    commit = store.save_run_control_batch

    def crash_after_commit(**kwargs):
        commit(**kwargs)
        if any(event["type"] == "multi_agent_plan_patched" for event in kwargs["events"]):
            raise RuntimeError("process interrupted after patch commit")

    monkeypatch.setattr(store, "save_run_control_batch", crash_after_commit)
    try:
        with pytest.raises(RuntimeError, match="interrupted after patch commit"):
            loop._handle_plan_patch_decision(run_id=parent.run_id, operation=patch)
    finally:
        manager.close()

    restored = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    try:
        saved = restored.get_run(parent.run_id)
        assert saved.metadata["multi_agent_plan"]["patch_revision"] == 1
        assert sum(event.type == "multi_agent_plan_patched"
                   for event in restored.list_events(parent.run_id)) == 1
        executor = PartialExecutor(restored)
        scheduler = MultiAgentScheduler(run_manager=restored, child_executor=executor, max_retries=2)
        loop.run_manager = restored
        loop.fork_execution = lambda run_id: asyncio.run(
            scheduler.execute_plan_async(run_id, context=context)
        )
        for _ in range(2):
            replay = loop._handle_plan_patch_decision(run_id=parent.run_id, operation=patch)
            assert replay["execution_status"] == "completed"
        assert [child.attempt for child in restored.child_tree(parent.run_id)] == [1, 2]
        assert executor.calls == [("leaf", 2)]
    finally:
        restored.close()


def test_cancelled_replacement_consumes_patch_without_creating_another_attempt(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "cancelled-replacement.sqlite3")
    manager, parent, context, _, scheduler, loop = setup_recovery(store=store)
    loop.fork_execution = lambda _: {"status": "running"}
    loop._handle_plan_patch_decision(
        run_id=parent.run_id, operation=patch_for(manager, parent, PlanPatchOperation.RETRY_STEP),
    )
    parent = manager.get_run(parent.run_id)
    plan = Plan.model_validate(parent.metadata["multi_agent_plan"])
    step = next(step for step in plan.steps if step.step_id == "leaf")
    _, child, snapshot, views = asyncio.run(
        scheduler._prepare_attempt(parent.run_id, parent, plan, step, context, {})
    )
    manager.attach_child_context(child.run_id, snapshot=snapshot.model_dump(mode="json"),
                                 views=views.model_dump(mode="json"))
    manager.cancel_run(child.run_id, reason="User cancelled this replacement.")
    manager.close()
    restored = InMemoryAgentRunManager(durable_store=SqliteAgentRunStore(store.db_path))
    try:
        parent = restored.get_run(parent.run_id)
        scheduler = MultiAgentScheduler(run_manager=restored, child_executor=PartialExecutor(restored))
        attempt, resumed, _, _ = asyncio.run(
            scheduler._prepare_attempt(parent.run_id, parent, plan, step, context, {})
        )
        assert attempt == 2 and resumed.run_id == child.run_id
        assert resumed.status == AgentRunStatus.CANCELLED
        assert len(restored.child_tree(parent.run_id)) == 2
    finally:
        restored.close()


def test_parent_cancellation_after_patch_commit_does_not_dispatch_replacement():
    manager, parent, context, executor, scheduler, loop = setup_recovery()
    loop.fork_execution = lambda _: {"status": "running"}
    loop._handle_plan_patch_decision(
        run_id=parent.run_id, operation=patch_for(manager, parent, PlanPatchOperation.RETRY_STEP),
    )
    manager.cancel_run(parent.run_id, reason="User cancelled the parent.")
    with pytest.raises(ValueError, match="active parent"):
        asyncio.run(scheduler.execute_plan_async(parent.run_id, context=context))
    assert len(manager.child_tree(parent.run_id)) == 1
    assert executor.calls == [("leaf", 1)]
