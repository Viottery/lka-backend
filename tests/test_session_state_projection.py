from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.core.sessions as sessions_module
from app.api.routes.sessions import get_session_state
from app.core.sessions import SessionService
from app.storage.db import connect, get_db_path, init_db


class _CountingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.selects = 0

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("SELECT"):
            self.selects += 1
        return self.connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def _service(tmp_path: Path):
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    counters: list[_CountingConnection] = []

    def factory():
        connection = _CountingConnection(connect(db_path))
        counters.append(connection)
        return connection

    return SessionService(factory), db_path, counters


def _request(service: SessionService, headers=None):
    runtime = SimpleNamespace(session_service=service)
    app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))
    return SimpleNamespace(app=app, headers=headers or {})


def test_state_etag_is_compact_and_short_cache_skips_projection_query(tmp_path, monkeypatch):
    service, db_path, counters = _service(tmp_path)
    session = service.create_session(
        title="State projection",
        initial_message="Private message body sentinel",
        metadata={"prompt": "Private metadata sentinel", "workspace": "/private/path"},
    ).session
    counters.clear()
    clock = [10.0]
    monkeypatch.setattr(sessions_module, "monotonic", lambda: clock[0])

    first = service.get_session_state(session_id=session.session_id)
    selects_after_first = sum(item.selects for item in counters)
    clock[0] += 0.2
    second = service.get_session_state(session_id=session.session_id)
    selects_after_second = sum(item.selects for item in counters)

    assert second == first
    assert selects_after_first == 2  # active-session check plus one projection query
    assert selects_after_second - selects_after_first == 1  # active-session check only
    payload = first.model_dump(mode="json")
    assert set(payload) == {
        "session_id", "status", "updated_at", "latest_root_run_id",
        "latest_root_run_status", "latest_root_run_updated_at", "input_queue_revision", "revision",
    }
    assert "Private message body sentinel" not in json.dumps(payload)
    assert "Private metadata sentinel" not in json.dumps(payload)
    assert "/private/path" not in json.dumps(payload)

    response = get_session_state(session.session_id, _request(service))
    assert response.status_code == 200
    etag = response.headers["etag"]
    assert json.loads(response.body)["revision"] == first.revision
    assert response.headers["cache-control"] == "private, no-cache, must-revalidate"
    not_modified = get_session_state(
        session.session_id, _request(service, {"if-none-match": "W/" + etag}),
    )
    assert not_modified.status_code == 304
    assert not not_modified.body

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE agent_sessions SET status='deleted' WHERE session_id=?",
            (session.session_id,),
        )
    with pytest.raises(HTTPException) as exc:
        get_session_state(session.session_id, _request(service, {"if-none-match": etag}))
    assert exc.value.status_code == 404


def test_state_revision_tracks_root_run_status_after_bounded_ttl(tmp_path, monkeypatch):
    service, db_path, _ = _service(tmp_path)
    session = service.create_session(title="Run state", initial_message="safe test input").session
    clock = [100.0]
    monkeypatch.setattr(sessions_module, "monotonic", lambda: clock[0])
    initial = service.get_session_state(session_id=session.session_id)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO agent_runs(run_id,session_id,status,record_payload,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                "run_state_projection", session.session_id, "running",
                json.dumps({"parent_run_id": None, "user_input": "must stay private"}),
                "2026-10-29T00:00:00+00:00", "2026-10-29T00:00:01+00:00",
            ),
        )

    clock[0] += 1.1
    running = service.get_session_state(session_id=session.session_id)
    assert running.latest_root_run_id == "run_state_projection"
    assert running.latest_root_run_status == "running"
    assert running.revision != initial.revision

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE agent_runs SET status='completed',updated_at=? WHERE run_id=?",
            ("2026-10-29T00:00:02+00:00", "run_state_projection"),
        )
    clock[0] += 1.1
    completed = service.get_session_state(session_id=session.session_id)
    assert completed.latest_root_run_status == "completed"
    assert completed.latest_root_run_updated_at == "2026-10-29T00:00:02+00:00"
    assert completed.revision != running.revision
    assert "must stay private" not in json.dumps(completed.model_dump(mode="json"))


def test_concurrent_state_cache_miss_runs_one_projection_query(tmp_path, monkeypatch):
    service, _, _ = _service(tmp_path)
    session = service.create_session(title="Concurrent state", initial_message=None).session
    original_factory = service._conn_factory
    projection_count = 0
    preflight_count = 0
    counter_lock = Lock()
    release_projection = Event()
    projection_started = Event()
    preflights_complete = Event()
    callers = Barrier(8)

    class DelayedProjectionConnection(_CountingConnection):
        def execute(self, sql, *args):
            nonlocal preflight_count, projection_count
            if "SELECT session_id,status,updated_at FROM agent_sessions" in sql:
                with counter_lock:
                    preflight_count += 1
                    if preflight_count >= 8:
                        preflights_complete.set()
            if "LEFT JOIN agent_runs" in sql:
                with counter_lock:
                    projection_count += 1
                projection_started.set()
                release_projection.wait(timeout=2)
            return super().execute(sql, *args)

    monkeypatch.setattr(
        service, "_conn_factory", lambda: DelayedProjectionConnection(original_factory()),
    )

    def read_state():
        callers.wait(timeout=2)
        return service.get_session_state(session_id=session.session_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read_state) for _ in range(8)]
        assert projection_started.wait(timeout=2)
        assert preflights_complete.wait(timeout=2)
        time.sleep(0.05)
        release_projection.set()
        states = [future.result(timeout=4) for future in futures]

    assert projection_count == 1
    assert len({state.revision for state in states}) == 1
