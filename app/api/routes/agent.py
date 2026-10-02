import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.api.schemas import (
    AgentChildRunSnapshot,
    AgentRunEventsResponse,
    AgentRunResponse,
    AgentRunSnapshotResponse,
    AgentTaskResultSnapshot,
    AgentTurnRequest,
    AgentTurnResponse,
    ContinueAgentRunRequest,
    ContinueAgentRunResponse,
    PendingAgentQuestionResponse,
    SafetyReviewDecisionRequest,
    SafetyReviewListResponse,
    SafetyReviewQueueResponse,
    SafetyReviewResponse,
)
from app.core.agent_graph import AgentTurnWaitingForConfirmation
from app.core.agent_runs import AgentRunEvent, AgentRunStatus
from app.core.agent_turn import AgentTurnResult
from app.core.llm import LLMResponseMode
from app.core.multi_agent import Plan, TaskResult
from app.core.safety import SafetyReviewQueueConflict, SafetyReviewRecord

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/turn", response_model=AgentTurnResponse)
async def run_agent_turn_endpoint(
    payload: AgentTurnRequest,
    request: Request,
) -> AgentTurnResponse | JSONResponse:
    llm_options = payload.llm
    try:
        result = await request.app.state.runtime.run_agent_turn_async(
            session_id=payload.session_id,
            user_input=payload.user_input,
            llm_client_name=llm_options.client_name if llm_options else None,
            llm_model=llm_options.model if llm_options else None,
            llm_response_mode=llm_options.response_mode if llm_options else LLMResponseMode.TEXT,
            safety_review_mode=payload.safety_review_mode,
        )
    except AgentTurnWaitingForConfirmation as exc:
        return JSONResponse(
            status_code=202,
            content={
                "status": "waiting_confirmation",
                "run_id": exc.run_id,
                "review_id": exc.review_id,
            },
        )
    return AgentTurnResponse.from_result(result)


def run_agent_turn(payload: AgentTurnRequest, request: Request) -> AgentTurnResult:
    """Synchronous test helper preserving the old direct-call path."""

    llm_options = payload.llm
    result = request.app.state.runtime.run_agent_turn(
        session_id=payload.session_id,
        user_input=payload.user_input,
        llm_client_name=llm_options.client_name if llm_options else None,
        llm_model=llm_options.model if llm_options else None,
        llm_response_mode=llm_options.response_mode if llm_options else LLMResponseMode.TEXT,
        safety_review_mode=payload.safety_review_mode,
    )
    return result


@router.post("/turn/stream")
async def stream_agent_turn(
    payload: AgentTurnRequest,
    request: Request,
) -> StreamingResponse:
    run = request.app.state.runtime.create_agent_run(
        session_id=payload.session_id,
        user_input=payload.user_input,
    )
    return StreamingResponse(
        _agent_turn_event_stream(request=request, run_id=run.run_id, start_payload=payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{run_id}", response_model=AgentRunResponse)
async def get_agent_run(run_id: str, request: Request) -> AgentRunResponse:
    run = request.app.state.runtime.agent_run_manager.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    return AgentRunResponse.from_record(run)


@router.get("/runs/{run_id}/snapshot", response_model=AgentRunSnapshotResponse)
async def get_agent_run_snapshot(run_id: str, request: Request) -> AgentRunSnapshotResponse:
    """Return validated plan state and safe, durable child result summaries."""
    runtime = request.app.state.runtime
    run_manager = runtime.agent_run_manager
    parent = run_manager.get_run(run_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")

    raw_plan = parent.metadata.get("multi_agent_plan")
    plan: Plan | None = None
    if isinstance(raw_plan, dict):
        try:
            plan = Plan.model_validate(raw_plan)
        except ValidationError as exc:
            raise HTTPException(status_code=500, detail="Persisted multi-Agent plan is invalid.") from exc
        if plan.parent_run_id != parent.run_id or plan.session_id != parent.session_id:
            raise HTTPException(status_code=500, detail="Persisted multi-Agent plan ownership is invalid.")

    tree_runs = run_manager.child_tree(run_id)
    by_parent: dict[str, dict[str, Plan]] = {}
    for record in [parent, *tree_runs]:
        record_plan = record.metadata.get("multi_agent_plan")
        if isinstance(record_plan, dict):
            parsed = _validate_plan(record_plan)
            if parsed is not None:
                by_parent[record.run_id] = {step.step_id: step for step in parsed.steps}

    task_results: dict[str, TaskResult] = {}
    for record in [parent, *tree_runs]:
        for event in run_manager.list_events(record.run_id):
            if event.type != "subtask_result":
                continue
            value = event.payload.get("task_result")
            if not isinstance(value, dict):
                continue
            task_result = _validate_task_result(value)
            if task_result is not None:
                task_results[task_result.child_run_id] = task_result

    def direct_children(parent_record):
        return [
            child for child_id in parent_record.child_run_ids
            if (child := run_manager.get_run(child_id)) is not None
        ]

    def child_snapshot(child, depth: int) -> AgentChildRunSnapshot:
        step = by_parent.get(child.parent_run_id or "", {}).get(child.step_id or "")
        task_result = task_results.get(child.run_id)
        public_result = _public_task_result(task_result) if task_result else None
        return AgentChildRunSnapshot(
            run_id=child.run_id,
            parent_run_id=child.parent_run_id or run_id,
            plan_id=child.plan_id,
            step_id=child.step_id,
            attempt=child.attempt,
            status=child.status,
            depth=depth,
            pending_user_question=(
                PendingAgentQuestionResponse.from_metadata(child.metadata)
                if child.status == AgentRunStatus.WAITING_USER
                and not child.metadata.get("waiting_child_user_run_ids")
                and not child.metadata.get("pending_user_answer_command_id")
                else None
            ),
            step_status=step.status.value if step else None,
            step=step.model_dump(mode="json") if step else None,
            has_result=public_result is not None or child.result_snapshot is not None,
            result=public_result,
            children=[child_snapshot(grandchild, depth + 1)
                      for grandchild in direct_children(child)],
        )

    return AgentRunSnapshotResponse(
        run=AgentRunResponse.from_record(parent),
        plan=plan.model_dump(mode="json") if plan else None,
        children=[child_snapshot(child, 1) for child in direct_children(parent)],
    )


@router.post(
    "/runs/{parent_run_id}/children/{child_run_id}/cancel",
    response_model=AgentRunResponse,
)
async def cancel_agent_child_run(
    parent_run_id: str,
    child_run_id: str,
    request: Request,
) -> AgentRunResponse:
    """Cancel one child through the scheduler's ownership-checked control path."""
    runtime = request.app.state.runtime
    run_manager = runtime.agent_run_manager
    parent = run_manager.get_run(parent_run_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Parent Agent run not found.")
    child = run_manager.get_run(child_run_id)
    if child is None or child.parent_run_id != parent_run_id:
        raise HTTPException(status_code=404, detail="Child Agent run not found under this parent.")
    if parent.status not in {
        AgentRunStatus.RUNNING,
        AgentRunStatus.WAITING_CONFIRMATION,
        AgentRunStatus.WAITING_USER,
    }:
        raise HTTPException(status_code=409, detail="Parent Agent run is not active.")
    cancel = getattr(runtime, "cancel_multi_agent_child", None)
    if not callable(cancel):
        raise HTTPException(status_code=409, detail="The scheduler does not support child cancellation.")
    try:
        cancelled = cancel(parent_run_id=parent_run_id, child_run_id=child_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if child.status in {
        AgentRunStatus.QUEUED, AgentRunStatus.RUNNING,
        AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER,
    } and cancelled.status == AgentRunStatus.CANCELLED:
        _start_parent_scheduler_resume_task(runtime=runtime, parent_run_id=parent_run_id)
    return AgentRunResponse.from_record(cancelled)


@router.post(
    "/runs/{parent_run_id}/children/{child_run_id}/retry",
    response_model=AgentRunSnapshotResponse,
)
async def retry_agent_child_run(
    parent_run_id: str,
    child_run_id: str,
    request: Request,
) -> AgentRunSnapshotResponse:
    """Retry one failed child attempt through the scheduler control path."""
    runtime = request.app.state.runtime
    run_manager = runtime.agent_run_manager
    parent = run_manager.get_run(parent_run_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Parent Agent run not found.")
    child = run_manager.get_run(child_run_id)
    if child is None or child.parent_run_id != parent_run_id:
        raise HTTPException(status_code=404, detail="Child Agent run not found under this parent.")
    retry = getattr(runtime, "retry_multi_agent_child", None)
    if not callable(retry):
        raise HTTPException(status_code=409, detail="The scheduler does not support child retries.")
    try:
        await retry(parent_run_id=parent_run_id, child_run_id=child_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _start_parent_scheduler_resume_task(runtime=runtime, parent_run_id=parent_run_id)
    return await get_agent_run_snapshot(parent_run_id, request)


@router.get("/runs/{run_id}/events", response_model=AgentRunEventsResponse)
async def list_agent_run_events(
    run_id: str,
    request: Request,
    after_sequence: int = 0,
) -> AgentRunEventsResponse:
    run_manager = request.app.state.runtime.agent_run_manager
    if run_manager.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    return AgentRunEventsResponse(
        run_id=run_id,
        events=[
            _public_agent_event(event)
            for event in run_manager.list_events(run_id, after_sequence=after_sequence)
        ],
    )


@router.get("/runs/{run_id}/stream")
async def reconnect_agent_turn_stream(
    run_id: str,
    request: Request,
    after_sequence: int = 0,
) -> StreamingResponse:
    run_manager = request.app.state.runtime.agent_run_manager
    if run_manager.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    return StreamingResponse(
        _agent_turn_event_stream(request=request, run_id=run_id, after_sequence=after_sequence),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/cancel", response_model=AgentRunResponse)
async def cancel_agent_run(run_id: str, request: Request) -> AgentRunResponse:
    run_manager = request.app.state.runtime.agent_run_manager
    run = run_manager.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    if run.status not in {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
        AgentRunStatus.TIMED_OUT,
    }:
        run_manager.cancel_run(run_id, reason="api_cancelled")
    return AgentRunResponse.from_record(run_manager.get_run(run_id) or run)


@router.post("/runs/{run_id}/resume", response_model=AgentRunResponse)
async def resume_agent_run(run_id: str, request: Request) -> AgentRunResponse:
    """Resume one incomplete LangGraph run after an explicit client request."""

    runtime = request.app.state.runtime
    run_manager = runtime.agent_run_manager
    run = run_manager.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    if run.status in {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
        AgentRunStatus.TIMED_OUT,
    }:
        raise HTTPException(status_code=409, detail="Terminal Agent runs cannot be resumed.")
    if run.status == AgentRunStatus.WAITING_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail="Waiting Agent runs must be resumed through their safety-review decision.",
        )
    if run.status == AgentRunStatus.WAITING_USER:
        raise HTTPException(
            status_code=409,
            detail="Waiting Agent runs must be continued with a user answer.",
        )
    if run.status != AgentRunStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Agent run has no resumable checkpoint yet.")
    if getattr(runtime.agent_turn_runner, "orchestrator_name", None) != "langgraph":
        raise HTTPException(status_code=409, detail="Run recovery requires the LangGraph orchestrator.")
    # ChildAgentExecutor and the scheduler share this runtime runner instance.
    # Keep HTTP recovery on that same path so AgentGraphRunner's per-run lease
    # serializes it with any in-flight scheduler execution of this child.
    _start_agent_resume_task(runtime=runtime, run_id=run_id)
    return AgentRunResponse.from_record(run_manager.get_run(run_id) or run)


@router.post(
    "/runs/{run_id}/continue",
    response_model=ContinueAgentRunResponse,
    status_code=202,
)
async def continue_agent_run(
    run_id: str,
    payload: ContinueAgentRunRequest,
    request: Request,
) -> ContinueAgentRunResponse:
    """Journal one answer to a pending Agent question, then optionally schedule resume."""

    runtime = request.app.state.runtime
    run_manager = runtime.agent_run_manager
    try:
        run, question_id, replayed = run_manager.continue_user_question(
            run_id=run_id,
            command_id=payload.command_id,
            answer=payload.answer,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent run not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    resume_scheduled = False
    hook = getattr(runtime, "resume_multi_agent_user_question_async", None)
    if (
        callable(hook)
        and run.status == AgentRunStatus.RUNNING
        and run.metadata.get("pending_user_answer_command_id") == payload.command_id
    ):
        _start_agent_user_continuation_task(
            runtime=runtime,
            run_id=run_id,
            command_id=payload.command_id,
        )
        resume_scheduled = True
    return ContinueAgentRunResponse(
        run_id=run_id,
        command_id=payload.command_id,
        question_id=question_id,
        status=run.status,
        replayed=replayed,
        resume_scheduled=resume_scheduled,
    )


@router.get(
    "/safety-reviews",
    response_model=SafetyReviewQueueResponse,
)
async def list_agent_safety_review_queue(request: Request) -> SafetyReviewQueueResponse:
    """Return actionable manual reviews in global FIFO order."""
    run_manager = request.app.state.runtime.agent_run_manager
    # This query reads the indexed local SQLite queue and materializes only
    # compact summaries. It is intentionally kept on the request thread so the
    # endpoint remains compatible with synchronous in-process consumers.
    reviews = run_manager.list_pending_safety_reviews()
    return SafetyReviewQueueResponse(
        reviews=[_public_safety_review(review, run_manager) for review in reviews]
    )


@router.get(
    "/runs/{run_id}/safety-reviews",
    response_model=SafetyReviewListResponse,
)
async def list_agent_run_safety_reviews(
    run_id: str,
    request: Request,
) -> SafetyReviewListResponse:
    run_manager = request.app.state.runtime.agent_run_manager
    if run_manager.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    reviews = run_manager.list_safety_reviews(run_id)
    return SafetyReviewListResponse(
        reviews=[
            _public_safety_review(review, run_manager)
            for review in reviews
        ]
    )


@router.get(
    "/safety-reviews/{review_id}",
    response_model=SafetyReviewResponse,
)
async def get_agent_safety_review(
    review_id: str,
    request: Request,
) -> SafetyReviewResponse:
    run_manager = request.app.state.runtime.agent_run_manager
    review = run_manager.get_safety_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Safety review not found.")
    return _public_safety_review(review, run_manager)


@router.post(
    "/safety-reviews/{review_id}/decision",
    response_model=SafetyReviewResponse,
)
async def decide_agent_safety_review(
    review_id: str,
    payload: SafetyReviewDecisionRequest,
    request: Request,
) -> SafetyReviewResponse:
    run_manager = request.app.state.runtime.agent_run_manager
    pending = run_manager.get_safety_review(review_id)
    if pending is not None and pending.status.value == "pending" and pending.tool_name.startswith("codex."):
        executor = getattr(request.app.state.runtime, "codex_expert_executor", None)
        if executor is None or not await executor.is_active(pending.run_id):
            raise HTTPException(
                status_code=409,
                detail="Codex execution is no longer active; this approval cannot be replayed.",
            )
    try:
        review, _ = run_manager.decide_safety_review_with_transition(
            review_id=review_id,
            decision=payload.decision,
            decided_by=payload.decided_by,
            reason=payload.reason,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Safety review not found.") from exc
    except SafetyReviewQueueConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "head_review_id": exc.head_review_id},
        ) from exc
    runtime = request.app.state.runtime
    run = run_manager.get_run(review.run_id)
    if (
        review.status.value in {"approved", "rejected"}
        and run is not None
        and run.status == AgentRunStatus.RUNNING
        and not review.tool_name.startswith("codex.")
    ):
        # Replaying a native Agent decision recovers its graph continuation.
        # Codex approvals are consumed by the active external executor; they
        # must never be routed into a LangGraph checkpoint.
        _start_agent_resume_task(runtime=runtime, run_id=review.run_id)
    return _public_safety_review(review, run_manager)


def _public_safety_review(review, run_manager) -> SafetyReviewResponse:
    run = run_manager.get_run(review.run_id)
    response = SafetyReviewResponse.from_record(review)
    return response.model_copy(update={
        "parent_run_id": run.parent_run_id if run is not None else None,
        "child_run_id": run.run_id if run is not None and run.parent_run_id else None,
    })


def _public_task_result(result: TaskResult) -> AgentTaskResultSnapshot:
    # TaskResult only stores references. Artifact payload rows may contain
    # arbitrary user data, so expose the reference IDs without loading payloads.
    artifacts = [{"artifact_id": artifact_id} for artifact_id in result.artifact_refs[:64]]
    verification = None
    if result.verification is not None:
        verification = {
            "status": result.verification.status.value,
            "summary": result.verification.summary[:4000],
            "evidence_refs": [ref.model_dump(mode="json") for ref in result.verification.evidence_refs[:64]],
            "missing_requirements": list(result.verification.missing_requirements[:64]),
        }
    failure = None
    if result.failure is not None:
        failure = {
            "category": result.failure.category,
            "code": result.failure.code,
            "retryable": result.failure.retryable,
        }
    return AgentTaskResultSnapshot(
        result_id=result.result_id,
        child_run_id=result.child_run_id,
        plan_id=result.plan_id,
        step_id=result.step_id,
        snapshot_id=result.snapshot_id,
        status=result.status.value,
        summary=result.summary[:4000],
        artifact_refs=list(result.artifact_refs[:64]),
        artifacts=artifacts,
        evidence_refs=[ref.model_dump(mode="json") for ref in result.evidence_refs[:64]],
        verification=verification,
        failure=failure,
        missing_requirements=list(result.missing_requirements[:64]),
        warnings=[warning[:1000] for warning in result.warnings[:64]],
        completed_at=result.completed_at.isoformat(),
    )


def _validate_plan(value: dict) -> Plan | None:
    try:
        return Plan.model_validate(value)
    except ValidationError:
        return None


def _validate_task_result(value: dict) -> TaskResult | None:
    try:
        return TaskResult.model_validate(value)
    except ValidationError:
        return None


async def _agent_turn_event_stream(
    *,
    request: Request,
    run_id: str,
    after_sequence: int = 0,
    start_payload: AgentTurnRequest | None = None,
) -> AsyncIterator[str]:
    runtime = request.app.state.runtime
    run_manager = runtime.agent_run_manager
    if start_payload is not None:
        _start_agent_turn_task(runtime=runtime, payload=start_payload, run_id=run_id)
    last_sequence = after_sequence
    last_sent_at = time.monotonic()
    while True:
        if await _is_client_disconnected(request):
            # A transport disconnect is not a run cancellation.  The
            # background task and durable event log allow reconnection by ID.
            break

        events = run_manager.list_events(run_id, after_sequence=last_sequence)
        for event in events:
            last_sequence = event.sequence
            last_sent_at = time.monotonic()
            yield _sse_event_frame(_public_agent_event(event))

        run = run_manager.get_run(run_id)
        if run and run.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.TIMED_OUT,
        }:
            # Completion may publish its final events after the first read.
            # Once terminal status is visible, drain that durable tail before
            # closing so this stream and a reconnect see the same sequence.
            for event in run_manager.list_events(run_id, after_sequence=last_sequence):
                last_sequence = event.sequence
                yield _sse_event_frame(_public_agent_event(event))
            break

        if time.monotonic() - last_sent_at >= 15:
            last_sent_at = time.monotonic()
            yield _heartbeat_frame(run_id=run_id, sequence=last_sequence)

        await asyncio.sleep(0.05)


def _start_agent_turn_task(*, runtime, payload: AgentTurnRequest, run_id: str) -> asyncio.Task:
    tasks = getattr(runtime, "_agent_turn_tasks", None)
    if tasks is None:
        tasks = {}
        runtime._agent_turn_tasks = tasks
    existing = tasks.get(run_id)
    if existing is not None and not existing.done():
        return existing
    llm_options = payload.llm
    task = asyncio.create_task(
        runtime.run_agent_turn_async(
            session_id=payload.session_id,
            user_input=payload.user_input,
            llm_client_name=llm_options.client_name if llm_options else None,
            llm_model=llm_options.model if llm_options else None,
            llm_response_mode=_stream_turn_llm_response_mode(payload),
            safety_review_mode=payload.safety_review_mode,
            existing_run_id=run_id,
        )
    )
    tasks[run_id] = task

    def clear(completed: asyncio.Task) -> None:
        _consume_task_exception(completed)
        if tasks.get(run_id) is completed:
            tasks.pop(run_id, None)

    task.add_done_callback(clear)
    return task


def _start_agent_resume_task(*, runtime, run_id: str) -> asyncio.Task:
    """Schedule at most one in-process resume for a durable graph thread."""

    tasks = getattr(runtime, "_agent_turn_tasks", None)
    if tasks is None:
        tasks = {}
        runtime._agent_turn_tasks = tasks
    existing = tasks.get(run_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(_resume_agent_and_parent(runtime=runtime, run_id=run_id))
    tasks[run_id] = task

    def clear(completed: asyncio.Task) -> None:
        _consume_task_exception(completed)
        if tasks.get(run_id) is completed:
            tasks.pop(run_id, None)

    task.add_done_callback(clear)
    return task


def _start_agent_user_continuation_task(*, runtime, run_id: str, command_id: str) -> asyncio.Task:
    """Schedule a trusted runtime hook with durable-answer lookup by command ID."""

    tasks = getattr(runtime, "_agent_turn_tasks", None)
    if tasks is None:
        tasks = {}
        runtime._agent_turn_tasks = tasks
    existing = tasks.get(run_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(
        runtime.resume_multi_agent_user_question_async(run_id, command_id)
    )
    tasks[run_id] = task

    def clear(completed: asyncio.Task) -> None:
        _consume_task_exception(completed)
        if tasks.get(run_id) is completed:
            tasks.pop(run_id, None)

    task.add_done_callback(clear)
    return task


def _start_parent_scheduler_resume_task(*, runtime, parent_run_id: str) -> asyncio.Task | None:
    resume = getattr(runtime, "resume_multi_agent_parent_async", None)
    if not callable(resume):
        return None
    tasks = getattr(runtime, "_agent_turn_tasks", None)
    if tasks is None:
        tasks = {}
        runtime._agent_turn_tasks = tasks
    existing = tasks.get(parent_run_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(resume(parent_run_id))
    tasks[parent_run_id] = task

    def clear(completed: asyncio.Task) -> None:
        _consume_task_exception(completed)
        if tasks.get(parent_run_id) is completed:
            tasks.pop(parent_run_id, None)

    task.add_done_callback(clear)
    return task


async def _resume_agent_and_parent(*, runtime, run_id: str) -> None:
    await runtime.resume_agent_run_async(run_id)
    run_manager = runtime.agent_run_manager
    resumed = run_manager.get_run(run_id)
    parent_run_id = resumed.parent_run_id if resumed is not None else None
    if (
        parent_run_id
        and resumed.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.TIMED_OUT,
        }
        and callable(getattr(runtime, "resume_multi_agent_parent_async", None))
    ):
        descendants = run_manager.child_tree(parent_run_id)
        if not any(
            child.status in {AgentRunStatus.WAITING_CONFIRMATION, AgentRunStatus.WAITING_USER}
            for child in descendants
        ):
            await runtime.resume_multi_agent_parent_async(parent_run_id)


def _stream_turn_llm_response_mode(payload: AgentTurnRequest) -> LLMResponseMode:
    llm_options = payload.llm
    if llm_options is None:
        return LLMResponseMode.STREAM
    if "response_mode" in llm_options.model_fields_set:
        return llm_options.response_mode
    return LLMResponseMode.STREAM


def _sse_event_frame(event: AgentRunEvent) -> str:
    payload = event.model_dump(mode="json")
    if event.type == "final_answer":
        answer = _full_final_answer_from_event_payload(payload)
        if answer is not None:
            payload["message"] = answer
    payload["stream_part"] = _stream_part_for_event(event)
    data = json.dumps(payload, ensure_ascii=False)
    return f"id: {event.run_id}:{event.sequence}\nevent: {event.type}\ndata: {data}\n\n"


def _public_agent_event(event: AgentRunEvent) -> AgentRunEvent:
    """Remove raw tool data from transport events while retaining UI state."""

    payload = dict(event.payload)
    # Run logs retain prompts and local evidence, so their filesystem location is
    # an internal implementation detail and must never cross the public boundary.
    payload.pop("log_path", None)
    if event.type in {"llm_started", "llm_completed", "llm_failed"}:
        payload.pop("audit_record", None)
    elif event.type in {
        "safety_review",
        "safety_review_required",
        "safety_review_decided",
    }:
        review = payload.get("review")
        if isinstance(review, dict):
            record = SafetyReviewRecord.model_validate(review)
            safe_review = SafetyReviewResponse.from_record(record)
            payload = {
                "review": safe_review.model_copy(update={
                    "parent_run_id": event.parent_run_id,
                    "child_run_id": event.child_run_id,
                }).model_dump(mode="json")
            }
        else:
            payload = {}
    elif event.type in {
        "tool_started",
        "tool_completed",
        "tool_feedback",
        "tool_recovered",
        "tool_execution_uncertain",
    }:
        payload = {
            key: payload[key]
            for key in ("tool_name", "package_name", "status")
            if key in payload
        }
    elif event.type == "subtask_result":
        raw_result = payload.get("task_result")
        if isinstance(raw_result, dict):
            try:
                safe_result = _public_task_result(TaskResult.model_validate(raw_result))
                payload = {"task_result": safe_result.model_dump(mode="json")}
            except ValidationError:
                payload = {}
        else:
            payload = {}
    return event.model_copy(update={"payload": payload})


def _full_final_answer_from_event_payload(payload: dict) -> str | None:
    event_payload = payload.get("payload")
    if not isinstance(event_payload, dict):
        return None
    metadata = event_payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    answer = metadata.get("answer")
    return answer if isinstance(answer, str) else None


def _stream_part_for_event(event: AgentRunEvent) -> str:
    if event.type in {"run_started", "run_completed", "run_failed", "run_cancelled"}:
        return "lifecycle"
    if event.type in {"tool_started", "tool_completed", "tool_failed"}:
        return "tool_result"
    if event.type == "llm_delta":
        return "llm_delta"
    if event.type in {"llm_started", "llm_completed", "llm_failed"}:
        return "llm_audit"
    if event.type in {"safety_review_required", "safety_review_decided"}:
        return "safety_review"
    if event.type == "final_answer":
        return "final_answer"
    return "progress"


def _heartbeat_frame(*, run_id: str, sequence: int) -> str:
    data = json.dumps(
        {
            "run_id": run_id,
            "sequence": sequence,
            "type": "heartbeat",
            "stage": "transport",
            "message": "heartbeat",
            "payload": {},
            "created_at": None,
        },
        ensure_ascii=False,
    )
    return f"id: {run_id}:{sequence}\nevent: heartbeat\ndata: {data}\n\n"


async def _is_client_disconnected(request: Request) -> bool:
    try:
        return await asyncio.wait_for(request.is_disconnected(), timeout=0.001)
    except TimeoutError:
        return False


def _consume_task_exception(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:  # noqa: BLE001 - task errors are persisted on the Agent run.
        return
