from __future__ import annotations

import threading
import time

from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import (
    ToolContext,
    ToolExecutor,
    ToolPackageSpec,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class _CountingTool:
    spec = ToolSpec(
        name="test.count",
        package="test",
        type="local_tool",
        description="Count invocations.",
        read_only=True,
        side_effects=["read_test_state"],
        input_schema={"type": "object"},
    )

    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def invoke(self, *, invocation, context):
        with self.lock:
            self.calls += 1
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool.name,
            status="completed",
            output={"calls": self.calls},
        )


def test_tool_executor_enforces_child_tool_call_budget_from_durable_events() -> None:
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="budget-session", user_input="parent")
    manager.mark_running(parent.run_id)
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="budget-plan",
        step_id="budget-step",
        attempt=1,
        user_input="child",
    )
    manager.mark_child_running(child.run_id)
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name="test", description="test"))
    registry.register_tool(tool)
    executor = ToolExecutor(registry)
    executor.run_manager = manager
    view = ToolView(
        snapshot_id="budget-snapshot",
        child_run_id=child.run_id,
        allowed_packages=("test",),
        allowed_tools=("test.count",),
        side_effect_level=SideEffectLevel.READ,
        max_tool_calls=1,
    )
    context = ToolContext(session_id=child.session_id, run_id=child.run_id, tool_view=view)

    manager.append_event(child.run_id, "tool_started", "Started", stage="tool")
    first = executor.execute(
        invocation_id="first", tool_name="test.count", tool_input={}, context=context
    )
    manager.append_event(child.run_id, "tool_started", "Started", stage="tool")
    second = executor.execute(
        invocation_id="second", tool_name="test.count", tool_input={}, context=context
    )

    assert first.status == "completed"
    assert second.status == "rejected"
    assert "tool-call budget" in (second.error or "")
    assert tool.calls == 1


class _ResourceLockedWriteTool:
    spec = ToolSpec(
        name="test.resource_write",
        package="test",
        type="local_tool",
        description="Write a resource under a shared lock.",
        read_only=False,
        resource_lock_group="test-resource-wait-events",
        resource_lock_fields=("target",),
        input_schema={"type": "object", "required": ["target"]},
    )

    def invoke(self, *, invocation, context):
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool.name,
            status="completed",
        )


def test_tool_executor_records_resource_lock_wait_without_lock_key() -> None:
    manager = InMemoryAgentRunManager()
    run = manager.create_run(session_id="resource-wait", user_input="write")
    manager.mark_running(run.run_id)
    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name="test", description="test"))
    registry.register_tool(_ResourceLockedWriteTool())
    executor = ToolExecutor(registry)
    executor.run_manager = manager
    context = ToolContext(
        session_id=run.session_id,
        run_id=run.run_id,
        safety_review_approved=True,
    )

    uncontended = executor.execute(
        invocation_id="uncontended",
        tool_name="test.resource_write",
        tool_input={"target": "private-target"},
        context=context,
    )
    assert uncontended.status == "completed"
    assert not any("resource_lock_wait" in event.type for event in manager.list_events(run.run_id))

    lock = ToolExecutor._resource_lock("test-resource-wait-events", "private-target")
    lock.acquire()
    result_holder: list[ToolResult] = []
    worker = threading.Thread(
        target=lambda: result_holder.append(
            executor.execute(
                invocation_id="contended",
                tool_name="test.resource_write",
                tool_input={"target": "private-target"},
                context=context,
            )
        )
    )
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            events = manager.list_events(run.run_id)
            if any(event.type == "resource_lock_wait_started" for event in events):
                break
            time.sleep(0.01)
        else:
            raise AssertionError("resource lock wait start event was not recorded")
    finally:
        lock.release()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result_holder[0].status == "completed"
    events = manager.list_events(run.run_id)
    wait_events = [event for event in events if event.type.startswith("resource_lock_wait_")]
    assert [event.type for event in wait_events] == [
        "resource_lock_wait_started",
        "resource_lock_wait_completed",
    ]
    assert wait_events[-1].payload["elapsed_ms"] >= 0
    assert all("private-target" not in str(event.payload) for event in wait_events)


class _PathTool:
    spec = ToolSpec(
        name="test.path",
        package="test",
        type="local_tool",
        description="Observe path argument enforcement.",
        read_only=True,
        side_effects=["read_local_file"],
        scope_path_fields=("path",),
        scope_uses_workspace=True,
        input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
    )

    def __init__(self):
        self.calls = 0

    def invoke(self, *, invocation, context):
        self.calls += 1
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool.name,
            status="completed",
            output={},
        )


def test_tool_executor_rejects_child_path_outside_narrowed_workspace_before_invoke() -> None:
    tool = _PathTool()
    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name="test", description="test"))
    registry.register_tool(tool)
    executor = ToolExecutor(registry)
    view = ToolView(
        snapshot_id="path-snapshot",
        child_run_id="child-path-run",
        allowed_packages=("test",),
        allowed_tools=("test.path",),
        allowed_paths=("/tmp/child-authorized",),
        side_effect_level=SideEffectLevel.READ,
    )
    result = executor.execute(
        invocation_id="path-outside",
        tool_name="test.path",
        tool_input={"path": "../outside.txt"},
        context=ToolContext(
            session_id="child-path-session",
            workspace_root="/tmp/child-authorized",
            tool_view=view,
        ),
    )

    assert result.status == "rejected"
    assert "outside the child workspace" in (result.error or "")
    assert tool.calls == 0


class _ConcurrentWriteTool:
    spec = ToolSpec(
        name="test.write",
        package="test",
        type="local_tool",
        description="Observe critical section concurrency.",
        read_only=False,
        side_effects=["write_test_state"],
        input_schema={"type": "object"},
    )

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()

    def invoke(self, *, invocation, context):
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.03)
        with self.guard:
            self.active -= 1
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool.name,
            status="completed",
            output={},
        )


def test_tool_executor_serializes_non_read_only_invocations_on_shared_resource() -> None:
    tool = _ConcurrentWriteTool()
    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name="test", description="test"))
    registry.register_tool(tool)
    executor = ToolExecutor(registry)
    context = ToolContext(
        session_id="shared-session",
        workspace_root="/tmp/shared-workspace",
        safety_review_approved=True,
    )
    start = threading.Barrier(3)

    def invoke(index: int) -> None:
        start.wait()
        executor.execute(
            invocation_id=f"write-{index}",
            tool_name="test.write",
            tool_input={},
            context=context,
        )

    threads = [threading.Thread(target=invoke, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert tool.max_active == 1


def test_timeout_terminal_wins_over_later_child_completion() -> None:
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="timeout-session", user_input="parent")
    manager.mark_running(parent.run_id)
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="timeout-plan",
        step_id="timeout-step",
        attempt=1,
        user_input="child",
    )
    manager.mark_child_running(child.run_id)

    timed_out = manager.timeout_child_run(child.run_id, error="test timeout")
    late_completion = manager.complete_child_run(child.run_id, result_snapshot={"answer": "late"})

    assert timed_out.status == AgentRunStatus.TIMED_OUT
    assert late_completion.status == AgentRunStatus.TIMED_OUT
    assert manager.get_run(child.run_id).status == AgentRunStatus.TIMED_OUT
