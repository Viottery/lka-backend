"""Cursor accounting contracts for the synthetic benchmark harness."""
import pytest

from scripts.benchmark_realistic_usage import SyncPullTracker


def _page(cursor, *, more=False, count=1):
    return {"cursor": cursor, "has_more": more, "changes": [{} for _ in range(count)]}


def test_incremental_cursors_are_per_device_and_empty_page_is_counted():
    tracker = SyncPullTracker("incremental")
    pages = {
        ("device_0", 0): _page(20),
        ("device_1", 0): _page(0, count=0),
    }

    for device in ("device_0", "device_1"):
        tracker.catch_up(device, lambda *, after, limit, _d=device: pages[(_d, after)])

    observed = tracker.poll("device_0", lambda *, after, limit: _page(after, count=0))
    assert observed == {"pages": 1, "changes": 0, "cursor": 20, "has_more": False}
    snapshot = tracker.snapshot()
    assert snapshot["cursors"] == {"device_0": 20, "device_1": 0}
    assert snapshot["baseline"] == {"pages": 2, "changes": 1}
    assert snapshot["measurement"] == {"poll_rounds": 1, "pages": 1, "changes": 0}


def test_incremental_poll_advances_across_pages_and_resumes_after_ten_page_cap():
    tracker = SyncPullTracker("incremental")
    tracker.catch_up("device_0", lambda *, after, limit: _page(0, count=0))
    pull = lambda *, after, limit: _page(after + 1, more=after + 1 < 12)
    first = tracker.poll("device_0", pull)
    second = tracker.poll("device_0", pull)
    assert first == {"pages": 10, "changes": 10, "cursor": 10, "has_more": True}
    assert second == {"pages": 2, "changes": 2, "cursor": 12, "has_more": False}
    assert tracker.snapshot()["has_more_rounds"] == {"device_0": 1}


def test_incremental_no_progress_with_has_more_fails_closed():
    tracker = SyncPullTracker("incremental")
    with pytest.raises(RuntimeError, match="no forward progress"):
        tracker.catch_up("device_0", lambda *, after, limit: _page(after, more=True, count=0))

    tracker.catch_up("device_1", lambda *, after, limit: _page(0, count=0))
    with pytest.raises(RuntimeError, match="no forward progress"):
        tracker.poll("device_1", lambda *, after, limit: _page(after, more=True, count=0))


def test_repeat_first_page_profile_always_uses_zero_and_counts_repeated_work():
    tracker = SyncPullTracker("repeat-first-page")
    calls = []

    def pull(*, after, limit):
        calls.append(after)
        return _page(25, more=True, count=3)

    assert tracker.catch_up("device_0", pull) == {"pages": 0, "changes": 0, "cursor": 0}
    first = tracker.poll("device_0", pull)
    second = tracker.poll("device_0", pull)
    assert first == second == {"pages": 1, "changes": 3, "cursor": 0, "has_more": True}
    assert calls == [0, 0]
    snapshot = tracker.snapshot()
    assert snapshot["profile"] == "repeat-first-page"
    assert snapshot["cursors"] == {"device_0": 0}
    assert snapshot["measurement"] == {"poll_rounds": 2, "pages": 2, "changes": 6}
