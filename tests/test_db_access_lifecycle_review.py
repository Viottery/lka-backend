"""Failure injection and lock-order regressions for the database access paths."""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.sessions import SessionService
from app.storage import sqlite_timing
from app.storage.sqlite_write_queue import (
    SqliteTransactionTimeoutError,
    WriteQueuePolicy,
    _watch_connection,
)
from scripts.workload_metrics import install


def _connect(path):
    conn = sqlite_timing.connect_sqlite(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_prepare_commit_failure_closes_rolls_back_and_does_not_publish_ready(tmp_path):
    path = tmp_path / "commit-failure.db"
    opened = []

    class FailCommit:
        def __init__(self):
            self.raw = _connect(path)
            self.raw.execute("BEGIN IMMEDIATE")
            self.closed = False

        def execute(self, *args):
            return self.raw.execute(*args)

        def commit(self):
            assert not service._context_schema_ready
            raise sqlite3.OperationalError("injected commit failure")

        def close(self):
            self.raw.close()
            self.closed = True

    def factory():
        conn = FailCommit() if not opened else _connect(path)
        opened.append(conn)
        return conn

    service = SessionService(factory)
    with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
        service.prepare_context_schema()
    assert opened[0].closed
    assert opened[0].raw._write_queue.owner is None
    assert not service._context_schema_ready
    with sqlite3.connect(path) as check:
        assert not check.execute("SELECT 1 FROM sqlite_master WHERE name='agent_session_context_state'").fetchone()
    service.prepare_context_schema()
    assert service._context_schema_ready


def test_prepare_close_failure_keeps_ready_false_and_can_retry(tmp_path):
    path = tmp_path / "close-failure.db"
    opened = []

    class FailClose:
        def __init__(self):
            self.raw = _connect(path)

        def execute(self, *args):
            return self.raw.execute(*args)

        def commit(self):
            assert not service._context_schema_ready
            self.raw.commit()

        def close(self):
            self.raw.close()
            raise sqlite3.OperationalError("injected close failure")

    def factory():
        conn = FailClose() if not opened else _connect(path)
        opened.append(conn)
        return conn

    service = SessionService(factory)
    with pytest.raises(sqlite3.OperationalError, match="injected close failure"):
        service.prepare_context_schema()
    assert not service._context_schema_ready
    service.prepare_context_schema()
    assert service._context_schema_ready


def test_two_service_instances_handle_the_same_stale_legacy_column_scan(tmp_path):
    path = tmp_path / "multi-instance.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_session_context_state (session_id TEXT PRIMARY KEY, "
                     "revision INTEGER NOT NULL, next_seq INTEGER NOT NULL, covered_seq INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO agent_session_context_state VALUES('s', 5, 11, 7)")
    barrier = threading.Barrier(2)

    class StaleColumnScan:
        def __init__(self):
            self.raw = _connect(path)
            self.scanned = False

        def execute(self, sql, *args):
            result = self.raw.execute(sql, *args)
            if sql == "PRAGMA table_info(agent_session_context_state)" and not self.scanned:
                self.scanned = True
                rows = list(result)
                barrier.wait(timeout=2)
                return rows
            return result

        def commit(self):
            return self.raw.commit()

        def close(self):
            self.raw.close()

    services = [SessionService(StaleColumnScan), SessionService(StaleColumnScan)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(service.prepare_context_schema) for service in services]
        for future in futures:
            future.result(timeout=4)
    assert all(service._context_schema_ready for service in services)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT revision,next_seq,covered_seq,summary_revision,summary_metadata "
                            "FROM agent_session_context_state").fetchone() == (5, 11, 7, 0, "{}")


def test_watchdog_keeps_admission_until_external_busy_wait_is_cleaned(tmp_path):
    path = tmp_path / "external-writer.db"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE sample(value INTEGER)")
    external = sqlite3.connect(path)
    external.execute("BEGIN IMMEDIATE")
    conn = sqlite_timing.connect_sqlite(path, timeout=0.5, write_policy=WriteQueuePolicy(
        capacity=8, wait_seconds=1, transaction_seconds=1, watchdog_seconds=1,
    ))
    errors = []

    def insert():
        try:
            conn.execute("INSERT INTO sample VALUES(1)")
            conn.commit()
        except sqlite3.Error as exc:
            errors.append(exc)

    thread = threading.Thread(target=insert)
    thread.start()
    try:
        deadline = time.monotonic() + 1
        while conn._write_started is None and time.monotonic() < deadline:
            threading.Event().wait(0.002)
        assert conn._write_started is not None
        # Force the same expiry decision as the watchdog without a long sleep.
        _watch_connection(conn, conn._write_started + 2)
        assert conn._write_expired
        assert conn._write_queue.owner is conn
        external.rollback()
        thread.join(2)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], SqliteTransactionTimeoutError)
        assert conn._write_queue.owner is None
        with sqlite3.connect(path) as check:
            assert check.execute("SELECT count(*) FROM sample").fetchone()[0] == 0
    finally:
        external.close()
        thread.join(2)
        conn.close()


def test_probe_preserves_explicit_acquire_write_distinct_from_fifo_wait(tmp_path):
    recorder = sqlite_timing.configure_sqlite_timing(tmp_path / "timing.jsonl", sample_rate=0)
    probe = install(tmp_path).start()
    conn = None
    try:
        conn = _connect(tmp_path / "lka.sqlite3")
        conn.execute("BEGIN IMMEDIATE")
        conn.commit()
        events = [event for event in probe.snapshot()["events"] if event["view"] == "sqlite_timing"]
        queue = [event for event in events if event["phase"] == "queue_wait"]
        acquire = [event for event in events if event["phase"] == "acquire_write"]
        assert sum(event["count"] for event in queue) == 1
        assert sum(event["count"] for event in acquire) == 1
        assert acquire[0]["operation"] == "BEGIN"
    finally:
        if conn is not None:
            conn.close()
        probe.stop()
        sqlite_timing.close_sqlite_timing(recorder)


@pytest.mark.parametrize("managed_context", [False, True])
def test_commit_is_counted_once_and_transaction_remains_a_separate_span(tmp_path, managed_context):
    conn = _connect(tmp_path / "lka.sqlite3")
    conn.execute("CREATE TABLE sample(value INTEGER)")
    recorder = sqlite_timing.configure_sqlite_timing(tmp_path / "commit-timing.jsonl", sample_rate=0)
    try:
        if managed_context:
            with conn:
                conn.execute("INSERT INTO sample VALUES(1)")
        else:
            conn.execute("INSERT INTO sample VALUES(1)")
            conn.commit()
        observations = {phase: stats for (database, phase), stats in recorder.histograms.items()}
        assert observations["commit"]["count"] == 1
        assert observations["transaction"]["count"] == 1
        assert observations["queue_wait"]["count"] == 1
        assert observations["execute"]["count"] == 1
        # Transaction spans nest SQL and commit time; these totals are not additive.
    finally:
        conn.close()
        sqlite_timing.close_sqlite_timing(recorder)
