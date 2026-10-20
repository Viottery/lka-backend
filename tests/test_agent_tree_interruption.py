"""Task-tree controls against disposable SQLite, real graphs/tools and scripted LLMs."""

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException

from app.api.routes.agent import resume_agent_run
from app.core.agent_graph import AgentTurnPaused
from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.multi_agent import (
    ForkPolicy,
    PlanStepStatus,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
)
from app.core.multi_agent_scheduler import MultiAgentScheduler
from tests.test_agent_interruption import pause_after_write, request_for, run_existing
from tests.test_multi_agent_scheduler import FakeChildExecutor, _active_parent, _step
from tests.test_unified_entry_quality import ScriptedClient, operation, runtime_for


def child(manager, parent, step, *, executor_kind="react"):
    return manager.create_child_run(parent_run_id=parent.run_id, plan_id="tree", step_id=step,
                                   attempt=1, user_input=step, executor_kind=executor_kind)


def test_nested_tree_pause_is_durable_and_resume_preserves_waits(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "runs.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    root = manager.create_run(session_id="tree", user_input="Work")
    manager.mark_running(root.run_id)
    middle = child(manager, root, "middle")
    manager.mark_child_running(middle.run_id)
    leaf = child(manager, middle, "leaf")
    manager.mark_child_running(leaf.run_id)
    manager.mark_waiting_confirmation(leaf.run_id, "pending-review")
    finished = child(manager, root, "finished")
    manager.mark_child_running(finished.run_id)
    manager.complete_child_run(finished.run_id, result_snapshot={"answer": "Keep me"})
    manager.request_pause(root.run_id)
    assert manager.acknowledge_pause(root.run_id).status == AgentRunStatus.RUNNING
    assert manager.acknowledge_pause(middle.run_id).status == AgentRunStatus.RUNNING
    with pytest.raises(ValueError, match="pausing"):
        child(manager, middle, "late-fork")
    manager.acknowledge_pause(leaf.run_id)
    assert manager.get_run(root.run_id).status == AgentRunStatus.PAUSED
    manager.close()
    reopened = InMemoryAgentRunManager(durable_store=store)
    try:
        reopened.resume_paused(root.run_id)
        assert reopened.get_run(middle.run_id).status == AgentRunStatus.RUNNING
        assert reopened.get_run(leaf.run_id).status == AgentRunStatus.WAITING_CONFIRMATION
        assert reopened.get_run(leaf.run_id).metadata["confirmation_id"] == "pending-review"
        assert reopened.get_run(finished.run_id).result_snapshot["answer"] == "Keep me"
    finally:
        reopened.close()


def test_tree_replace_is_atomic_and_preserves_child_trace(tmp_path, monkeypatch):
    store = SqliteAgentRunStore(tmp_path / "runs.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    root = manager.create_run(session_id="tree", user_input="Work")
    manager.mark_running(root.run_id)
    worker = child(manager, root, "worker")
    manager.append_event(worker.run_id, "tool_completed", "Already wrote a file.",
                         payload={"tool_name": "filesystem.edit_file"})
    manager.request_pause(root.run_id)
    manager.acknowledge_pause(worker.run_id)
    manager.acknowledge_pause(root.run_id)
    original = store.save_run_control_batch
    attempted_new = []

    def fail(**kwargs):
        attempted_new.extend(item["run_id"] for item in kwargs.get("created", []))
        events = [dict(event) for event in kwargs["events"]]
        events[-1]["sequence"] = 9999
        original(**{**kwargs, "events": events})

    monkeypatch.setattr(store, "save_run_control_batch", fail)
    with pytest.raises(ValueError, match="sequence"):
        manager.replace_paused(root.run_id, user_input="New", command_id="replace", request_fingerprint="f")
    assert manager.get_run(worker.run_id).status == AgentRunStatus.PAUSED
    assert manager.get_run(root.run_id).status == AgentRunStatus.PAUSED
    assert store.load_run(worker.run_id)["status"] == "paused"
    assert all(store.load_run(identifier) is None for identifier in attempted_new)
    monkeypatch.setattr(store, "save_run_control_batch", original)
    new = manager.replace_paused(root.run_id, user_input="New", command_id="replace", request_fingerprint="f")
    assert manager.get_run(worker.run_id).status == AgentRunStatus.CANCELLED
    assert manager.get_run(worker.run_id).metadata["superseded_by_run_id"] == new.run_id
    assert manager.get_run(root.run_id).child_run_ids == (worker.run_id,)
    assert any(event.type == "tool_completed" for event in manager.list_events(worker.run_id))
    # Neither a delayed completion nor restoring an old checkpoint revives the old child.
    manager.complete_child_run(worker.run_id, result_snapshot={"answer": "late"})
    manager.restore_run(worker)
    assert manager.get_run(worker.run_id).status == AgentRunStatus.CANCELLED
    assert manager.replace_paused(root.run_id, user_input="New", command_id="replace",
                                  request_fingerprint="f").run_id == new.run_id
    manager.close()


def test_scheduler_pause_is_not_a_failure_and_reuses_attempt():
    manager, root, _, context = _active_parent(_step("a"), _step("b", "a"))
    executor = FakeChildExecutor(manager)
    paused_ids = []

    async def run_child(**kwargs):
        identifier = kwargs["child_run_id"]
        if not paused_ids:
            paused_ids.append(identifier)
            manager.request_pause(root.run_id)
            manager.acknowledge_pause(identifier)
            raise AgentTurnPaused(identifier)
        return await executor.execute(**kwargs)

    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=executor, child_runner=run_child)
    paused = asyncio.run(scheduler.execute_plan_async(root.run_id, context=context))
    assert paused.status == "paused" and not paused.replan_required
    assert paused.plan.steps[1].status == PlanStepStatus.RUNNING
    assert len(manager.get_run(root.run_id).child_run_ids) == 1
    manager.acknowledge_pause(root.run_id)
    manager.resume_paused(root.run_id)
    completed = asyncio.run(scheduler.execute_plan_async(root.run_id, context=context))
    assert completed.status == "completed"
    assert len(manager.get_run(root.run_id).child_run_ids) == 2
    assert manager.get_run(paused_ids[0]).attempt == 1


def fork_runtime(tmp_path, monkeypatch, *, orchestrator="langgraph"):
    path = tmp_path / "child-file.txt"
    path.write_text("BEFORE", encoding="utf-8")
    fork = operation("fork_subtasks", operation_id="fork-write", parent_step_id="root_coordinator",
                     subtasks=[{"step_id": "write", "objective": "Edit the specified child file.",
                                "output_contract": "State whether the file was edited.",
                                "requested_scope": {"allowed_packages": ["filesystem"],
                                    "allowed_tools": ["filesystem.edit_file"],
                                    "workspace_paths": [str(tmp_path)], "side_effect_level": "write"}}])
    runtime, client = runtime_for(tmp_path, monkeypatch, orchestrator, [
        fork,
        operation("tool_call", tool_name="filesystem.edit_file", tool_input={"path": str(path),
                  "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "edits": [{"old_text": "BEFORE", "new_text": "AFTER"}]}),
        operation("final_answer", reason="File edited."), "The file was edited.",
        operation("final_answer", reason="Child result checked."), "Child task completed.",
    ])
    runtime.agent_turn_loop.fork_policy = ForkPolicy(max_depth=2, max_children=4, max_fork_size=3,
        coordinator_budget=RuntimeBudget(max_tokens=200_000, max_llm_calls=16, max_tool_calls=16),
        allowed_scope=ScopeGrant(allowed_packages=("filesystem",), allowed_tools=("filesystem.edit_file",),
                                workspace_paths=(str(tmp_path),), side_effect_level=SideEffectLevel.WRITE))
    root = runtime.create_agent_run(session_id="tree-graph", user_input="Delegate the file edit.", resumable=True)
    tool = runtime.tool_registry.get_tool("filesystem.edit_file")
    original = tool.invoke
    calls = []

    def write_pause(**kwargs):
        result = original(**kwargs)
        calls.append(result)
        runtime.interrupt_agent_run(root.run_id)
        return result

    monkeypatch.setattr(tool, "invoke", write_pause)
    with pytest.raises(AgentTurnPaused):
        run_existing(runtime, root)
    return runtime, client, root, path, calls


@pytest.mark.parametrize("orchestrator", ["langgraph", "legacy"])
def test_real_child_write_pause_resume_does_not_replay(tmp_path, monkeypatch, orchestrator):
    runtime, _, root, path, calls = fork_runtime(tmp_path, monkeypatch, orchestrator=orchestrator)
    try:
        children = runtime.agent_run_manager.child_tree(root.run_id)
        assert len(children) == 1
        assert children[0].status == AgentRunStatus.PAUSED
        assert runtime.agent_run_manager.get_run(root.run_id).status == AgentRunStatus.PAUSED
        assert path.read_text() == "AFTER" and len(calls) == 1
        assert runtime.resume_agent_run(root.run_id).answer == "Child task completed."
        assert len(runtime.agent_run_manager.child_tree(root.run_id)) == 1
        assert len(calls) == 1 and path.read_text() == "AFTER"
        assert runtime.agent_run_manager.get_run(children[0].run_id).status == AgentRunStatus.COMPLETED
    finally:
        runtime.stop()


def test_replacement_handoff_contains_child_tool_input_and_cache(tmp_path, monkeypatch):
    runtime, _, root, path, calls = fork_runtime(tmp_path, monkeypatch)
    try:
        children = runtime.agent_run_manager.child_tree(root.run_id)
        events_before = runtime.agent_run_manager.list_events(children[0].run_id)
        new = runtime.replace_agent_run(root.run_id, user_input="Inspect what was changed; do not undo it.",
                                        command_id="replace", request_fingerprint="fingerprint")
        old = runtime.agent_run_manager.get_run(root.run_id)
        assert old.metadata["dag_retired"]
        assert old.metadata["multi_agent_plan"]["status"] == "cancelled"
        assert old.metadata["multi_agent_plan"]["steps"][1]["status"] == "cancelled"
        assert runtime.agent_run_manager.get_run(children[0].run_id).status == AgentRunStatus.CANCELLED
        runtime.agent_turn_loop.llm_client = ScriptedClient([
            operation("expand_package", package_name="observation"),
            operation("tool_call", tool_name="observation.read", tool_input={
                "artifact_id": f"replacement_handoff_{new.run_id}", "path": "/output/operations"}),
            operation("final_answer", reason="Prior edit inspected."), "The file has already been edited.",
        ])
        result = run_existing(runtime, new)
        snapshot = runtime._runner_for_turn(existing_run_id=new.run_id).get_state(new.run_id)
        data = runtime.agent_turn_runner._load_data(snapshot.values["runtime_artifact_ref"])
        handoff = next(item for item in data["observations"] if item.get("action") == "replacement_handoff")
        assert handoff["operation_count"] == 1
        assert handoff["operations"][0]["tool_name"] == "filesystem.edit_file"
        assert handoff["operations"][0]["input"]["path"] == str(path)
        assert handoff["operations"][0]["read_only"] is False
        artifact = runtime.agent_run_store.load_tool_result_artifact(handoff["_result_cache"]["artifact_id"], new.run_id)
        assert artifact["output"]["operations"][0]["input"]["edits"][0]["old_text"] == "BEFORE"
        assert result.tool_events[0].tool_name == "observation.read"
        assert result.tool_events[0].result["status"] == "completed"
        assert all(event in runtime.agent_run_manager.list_events(children[0].run_id) for event in events_before)
        assert len(calls) == 1 and path.read_text() == "AFTER"
        assert "edited" in result.answer
    finally:
        runtime.stop()


def test_explicit_user_rollback_uses_history_without_deleting_trace(tmp_path, monkeypatch):
    runtime, _, root, path, calls = fork_runtime(tmp_path, monkeypatch)
    try:
        tool = runtime.tool_registry.get_tool("filesystem.edit_file")
        monkeypatch.setattr(tool, "invoke", type(tool).invoke.__get__(tool, type(tool)))
        old_child = runtime.agent_run_manager.child_tree(root.run_id)[0]
        new = runtime.replace_agent_run(root.run_id, user_input="Read the previous edit record and revert that edit.",
                                        command_id="rollback", request_fingerprint="rollback")
        runtime.agent_turn_loop.llm_client = ScriptedClient([
            operation("expand_package", package_name="observation"),
            operation("tool_call", tool_name="observation.read", tool_input={
                "artifact_id": f"replacement_handoff_{new.run_id}", "path": "/output/operations"}),
            operation("expand_package", package_name="filesystem"),
            operation("tool_call", tool_name="filesystem.edit_file", tool_input={"path": str(path),
                "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "edits": [{"old_text": "AFTER", "new_text": "BEFORE"}]}),
            operation("final_answer", reason="Explicitly requested rollback completed."), "The edit was reverted.",
        ])
        result = run_existing(runtime, new)
        assert path.read_text() == "BEFORE" and len(calls) == 1
        assert [event.tool_name for event in result.tool_events] == ["observation.read", "filesystem.edit_file"]
        assert all(event.result["status"] == "completed" for event in result.tool_events)
        previous_invocations = runtime.agent_run_store.list_tool_invocations(old_child.run_id)
        assert len(previous_invocations) == 1
        assert previous_invocations[0]["tool_name"] == "filesystem.edit_file"
        assert runtime.agent_run_manager.get_run(old_child.run_id).status == AgentRunStatus.CANCELLED
        assert runtime.agent_run_manager.list_events(old_child.run_id)
    finally:
        runtime.stop()


def test_unknown_effect_is_handed_off_without_assuming_no_change(tmp_path, monkeypatch):
    runtime, _, old, _, _ = pause_after_write(tmp_path, monkeypatch)
    try:
        runtime.agent_run_store.claim_tool_invocation(invocation_id="uncertain", run_id=old.run_id,
            tool_name="filesystem.edit_file", tool_input={"path": "unknown"}, claimed_at=old.created_at)
        new = runtime.replace_agent_run(old.run_id, user_input="Check previous operations.",
                                        command_id="new", request_fingerprint="new")
        handoff = runtime.agent_turn_runner._replacement_handoff(new)[0]
        assert any(item["effect_state"] == "unknown" for item in handoff["operations"])
        artifact_id = handoff["_result_cache"]["artifact_id"]
        assert runtime.agent_run_store.load_tool_result_artifact(artifact_id, old.run_id) is None
    finally:
        runtime.stop()


def test_long_handoff_prioritizes_writes_without_losing_read_records(tmp_path, monkeypatch):
    runtime, _, old, _, _ = pause_after_write(tmp_path, monkeypatch)
    try:
        for index in range(10):
            invocation_id = f"earlier-read-{index}"
            runtime.agent_run_store.claim_tool_invocation(invocation_id=invocation_id, run_id=old.run_id,
                tool_name="filesystem.read_file", tool_input={"path": f"file-{index}"}, claimed_at=old.created_at)
            runtime.agent_run_store.complete_tool_invocation(invocation_id=invocation_id, completed_at=old.created_at,
                result={"invocation_id": invocation_id, "tool_name": "filesystem.read_file", "status": "completed"})
        new = runtime.replace_agent_run(old.run_id, user_input="Inspect previous changes.",
                                        command_id="new", request_fingerprint="new")
        handoff = runtime.agent_turn_runner._replacement_handoff(new)[0]
        assert handoff["operation_count"] == 11
        assert handoff["operations_preview_complete"] is False
        assert handoff["operations"][0]["tool_name"] == "filesystem.edit_file"
        payload = runtime.agent_run_store.load_tool_result_artifact(handoff["_result_cache"]["artifact_id"], new.run_id)
        assert len(payload["output"]["operations"]) == 11
    finally:
        runtime.stop()


def test_real_tree_restart_resumes_same_child_attempt(tmp_path, monkeypatch):
    runtime, _, root, path, _ = fork_runtime(tmp_path, monkeypatch)
    policy = runtime.agent_turn_loop.fork_policy
    old_ids = tuple(item.run_id for item in runtime.agent_run_manager.child_tree(root.run_id))
    runtime.stop()
    reopened, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [
        operation("final_answer", reason="File already edited."), "The file was edited.",
        operation("final_answer", reason="Child recovered."), "Recovered child result.",
    ])
    reopened.agent_turn_loop.fork_policy = policy
    try:
        assert reopened.resume_agent_run(root.run_id).answer == "Recovered child result."
        assert tuple(item.run_id for item in reopened.agent_run_manager.child_tree(root.run_id)) == old_ids
        assert len(reopened.agent_run_store.list_tool_invocations(old_ids[0])) == 1
        assert path.read_text() == "AFTER"
    finally:
        reopened.stop()


def test_child_cannot_independently_resume_paused_root(tmp_path, monkeypatch):
    runtime, _, root, _, _ = fork_runtime(tmp_path, monkeypatch)
    try:
        worker = runtime.agent_run_manager.child_tree(root.run_id)[0]
        with pytest.raises(HTTPException) as rejected:
            asyncio.run(resume_agent_run(worker.run_id, request_for(runtime)))
        assert rejected.value.status_code == 409
        with pytest.raises(RuntimeError, match="root"):
            runtime.resume_agent_run(worker.run_id)
        assert runtime.agent_run_manager.get_run(worker.run_id).status == AgentRunStatus.PAUSED
    finally:
        runtime.stop()


def test_repeated_replacement_keeps_earlier_effects(tmp_path, monkeypatch):
    runtime, _, old, path, _ = pause_after_write(tmp_path, monkeypatch)
    try:
        first = runtime.replace_agent_run(old.run_id, user_input="Intermediate request.",
                                          command_id="first", request_fingerprint="first")
        # Pause before the first replacement executes any tools; replace it again.
        runner = runtime.agent_turn_runner
        runner.interrupt_after = ["prepare_context"]
        with pytest.raises(RuntimeError, match="stopped|result|completed"):
            run_existing(runtime, first)
        runtime.interrupt_agent_run(first.run_id)
        second = runtime.replace_agent_run(first.run_id, user_input="Inspect the original edit.",
                                           command_id="second", request_fingerprint="second")
        handoff = runner._replacement_handoff(second)[0]
        assert handoff["operation_count"] == 1
        assert handoff["operations"][0]["run_id"] == old.run_id
        assert handoff["operations"][0]["input"]["path"] == str(path)
    finally:
        runtime.stop()


def test_waiting_for_execution_slot_parks_without_starting_child():
    manager, root, _, context = _active_parent(_step("a"))
    executor = FakeChildExecutor(manager)
    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=executor, max_global_concurrency=1)
    assert scheduler._global_slots.acquire(blocking=False)

    async def scenario():
        task = asyncio.create_task(scheduler.execute_plan_async(root.run_id, context=context))
        for _ in range(100):
            if manager.get_run(root.run_id).child_run_ids:
                break
            await asyncio.sleep(0.01)
        assert manager.get_run(root.run_id).child_run_ids
        manager.request_pause(root.run_id)
        outcome = await asyncio.wait_for(task, 3)
        assert outcome.status == "paused"
        assert not executor.calls
        assert manager.child_tree(root.run_id)[0].status == AgentRunStatus.PAUSED

    try:
        asyncio.run(scenario())
    finally:
        scheduler._global_slots.release()


def test_non_checkpoint_executor_is_drained_not_falsely_paused(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [])
    runner = runtime.agent_turn_runner
    runner.interrupt_after = ["initialize_run"]
    root = runtime.create_agent_run(session_id="drain", user_input="Work")
    try:
        with pytest.raises(RuntimeError, match="stopped|result|completed"):
            run_existing(runtime, root)
        worker = child(runtime.agent_run_manager, root, "external", executor_kind="external_cli")
        assert runtime.agent_run_manager.admit_child_execution(worker.run_id)
        # Even a queued child is not safe to acknowledge if its executor was admitted.
        assert runtime.interrupt_agent_run(root.run_id).status == AgentRunStatus.RUNNING
        assert runtime.agent_run_manager.get_run(worker.run_id).status == AgentRunStatus.QUEUED
        runtime.agent_run_manager.mark_child_running(worker.run_id)
        assert runtime.agent_run_manager.get_run(root.run_id).status == AgentRunStatus.RUNNING
        runtime.agent_run_manager.complete_child_run(worker.run_id, result_snapshot={"answer": "Drained"})
        assert runtime.agent_run_manager.get_run(root.run_id).status == AgentRunStatus.PAUSED
        assert runtime.agent_run_manager.get_run(worker.run_id).status == AgentRunStatus.COMPLETED
    finally:
        runtime.stop()


def test_restart_does_not_leave_queued_admission_permanently_busy(tmp_path, monkeypatch):
    runtime, _ = runtime_for(tmp_path, monkeypatch, "langgraph", [])
    runtime.agent_turn_runner.interrupt_after = ["initialize_run"]
    root = runtime.create_agent_run(session_id="restart-admission", user_input="Work")
    with pytest.raises(RuntimeError, match="stopped|result|completed"):
        run_existing(runtime, root)
    worker = child(runtime.agent_run_manager, root, "not-started", executor_kind="workflow")
    assert runtime.agent_run_manager.admit_child_execution(worker.run_id)
    assert runtime.agent_run_manager.is_child_execution_active(worker.run_id)
    runtime.stop()
    reopened, client = runtime_for(tmp_path, monkeypatch, "langgraph", [])
    try:
        assert not reopened.agent_run_manager.is_child_execution_active(worker.run_id)
        assert reopened.interrupt_agent_run(root.run_id).status == AgentRunStatus.PAUSED
        assert reopened.agent_run_manager.get_run(worker.run_id).status == AgentRunStatus.PAUSED
        assert not client.calls
    finally:
        reopened.stop()


def test_parallel_children_do_not_publish_root_paused_until_both_stop():
    manager, root, _, context = _active_parent(_step("a"), _step("b"))
    executor = FakeChildExecutor(manager)
    entered = {name: Event() for name in ("a", "b")}
    releases = {name: Event() for name in ("a", "b")}

    async def run_child(**kwargs):
        worker = manager.get_run(kwargs["child_run_id"])
        manager.mark_child_running(worker.run_id)
        entered[worker.step_id].set()
        await asyncio.to_thread(releases[worker.step_id].wait, 5)
        manager.acknowledge_pause(worker.run_id)
        raise AgentTurnPaused(worker.run_id)

    scheduler = MultiAgentScheduler(run_manager=manager, child_executor=executor, child_runner=run_child)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, scheduler.execute_plan_async(root.run_id, context=context))
        try:
            assert entered["a"].wait(5) and entered["b"].wait(5)
            manager.request_pause(root.run_id)
            manager.acknowledge_pause(root.run_id)
            releases["a"].set()
            assert manager.get_run(root.run_id).status == AgentRunStatus.RUNNING
            releases["b"].set()
            assert future.result(timeout=5).status == "paused"
            assert manager.get_run(root.run_id).status == AgentRunStatus.PAUSED
        finally:
            for release in releases.values():
                release.set()
