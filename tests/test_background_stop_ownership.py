"""Unfinished background work must keep shutdown retryable and retain authority."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.background_jobs import BackgroundJobWorker
from app.core.memory_background import MemoryBackgroundCoordinator
from app.core.message_analysis import MessageAnalysisCoordinator


def _memory(worker, maintenance=None):
    coordinator = MemoryBackgroundCoordinator.__new__(MemoryBackgroundCoordinator)
    coordinator.worker = worker
    coordinator._maintenance_stop = threading.Event()
    coordinator._maintenance_thread = maintenance
    coordinator._saved_worker_handlers = {"memory_extract": object(), "context_records": object()}
    coordinator._context_only = True
    coordinator._started_mode = True
    return coordinator


def _message(worker):
    coordinator = MessageAnalysisCoordinator.__new__(MessageAnalysisCoordinator)
    coordinator.worker = worker
    return coordinator


def test_memory_worker_timeout_preserves_context_mode_and_handlers_until_retry():
    worker = SimpleNamespace(stop=Mock(side_effect=[False, True]), handlers={"context_records": object()})
    maintenance = SimpleNamespace(join=Mock(), is_alive=Mock(return_value=False))
    coordinator = _memory(worker, maintenance)
    saved = coordinator._saved_worker_handlers
    active = worker.handlers

    with pytest.raises(RuntimeError, match="Memory background workers did not drain"):
        coordinator.stop()
    assert coordinator._maintenance_stop.is_set()
    assert coordinator._saved_worker_handlers is saved
    assert coordinator._maintenance_thread is maintenance
    assert worker.handlers is active
    assert coordinator._context_only is True
    assert coordinator._started_mode is True

    coordinator.stop()
    assert worker.stop.call_count == 2
    assert worker.handlers is saved
    assert coordinator._saved_worker_handlers is None
    assert coordinator._maintenance_thread is None
    assert coordinator._context_only is False
    assert coordinator._started_mode is None


def test_memory_maintenance_timeout_preserves_thread_reference_and_requests_worker_stop():
    maintenance = SimpleNamespace(join=Mock(), is_alive=Mock(return_value=True))
    worker = SimpleNamespace(stop=Mock(return_value=True), handlers={"context_records": object()})
    coordinator = _memory(worker, maintenance)
    saved = coordinator._saved_worker_handlers

    with pytest.raises(RuntimeError, match="Memory maintenance did not drain"):
        coordinator.stop()
    maintenance.join.assert_called_once_with(timeout=5)
    worker.stop.assert_called_once_with()
    assert coordinator._maintenance_thread is maintenance
    assert coordinator._saved_worker_handlers is saved
    assert coordinator._context_only is True
    assert coordinator._started_mode is True

    maintenance.is_alive.return_value = False
    coordinator.stop()
    assert coordinator._maintenance_thread is None
    assert coordinator._saved_worker_handlers is None
    assert worker.handlers is saved
    assert coordinator._started_mode is None


@pytest.mark.parametrize("factory", [_memory, _message])
def test_worker_exception_propagates_and_keeps_coordinator_retryable(factory):
    worker = SimpleNamespace(
        stop=Mock(side_effect=[RuntimeError("synthetic stop failure"), True]), handlers={},
    )
    coordinator = factory(worker)
    with pytest.raises(RuntimeError, match="synthetic stop failure"):
        coordinator.stop()
    assert coordinator.worker is worker
    if factory is _memory:
        assert coordinator._saved_worker_handlers is not None
        assert coordinator._started_mode is True
    coordinator.stop()
    assert worker.stop.call_count == 2


@pytest.mark.parametrize("factory", [_memory, _message])
def test_stop_remains_compatible_with_none_returning_worker_stubs(factory):
    worker = SimpleNamespace(stop=Mock(return_value=None), handlers={})
    coordinator = factory(worker)
    coordinator.stop()
    worker.stop.assert_called_once_with()
    if factory is _memory:
        assert coordinator._started_mode is None


@pytest.mark.parametrize("factory", [_memory, _message])
def test_real_worker_retains_live_thread_after_timeout_and_drains_on_retry(factory, monkeypatch):
    worker = BackgroundJobWorker(SimpleNamespace(), {})
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    worker._threads = [thread]
    thread.start()
    stop = worker.stop
    # Exercise the real worker's timeout/retention logic without a five-second
    # test delay or any model/database operations.
    monkeypatch.setattr(worker, "stop", lambda: stop(timeout=0))
    coordinator = factory(worker)
    try:
        with pytest.raises(RuntimeError, match="workers did not drain"):
            coordinator.stop()
        assert worker._threads == [thread]
        assert thread.is_alive()
        assert worker._stop.is_set()
        if factory is _memory:
            assert coordinator._started_mode is True
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    coordinator.stop()
    assert worker._threads == []
