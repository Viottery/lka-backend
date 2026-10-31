import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.core.llm_workloads import (
    BackgroundCircuitOpen,
    LLMWorkloadController,
    UsageTicket,
    Workload,
)


def test_expired_reservation_maintenance_uses_small_partial_index(tmp_path):
    path = tmp_path / "usage.sqlite3"
    with sqlite3.connect(path) as legacy:
        legacy.execute("""CREATE TABLE llm_workload_usage(
            call_id TEXT PRIMARY KEY, pool TEXT NOT NULL, task_id TEXT,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
            cost REAL NOT NULL, duration_seconds REAL, count_method TEXT NOT NULL)""")
    controller = LLMWorkloadController(path)
    LLMWorkloadController(path)  # Reopening must keep the migration idempotent.
    now = datetime.now(UTC)
    expired = (now - timedelta(minutes=1)).isoformat()
    future = (now + timedelta(minutes=1)).isoformat()
    created = now.isoformat()

    with controller._connect() as conn:
        indexes = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_llm_usage_reserved_expiry'"
        )]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_workload_usage)")}
        assert indexes == ["idx_llm_usage_reserved_expiry"]
        assert {"cost_known", "pricing_client", "pricing_model", "pricing_revision"} <= columns
        conn.executemany(
            """INSERT INTO llm_workload_usage
               (call_id,pool,task_id,created_at,expires_at,status,input_tokens,output_tokens,
                cost,duration_seconds,count_method,cost_known)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                ("expired", "background_io", None, created, expired, "reserved", 1, 1, 0, None, "test", 1),
                ("live", "background_io", None, created, future, "reserved", 1, 1, 0, None, "test", 1),
                ("settled", "background_io", None, created, expired, "completed", 1, 1, 0, 0, "test", 1),
            ],
        )
        plan = conn.execute(
            "EXPLAIN QUERY PLAN UPDATE llm_workload_usage SET status='estimated' "
            "WHERE status='reserved' AND expires_at<=?",
            (now.isoformat(),),
        ).fetchall()
        assert any("idx_llm_usage_reserved_expiry" in row[3] for row in plan), plan

        conn.execute(
            "UPDATE llm_workload_usage SET status='estimated' "
            "WHERE status='reserved' AND expires_at<=?",
            (now.isoformat(),),
        )
        statuses = dict(conn.execute(
            "SELECT call_id,status FROM llm_workload_usage"
        ).fetchall())

    assert statuses == {"expired": "estimated", "live": "reserved", "settled": "completed"}


def test_partial_index_migration_preserves_persistent_memory_circuit(tmp_path):
    path = tmp_path / "circuit.sqlite3"
    controller = LLMWorkloadController(path, memory_hourly_fuse_tokens=1)
    ticket = UsageTicket(
        "circuit-call", Workload("background_memory"), input_tokens=2, output_tokens=0,
    )

    with pytest.raises(BackgroundCircuitOpen):
        controller._reserve(ticket)

    reopened = LLMWorkloadController(path, memory_hourly_fuse_tokens=1)
    with reopened._connect() as conn:
        state = conn.execute(
            "SELECT reason FROM llm_workload_pool_state WHERE pool='background_memory'"
        ).fetchone()
        index = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_llm_usage_reserved_expiry'"
        ).fetchone()

    assert state["reason"].startswith("hourly_tokens:")
    assert index is not None
