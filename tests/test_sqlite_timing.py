"""Timing must preserve transactions and never disclose stored content or bindings."""

import asyncio
import json
import sqlite3

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.agent_checkpoints import SqliteCheckpointRuntime
from app.core.config import Settings
from app.storage import sqlite_timing as timing


@pytest.fixture
def recorded(tmp_path):
    path = tmp_path / "database_timing.jsonl"
    recorder = timing.configure_sqlite_timing(path, sample_rate=1, slow_ms=50)
    yield recorder, path
    timing.configure_sqlite_timing(path, enabled=False)


def records(path, kind=None):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [row for row in rows if kind is None or row["kind"] == kind]


def test_cursor_reads_context_transactions_and_private_data(recorded, tmp_path):
    _, path = recorded
    conn = timing.connect_sqlite(tmp_path / "lka.sqlite3")
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("CREATE TABLE private_name(value TEXT)")
        conn.executemany("INSERT INTO private_name VALUES (?)", [("private-body",), ("other",)])
    cursor = conn.cursor()
    cursor.arraysize = 2
    cursor.execute("SELECT value FROM private_name ORDER BY rowid")
    assert [row["value"] for row in cursor.fetchmany()] == ["private-body", "other"]
    assert cursor.fetchone() is None
    assert len(list(conn.execute("SELECT * FROM private_name"))) == 2
    assert len(conn.execute("SELECT * FROM private_name").fetchall()) == 2
    with pytest.raises(ValueError), conn:
        conn.execute("INSERT INTO private_name VALUES ('secret-literal')")
        raise ValueError("secret-exception")
    assert conn.execute("SELECT count(*) FROM private_name").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("CREATE UNIQUE INDEX once_only ON private_name(value)")
        conn.execute("INSERT INTO private_name VALUES (?)", ("private-body",))
    conn.executescript("CREATE TABLE script_table(id INTEGER); INSERT INTO script_table VALUES (1);")
    conn.close()
    rows = records(path, "sqlite")
    assert {"connect", "execute", "fetch", "commit", "rollback", "script"} <= {
        row["phase"] for row in rows
    }
    assert any(row["sqlite_error"] == "SQLITE_CONSTRAINT_UNIQUE" for row in rows)
    assert all(row["database"] == "main" and row["elapsed_ms"] >= 0 for row in rows)
    text = path.read_text()
    for private in ("private_name", "private-body", "secret-literal", "secret-exception", str(tmp_path)):
        assert private not in text


def test_write_lock_timeout_remains_an_error_and_is_timed(recorded, tmp_path):
    _, path = recorded
    first = timing.connect_sqlite(tmp_path / "lka.sqlite3", isolation_level=None)
    second = timing.connect_sqlite(tmp_path / "lka.sqlite3", timeout=0.03, isolation_level=None)
    try:
        first.execute("CREATE TABLE entries(id INTEGER)")
        first.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            second.execute("BEGIN IMMEDIATE")
        first.rollback()
        second.execute("BEGIN IMMEDIATE")
        second.rollback()
    finally:
        first.close()
        second.close()
    failed = [r for r in records(path, "sqlite") if r["phase"] == "acquire_write" and not r["success"]]
    assert len(failed) == 1
    assert failed[0]["sqlite_error"] == "SQLITE_BUSY"
    assert failed[0]["elapsed_ms"] >= 15


def test_failed_deferred_commit_preserves_sqlite_context_rollback(recorded, tmp_path):
    _, path = recorded
    conn = timing.connect_sqlite(tmp_path / "lka.sqlite3")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("CREATE TABLE parent(id INTEGER PRIMARY KEY);"
                       "CREATE TABLE child(id INTEGER REFERENCES parent DEFERRABLE INITIALLY DEFERRED);")
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("INSERT INTO child VALUES (1)")
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM child").fetchone()[0] == 0
    conn.close()
    assert any(r["phase"] == "commit" and not r["success"] for r in records(path, "sqlite"))


def test_zero_sample_keeps_all_histogram_observations_and_slow_errors(recorded):
    recorder, path = recorded
    recorder.sample_rate = 0
    for elapsed in (0.05, 0.4, 3, 60):
        recorder.record(database="main", phase="fetch", operation="SELECT", statement_id="abc",
                        elapsed_ms=elapsed, error=None)
    recorder.record(database="main", phase="fetch", operation="SELECT", statement_id="abc",
                    elapsed_ms=0.1, error=sqlite3.OperationalError("private-error"))
    recorder.flush_summary(force=True)
    details = records(path, "sqlite")
    assert len(details) == 2 and not details[1]["success"]
    summary, = records(path, "sqlite_summary")
    assert summary["count"] == 5 and sum(summary["bucket_counts"]) == 5
    assert summary["errors"] == 1
    assert summary["p50_upper_ms"] == 0.5
    assert summary["p95_upper_ms"] == summary["p99_upper_ms"] == 100
    assert "private-error" not in path.read_text()


def test_shutdown_flushes_remaining_statistics_and_disables_instrumentation(recorded):
    recorder, path = recorded
    conn = timing.connect_sqlite(":memory:")
    conn.execute("SELECT 1").fetchone()
    conn.close()
    timing.close_sqlite_timing(recorder)
    before = path.read_text()
    assert records(path, "sqlite_summary")
    timing.close_sqlite_timing(recorder)
    recorder.write({"kind": "should-not-reopen"})
    assert path.read_text() == before
    conn = timing.connect_sqlite(":memory:")
    assert type(conn) is sqlite3.Connection
    conn.close()


def test_disabled_and_logging_failure_do_not_change_database_results(recorded, tmp_path, monkeypatch):
    recorder, path = recorded

    def broken(**kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(recorder, "record", broken)
    conn = timing.connect_sqlite(":memory:")
    assert conn.execute("SELECT 42").fetchone() == (42,)
    conn.close()
    timing.configure_sqlite_timing(path, enabled=False)
    conn = timing.connect_sqlite(":memory:", isolation_level=None)
    assert type(conn) is sqlite3.Connection and conn.isolation_level is None
    conn.close()


def test_sync_and_async_checkpoint_roundtrip_is_timed(recorded, tmp_path):
    from langgraph.checkpoint.base import empty_checkpoint

    _, path = recorded
    runtime = SqliteCheckpointRuntime(tmp_path / "agent_checkpoints.sqlite3")
    checkpoint = empty_checkpoint()
    config = {"configurable": {"thread_id": "private-thread", "checkpoint_ns": ""}}
    with runtime.sync_saver() as saver:
        saved = saver.put(config, checkpoint, {"source": "input", "step": 0}, {})
        assert saver.get(saved)["id"] == checkpoint["id"]

    async def roundtrip():
        async with runtime.async_saver() as saver:
            assert (await saver.aget(saved))["id"] == checkpoint["id"]
            await saver.aput(config, checkpoint, {"source": "input", "step": 1}, {})
            await saver.aput_writes(saved, [("channel", "private-checkpoint-data")], "task")
            assert (await saver.aget_tuple(saved)).pending_writes

    asyncio.run(roundtrip())
    rows = [row for row in records(path, "sqlite") if row["database"] == "checkpoints"]
    assert sum(row["phase"] == "connect" for row in rows) == 2
    assert {"execute", "fetch", "commit"} <= {row["phase"] for row in rows}
    for private in ("private-thread", "private-checkpoint-data"):
        assert private not in path.read_text()


def test_http_threadpool_correlation_uses_route_template_and_resets_context(recorded, tmp_path):
    _, path = recorded
    app = FastAPI()
    app.add_middleware(timing.DatabaseTimingMiddleware)

    @app.get("/items/{item_id}")
    def item(item_id: str):
        conn = timing.connect_sqlite(":memory:")
        try:
            assert conn.execute("SELECT ?", (item_id,)).fetchone()[0] == item_id
        finally:
            conn.close()
        return {"ok": True}

    async def call():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/items/private-value?token=private-token",
                                    headers={"x-request-id": "untrusted-private-header"})

    response = asyncio.run(call())
    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    http, = records(path, "http")
    assert http["request_id"] == request_id and len(request_id) == 32
    assert http["route"] == "/items/{item_id}"
    assert http["headers_ms"] <= http["elapsed_ms"]
    assert http["database_phases"]["execute"]["count"] == 1
    assert all(row["request_id"] == request_id for row in records(path, "sqlite"))
    conn = timing.connect_sqlite(":memory:")
    conn.close()
    assert records(path, "sqlite")[-1]["request_id"] is None
    for private in ("private-value", "private-token", "untrusted-private-header"):
        assert private not in path.read_text()


@pytest.mark.parametrize("settings", [
    {"LKA_DB_TIMING_SAMPLE_RATE": -0.1}, {"LKA_DB_TIMING_SAMPLE_RATE": 1.1},
    {"LKA_DB_TIMING_SLOW_MS": -1}, {"LKA_DB_TIMING_SLOW_MS": "nan"},
])
def test_invalid_timing_settings_are_rejected(settings):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **settings)
