"""Local recurring-watch controls and briefing inbox."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.domains.watch import WatchInput, WatchPatch

router = APIRouter(prefix="/watches", tags=["watches"])


@router.post("")
def create_watch(payload: WatchInput, request: Request) -> dict:
    try:
        return request.app.state.runtime.watch_service.create(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("")
def list_watches(
    request: Request,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    try:
        return request.app.state.runtime.watch_service.list(status=status, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/briefings")
def list_briefings(
    request: Request,
    unread_only: bool = False,
    watch_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return request.app.state.runtime.watch_service.list_briefings(
        unread_only=unread_only,
        watch_id=watch_id,
        limit=limit,
    )


@router.get("/briefings/{briefing_id}")
def get_briefing(briefing_id: str, request: Request) -> dict:
    try:
        return request.app.state.runtime.watch_service.get_briefing(briefing_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Briefing not found") from exc


@router.post("/briefings/{briefing_id}/read")
def mark_briefing_read(briefing_id: str, request: Request) -> dict:
    try:
        return request.app.state.runtime.watch_service.mark_briefing_read(briefing_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Briefing not found") from exc


@router.get("/{watch_id}")
def get_watch(watch_id: str, request: Request) -> dict:
    try:
        return request.app.state.runtime.watch_service.get(watch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watch not found") from exc


@router.get("/{watch_id}/runs")
def list_watch_runs(
    watch_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    try:
        return request.app.state.runtime.watch_service.list_occurrences(watch_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watch not found") from exc


@router.post("/{watch_id}/run-now")
def run_watch_now(watch_id: str, request: Request) -> dict:
    try:
        return request.app.state.runtime.watch_scheduler.enqueue_now(watch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watch not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/{watch_id}")
def update_watch(watch_id: str, payload: WatchPatch, request: Request) -> dict:
    try:
        runtime = request.app.state.runtime
        watch = runtime.watch_service.update(watch_id, payload)
        runtime.watch_scheduler.cancel_watch(watch_id)
        return watch
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watch not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{watch_id}/pause")
def pause_watch(watch_id: str, request: Request) -> dict:
    try:
        runtime = request.app.state.runtime
        watch = runtime.watch_service.set_paused(watch_id, True)
        runtime.watch_scheduler.cancel_watch(watch_id)
        return watch
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watch not found") from exc


@router.post("/{watch_id}/resume")
def resume_watch(watch_id: str, request: Request) -> dict:
    try:
        return request.app.state.runtime.watch_service.set_paused(watch_id, False)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watch not found") from exc


@router.delete("/{watch_id}")
def delete_watch(watch_id: str, request: Request) -> dict:
    try:
        runtime = request.app.state.runtime
        watch = runtime.watch_service.delete(watch_id)
        runtime.watch_scheduler.cancel_watch(watch_id)
        return watch
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watch not found") from exc
