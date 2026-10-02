from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes.watches import (
    create_watch,
    delete_watch,
    get_watch,
    list_briefings,
    list_watch_runs,
    list_watches,
    pause_watch,
    resume_watch,
    run_watch_now,
    update_watch,
)
from app.core.config import get_settings
from app.domains.watch import WatchInput, WatchPatch, WatchService
from app.storage.db import connect, get_db_path


def _request(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    from app.api.main import create_app

    app = create_app()
    assert any(
        getattr(route, "path", None) == "/watches"
        for included in app.routes
        for route in getattr(getattr(included, "original_router", included), "routes", ())
    )
    return SimpleNamespace(app=app)


def test_watch_crud_and_briefing_routes(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    created = create_watch(
        WatchInput(
            title="Performance tickets",
            goal="Check official ticket availability",
            timezone="Asia/Shanghai",
            daily_time="08:00",
            categories=["web"],
            scope={"domains": ["example.org"]},
        ),
        request,
    )
    watch_id = created["watch_id"]
    assert "session_id" not in created
    assert get_watch(watch_id, request)["goal"] == "Check official ticket availability"
    assert len(list_watches(request, limit=100)) == 1
    assert pause_watch(watch_id, request)["status"] == "paused"
    with pytest.raises(HTTPException) as paused_error:
        run_watch_now(watch_id, request)
    assert paused_error.value.status_code == 409
    assert resume_watch(watch_id, request)["status"] == "active"
    occurrence = run_watch_now(watch_id, request)
    assert occurrence["status"] == "pending"
    assert occurrence["session_id"].startswith("watch_session_")
    assert (
        list_watch_runs(watch_id, request, limit=50)[0]["occurrence_id"]
        == occurrence["occurrence_id"]
    )
    assert list_briefings(request, limit=100) == []
    assert delete_watch(watch_id, request)["status"] == "deleted"
    with pytest.raises(HTTPException) as exc:
        get_watch(watch_id, request)
    assert exc.value.status_code == 404


def test_watch_rejects_invalid_schedule(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        create_watch(
            WatchInput(
                title="News",
                goal="News",
                timezone="Not/AZone",
                daily_time="08:00",
            ),
            request,
        )
    assert exc.value.status_code == 422


def test_runtime_starts_and_stops_watch_workers(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    runtime = request.app.state.runtime
    runtime.start()
    try:
        assert len(runtime.watch_scheduler._threads) == 2
        assert all(thread.is_alive() for thread in runtime.watch_scheduler._threads)
    finally:
        runtime.stop()
    assert runtime.watch_scheduler._threads == []


def test_api_update_cancels_active_watch_execution(tmp_path):
    service = WatchService(lambda: connect(get_db_path(tmp_path / "data")))
    service.initialize()
    watch = service.create(
        WatchInput(
            title="Mail follow-up",
            goal="Check approved mailbox",
            timezone="UTC",
            daily_time="08:00",
            categories=[],
            scope={"source_ids": ["src"], "account_ids": ["acct"]},
        )
    )
    calls = []
    runtime = SimpleNamespace(
        watch_service=service, watch_scheduler=SimpleNamespace(cancel_watch=calls.append)
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    updated = update_watch(
        watch["watch_id"],
        WatchPatch(scope={"source_ids": ["src-2"], "account_ids": ["acct-2"]}),
        request,
    )
    assert updated["scope"]["source_ids"] == ["src-2"]
    assert calls == [watch["watch_id"]]
