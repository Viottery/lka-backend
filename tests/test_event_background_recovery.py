"""Notifications are hints; pending outbox state remains the recoverable authority."""

import threading

from app.core.background_jobs import BackgroundJobStore, BackgroundJobWorker


def test_full_queue_materializes_pending_watermark_after_capacity_is_released(tmp_path):
    store = BackgroundJobStore(tmp_path / "lka.sqlite3", max_pending_jobs=1)
    store.ensure_schema()
    first_started, release, second_done = threading.Event(), threading.Event(), threading.Event()
    seen = []

    def handle(job):
        seen.append(job["scope_id"])
        if job["scope_id"] == "first":
            first_started.set()
            assert release.wait(3)
        else:
            second_done.set()

    worker = BackgroundJobWorker(store, {"synthetic": handle}, scheduling_mode="event")
    worker.start()
    try:
        store.enqueue("synthetic", "first", "one", {})
        assert first_started.wait(3)
        with store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = store.enqueue_latest_watermark("synthetic", "second", "two", {}, conn=conn)
            conn.commit()
        assert result["job_id"] is None
        assert not store.has_ready(kinds=("synthetic",))
        release.set()
        assert second_done.wait(3)
        assert seen == ["first", "second"]
    finally:
        release.set()
        assert worker.stop(timeout=3)


def test_broken_outbox_notifier_cannot_fail_committed_acceptance(tmp_path):
    store = BackgroundJobStore(tmp_path / "lka.sqlite3")
    store.ensure_schema()
    seen = []

    def broken():
        raise RuntimeError("synthetic notification failure")

    store.add_notifier(broken)
    store.add_notifier(lambda: seen.append("notified"))
    result = store.enqueue("synthetic", "scope", "same", {})
    assert store.get(result["job_id"])["status"] == "queued"
    assert store.enqueue("synthetic", "scope", "same", {})["job_id"] == result["job_id"]
    assert seen == ["notified", "notified"]
