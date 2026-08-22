import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.schemas import (
    AgentTurnRequest,
    AgentTurnResponse,
    SafetyReviewDecisionRequest,
    SafetyReviewListResponse,
    SafetyReviewResponse,
)
from app.core.agent_runs import AgentRunEvent, AgentRunStatus
from app.core.llm import LLMResponseMode

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/turn", response_model=AgentTurnResponse)
async def run_agent_turn_endpoint(
    payload: AgentTurnRequest,
    request: Request,
) -> AgentTurnResponse:
    llm_options = payload.llm
    result = await request.app.state.runtime.run_agent_turn_async(
        session_id=payload.session_id,
        user_input=payload.user_input,
        llm_client_name=llm_options.client_name if llm_options else None,
        llm_model=llm_options.model if llm_options else None,
        llm_response_mode=llm_options.response_mode if llm_options else LLMResponseMode.TEXT,
    )
    return AgentTurnResponse(**result.model_dump())


def run_agent_turn(payload: AgentTurnRequest, request: Request) -> AgentTurnResponse:
    """Synchronous test helper preserving the old direct-call path."""

    llm_options = payload.llm
    result = request.app.state.runtime.run_agent_turn(
        session_id=payload.session_id,
        user_input=payload.user_input,
        llm_client_name=llm_options.client_name if llm_options else None,
        llm_model=llm_options.model if llm_options else None,
        llm_response_mode=llm_options.response_mode if llm_options else LLMResponseMode.TEXT,
    )
    return AgentTurnResponse(**result.model_dump())


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
        _agent_turn_event_stream(payload=payload, request=request, run_id=run.run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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
    return SafetyReviewListResponse(reviews=run_manager.list_safety_reviews(run_id))


@router.get(
    "/safety-reviews/{review_id}",
    response_model=SafetyReviewResponse,
)
async def get_agent_safety_review(
    review_id: str,
    request: Request,
) -> SafetyReviewResponse:
    review = request.app.state.runtime.agent_run_manager.get_safety_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Safety review not found.")
    return SafetyReviewResponse(**review.model_dump(mode="python"))


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
    if run_manager.get_safety_review(review_id) is None:
        raise HTTPException(status_code=404, detail="Safety review not found.")
    review = run_manager.decide_safety_review(
        review_id=review_id,
        decision=payload.decision,
        decided_by=payload.decided_by,
        reason=payload.reason,
    )
    return SafetyReviewResponse(**review.model_dump(mode="python"))


async def _agent_turn_event_stream(
    *,
    payload: AgentTurnRequest,
    request: Request,
    run_id: str,
) -> AsyncIterator[str]:
    llm_options = payload.llm
    llm_response_mode = _stream_turn_llm_response_mode(payload)
    runtime = request.app.state.runtime
    run_manager = runtime.agent_run_manager
    task = asyncio.create_task(
        runtime.run_agent_turn_async(
            session_id=payload.session_id,
            user_input=payload.user_input,
            llm_client_name=llm_options.client_name if llm_options else None,
            llm_model=llm_options.model if llm_options else None,
            llm_response_mode=llm_response_mode,
            existing_run_id=run_id,
        )
    )
    task.add_done_callback(_consume_task_exception)
    last_sequence = 0
    last_sent_at = time.monotonic()
    try:
        while True:
            if await _is_client_disconnected(request):
                run_manager.request_cancel(run_id, reason="client_disconnected")
                break

            events = run_manager.list_events(run_id, after_sequence=last_sequence)
            for event in events:
                last_sequence = event.sequence
                last_sent_at = time.monotonic()
                yield _sse_event_frame(event)

            run = run_manager.get_run(run_id)
            if run and run.status in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
            }:
                if task.done():
                    break

            if task.done() and not events:
                break

            if time.monotonic() - last_sent_at >= 15:
                last_sent_at = time.monotonic()
                yield _heartbeat_frame(run_id=run_id, sequence=last_sequence)

            await asyncio.sleep(0.05)
    finally:
        if not task.done():
            run_manager.request_cancel(run_id, reason="stream_closed")


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
    except Exception:
        return
