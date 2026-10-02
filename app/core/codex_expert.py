"""Fail-closed Codex expert executor with private trace and approval routing.

Runtime may register this executor when explicitly enabled. The client factory
owns the external process; staged edits are never applied to source files here.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from app.core.agent_runs import AgentRunStatus, InMemoryAgentRunManager
from app.core.codex_app_server import ApprovalRequest, ServerNotification, ServerRequest
from app.core.context_driver import ContextViews
from app.core.multi_agent import ContextSnapshot, FailureDetail, TaskResult, TaskResultStatus
from app.core.safety import (
    SafetyReviewMode,
    SafetyReviewRequest,
    SafetyReviewStatus,
    stable_safety_review_id,
)


class CodexClient(Protocol):
    async def initialize(self, *, name: str, title: str, version: str) -> dict[str, Any]: ...

    async def start_thread(self, *, cwd: str, model: str | None = None) -> dict[str, Any]: ...

    async def start_turn(
        self,
        *,
        thread_id: str,
        text: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]: ...

    async def next_event(self) -> ApprovalRequest | ServerNotification | ServerRequest: ...

    async def respond_to_approval(self, request_id: int | str, decision: str) -> None: ...

    async def respond_to_server_request(
        self, request_id: int | str, response: dict[str, Any]
    ) -> None: ...

    async def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None: ...


@dataclass
class _ActiveTurn:
    client: CodexClient
    thread_id: str | None = None
    turn_id: str | None = None
    workspace_root: Path | None = None


class CodexAppServerExpertExecutor:
    """Run one Codex app-server turn as a scoped expert Child Run."""

    AGENT_ID = "codex_expert"
    VERSION = "0.1"

    def __init__(
        self,
        run_manager: InMemoryAgentRunManager,
        *,
        client_factory: Callable[[str], CodexClient | Awaitable[CodexClient]],
        codex_model: str | None = None,
        reasoning_effort: str | None = None,
        route_human_approvals: bool = False,
        approval_guard: Callable[[ApprovalRequest, Path], bool] | None = None,
        trace_journal: Any | None = None,
        workspace_factory: Any | None = None,
    ) -> None:
        self.run_manager = run_manager
        self.client_factory = client_factory
        self.codex_model = codex_model
        self.reasoning_effort = reasoning_effort
        self.route_human_approvals = route_human_approvals
        self.approval_guard = approval_guard
        self.trace_journal = trace_journal
        self.workspace_factory = workspace_factory
        self._active: dict[str, _ActiveTurn] = {}
        self._active_lock = asyncio.Lock()

    async def execute(
        self,
        *,
        child_run_id: str,
        snapshot: ContextSnapshot,
        views: ContextViews,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
    ) -> TaskResult:
        del llm_client_name  # Codex chooses its own provider account.
        child = self.run_manager.get_run(child_run_id)
        if child is None or child.parent_run_id is None:
            raise ValueError("Codex expert requires an existing Child Run.")
        if child.status != AgentRunStatus.QUEUED:
            return self.from_terminal(child, snapshot)
        if llm_model is not None and llm_model != self.codex_model:
            return self._blocked(
                child,
                snapshot,
                code="codex_model_not_allowed",
                message="The requested model is not in the server-owned Codex model configuration.",
            )
        if (
            snapshot.child_run_id != child_run_id
            or snapshot.parent_run_id != child.parent_run_id
            or snapshot.session_id != child.session_id
            or snapshot.plan_id != child.plan_id
            or snapshot.step_id != child.step_id
            or snapshot.agent_id != self.AGENT_ID
            or views.tool.snapshot_id != snapshot.snapshot_id
            or views.tool.child_run_id != child_run_id
        ):
            return self._blocked(
                child,
                snapshot,
                code="context_mismatch",
                message="Codex expert context does not match the Child Run.",
            )
        workspace_paths = snapshot.effective_scope.workspace_paths
        if (
            len(workspace_paths) != 1
            or views.tool.allowed_paths != workspace_paths
            or views.tool.side_effect_level.value != "write"
            or snapshot.effective_scope.side_effect_level.value != "write"
            or snapshot.effective_scope.allowed_packages
            or snapshot.effective_scope.allowed_tools
            or snapshot.effective_scope.source_ids
            or snapshot.effective_scope.account_ids
        ):
            return self._blocked(
                child,
                snapshot,
                code="scope_not_enforceable",
                message=(
                    "Codex's workspace-write sandbox cannot enforce this ContextSnapshot scope; "
                    "it requires one workspace path, write permission, and no additional package, tool, "
                    "source or account grants."
                ),
            )
        raw_workspace_root = Path(workspace_paths[0])
        if not raw_workspace_root.is_absolute():
            return self._blocked(
                child,
                snapshot,
                code="workspace_root_invalid",
                message="Codex requires an absolute workspace root.",
            )
        try:
            workspace_root = raw_workspace_root.resolve(strict=True)
        except (OSError, RuntimeError):
            workspace_root = None
        if (
            workspace_root is None
            or not workspace_root.is_absolute()
            or not workspace_root.is_dir()
            or workspace_root != raw_workspace_root
        ):
            return self._blocked(
                child,
                snapshot,
                code="workspace_root_invalid",
                message="Codex requires an existing absolute workspace directory from the ContextSnapshot.",
            )

        lease: Any | None = None
        if self.workspace_factory is not None:
            try:
                lease = self.workspace_factory.create(workspace_root)
                workspace_root = lease.cwd
                self._record_native(
                    child_run_id, "workspace/staged",
                    {"source_root": str(lease.source_root), "isolated_cwd": str(lease.cwd)},
                )
            except Exception as exc:  # noqa: BLE001 - staging must fail before any Codex execution.
                return self._blocked(
                    child, snapshot,
                    code="codex_workspace_staging_failed",
                    message=f"Codex workspace could not be safely staged ({type(exc).__name__}).",
                )

        self.run_manager.attach_child_context(
            child_run_id,
            snapshot=snapshot.model_dump(mode="json"),
            views=views.model_dump(mode="json"),
        )
        if self.run_manager.mark_child_running(child_run_id).status != AgentRunStatus.RUNNING:
            return self.from_terminal(self.run_manager.get_run(child_run_id) or child, snapshot)

        active: _ActiveTurn | None = None
        try:
            client = self.client_factory(child_run_id)
            if inspect.isawaitable(client):
                client = await client
            active = _ActiveTurn(client=client, workspace_root=workspace_root)
            async with self._active_lock:
                self._active[child_run_id] = active
            self._record(child, "codex_executor_started", "Codex expert protocol execution started.")
            await client.initialize(name="local_knowledge_agent", title="Local Knowledge Agent", version="0.1")
            thread = await client.start_thread(cwd=str(workspace_root), model=self.codex_model)
            thread_id = _required_id(thread, "id", "thread/start")
            active.thread_id = thread_id
            self._record(
                child,
                "codex_session_started",
                "Codex session started.",
                {"thread_id": thread_id},
            )
            self._record_native(child.run_id, "thread/start", {"thread": thread}, thread_id=thread_id)
            if self._is_cancelled(child_run_id):
                return self.from_terminal(self.run_manager.get_run(child_run_id) or child, snapshot)

            turn = await client.start_turn(
                thread_id=thread_id,
                text=(
                    f"Task: {snapshot.objective}\n\n"
                    f"Output contract: {snapshot.output_contract}\n\n"
                    f"Verification criteria: {', '.join(snapshot.verification_criteria) or 'none'}"
                    + (
                        "\n\nYou are working in an isolated copy. Report changes and verification "
                        "honestly; changes are not applied to the source workspace automatically."
                        if lease is not None else ""
                    )
                ),
                model=self.codex_model,
                effort=self.reasoning_effort,
            )
            active.turn_id = _required_id(turn, "id", "turn/start")
            self._record(
                child,
                "codex_turn_started",
                "Codex turn started.",
                {"thread_id": thread_id, "turn_id": active.turn_id},
            )
            self._record_native(
                child.run_id, "turn/start", {"turn": turn},
                thread_id=thread_id, turn_id=active.turn_id,
            )
            if self._is_cancelled(child_run_id):
                await client.interrupt_turn(thread_id=thread_id, turn_id=active.turn_id)
                return self.from_terminal(self.run_manager.get_run(child_run_id) or child, snapshot)

            answer_parts: dict[str, str] = {}
            while True:
                if self._is_cancelled(child_run_id):
                    return self.from_terminal(self.run_manager.get_run(child_run_id) or child, snapshot)
                event = await client.next_event()
                if isinstance(event, ServerRequest):
                    self._record_native(
                        child.run_id, event.method, event.params,
                        thread_id=active.thread_id, turn_id=active.turn_id,
                        item_id=event.params.get("itemId"), request_id=event.request_id,
                    )
                    if _correlation_mismatch(
                        event.params, thread_id=active.thread_id, turn_id=active.turn_id
                    ):
                        await self._best_effort_interrupt(active)
                        return self._fail(
                            child, snapshot, "codex_event_scope_mismatch",
                            "Codex request did not match the active session and turn.",
                        )
                    # These request families need a separate typed scope/user
                    # input bridge. Never infer permission from an approval of
                    # the process or its workspace.
                    if event.method == "item/permissions/requestApproval":
                        await client.respond_to_server_request(
                            event.request_id, {"permissions": {}, "scope": "turn"}
                        )
                    elif event.method == "mcpServer/elicitation/request":
                        await client.respond_to_server_request(
                            event.request_id, {"action": "decline"}
                        )
                    await self._best_effort_interrupt(active)
                    return self._blocked(
                        child, snapshot,
                        code="codex_server_request_not_integrated",
                        message=f"Codex requested unsupported interaction: {event.method}.",
                    )
                if isinstance(event, ApprovalRequest):
                    self._record_native(
                        child.run_id, event.method, event.params,
                        thread_id=active.thread_id, turn_id=active.turn_id,
                        item_id=event.params.get("itemId"), request_id=event.request_id,
                    )
                    correlation_failure = _correlation_mismatch(
                        event.params, thread_id=active.thread_id, turn_id=active.turn_id
                    )
                    self._record(
                        child,
                        "codex_approval_requested",
                        "Codex requested an approval decision.",
                        {
                            "request_id": event.request_id,
                            "item_id": event.params.get("itemId"),
                            "received_thread_id": event.params.get("threadId"),
                            "received_turn_id": event.params.get("turnId"),
                            "active_thread_id": active.thread_id,
                            "active_turn_id": active.turn_id,
                        },
                    )
                    if correlation_failure:
                        await client.respond_to_approval(event.request_id, "decline")
                        await self._best_effort_interrupt(active)
                        return self._fail(
                            child,
                            snapshot,
                            "codex_event_scope_mismatch",
                            "Codex approval request did not match the active session and turn; it was declined.",
                        )
                    if self.route_human_approvals:
                        if (
                            self.approval_guard is None
                            or active.workspace_root is None
                            or not self.approval_guard(event, active.workspace_root)
                        ):
                            await client.respond_to_approval(event.request_id, "decline")
                            await self._best_effort_interrupt(active)
                            return self._blocked(
                                child, snapshot,
                                code="codex_approval_out_of_scope",
                                message="Codex approval could not be proven within the delegated scope.",
                            )
                        decision = await self._route_approval(child, active, event)
                        if decision is None:
                            return self.from_terminal(
                                self.run_manager.get_run(child_run_id) or child, snapshot
                            )
                        await client.respond_to_approval(event.request_id, decision)
                        self._record_native(
                            child.run_id, "approval/response",
                            {"method": event.method, "decision": decision},
                            thread_id=active.thread_id, turn_id=active.turn_id,
                            item_id=event.params.get("itemId"), request_id=event.request_id,
                        )
                        continue
                    await client.respond_to_approval(event.request_id, "decline")
                    await self._best_effort_interrupt(active)
                    failure = FailureDetail(
                        category="approval",
                        code="codex_approval_queue_not_integrated",
                        message=(
                            "Codex requested an action requiring approval. This executor cannot "
                            "safely route the existing approval queue to Codex, so the request was declined."
                        ),
                        retryable=False,
                        recommended_actions=("Use the general Agent executor or wait for Codex approval routing support.",),
                    )
                    self._record(
                        child,
                        "codex_approval_blocked",
                        failure.message,
                        {
                            "approval_method": event.method,
                            "request_id": event.request_id,
                            "item_id": event.params.get("itemId"),
                            "thread_id": event.params.get("threadId"),
                            "turn_id": event.params.get("turnId"),
                            "decision": "decline",
                        },
                    )
                    self.run_manager.fail_child_run(
                        child_run_id,
                        error_type=failure.code,
                        error=failure.message,
                    )
                    return self._task_result(
                        child,
                        snapshot,
                        status=TaskResultStatus.BLOCKED,
                        summary=failure.message,
                        failure=failure,
                    )

                method = event.method
                params = event.params
                self._record_native(
                    child.run_id, method, params,
                    thread_id=params.get("threadId") or active.thread_id,
                    turn_id=params.get("turnId") or active.turn_id,
                    item_id=params.get("itemId") or (
                        params.get("item", {}).get("id")
                        if isinstance(params.get("item"), dict) else None
                    ),
                )
                if _correlation_mismatch(params, thread_id=active.thread_id, turn_id=active.turn_id):
                    await self._best_effort_interrupt(active)
                    return self._fail(
                        child,
                        snapshot,
                        "codex_event_scope_mismatch",
                        "Codex notification did not match the active session and turn.",
                    )
                self._record(
                    child,
                    _normalized_event_type(method),
                    f"Codex event: {method}.",
                    _safe_notification_payload(method, params),
                )
                if method == "item/agentMessage/delta":
                    item_id = str(params.get("itemId") or "message")
                    delta = params.get("delta")
                    if isinstance(delta, str):
                        answer_parts[item_id] = answer_parts.get(item_id, "") + delta
                elif method == "item/completed":
                    item = params.get("item")
                    if isinstance(item, dict) and item.get("type") == "agentMessage":
                        item_id = str(item.get("id") or "message")
                        text = item.get("text")
                        if isinstance(text, str):
                            answer_parts[item_id] = text
                elif method == "error":
                    error = params.get("error")
                    error_code = error.get("codexErrorInfo") if isinstance(error, dict) else None
                    safe_code = error_code if error_code in _KNOWN_CODEX_ERROR_CODES else "codex_error"
                    await self._best_effort_interrupt(active)
                    return self._fail(child, snapshot, safe_code, "Codex app-server reported an execution error.")
                elif method == "turn/completed":
                    turn_data = params.get("turn")
                    status = turn_data.get("status") if isinstance(turn_data, dict) else None
                    current = self.run_manager.get_run(child_run_id)
                    if current is None:
                        return self._fail(child, snapshot, "child_missing", "Child Run disappeared during Codex execution.")
                    if current.status in {
                        AgentRunStatus.CANCELLED,
                        AgentRunStatus.TIMED_OUT,
                        AgentRunStatus.FAILED,
                    } or self._is_cancelled(child_run_id):
                        return self.from_terminal(current, snapshot)
                    if status == "completed":
                        answer = "\n".join(part for part in answer_parts.values() if part).strip()
                        if not answer and isinstance(turn_data, dict):
                            answer = _answer_from_items(turn_data.get("items"))
                        if not answer:
                            answer = "Codex completed without a textual answer."
                        staged_changes: tuple[Any, ...] = ()
                        if lease is not None:
                            staged_changes = lease.collect_changes()
                            self._record_native(
                                child_run_id, "workspace/changes_staged",
                                {"changes": [
                                    {
                                        "path": change.path,
                                        "status": change.status,
                                        "original_sha256": change.original_sha256,
                                        "result_sha256": change.result_sha256,
                                        "diff": change.diff,
                                        "diff_reason": change.diff_reason,
                                        "source_conflict": change.source_conflict,
                                    }
                                    for change in staged_changes
                                ]},
                                thread_id=thread_id, turn_id=active.turn_id,
                            )
                            self._record(
                                child, "child_tool_audit", "Isolated workspace effects audited.",
                                {"audit": {
                                    "protocol_version": "isolated_workspace_audit_v1",
                                    "complete": True,
                                    "snapshot_id": snapshot.snapshot_id,
                                    "source_workspace_sha256": sha256(str(lease.source_root).encode()).hexdigest(),
                                    "isolated_workspace_sha256": sha256(str(lease.cwd).encode()).hexdigest(),
                                    "staged_change_count": len(staged_changes),
                                    "source_applied": False,
                                    "external_effects_ruled_out": (
                                        not self.route_human_approvals
                                        or self.approval_guard is staged_file_change_approval_guard
                                    ),
                                }},
                            )
                        completed = self.run_manager.complete_child_run(
                            child_run_id,
                            result_snapshot={
                                "answer": answer,
                                "codex_thread_id": thread_id,
                                "codex_turn_id": active.turn_id,
                                "staged_change_count": len(staged_changes),
                                "staged_workspace": str(lease.cwd) if lease is not None else None,
                            },
                        )
                        if completed.status != AgentRunStatus.COMPLETED:
                            return self.from_terminal(completed, snapshot)
                        if staged_changes:
                            return self._task_result(
                                child, snapshot, status=TaskResultStatus.PARTIAL,
                                summary=f"{answer}\n\nCodex staged {len(staged_changes)} file change(s); source files were not modified.",
                                missing_requirements=("approve_and_apply_staged_changes",),
                                warnings=("Codex changes are isolated and have not been applied to the source workspace.",),
                            )
                        return self._task_result(
                            child, snapshot, status=TaskResultStatus.COMPLETED, summary=answer
                        )
                    if status == "interrupted":
                        if self._is_cancelled(child_run_id):
                            self.run_manager.cancel_run(child_run_id, reason="Codex turn interrupted after cancellation.")
                            return self.from_terminal(self.run_manager.get_run(child_run_id) or child, snapshot)
                        return self._fail(
                            child,
                            snapshot,
                            "codex_unrequested_interrupt",
                            "Codex turn was interrupted without a local cancellation request.",
                        )
                    return self._fail(
                        child,
                        snapshot,
                        "codex_turn_failed",
                        f"Codex turn ended with status {status!r}.",
                    )
        except asyncio.CancelledError:
            # The scheduler owns the terminal winner. In particular, an
            # asyncio.wait_for timeout cancels this coroutine before the
            # scheduler can durably mark TIMED_OUT; do not preempt it with a
            # CANCELLED terminal record here.
            self.run_manager.request_cancel(
                child_run_id, reason="Codex executor task cancellation requested."
            )
            if active is not None:
                await self._best_effort_interrupt(active)
            raise
        except Exception as exc:  # noqa: BLE001 - normalize injected protocol failures.
            if active is not None:
                await self._best_effort_interrupt(active)
                if self.trace_journal is not None:
                    try:
                        self.trace_journal.mark_gap(
                            child_run_id,
                            reason="codex_transport_or_recording_failure",
                            details={"error_type": type(exc).__name__},
                            thread_id=active.thread_id,
                            turn_id=active.turn_id,
                        )
                    except Exception as journal_exc:  # noqa: BLE001 - preserve the original failure.
                        exc.add_note(f"Codex trace gap could not be recorded: {type(journal_exc).__name__}")
            return self._fail(
                child,
                snapshot,
                "codex_protocol_failure",
                f"Codex app-server execution failed ({type(exc).__name__}).",
            )
        finally:
            if active is not None:
                async with self._active_lock:
                    self._active.pop(child_run_id, None)
                close = getattr(active.client, "close", None)
                if callable(close):
                    try:
                        await close()
                    except Exception as exc:  # noqa: BLE001 - execution has already reached a terminal result.
                        self._record(
                            child, "codex_transport_close_failed",
                            "Codex transport could not be closed cleanly.",
                            {"error_type": type(exc).__name__},
                        )

    async def _route_approval(
        self, child: Any, active: _ActiveTurn, event: ApprovalRequest
    ) -> str | None:
        """Park a Codex request in the same durable FIFO as native tool reviews."""
        if event.method not in {
            "item/commandExecution/requestApproval", "item/fileChange/requestApproval"
        }:
            await active.client.respond_to_approval(event.request_id, "decline")
            await self._best_effort_interrupt(active)
            self.run_manager.fail_child_run(
                child.run_id, error_type="codex_approval_unsupported",
                error="Unsupported Codex approval request type.",
            )
            return None
        request_id = stable_safety_review_id(
            child.run_id, active.thread_id, active.turn_id,
            str(event.params.get("itemId")), str(event.request_id),
        )
        review = self.run_manager.create_safety_review(SafetyReviewRequest(
            review_id=request_id,
            run_id=child.run_id,
            session_id=child.session_id,
            trace_id=child.trace_id,
            invocation_id=f"codex:{active.thread_id}:{active.turn_id}:{event.request_id}",
            tool_name="codex.command" if "commandExecution" in event.method else "codex.file_change",
            tool_input={
                "thread_id": active.thread_id,
                "turn_id": active.turn_id,
                "item_id": event.params.get("itemId"),
                "command": event.params.get("command"),
                "cwd": event.params.get("cwd"),
                "reason": event.params.get("reason"),
                "grant_root": event.params.get("grantRoot"),
                "network_context": event.params.get("networkApprovalContext"),
            },
            tool_risk="high",
            side_effects=["Codex external execution"],
            read_only=False,
            mode=SafetyReviewMode.MANUAL,
            reason=str(event.params.get("reason") or "Codex requests approval for an action."),
            created_at=datetime.now(UTC).isoformat(),
        ))
        self._record(child, "codex_approval_waiting", "Codex is waiting for user approval.", {
            "review_id": review.review_id,
            "request_id": event.request_id,
            "thread_id": active.thread_id,
            "turn_id": active.turn_id,
            "item_id": event.params.get("itemId"),
        })
        while True:
            current = self.run_manager.get_run(child.run_id)
            if current is None or current.status in {
                AgentRunStatus.CANCELLED, AgentRunStatus.TIMED_OUT, AgentRunStatus.FAILED,
            } or self.run_manager.is_cancel_requested(child.run_id):
                await active.client.respond_to_approval(event.request_id, "cancel")
                await self._best_effort_interrupt(active)
                return None
            decided = self.run_manager.get_safety_review(review.review_id)
            if decided is not None and decided.status != SafetyReviewStatus.PENDING:
                decision = "accept" if decided.status == SafetyReviewStatus.APPROVED else "decline"
                self._record(child, "codex_approval_decided", "Codex approval was decided.", {
                    "review_id": review.review_id, "decision": decision,
                })
                return decision
            await asyncio.sleep(0.25)

    async def cancel(self, child_run_id: str, *, reason: str = "Child run cancelled.") -> Any:
        """Persist cancellation and interrupt the active app-server turn."""
        current = self.run_manager.get_run(child_run_id)
        if current is None:
            raise KeyError(f"Agent run not found: {child_run_id}")
        cancelled = self.run_manager.cancel_run(child_run_id, reason=reason)
        async with self._active_lock:
            active = self._active.get(child_run_id)
        if (
            cancelled.status == AgentRunStatus.CANCELLED
            and active is not None
            and active.thread_id is not None
            and active.turn_id is not None
        ):
            try:
                await active.client.interrupt_turn(
                    thread_id=active.thread_id,
                    turn_id=active.turn_id,
                )
                self._record(
                    current,
                    "codex_turn_interrupt_requested",
                    "Codex turn interruption requested after Child Run cancellation.",
                    {"thread_id": active.thread_id, "turn_id": active.turn_id},
                )
            except Exception as exc:  # noqa: BLE001 - cancellation already won durably.
                self._record(
                    current,
                    "codex_turn_interrupt_failed",
                    "Codex interruption request failed after cancellation was recorded.",
                    {"error_type": type(exc).__name__},
                )
        return self.run_manager.get_run(child_run_id) or cancelled

    async def is_active(self, child_run_id: str) -> bool:
        async with self._active_lock:
            return child_run_id in self._active

    async def resume(self, child_run_id: str) -> None:
        # No durable Codex thread/process restoration is implemented in this slice.
        raise RuntimeError(f"Codex executor cannot resume child run {child_run_id} yet.")

    async def _best_effort_interrupt(self, active: _ActiveTurn) -> None:
        if active.thread_id is None or active.turn_id is None:
            return
        try:
            await active.client.interrupt_turn(thread_id=active.thread_id, turn_id=active.turn_id)
        except Exception:  # noqa: BLE001 - caller already fails the run closed.
            return

    def from_terminal(self, child: Any, snapshot: ContextSnapshot) -> TaskResult:
        current = self.run_manager.get_run(child.run_id) or child
        if current.status == AgentRunStatus.COMPLETED:
            answer = str((current.result_snapshot or {}).get("answer") or "Codex task completed.")
            staged_count = int((current.result_snapshot or {}).get("staged_change_count") or 0)
            if staged_count:
                return self._task_result(
                    child, snapshot, status=TaskResultStatus.PARTIAL,
                    summary=f"{answer}\n\nCodex staged {staged_count} file change(s); source files were not modified.",
                    missing_requirements=("approve_and_apply_staged_changes",),
                    warnings=("Codex changes are isolated and have not been applied to the source workspace.",),
                )
            return self._task_result(child, snapshot, status=TaskResultStatus.COMPLETED, summary=answer)
        if current.status == AgentRunStatus.CANCELLED:
            return self._task_result(child, snapshot, status=TaskResultStatus.CANCELLED, summary="Codex child run cancelled.")
        if current.status == AgentRunStatus.TIMED_OUT:
            return self._task_result(
                child,
                snapshot,
                status=TaskResultStatus.TIMED_OUT,
                summary=current.error or "Codex child run timed out.",
                failure=FailureDetail(category="runtime", code="timeout", message=current.error or "Codex child run timed out.", retryable=True),
            )
        failure = FailureDetail(
            category="codex",
            code=current.error_type or current.status.value,
            message=current.error or f"Codex child run is {current.status.value}.",
        )
        return self._task_result(
            child,
            snapshot,
            status=TaskResultStatus.BLOCKED if current.status in {AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER} else TaskResultStatus.FAILED,
            summary=failure.message,
            failure=failure,
        )

    def _blocked(
        self,
        child: Any,
        snapshot: ContextSnapshot,
        *,
        code: str,
        message: str,
    ) -> TaskResult:
        current = self.run_manager.get_run(child.run_id)
        if current is not None and current.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING}:
            self.run_manager.fail_child_run(child.run_id, error_type=code, error=message)
        self._record(child, "codex_execution_blocked", message, {"code": code})
        failure = FailureDetail(category="codex", code=code, message=message, retryable=False)
        return self._task_result(child, snapshot, status=TaskResultStatus.BLOCKED, summary=message, failure=failure)

    def _fail(self, child: Any, snapshot: ContextSnapshot, code: str, message: str) -> TaskResult:
        current = self.run_manager.get_run(child.run_id)
        if current is not None and current.status in {
            AgentRunStatus.QUEUED,
            AgentRunStatus.RUNNING,
            AgentRunStatus.WAITING_CONFIRMATION,
            AgentRunStatus.WAITING_USER,
        }:
            self.run_manager.fail_child_run(child.run_id, error_type=code, error=message)
        current = self.run_manager.get_run(child.run_id) or child
        if current.status == AgentRunStatus.CANCELLED:
            return self.from_terminal(current, snapshot)
        failure = FailureDetail(category="codex", code=code, message=message, retryable=False)
        return self._task_result(child, snapshot, status=TaskResultStatus.FAILED, summary=message, failure=failure)

    def _task_result(
        self,
        child: Any,
        snapshot: ContextSnapshot,
        *,
        status: TaskResultStatus,
        summary: str,
        failure: FailureDetail | None = None,
        missing_requirements: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> TaskResult:
        return TaskResult(
            correlation_id=child.trace_id,
            result_id=f"result_{child.run_id}",
            child_run_id=child.run_id,
            plan_id=child.plan_id or snapshot.plan_id,
            step_id=child.step_id or snapshot.step_id,
            snapshot_id=snapshot.snapshot_id,
            attempt=int(child.attempt or 1),
            status=status,
            summary=summary or "Codex expert returned an empty result.",
            failure=failure,
            missing_requirements=missing_requirements,
            warnings=warnings,
        )

    def _record(
        self,
        child: Any,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.run_manager.append_event(
            child.run_id,
            event_type,
            message,
            stage="codex_expert",
            payload=payload or {},
            parent_run_id=child.parent_run_id,
            child_run_id=child.run_id,
            plan_id=child.plan_id,
            step_id=child.step_id,
            attempt=child.attempt,
        )

    def _record_native(
        self,
        child_run_id: str,
        method: str,
        payload: dict[str, Any],
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        request_id: int | str | None = None,
    ) -> None:
        """Keep complete Codex data in a private journal, never public run events."""
        if self.trace_journal is None:
            return
        self.trace_journal.append(
            child_run_id, method, payload,
            thread_id=thread_id, turn_id=turn_id,
            item_id=item_id, request_id=request_id,
        )

    def _is_cancelled(self, child_run_id: str) -> bool:
        current = self.run_manager.get_run(child_run_id)
        return (
            current is None
            or current.status in {
                AgentRunStatus.CANCELLED, AgentRunStatus.TIMED_OUT,
            }
            or self.run_manager.is_cancel_requested(child_run_id)
        )


def _required_id(value: dict[str, Any], key: str, method: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{method} response is missing {key}.")
    return item


def staged_file_change_approval_guard(event: ApprovalRequest, workspace_root: Path) -> bool:
    """Allow only file-change approvals that stay within a disposable lease.

    Command approvals may represent sandbox or network escalation and cannot
    be proven safe from their command text or cwd alone.
    """
    if event.method != "item/fileChange/requestApproval":
        return False
    grant_root = event.params.get("grantRoot")
    if not isinstance(grant_root, str) or not grant_root:
        return False
    try:
        requested = Path(grant_root).resolve(strict=True)
        root = workspace_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return requested == root or requested.is_relative_to(root)


def _normalized_event_type(method: str) -> str:
    return "codex_" + method.replace("/", "_").replace(".", "_")


def _answer_from_items(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    messages = [
        item.get("text", "")
        for item in items
        if isinstance(item, dict) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str)
    ]
    return "\n".join(text for text in messages if text).strip()


def _correlation_mismatch(
    params: dict[str, Any], *, thread_id: str | None, turn_id: str | None
) -> bool:
    event_thread_id = params.get("threadId")
    event_turn_id = params.get("turnId")
    turn = params.get("turn")
    if event_turn_id is None and isinstance(turn, dict):
        event_turn_id = turn.get("id")
    item = params.get("item")
    if event_turn_id is None and isinstance(item, dict):
        event_turn_id = item.get("turnId")
    return bool(
        (event_thread_id is not None and event_thread_id != thread_id)
        or (event_turn_id is not None and event_turn_id != turn_id)
    )


def _safe_notification_payload(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Keep durable public run events to identifiers, statuses and counts."""
    safe: dict[str, Any] = {}
    for key in ("threadId", "turnId", "itemId", "requestId"):
        value = params.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            safe[key] = value
    if method == "turn/completed":
        turn = params.get("turn")
        if isinstance(turn, dict):
            if isinstance(turn.get("status"), str):
                safe["status"] = turn["status"]
            if isinstance(turn.get("id"), str):
                safe["turnId"] = turn["id"]
            if isinstance(turn.get("items"), list):
                safe["item_count"] = len(turn["items"])
    else:
        for key in ("status", "phase"):
            value = params.get(key)
            if isinstance(value, str):
                safe[key] = value
        item = params.get("item")
        if isinstance(item, dict):
            for key in ("id", "type", "status", "phase"):
                value = item.get(key)
                if isinstance(value, str):
                    safe[f"item_{key}"] = value
    if method.endswith("/delta"):
        delta = params.get("delta")
        if isinstance(delta, str):
            safe["delta_length"] = len(delta)
    if method == "error":
        error = params.get("error")
        if isinstance(error, dict):
            info = error.get("codexErrorInfo")
            if info in _KNOWN_CODEX_ERROR_CODES:
                safe["error_code"] = info
    return safe


_KNOWN_CODEX_ERROR_CODES = {
    "ContextWindowExceeded",
    "UsageLimitExceeded",
    "HttpConnectionFailed",
    "ResponseStreamConnectionFailed",
    "ResponseStreamDisconnected",
    "ResponseTooManyFailedAttempts",
    "BadRequest",
    "Unauthorized",
    "SandboxError",
    "InternalServerError",
    "Other",
}
