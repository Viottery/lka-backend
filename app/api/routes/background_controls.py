"""Payload-free controls and observability for durable background work."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.routes.memories import require_local_memory_control
from app.api.routes.memory_settings import get_background_config

router = APIRouter(dependencies=[Depends(require_local_memory_control)])
_RETRYABLE_KINDS = {"memory_extract", "context_compact"}


class JobControlRequest(BaseModel):
    expected_updated_at: str = Field(min_length=1, max_length=64)


def _store(request: Request):
    return request.app.state.runtime.background_job_store


@router.get("/background/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = _store(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="background job not found")
    return {key: job[key] for key in (
        "job_id", "kind", "scope_id", "status", "priority", "available_at",
        "max_attempts", "attempts", "error_class", "lease_epoch",
        "lease_recovery_count", "created_at", "started_at", "finished_at", "updated_at",
    )}


@router.post("/background/jobs/{job_id}/retry")
def retry_job(job_id: str, payload: JobControlRequest, request: Request):
    state, job = _store(request).retry_controlled(
        job_id, payload.expected_updated_at, _RETRYABLE_KINDS,
    )
    if state == "missing":
        raise HTTPException(status_code=404, detail="background job not found")
    if state == "unsupported":
        raise HTTPException(status_code=422, detail="background job kind cannot be retried")
    if state == "expired":
        raise HTTPException(status_code=409, detail="background job payload or deadline has expired")
    if state == "conflict":
        raise HTTPException(status_code=409, detail="background job state changed or is not retryable")
    return {key: job[key] for key in ("job_id", "kind", "status", "attempts", "max_attempts", "updated_at")}


@router.post("/background/jobs/{job_id}/cancel")
def cancel_job(job_id: str, payload: JobControlRequest, request: Request):
    state, job = _store(request).cancel_controlled(job_id, payload.expected_updated_at)
    if state == "missing":
        raise HTTPException(status_code=404, detail="background job not found")
    if state == "conflict":
        raise HTTPException(status_code=409, detail="background job state changed or cannot be cancelled")
    return {key: job[key] for key in ("job_id", "kind", "status", "attempts", "updated_at")}


def _health(request: Request) -> dict:
    snapshot = _store(request).health_metrics()
    config = get_background_config(request)
    snapshot["config_revision"] = config["revision"]
    snapshot["config_restart_required"] = config["restart_required"]
    snapshot["llm_workloads"] = request.app.state.runtime.llm_workloads.health()
    return snapshot


async def _events(request: Request, interval: float) -> AsyncIterator[str]:
    previous = None
    while not await request.is_disconnected():
        snapshot = await asyncio.to_thread(_health, request)
        encoded = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
        if encoded != previous:
            yield f"event: background_health\ndata: {encoded}\n\n"
            previous = encoded
        else:
            yield ": keepalive\n\n"
        await asyncio.sleep(interval)


@router.get("/background/events")
async def background_events(
    request: Request,
    interval: float = Query(default=5, ge=1, le=30),
):
    return StreamingResponse(
        _events(request, interval), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}/context-status")
def context_status(session_id: str, request: Request):
    runtime = request.app.state.runtime
    if runtime.session_service.get_session_or_none(session_id=session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    conn = runtime._conn()
    try:
        tables = {item[0] for item in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('agent_session_context_windows','agent_session_context_state','agent_session_context_messages')"
        )}
        row = conn.execute(
            """SELECT token_budget,token_estimate,updated_at
               FROM agent_session_context_windows WHERE session_id=?""", (session_id,),
        ).fetchone() if "agent_session_context_windows" in tables else None
        state = conn.execute(
            """SELECT revision,next_seq,covered_seq,summary_revision,summary_metadata
               FROM agent_session_context_state WHERE session_id=?""", (session_id,),
        ).fetchone() if "agent_session_context_state" in tables else None
        if state is None:
            recent_count = conn.execute(
                "SELECT COUNT(*) FROM agent_session_context_messages WHERE session_id=?", (session_id,),
            ).fetchone()[0] if "agent_session_context_messages" in tables else 0
        else:
            recent_count = conn.execute(
                "SELECT COUNT(*) FROM agent_session_context_messages WHERE session_id=? AND seq>?",
                (session_id, state["covered_seq"]),
            ).fetchone()[0]
    finally:
        conn.close()
    metadata = json.loads(state["summary_metadata"]) if state else {}
    jobs = {
        status: len(_store(request).list(
            status=status, scope_id=session_id, kind="context_compact", limit=500,
        ))
        for status in ("queued", "retry_wait", "running")
    }
    return {
        "session_id": session_id,
        "method": metadata.get("method", "none"),
        "token_count_method": runtime.session_service.context_token_count_method,
        "token_budget": row["token_budget"] if row else runtime.session_service.default_context_token_budget,
        "token_estimate": row["token_estimate"] if row else 0,
        "estimate_as_of": row["updated_at"] if row else None,
        "summary_sequence": state["covered_seq"] if state else 0,
        "revision": state["revision"] if state else 0,
        "summary_revision": state["summary_revision"] if state else 0,
        "pending_sequence": state["next_seq"] - 1 if state else 0,
        "recent_message_count": recent_count,
        "degradation": {key: value for key, value in metadata.items()
                        if key in {"method", "model", "lossy_fallback_possible", "degraded"}},
        "degraded": metadata.get("method") == "local_fallback" or bool(metadata.get("degraded")),
        "active_compaction_jobs": jobs,
        "updated_at": row["updated_at"] if row else None,
    }
