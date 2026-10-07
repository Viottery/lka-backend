from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.api.routes import background_controls
from app.api.routes.background_controls import _events, router
from app.core.background_jobs import BackgroundJobStore
from app.core.llm_workloads import BackgroundCircuitOpen, LLMWorkloadController, workload_scope
from app.core.sessions import SessionService
from app.storage.db import connect, init_db


def _api(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_MEMORY_API_TOKEN", "control-test-token")
    path = tmp_path / "controls.sqlite3"
    store = BackgroundJobStore(path)
    store.ensure_schema()
    init_db(path)
    sessions = SessionService(lambda: connect(path))
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = type("Runtime", (), {
        "background_job_store": store, "session_service": sessions,
        "llm_workloads": LLMWorkloadController(path),
        "_conn": lambda self: connect(path),
    })()
    return app, store, sessions


def test_budget_reset_is_local_cas_control_not_message_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_MESSAGES_CONTROL_TOKEN", "message-control-only")
    app, _, _ = _api(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer control-test-token"}
    payload = {"expected_revisions": {"background_memory": 0, "background_message": 0}, "reason": "user reset"}

    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            assert (await client.post("/background/budgets/reset", json=payload)).status_code == 401
            assert (await client.post("/background/budgets/reset", json=payload,
                                     headers={"Authorization": "Bearer message-control-only"})).status_code == 401
            for revisions in ({"interactive": 0}, {"background_memory": True}, {"background_memory": -1}):
                assert (await client.post("/background/budgets/reset", json={**payload, "expected_revisions": revisions}, headers=headers)).status_code == 422
            response = await client.post("/background/budgets/reset", json=payload, headers=headers)
            assert response.status_code == 200
            states = {row["pool"]: row for row in response.json()["budget_pools"]}
            assert states["background_memory"]["revision"] == states["background_message"]["revision"] == 1
            assert len(response.json()["budget_events"]) == 2
            assert (await client.post("/background/budgets/reset", json=payload, headers=headers)).status_code == 409
            with workload_scope("background_memory"):
                async with app.state.runtime.llm_workloads.admit(input_tokens=1, output_tokens=1):
                    assert (await client.post("/background/budgets/reset", json={**payload, "expected_revisions": {"background_memory": 1}}, headers=headers)).status_code == 409
    asyncio.run(run())


def test_emergency_stop_keeps_jobs_recoverable_without_spending_retries(tmp_path, monkeypatch):
    from app.core.background_jobs import BackgroundJobWorker

    _, store, _ = _api(tmp_path, monkeypatch)
    job = _create(store)

    def emergency(_job):
        raise BackgroundCircuitOpen("hourly_tokens: 101 > 100")
    worker = BackgroundJobWorker(store, {"memory_extract": emergency})
    assert worker.run_one()
    row = store.get(job["job_id"])
    assert row["status"] == "retry_wait"
    assert row["error_class"] == "background_circuit_open"
    assert row["attempts"] == 0


def _create(store, kind="memory_extract", payload=None):
    return store.enqueue(kind, "scope", f"key-{time.time_ns()}", payload or {"message_id": "m1"})


def test_retry_control_api_cas_checkpoint_and_replay_scope(tmp_path, monkeypatch):
    app, store, _ = _api(tmp_path, monkeypatch)
    failed = _create(store)
    claim = store.claim("worker", 60)
    store.complete_input(claim["job_id"], "worker", claim["lease_epoch"], "m1")
    store.fail(claim["job_id"], "worker", claim["lease_epoch"], "temporary", False)
    failed = store.get(failed["job_id"])
    headers = {"Authorization": "Bearer control-test-token"}
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            assert (await client.post(f"/background/jobs/{failed['job_id']}/retry", json={})).status_code == 401
            bad = await client.post(f"/background/jobs/{failed['job_id']}/retry",
                                    json={"expected_updated_at": "stale"}, headers=headers)
            assert bad.status_code == 409
            response = await client.post(f"/background/jobs/{failed['job_id']}/retry",
                                         json={"expected_updated_at": failed["updated_at"]}, headers=headers)
            assert response.status_code == 200
            assert response.json()["job_id"] == failed["job_id"]
            detail = await client.get(f"/background/jobs/{failed['job_id']}", headers=headers)
            assert detail.status_code == 200
            assert "payload" not in detail.json()
            assert (await client.get("/background/jobs/missing", headers=headers)).status_code == 404
            assert store.completed_inputs(failed["job_id"]) == {"m1"}
            assert store.claim("worker-2", 60)["job_id"] == failed["job_id"]
            watch = _create(store, "watch_run", {"watch_id": "w1"})
            watch = store.get(watch["job_id"])
            denied = await client.post(f"/background/jobs/{watch['job_id']}/retry",
                                       json={"expected_updated_at": watch["updated_at"]}, headers=headers)
            assert denied.status_code == 422

    asyncio.run(exercise())


def test_cancel_fences_running_publisher_and_is_idempotent(tmp_path, monkeypatch):
    app, store, _ = _api(tmp_path, monkeypatch)
    item = _create(store, "context_compact", {"revision": 3, "target_seq": 12})
    claimed = store.claim("worker", 60)
    headers = {"Authorization": "Bearer control-test-token"}
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post(f"/background/jobs/{item['job_id']}/cancel",
                                         json={"expected_updated_at": claimed["updated_at"]}, headers=headers)
            assert response.status_code == 200
            assert response.json()["status"] == "cancelled"
            assert not store.complete(item["job_id"], "worker", claimed["lease_epoch"])
            again = await client.post(f"/background/jobs/{item['job_id']}/cancel",
                                      json={"expected_updated_at": "old-version"}, headers=headers)
            assert again.status_code == 200

    asyncio.run(exercise())


def test_context_status_is_payload_free_and_checks_deleted_sessions(tmp_path, monkeypatch):
    app, _, sessions = _api(tmp_path, monkeypatch)
    created = sessions.create_session(title="private title", initial_message="secret message")
    sid = created.session.session_id
    headers = {"Authorization": "Bearer control-test-token"}
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.get(f"/sessions/{sid}/context-status", headers=headers)
            assert response.status_code == 200
            assert "secret message" not in response.text
            assert "summary" not in response.json()
            assert "messages" not in response.json()
            assert (await client.get("/sessions/missing/context-status", headers=headers)).status_code == 404
            assert (await client.get(f"/sessions/{sid}/context-status")).status_code == 401
            sessions.delete_session(session_id=sid)
            assert (await client.get(f"/sessions/{sid}/context-status", headers=headers)).status_code == 404

    asyncio.run(exercise())


def test_retry_expired_deadline_missing_job_and_http_auth(tmp_path, monkeypatch):
    app, store, _ = _api(tmp_path, monkeypatch)
    item = _create(store)
    store.claim("worker", 60)
    with store._connect() as conn:
        conn.execute("UPDATE background_jobs SET status='failed',finished_at=?,updated_at=?,deadline=? WHERE job_id=?",
                     ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00",
                      "2020-01-01T00:00:00+00:00", item["job_id"]))
    failed = store.get(item["job_id"])

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            assert (await client.post(f"/background/jobs/{failed['job_id']}/retry",
                                      json={"expected_updated_at": failed["updated_at"]})).status_code == 401
            headers = {"Authorization": "Bearer control-test-token"}
            assert (await client.post("/background/jobs/no-such-job/retry",
                                      json={"expected_updated_at": "x"}, headers=headers)).status_code == 404
            assert (await client.post(f"/background/jobs/{failed['job_id']}/retry",
                                      json={"expected_updated_at": failed["updated_at"]}, headers=headers)).status_code == 409

    asyncio.run(exercise())


def test_sse_emits_payload_free_snapshot_then_keepalive(tmp_path, monkeypatch):
    app, store, _ = _api(tmp_path, monkeypatch)
    _create(store, payload={"message_id": "private-message"})
    monkeypatch.setattr(background_controls, "_health", lambda _request: {"queue_depth_by_status": {"queued": 1}})

    async def check_generator():
        disconnect = asyncio.Event()
        scope = {"type": "http", "method": "GET", "path": "/background/events", "app": app,
                 "headers": [], "client": ("127.0.0.1", 1234), "server": ("test", 80),
                 "scheme": "http", "query_string": b"", "root_path": ""}
        request = Request(scope, receive=lambda: _receive(disconnect))
        events = _events(request, 1)
        first = await anext(events)
        assert "background_health" in first
        assert "private-message" not in first
        second = await anext(events)
        assert "keepalive" in second
        disconnect.set()
        await events.aclose()

    async def _receive(_event):
        await asyncio.sleep(0)
        return {"type": "http.disconnect" if _event.is_set() else "http.request", "body": b""}

    async def unauthorized_route():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.get("/background/events")
            assert response.status_code == 401
            detail = await client.get("/background/jobs/missing")
            assert detail.status_code == 401

    asyncio.run(unauthorized_route())
    asyncio.run(check_generator())
