"""Offline contracts for the opt-in Codex expert protocol adapter."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.codex_app_server import ApprovalRequest, ServerNotification, ServerRequest
from app.core.codex_expert import (
    CodexAppServerExpertExecutor,
    staged_file_change_approval_guard,
)
from app.core.codex_trace import CodexTraceJournal
from app.core.codex_workspace import CodexWorkspaceFactory
from app.core.context_driver import AgentView, AuditView, ContextViews, PlannerView, ToolView
from app.core.multi_agent import (
    ContextSnapshot,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    TaskResultStatus,
)
from app.core.safety import SafetyReviewDecision


class FakeCodexClient:
    def __init__(self) -> None:
        self.events: asyncio.Queue[ApprovalRequest | ServerNotification] = asyncio.Queue()
        self.turn_started = asyncio.Event()
        self.decisions: list[tuple[int | str, str]] = []
        self.server_responses: list[tuple[int | str, dict]] = []
        self.interrupts: list[tuple[str, str]] = []

    async def initialize(self, *, name: str, title: str, version: str) -> dict:
        return {"userAgent": "offline-fake"}

    async def start_thread(self, *, cwd: str, model: str | None = None) -> dict:
        self.cwd = cwd
        return {"id": "thread-test"}

    async def start_turn(
        self,
        *,
        thread_id: str,
        text: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict:
        self.turn_started.set()
        return {"id": "turn-test", "status": "inProgress"}

    async def next_event(self) -> ApprovalRequest | ServerNotification:
        return await self.events.get()

    async def respond_to_approval(self, request_id: int | str, decision: str) -> None:
        self.decisions.append((request_id, decision))

    async def respond_to_server_request(self, request_id: int | str, response: dict) -> None:
        self.server_responses.append((request_id, response))

    async def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None:
        self.interrupts.append((thread_id, turn_id))
        await self.events.put(
            ServerNotification(
                method="turn/completed",
                params={"turn": {"id": turn_id, "status": "interrupted"}},
            )
        )


def _setup(workspace_root: str):
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="session-parent", user_input="Run Codex child.")
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan-codex",
        step_id="codex-step",
        attempt=1,
        user_input="Execute the Codex task.",
        agent_id=CodexAppServerExpertExecutor.AGENT_ID,
        agent_version=CodexAppServerExpertExecutor.VERSION,
    )
    snapshot = ContextSnapshot(
        correlation_id="codex-test-trace",
        snapshot_id="snapshot-codex",
        parent_run_id=parent.run_id,
        child_run_id=child.run_id,
        session_id=child.session_id,
        plan_id=child.plan_id,
        step_id=child.step_id,
        agent_id=CodexAppServerExpertExecutor.AGENT_ID,
        agent_version=CodexAppServerExpertExecutor.VERSION,
        objective="Inspect the project structure.",
        output_contract="Return a concise summary.",
        effective_scope=ScopeGrant(
            workspace_paths=(workspace_root,),
            side_effect_level=SideEffectLevel.WRITE,
        ),
        budget=RuntimeBudget(max_wall_time_seconds=60),
        policy_version="policy-1",
        workspace_version="workspace-1",
        permission_version="permission-1",
    )
    views = ContextViews(
        agent=AgentView(
            objective=snapshot.objective,
            output_contract=snapshot.output_contract,
            mode="working",
        ),
        tool=ToolView(
            snapshot_id=snapshot.snapshot_id,
            child_run_id=child.run_id,
            allowed_paths=(workspace_root,),
            side_effect_level=SideEffectLevel.WRITE,
        ),
        planner=PlannerView(plan_id=snapshot.plan_id, step_id=snapshot.step_id),
        audit=AuditView(
            snapshot_id=snapshot.snapshot_id,
            child_run_id=child.run_id,
            session_id=child.session_id,
            policy_version=snapshot.policy_version,
            workspace_version=snapshot.workspace_version,
            permission_version=snapshot.permission_version,
        ),
    )
    return manager, child, snapshot, views


def test_fake_codex_notifications_map_to_completed_child_and_task_result(tmp_path) -> None:
    manager, child, snapshot, views = _setup(str(tmp_path))
    client = FakeCodexClient()
    client.events.put_nowait(
        ServerNotification(
            method="item/agentMessage/delta",
            params={"itemId": "message-1", "delta": "Project looks healthy."},
        )
    )
    client.events.put_nowait(
        ServerNotification(
            method="turn/completed",
            params={"turn": {"id": "turn-test", "status": "completed"}},
        )
    )
    executor = CodexAppServerExpertExecutor(manager, client_factory=lambda _run_id: client)

    result = asyncio.run(
        executor.execute(child_run_id=child.run_id, snapshot=snapshot, views=views)
    )

    assert result.status == TaskResultStatus.COMPLETED
    assert result.summary == "Project looks healthy."
    assert manager.get_run(child.run_id).status == AgentRunStatus.COMPLETED
    assert [event.type for event in manager.list_events(child.run_id)] == [
        "subtask_created",
        "subtask_queued",
        "context_snapshot_attached",
        "subtask_started",
        "codex_executor_started",
        "codex_session_started",
        "codex_turn_started",
        "codex_item_agentMessage_delta",
        "codex_turn_completed",
        "subtask_completed",
    ]
    delta_event = manager.list_events(child.run_id)[-3]
    assert delta_event.payload == {"itemId": "message-1", "delta_length": 22}


def test_unenforceable_read_scope_is_blocked_before_client_creation(tmp_path) -> None:
    manager, child, snapshot, views = _setup(str(tmp_path))
    read_snapshot = snapshot.model_copy(
        update={
            "effective_scope": snapshot.effective_scope.model_copy(
                update={"side_effect_level": SideEffectLevel.READ}
            )
        }
    )
    created: list[str] = []
    executor = CodexAppServerExpertExecutor(
        manager,
        client_factory=lambda run_id: created.append(run_id) or FakeCodexClient(),
    )

    result = asyncio.run(
        executor.execute(child_run_id=child.run_id, snapshot=read_snapshot, views=views)
    )

    assert result.status == TaskResultStatus.BLOCKED
    assert result.failure is not None and result.failure.code == "scope_not_enforceable"
    assert created == []
    assert manager.get_run(child.run_id).status == AgentRunStatus.FAILED


@pytest.mark.parametrize(
    "extra_scope",
    [
        {"workspace_paths": ("second",)},
        {"allowed_packages": ("filesystem",)},
        {"allowed_tools": ("filesystem.read_file",)},
        {"source_ids": ("source-1",)},
        {"account_ids": ("account-1",)},
    ],
)
def test_codex_rejects_unenforceable_scope_dimensions_before_start(tmp_path, extra_scope) -> None:
    manager, child, snapshot, views = _setup(str(tmp_path))
    if "workspace_paths" in extra_scope:
        extra_scope = {"workspace_paths": (str(tmp_path), str(tmp_path / "second"))}
    unsafe_snapshot = snapshot.model_copy(update={
        "effective_scope": snapshot.effective_scope.model_copy(update=extra_scope),
    })
    created: list[str] = []
    executor = CodexAppServerExpertExecutor(
        manager,
        client_factory=lambda run_id: created.append(run_id) or FakeCodexClient(),
    )

    result = asyncio.run(executor.execute(
        child_run_id=child.run_id, snapshot=unsafe_snapshot, views=views,
    ))

    assert result.status == TaskResultStatus.BLOCKED
    assert result.failure is not None and result.failure.code == "scope_not_enforceable"
    assert created == []


def test_codex_approval_is_declined_and_returns_explicit_blocked_result(tmp_path) -> None:
    manager, child, snapshot, views = _setup(str(tmp_path))
    client = FakeCodexClient()
    client.events.put_nowait(
        ApprovalRequest(
            request_id=77,
            method="item/commandExecution/requestApproval",
            params={
                "itemId": "command-1",
                "threadId": "thread-test",
                "turnId": "turn-test",
                "command": ["touch", "output.txt"],
            },
        )
    )
    executor = CodexAppServerExpertExecutor(manager, client_factory=lambda _run_id: client)

    result = asyncio.run(
        executor.execute(child_run_id=child.run_id, snapshot=snapshot, views=views)
    )

    assert result.status == TaskResultStatus.BLOCKED
    assert result.failure is not None
    assert result.failure.code == "codex_approval_queue_not_integrated"
    assert client.decisions == [(77, "decline")]
    assert manager.list_pending_safety_reviews() == []
    assert manager.get_run(child.run_id).status == AgentRunStatus.FAILED
    event = manager.list_events(child.run_id)[-2]
    assert event.type == "codex_approval_blocked"
    assert event.payload["decision"] == "decline"
    assert "command" not in event.payload
    assert client.interrupts == [("thread-test", "turn-test")]


def test_cancel_interrupts_active_fake_codex_turn_and_returns_cancelled_result(tmp_path) -> None:
    manager, child, snapshot, views = _setup(str(tmp_path))
    client = FakeCodexClient()
    executor = CodexAppServerExpertExecutor(manager, client_factory=lambda _run_id: client)

    async def exercise():
        running = asyncio.create_task(
            executor.execute(child_run_id=child.run_id, snapshot=snapshot, views=views)
        )
        await asyncio.wait_for(client.turn_started.wait(), timeout=1)
        cancelled = await executor.cancel(child.run_id, reason="Test cancellation.")
        result = await asyncio.wait_for(running, timeout=1)
        return cancelled, result

    cancelled, result = asyncio.run(exercise())
    assert cancelled.status == AgentRunStatus.CANCELLED
    assert result.status == TaskResultStatus.CANCELLED
    assert client.interrupts == [("thread-test", "turn-test")]


def test_codex_approval_uses_durable_fifo_and_continues_same_turn(tmp_path) -> None:
    manager, child, snapshot, views = _setup(str(tmp_path))
    client = FakeCodexClient()
    client.events.put_nowait(ApprovalRequest(
        request_id=81,
        method="item/commandExecution/requestApproval",
        params={
            "itemId": "command-1", "threadId": "thread-test", "turnId": "turn-test",
            "command": ["mkdir", "output"], "cwd": str(tmp_path),
        },
    ))
    executor = CodexAppServerExpertExecutor(
        manager, client_factory=lambda _run_id: client, route_human_approvals=True,
        approval_guard=lambda _event, _root: True,
    )

    async def exercise():
        running = asyncio.create_task(executor.execute(
            child_run_id=child.run_id, snapshot=snapshot, views=views,
        ))
        await asyncio.wait_for(client.turn_started.wait(), timeout=1)
        for _ in range(20):
            pending = manager.list_pending_safety_reviews()
            if pending:
                break
            await asyncio.sleep(0.05)
        assert len(pending) == 1
        assert pending[0].tool_name == "codex.command"
        assert manager.get_run(child.run_id).status == AgentRunStatus.WAITING_CONFIRMATION
        decided, transitioned = manager.decide_safety_review_with_transition(
            review_id=pending[0].review_id,
            decision=SafetyReviewDecision.APPROVE,
            decided_by="user",
            reason="Approved for test.",
        )
        assert transitioned and decided.status.value == "approved"
        for _ in range(20):
            if client.decisions:
                break
            await asyncio.sleep(0.05)
        assert client.decisions == [(81, "accept")]
        await client.events.put(ServerNotification(
            method="turn/completed",
            params={"turn": {"id": "turn-test", "status": "completed"}},
        ))
        return await asyncio.wait_for(running, timeout=1)

    result = asyncio.run(exercise())
    assert result.status == TaskResultStatus.COMPLETED
    assert manager.get_run(child.run_id).status == AgentRunStatus.COMPLETED


def test_codex_native_trace_preserves_content_outside_public_run_events(tmp_path) -> None:
    manager, child, snapshot, views = _setup(str(tmp_path))
    client = FakeCodexClient()
    client.events.put_nowait(ServerNotification(
        method="item/completed",
        params={"threadId": "thread-test", "turnId": "turn-test", "item": {
            "id": "command-1", "type": "commandExecution",
            "command": "printf secret", "aggregatedOutput": "secret output",
        }},
    ))
    client.events.put_nowait(ServerNotification(
        method="turn/completed",
        params={"turn": {"id": "turn-test", "status": "completed"}},
    ))
    journal = CodexTraceJournal(tmp_path / "codex-trace.sqlite3")
    executor = CodexAppServerExpertExecutor(
        manager, client_factory=lambda _run_id: client, trace_journal=journal,
    )

    result = asyncio.run(executor.execute(
        child_run_id=child.run_id, snapshot=snapshot, views=views,
    ))

    assert result.status == TaskResultStatus.COMPLETED
    native = journal.list(child.run_id)
    command = next(item for item in native if item.method == "item/completed")
    assert command.payload["item"]["aggregatedOutput"] == "secret output"
    assert all("secret output" not in str(event.payload) for event in manager.list_events(child.run_id))


def test_human_approval_cannot_expand_scope_without_guard(tmp_path) -> None:
    manager, child, snapshot, views = _setup(str(tmp_path))
    client = FakeCodexClient()
    client.events.put_nowait(ApprovalRequest(
        request_id=82,
        method="item/commandExecution/requestApproval",
        params={
            "itemId": "command-2", "threadId": "thread-test", "turnId": "turn-test",
            "command": ["curl", "https://example.com"],
        },
    ))
    executor = CodexAppServerExpertExecutor(
        manager, client_factory=lambda _run_id: client, route_human_approvals=True,
    )

    result = asyncio.run(executor.execute(
        child_run_id=child.run_id, snapshot=snapshot, views=views,
    ))

    assert result.status == TaskResultStatus.BLOCKED
    assert result.failure is not None and result.failure.code == "codex_approval_out_of_scope"
    assert client.decisions == [(82, "decline")]
    assert manager.list_pending_safety_reviews() == []


def test_codex_edits_remain_staged_and_result_is_partial(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "module.py").write_text("value = 1\n", encoding="utf-8")
    manager, child, snapshot, views = _setup(str(source))

    class EditingClient(FakeCodexClient):
        async def start_turn(self, **kwargs):
            (Path(self.cwd) / "module.py").write_text("value = 2\n", encoding="utf-8")
            return await super().start_turn(**kwargs)

    client = EditingClient()
    client.events.put_nowait(ServerNotification(
        method="turn/completed",
        params={"turn": {"id": "turn-test", "status": "completed"}},
    ))
    journal = CodexTraceJournal(tmp_path / "trace.sqlite3")
    executor = CodexAppServerExpertExecutor(
        manager,
        client_factory=lambda _run_id: client,
        workspace_factory=CodexWorkspaceFactory(tmp_path / "leases"),
        trace_journal=journal,
    )

    result = asyncio.run(executor.execute(
        child_run_id=child.run_id, snapshot=snapshot, views=views,
    ))

    assert result.status == TaskResultStatus.PARTIAL
    assert result.missing_requirements == ("approve_and_apply_staged_changes",)
    assert (source / "module.py").read_text(encoding="utf-8") == "value = 1\n"
    staged = manager.get_run(child.run_id).result_snapshot
    assert staged["staged_change_count"] == 1
    assert (Path(staged["staged_workspace"]) / "module.py").read_text(encoding="utf-8") == "value = 2\n"
    event = next(item for item in journal.list(child.run_id) if item.method == "workspace/changes_staged")
    assert event.payload["changes"][0]["path"] == "module.py"


def test_codex_permission_request_is_denied_without_expanding_scope(tmp_path) -> None:
    manager, child, snapshot, views = _setup(str(tmp_path))
    client = FakeCodexClient()
    client.events.put_nowait(ServerRequest(
        request_id=91,
        method="item/permissions/requestApproval",
        params={
            "itemId": "permission-1", "threadId": "thread-test", "turnId": "turn-test",
            "permissions": {"network": True},
        },
    ))
    executor = CodexAppServerExpertExecutor(manager, client_factory=lambda _run_id: client)

    result = asyncio.run(executor.execute(
        child_run_id=child.run_id, snapshot=snapshot, views=views,
    ))

    assert result.status == TaskResultStatus.BLOCKED
    assert result.failure is not None and result.failure.code == "codex_server_request_not_integrated"
    assert client.server_responses == [(91, {"permissions": {}, "scope": "turn"})]
    assert client.interrupts == [("thread-test", "turn-test")]


def test_staged_file_approval_guard_rejects_command_and_other_root(tmp_path) -> None:
    workspace = tmp_path / "lease"
    workspace.mkdir()
    allowed = ApprovalRequest(
        request_id=1,
        method="item/fileChange/requestApproval",
        params={"grantRoot": str(workspace)},
    )
    outside = ApprovalRequest(
        request_id=2,
        method="item/fileChange/requestApproval",
        params={"grantRoot": str(tmp_path)},
    )
    command = ApprovalRequest(
        request_id=3,
        method="item/commandExecution/requestApproval",
        params={"cwd": str(workspace)},
    )
    assert staged_file_change_approval_guard(allowed, workspace)
    assert not staged_file_change_approval_guard(outside, workspace)
    assert not staged_file_change_approval_guard(command, workspace)
