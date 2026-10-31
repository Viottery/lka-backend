from __future__ import annotations

import re
import sqlite3

from app.core.agent_storage import connect as connect_agent_storage
from app.core.background_jobs import BackgroundJobStore
from app.domains.memory import MemoryService
from app.storage import sqlite_timing
from app.storage.db import connect as connect_main_database
from app.storage.sqlite_write_queue import QueuedConnection


def _install_trace_on_native_connect(monkeypatch, statements):
    original = sqlite_timing.sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite_timing.sqlite3, "connect", traced_connect)


def _busy_timeout(conn):
    return conn.execute("PRAGMA busy_timeout").fetchone()[0]


def test_connect_wrappers_use_native_timeout_without_duplicate_busy_setter(tmp_path, monkeypatch):
    statements = []
    _install_trace_on_native_connect(monkeypatch, statements)

    cases = [
        (connect_main_database(tmp_path / "lka.sqlite3"), 5000),
        (connect_agent_storage(tmp_path / "agent-storage.sqlite3"), 5000),
    ]
    memory = object.__new__(MemoryService)
    memory.db_path = str(tmp_path / "memory.sqlite3")
    memory_conn = memory._connect()
    cases.append((memory_conn, 10000))
    jobs = object.__new__(BackgroundJobStore)
    jobs.db_path = str(tmp_path / "background-jobs.sqlite3")
    cases.append((jobs._connect(), 10000))

    try:
        for conn, expected_ms in cases:
            assert _busy_timeout(conn) == expected_ms
        assert cases[2][0].execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        for conn, _ in cases:
            conn.close()

    busy_setters = [statement for statement in statements
                    if re.match(r"\s*PRAGMA\s+busy_timeout\s*=", statement, re.IGNORECASE)]
    assert busy_setters == []
    assert any(statement.lstrip().upper().startswith("PRAGMA FOREIGN_KEYS=ON")
               for statement in statements)


def test_native_timeout_survives_queued_connection_setup(tmp_path):
    conn = sqlite3.connect(
        tmp_path / "queued-timeout.sqlite3", timeout=2.75,
        factory=QueuedConnection, check_same_thread=False, cached_statements=0,
    )
    try:
        before = sqlite3.Connection.execute(conn, "PRAGMA busy_timeout").fetchone()[0]
        conn.configure_write_queue(tmp_path / "queued-timeout.sqlite3")
        after = _busy_timeout(conn)
        assert before == after == 2750
    finally:
        conn.close()
