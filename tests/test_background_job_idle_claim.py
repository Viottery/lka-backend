from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.core.message_analysis import _AnalysisWorker


def test_empty_claim_does_not_open_a_write_transaction(tmp_path, monkeypatch):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()

    def unexpected_write(_conn):
        raise AssertionError("empty claim entered a write transaction")

    monkeypatch.setattr("app.core.background_jobs.begin_sqlite_write", unexpected_write)
    assert store.claim("idle-worker", 30, kinds=("synthetic",)) is None


def test_claim_after_positive_probe_skips_only_redundant_read(tmp_path, monkeypatch):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    job = store.enqueue("synthetic", "scope", "ready", {})
    assert store.has_ready(kinds=("synthetic",))

    def unexpected_probe(**_kwargs):
        raise AssertionError("already-probed claim repeated the readiness query")

    monkeypatch.setattr(store, "has_ready", unexpected_probe)
    claimed = store._claim_after_probe("worker", 30, kinds=("synthetic",))
    assert claimed and claimed["job_id"] == job["job_id"]
    assert claimed["lease_epoch"] == 1


def test_notifications_can_be_scoped_and_unknown_changes_broadcast(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    seen = {"a": 0, "b": 0, "all": 0}
    store.add_notifier(lambda: seen.__setitem__("a", seen["a"] + 1), kinds=("a",))
    store.add_notifier(lambda: seen.__setitem__("b", seen["b"] + 1), kinds=("b",))
    store.add_notifier(lambda: seen.__setitem__("all", seen["all"] + 1))

    store.notify_committed(kinds=("a",))
    assert seen == {"a": 1, "b": 0, "all": 1}
    store.notify_committed()
    assert seen == {"a": 2, "b": 1, "all": 2}


def test_claim_can_be_restricted_to_exact_dispatch_candidates(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    preferred = store.enqueue("synthetic", "scope-a", "not-selected", {}, priority=100)
    candidate = store.enqueue("synthetic", "scope-b", "selected", {})
    assert not store.has_ready(kinds=("synthetic",), job_ids=("missing",))
    assert store.has_ready(kinds=("synthetic",), job_ids=(candidate["job_id"],))
    claimed = store.claim("governed-worker", 30, kinds=("synthetic",), job_ids=(candidate["job_id"],))
    assert claimed["job_id"] == candidate["job_id"]
    assert store.get(preferred["job_id"])["status"] == "queued"


def test_exact_candidate_filter_is_bounded_and_empty_is_read_only(tmp_path, monkeypatch):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    with pytest.raises(ValueError, match="job_ids"):
        store.has_ready(job_ids=tuple(f"job-{index}" for index in range(129)))
    monkeypatch.setattr("app.core.background_jobs.begin_sqlite_write", lambda _conn: pytest.fail("write"))
    assert store.claim("worker", 30, job_ids=()) is None


def test_deadline_cleanup_is_bounded_and_rate_limited(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    expired = [store.enqueue("synthetic", f"scope-{index}", "expired", {},
                             deadline=datetime.now(UTC) + timedelta(minutes=1))
               for index in range(40)]
    old = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store._connect() as conn:
        conn.executemany("UPDATE background_jobs SET deadline=? WHERE job_id=?",
                         [(old, item["job_id"]) for item in expired])
    store._next_deadline_sweep = time.monotonic() - 1
    assert store.claim("maintenance", 30, kinds=("synthetic",)) is None
    states = [store.get(item["job_id"])["status"] for item in expired]
    assert states.count("failed") == 32
    assert states.count("queued") == 8
    assert not store.has_ready(kinds=("synthetic",))


def test_event_analysis_worker_recovers_durable_source_without_polling(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    done = threading.Event()
    scheduled = threading.Event()

    def recover():
        if scheduled.is_set():
            return []
        scheduled.set()
        store.enqueue("message_analysis", "synthetic-scope", "recovered", {})
        return ["recovered"]

    worker = _AnalysisWorker(
        store, {"message_analysis": lambda _job: done.set()},
        worker_count=1, lease_seconds=30, scheduling_mode="event", reconcile_seconds=0.1,
        recover=recover,
    )
    worker.start()
    try:
        assert done.wait(2)
    finally:
        assert worker.stop(timeout=2)
