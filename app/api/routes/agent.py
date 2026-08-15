import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.schemas import AgentTurnRequest, AgentTurnResponse
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


async def _agent_turn_event_stream(
    *,
    payload: AgentTurnRequest,
    request: Request,
    run_id: str,
) -> AsyncIterator[str]:
    llm_options = payload.llm
    runtime = request.app.state.runtime
    run_manager = runtime.agent_run_manager
    task = asyncio.create_task(
        runtime.run_agent_turn_async(
            session_id=payload.session_id,
            user_input=payload.user_input,
            llm_client_name=llm_options.client_name if llm_options else None,
            llm_model=llm_options.model if llm_options else None,
            llm_response_mode=llm_options.response_mode
            if llm_options
            else LLMResponseMode.TEXT,
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


def _sse_event_frame(event: AgentRunEvent) -> str:
    payload = event.model_dump(mode="json")
    payload["stream_part"] = _stream_part_for_event(event)
    data = json.dumps(payload, ensure_ascii=False)
    return f"id: {event.run_id}:{event.sequence}\nevent: {event.type}\ndata: {data}\n\n"


def _stream_part_for_event(event: AgentRunEvent) -> str:
    if event.type in {"run_started", "run_completed", "run_failed", "run_cancelled"}:
        return "lifecycle"
    if event.type in {"tool_started", "tool_completed", "tool_failed"}:
        return "tool_result"
    if event.type == "llm_delta":
        return "llm_delta"
    if event.type in {"llm_started", "llm_completed", "llm_failed"}:
        return "llm_audit"
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
