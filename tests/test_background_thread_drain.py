"""Stopping must preserve thread handles until every writer has exited."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.core.watch_scheduler import WatchScheduler
from app.domains.remote_execution import RemoteExecutionService
from app.integrations.cloud_sync import CloudReplicaSynchronizer
from app.integrations.remote_execution import RemoteExecutionNode


class _Thread:
    def __init__(self, *, alive=True, exits_on_join=False):
        self.alive = alive
        self.exits_on_join = exits_on_join
        self.join_timeouts = []

    def is_alive(self):
        return self.alive

    def join(self, *, timeout):
        self.join_timeouts.append(timeout)
        if self.exits_on_join:
            self.alive = False


def _watch_scheduler():
    cancelled = []
    runtime = SimpleNamespace(
        agent_run_manager=SimpleNamespace(
            cancel_run=lambda run_id, **kwargs: cancelled.append(run_id),
        ),
    )
    scheduler = WatchScheduler(runtime, object(), adapter=object())
    return scheduler, cancelled


def test_watch_stop_retains_all_threads_on_timeout_and_can_be_retried():
    scheduler, cancelled = _watch_scheduler()
    active, stopped = _Thread(), _Thread(exits_on_join=True)
    original_threads = [active, stopped]
    scheduler._threads = original_threads
    scheduler._active_run_ids.add("synthetic-watch-run")

    with pytest.raises(RuntimeError, match="Watch scheduler workers did not drain"):
        scheduler.stop()

    assert scheduler._stop.is_set() and scheduler._wake.is_set()
    assert cancelled == ["synthetic-watch-run"]
    assert active.join_timeouts == stopped.join_timeouts == [3]
    assert scheduler._threads is original_threads
    assert scheduler._threads == [active, stopped]
    scheduler.start()
    assert scheduler._stop.is_set()  # start cannot revive a timed-out worker.
    assert scheduler._threads is original_threads

    active.alive = False
    scheduler.stop()
    assert active.join_timeouts == stopped.join_timeouts == [3, 3]
    assert scheduler._threads == []


@pytest.mark.parametrize("has_threads", [False, True])
def test_watch_stop_accepts_fully_drained_workers(has_threads):
    scheduler, _cancelled = _watch_scheduler()
    if has_threads:
        scheduler._threads = [_Thread(exits_on_join=True)]
    scheduler.stop()
    scheduler.stop()
    assert scheduler._stop.is_set() and scheduler._threads == []


@pytest.mark.parametrize(
    ("worker_type", "timeout", "message"),
    [
        (RemoteExecutionService, 5, "Remote execution reconciler did not drain"),
        (CloudReplicaSynchronizer, 8, "Cloud replica synchronizer did not drain"),
    ],
)
def test_single_writer_stop_reports_timeout_retains_handle_and_allows_retry(
    worker_type, timeout, message,
):
    worker = worker_type.__new__(worker_type)
    worker._stop = threading.Event()
    worker.config = SimpleNamespace(request_timeout_seconds=7)
    original_thread = _Thread()
    worker._thread = original_thread

    with pytest.raises(RuntimeError, match=message):
        worker.stop()

    assert worker._stop.is_set()
    assert worker._thread is original_thread
    assert original_thread.join_timeouts == [timeout]
    worker.start()
    assert worker._thread is original_thread and worker._stop.is_set()

    original_thread.alive = False
    worker.stop()
    assert original_thread.join_timeouts == [timeout, timeout]


@pytest.mark.parametrize("worker_type", [RemoteExecutionService, CloudReplicaSynchronizer])
@pytest.mark.parametrize("has_thread", [False, True])
def test_single_writer_stop_accepts_drained_or_unstarted_worker(worker_type, has_thread):
    worker = worker_type.__new__(worker_type)
    worker._stop = threading.Event()
    worker.config = SimpleNamespace(request_timeout_seconds=7)
    worker._thread = _Thread(exits_on_join=True) if has_thread else None
    worker.stop()
    worker.stop()
    assert worker._stop.is_set()
    assert worker._thread is None or not worker._thread.is_alive()


def _remote_node(thread):
    node = RemoteExecutionNode.__new__(RemoteExecutionNode)
    node._stop = threading.Event()
    node._thread = thread
    node.sync_config = SimpleNamespace(request_timeout_seconds=7)
    active_calls, fenced = [], []
    row = {"run_id": "synthetic-remote-run"}

    def active():
        active_calls.append(True)
        return [row]

    node._active = active
    node._fence_local = fenced.append
    node.runtime = SimpleNamespace(
        agent_run_manager=SimpleNamespace(get_run=lambda _run_id: None),
        agent_dispatch=SimpleNamespace(_guards_lock=threading.Lock(), guards={}),
    )
    return node, active_calls, fenced, row


def test_remote_node_timeout_prevents_local_fencing_until_retry_drains():
    thread = _Thread()
    node, active_calls, fenced, row = _remote_node(thread)
    with pytest.raises(RuntimeError, match="Remote execution node did not drain"):
        node.stop()
    assert node._stop.is_set() and node._thread is thread
    assert thread.join_timeouts == [8]
    assert active_calls == [] and fenced == []

    node.start()
    assert node._stop.is_set() and node._thread is thread
    thread.alive = False
    node.stop()
    assert thread.join_timeouts == [8, 8]
    assert active_calls == [True] and fenced == [row]


@pytest.mark.parametrize("has_thread", [False, True])
def test_remote_node_drained_stop_preserves_local_fencing(has_thread):
    thread = _Thread(exits_on_join=True) if has_thread else None
    node, active_calls, fenced, row = _remote_node(thread)
    node.stop()
    assert node._stop.is_set()
    assert active_calls == [True] and fenced == [row]
