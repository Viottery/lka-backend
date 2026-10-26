import threading
import time

import pytest

from app.core.scheduling_signals import KeyedScheduler, RecoveryPage


def test_notifications_coalesce_and_inflight_reschedule():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def handler(key):
        calls.append(key)
        if len(calls) == 1:
            entered.set()
            release.wait(2)

    scheduler = KeyedScheduler(workers=1)
    scheduler.register("n", handler)
    scheduler.start()
    scheduler.notify("n", "k")
    assert entered.wait(1)
    for _ in range(8):
        scheduler.notify("n", "k")
    release.set()
    deadline = time.monotonic() + 2
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert calls == ["k", "k"]
    assert scheduler.stop(1)


def test_timer_update_uses_latest_generation():
    calls = []
    scheduler = KeyedScheduler(workers=1)
    scheduler.register("n", calls.append)
    scheduler.start()
    scheduler.schedule("n", "old", 0.15)
    scheduler.schedule("n", "new", 0)
    deadline = time.monotonic() + 1
    while not calls and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.2)
    assert calls == ["new", "old"]
    scheduler.stop(1)


@pytest.mark.parametrize("workers", [1, 2])
def test_recovery_pages_larger_than_capacity_make_progress(workers):
    handled, pages = [], []
    all_keys = [str(i) for i in range(9)]

    def recover(cursor, limit):
        start = cursor or 0
        keys = all_keys[start : start + limit]
        nxt = start + len(keys)
        pages.append(start)
        return RecoveryPage(keys, nxt, nxt >= len(all_keys))

    scheduler = KeyedScheduler(
        capacity=2, workers=workers, recovery_page_size=3, reconcile_seconds=100
    )
    scheduler.register("n", handled.append, recover)
    scheduler.start()
    deadline = time.monotonic() + 3
    while len(set(handled)) < len(all_keys) and time.monotonic() < deadline:
        time.sleep(0.005)
    scheduler.stop(1)
    assert set(handled) == set(all_keys)
    assert pages[:3] == [0, 0, 2] or len(pages) >= 3


def test_overflow_during_active_scan_restarts_at_new_lower_key():
    entered, release = threading.Event(), threading.Event()
    handled, cursors = [], []

    def recover(cursor, limit):
        cursors.append(cursor)
        if cursor is None and len(cursors) == 1:
            entered.set()
            assert release.wait(2)
            return RecoveryPage(["middle"], "middle", False)
        if cursor is None:
            return RecoveryPage(["aaa", "zzz"][:limit], "zzz", True)
        return RecoveryPage(["zzz"], "zzz", True)

    scheduler = KeyedScheduler(capacity=4, workers=1, recovery_page_size=2, reconcile_seconds=100)
    scheduler.register("n", handled.append, recover)
    scheduler.start()
    assert entered.wait(1)
    scheduler.request_rescan("n")
    release.set()
    deadline = time.monotonic() + 2
    while "aaa" not in handled and time.monotonic() < deadline:
        time.sleep(0.005)
    assert "aaa" in handled
    assert cursors[:2] == [None, None]
    assert scheduler.stop(1)


def test_handler_exception_retries_with_backoff_and_stop_is_idempotent():
    calls = []

    def handler(key):
        calls.append(key)
        if len(calls) == 1:
            raise ValueError("transient")

    scheduler = KeyedScheduler(workers=1)
    scheduler.register("n", handler)
    scheduler.start()
    scheduler.notify("n", "x")
    deadline = time.monotonic() + 2
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == ["x", "x"]
    assert scheduler.status()["handler_failures"] == 1
    assert scheduler.status()["last_error_type"] == "ValueError"
    assert scheduler.stop(1) and scheduler.stop(1)


def test_stop_deadline_reports_running_handler():
    entered, release = threading.Event(), threading.Event()
    scheduler = KeyedScheduler(workers=1)
    scheduler.register("n", lambda key: (entered.set(), release.wait(2)))
    scheduler.start()
    scheduler.notify("n", "x")
    assert entered.wait(1)
    assert not scheduler.stop(0)
    release.set()
    assert scheduler.stop(1)


def test_timer_storage_is_hard_bounded_and_same_key_updates_compact():
    scheduler = KeyedScheduler(capacity=3, workers=1)
    scheduler.register("n", lambda key: None)
    for i in range(100):
        scheduler.schedule("n", "same", 1000 + i)
    assert len(scheduler._timer_generation) == 1
    assert len(scheduler._timers) <= 6
    for i in range(20):
        scheduler.schedule("n", str(i), 10_000)
    assert len(scheduler._timer_generation) <= 3
    assert len(scheduler._timers) <= 6
    assert scheduler.status()["timer_overflow"] > 0
    assert scheduler.status()["needs_rescan"]


def test_overflow_during_recovery_keeps_rescan_generation():
    entered, release = threading.Event(), threading.Event()
    handler_release = threading.Event()
    source = ["durable-a", "durable-b"]
    handled = []

    def recover(cursor, limit):
        if cursor is None and not entered.is_set():
            entered.set()
            assert release.wait(2)
        start = cursor or 0
        keys = source[start : start + limit]
        nxt = start + len(keys)
        return RecoveryPage(keys, nxt, nxt >= len(source))

    scheduler = KeyedScheduler(capacity=1, workers=2, recovery_page_size=1, reconcile_seconds=100)

    def handler(key):
        if key == "queued":
            handler_release.wait(2)
        handled.append(key)

    scheduler.register("n", handler, recover)
    scheduler.start()
    assert entered.wait(1)
    scheduler.notify("n", "queued")
    assert not scheduler.notify("n", "overflow")
    before = scheduler.status()["rescan_generation"]
    handler_release.set()
    release.set()
    deadline = time.monotonic() + 2
    while scheduler.status()["needs_rescan"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert scheduler.status()["rescan_generation"] == before
    assert not scheduler.status()["needs_rescan"]
    assert {"durable-a", "durable-b"}.issubset(set(handled))
    assert scheduler.stop(1)


def test_no_recovery_namespace_runs_low_frequency_wildcard_at_most_35_per_15m():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    calls = []
    scheduler = KeyedScheduler(
        workers=1, reconcile_seconds=30, jitter_ratio=0, clock=clock, random_source=lambda: 0.5
    )
    scheduler.register("n", calls.append)
    scheduler.start()
    for _ in range(900):
        with scheduler._cv:
            clock.value += 1
            scheduler._cv.notify_all()
        time.sleep(0.001)
    assert scheduler.stop(1)
    assert calls.count("*") <= 35


def test_recovery_failure_retries_outside_condition_lock():
    scheduler = KeyedScheduler(workers=1, reconcile_seconds=0.01, jitter_ratio=0)
    calls = []
    recovery_calls = []

    def recover(cursor, limit):
        recovery_calls.append(cursor)
        # A provider may safely signal another key; this would deadlock if the
        # scheduler invoked recovery while holding its condition lock.
        scheduler.notify("n", "from-recovery")
        if len(recovery_calls) == 1:
            raise OSError("temporary store failure")
        return RecoveryPage([], None, True)

    scheduler.register("n", calls.append, recover)
    scheduler.start()
    deadline = time.monotonic() + 1
    while len(recovery_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(recovery_calls) >= 2
    assert "from-recovery" in calls
    assert scheduler.stop(1)


def test_recovery_provider_is_single_flight_per_namespace():
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def recover(cursor, limit):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        entered.set()
        release.wait(1)
        with lock:
            active -= 1
        return RecoveryPage([], None, True)

    scheduler = KeyedScheduler(workers=3, reconcile_seconds=100)
    scheduler.register("n", lambda key: None, recover)
    scheduler.start()
    assert entered.wait(1)
    time.sleep(0.03)
    release.set()
    assert scheduler.stop(1)
    assert peak == 1


def test_no_recovery_wildcard_is_single_flight_when_interval_elapses():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def handler(key):
        calls.append(key)
        if key == "*":
            entered.set()
            release.wait(2)

    scheduler = KeyedScheduler(
        workers=2, reconcile_seconds=30, jitter_ratio=0, clock=clock, random_source=lambda: 0.5
    )
    scheduler.register("n", handler)
    scheduler.start()
    with scheduler._cv:
        clock.value = 30
        scheduler._cv.notify_all()
    assert entered.wait(1)
    with scheduler._cv:
        clock.value = 60
        scheduler._cv.notify_all()
    time.sleep(0.03)
    assert calls.count("*") == 1
    release.set()
    assert scheduler.stop(1)


def test_rescan_during_no_recovery_wildcard_runs_another_pass():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def handler(key):
        calls.append(key)
        if key == "*" and calls.count("*") == 1:
            entered.set()
            assert release.wait(2)

    scheduler = KeyedScheduler(
        workers=1, reconcile_seconds=30, jitter_ratio=0, clock=clock, random_source=lambda: 0.5
    )
    scheduler.register("n", handler)
    scheduler.start()
    with scheduler._cv:
        clock.value = 30
        scheduler._cv.notify_all()
    assert entered.wait(1)

    scheduler.request_rescan("n")
    release.set()
    deadline = time.monotonic() + 1
    while (calls.count("*") < 2 or scheduler.status()["needs_rescan"]) and time.monotonic() < deadline:
        time.sleep(0.005)

    assert calls.count("*") == 2
    assert not scheduler.status()["needs_rescan"]
    assert scheduler.stop(1)


def test_recovery_slot_race_clears_single_flight_marker():
    scheduler = KeyedScheduler(capacity=1, workers=1)
    scheduler.register("n", lambda key: None, lambda cursor, limit: RecoveryPage([], None, True))
    assert scheduler.notify("n", "occupy")
    with scheduler._cv:
        space = scheduler._spaces["n"]
        space.recovery_active = True
    scheduler._run_recovery("n", None)
    assert not scheduler._spaces["n"].recovery_active


def test_persistent_failures_and_unique_notifications_remain_bounded_without_spin():
    calls = []
    lock = threading.Lock()

    def handler(key):
        with lock:
            calls.append(key)
        raise RuntimeError("persistent transient failure")

    scheduler = KeyedScheduler(capacity=4, workers=2, reconcile_seconds=100)
    scheduler.register("n", handler)
    scheduler.start()
    for i in range(4):
        scheduler.notify("n", f"failed-{i}")
    deadline = time.monotonic() + 1
    while len(calls) < 4 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(calls) == 4
    for i in range(20):
        scheduler.notify("n", f"overflow-{i}")
    deadline = time.monotonic() + 0.5
    while "*" not in calls and time.monotonic() < deadline:
        time.sleep(0.005)
    count_at_overflow = len(calls)
    time.sleep(0.1)
    with scheduler._cv:
        assert len(scheduler._tracked_keys_locked()) <= scheduler.capacity
        assert sum(len(space.retry) for space in scheduler._spaces.values()) <= scheduler.capacity
        assert len(scheduler._timer_generation) <= scheduler.capacity
    assert len(calls) == count_at_overflow
    assert scheduler.status()["needs_rescan"]
    assert scheduler.stop(1)
