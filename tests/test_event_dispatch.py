import threading
import time
from datetime import UTC, datetime, timedelta

from app.core.agent_dispatch import _DispatchWorker
from app.core.background_jobs import BackgroundJobStore, BackgroundJobWorker


def test_event_worker_wakes_after_committed_enqueue(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    seen = []
    done = threading.Event()

    def handle(job):
        seen.append(job["job_id"])
        done.set()

    worker = BackgroundJobWorker(store, {"kind": handle}, scheduling_mode="event", reconcile_seconds=30)
    worker.start()
    try:
        time.sleep(0.05)
        job = store.enqueue("kind", "scope", "key", {})
        assert done.wait(2)
        assert seen == [job["job_id"]]
    finally:
        worker.stop()


def test_external_transaction_enqueue_requires_commit_notification(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    seen = []
    done = threading.Event()
    worker = BackgroundJobWorker(store, {"kind": lambda job: (seen.append(job), done.set())},
                                 scheduling_mode="event", reconcile_seconds=30)
    worker.start()
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        store.enqueue("kind", "scope", "key", {}, conn=conn)
        assert not done.wait(0.1)
        conn.commit()
        store.notify_committed()
        assert done.wait(2)
        assert len(seen) == 1
    finally:
        conn.close()
        worker.stop()


def test_next_due_reports_delayed_job(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    due = datetime.now(UTC) + timedelta(seconds=60)
    store.enqueue("kind", "scope", "key", {}, available_at=due)
    actual = store.next_due(kinds=("kind",))
    assert actual is not None
    assert abs((actual - due).total_seconds()) < 0.01


def test_event_worker_runs_delayed_job_at_due_time(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    done = threading.Event()
    finished_at = []
    delay = 0.2
    due = datetime.now(UTC) + timedelta(seconds=delay)
    store.enqueue("kind", "scope", "key", {}, available_at=due)
    worker = BackgroundJobWorker(store, {"kind": lambda job: (finished_at.append(time.monotonic()), done.set())},
                                 scheduling_mode="event", reconcile_seconds=30)
    started_at = time.monotonic()
    worker.start()
    try:
        assert done.wait(1.5)
        assert finished_at[0] - started_at < 0.8
        assert finished_at[0] - started_at >= delay - 0.04
    finally:
        assert worker.stop(timeout=1)


def test_event_worker_storage_errors_use_bounded_backoff(tmp_path, monkeypatch):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    checks = []

    def fail_ready(**kwargs):
        checks.append(time.monotonic())
        raise OSError("temporary store failure")

    monkeypatch.setattr(store, "has_ready", fail_ready)
    worker = BackgroundJobWorker(store, {"kind": lambda job: None}, scheduling_mode="event")
    worker.start()
    try:
        time.sleep(0.08)
        assert len(checks) == 1
    finally:
        assert worker.stop(timeout=1)


def test_scope_blocked_expired_job_waits_for_release_without_empty_claims(tmp_path, monkeypatch):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    blocker = store.enqueue("kind", "scope", "blocker", {})
    claimed_blocker = store.claim("holder", 30, kinds=("kind",))
    assert claimed_blocker and claimed_blocker["job_id"] == blocker["job_id"]
    blocked = store.enqueue("kind", "scope", "blocked", {})
    old = datetime.now(UTC) - timedelta(seconds=5)
    with store._connect() as conn:
        conn.execute(
            "UPDATE background_jobs SET status='running',lease_owner='stale',lease_epoch=1,"
            "lease_expires_at=?,available_at=? WHERE job_id=?",
            (old.isoformat(), old.isoformat(), blocked["job_id"]),
        )

    due = store.next_due(kinds=("kind",))
    assert due is not None
    assert due > datetime.now(UTC)
    assert not store.has_ready(kinds=("kind",))

    original_claim = store.claim
    calls = []

    def counted_claim(*args, **kwargs):
        calls.append(time.monotonic())
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(store, "claim", counted_claim)
    done = threading.Event()
    seen = []
    worker = BackgroundJobWorker(
        store, {"kind": lambda job: (seen.append(job["job_id"]), done.set())},
        scheduling_mode="event", reconcile_seconds=30,
    )
    worker.start()
    try:
        time.sleep(0.15)
        assert calls == []
        assert store.complete(blocker["job_id"], "holder", claimed_blocker["lease_epoch"])
        worker.notify()
        assert done.wait(1)
        assert seen == [blocked["job_id"]]
        assert len(calls) <= 2
    finally:
        assert worker.stop(timeout=1)


def test_expired_deadlines_trigger_cleanup_despite_scope_and_capacity_blocks(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite", max_pending_jobs=1)
    store.ensure_schema()
    blocker = store.enqueue("kind", "scope", "blocker", {})
    claimed = store.claim("holder", 30, kinds=("kind",))
    assert claimed and claimed["job_id"] == blocker["job_id"]
    expired = store.enqueue("kind", "scope", "expired", {}, deadline=datetime.now(UTC) + timedelta(minutes=1))
    with store._connect() as conn:
        conn.execute("UPDATE background_jobs SET deadline=? WHERE job_id=?",
                     ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), expired["job_id"]))
        store.enqueue_latest_watermark(
            "other", "scope", "watermark", {}, conn=conn,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        )
        conn.execute("UPDATE background_pending_watermarks SET deadline=? WHERE kind='other'",
                     ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))

    assert store.has_ready(kinds=("kind",))
    assert store.next_due(kinds=("kind",)) <= datetime.now(UTC)
    assert store.claim("cleanup", 10, kinds=("kind",)) is None
    with store._connect() as conn:
        status = conn.execute("SELECT status FROM background_jobs WHERE job_id=?", (expired["job_id"],)).fetchone()[0]
        watermark_count = conn.execute("SELECT count(*) FROM background_pending_watermarks").fetchone()[0]
    assert status == "failed"
    assert watermark_count == 0


def test_dispatch_worker_stop_wakes_idle_pump_and_reports_timeout(tmp_path):
    store = BackgroundJobStore(tmp_path / "jobs.sqlite")
    store.ensure_schema()
    worker = _DispatchWorker(store, {"kind": lambda job: None}, lease_seconds=1,
                             scheduling_mode="event", reconcile=None)
    worker.start()
    time.sleep(0.05)
    started = time.monotonic()
    assert worker.stop(timeout=0.8)
    assert time.monotonic() - started < 0.8

    entered, release = threading.Event(), threading.Event()
    blocked = _DispatchWorker(store, {"kind": lambda job: (entered.set(), release.wait(2))},
                              lease_seconds=1, scheduling_mode="event", reconcile=None)
    blocked.start()
    store.enqueue("kind", "scope", "blocked", {})
    blocked.notify()
    assert entered.wait(1)
    assert not blocked.stop(timeout=0.02)
    release.set()
    assert blocked.stop(timeout=1)
