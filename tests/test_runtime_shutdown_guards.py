from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.core.runtime import LocalKnowledgeAgentRuntime


def _runtime_shell():
    runtime = object.__new__(LocalKnowledgeAgentRuntime)
    runtime._lifecycle_lock = threading.RLock()
    runtime._runtime_started = runtime._runtime_closed = False
    runtime._mysql_owner = None
    state = {"owned": False, "released": 0}

    def acquire():
        state["owned"] = True
        return True

    def release():
        state["owned"] = False
        state["released"] += 1

    runtime._runtime_owner = SimpleNamespace(acquire=acquire, release=release)
    runtime._start_services = lambda: None
    runtime.settings = SimpleNamespace(scheduling_stop_seconds=1)
    runtime.scheduler = SimpleNamespace(stop=lambda **kwargs: True)
    for name in ("session_inputs", "management_coordinator", "agent_dispatch", "watch_scheduler",
                 "memory_background", "message_analysis"):
        setattr(runtime, name, SimpleNamespace(stop=lambda: None))
    runtime.remote_execution_worker = runtime.remote_execution_service = runtime.cloud_replica_sync = None
    runtime.bash_session_manager = runtime.instruction_files = SimpleNamespace(close=lambda: None)
    runtime.agent_turn_runner = runtime.agent_run_manager = SimpleNamespace(close=lambda: None)
    runtime._resumable_runner = None
    runtime._mail_sync_stop_event = threading.Event()
    runtime._mail_sync_thread = None
    return runtime, state


def test_mail_shutdown_timeout_preserves_thread_and_owner_until_retry():
    runtime, state = _runtime_shell()
    runtime.start()

    class PendingMailThread:
        alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            assert timeout == 5

    thread = PendingMailThread()
    runtime._mail_sync_thread = thread
    with pytest.raises(RuntimeError, match="Mail synchronization did not drain"):
        runtime.stop()
    assert runtime._mail_sync_thread is thread
    assert runtime._mail_sync_stop_event.is_set()
    assert runtime._runtime_started and state["owned"] and state["released"] == 0
    thread.alive = False
    runtime.stop()
    assert runtime._mail_sync_thread is None
    assert not state["owned"] and state["released"] == 1


def test_failed_scheduler_drain_preserves_ownership_and_allows_stop_retry():
    runtime, state = _runtime_shell()
    runtime.start()
    runtime.scheduler.stop = lambda **kwargs: False
    with pytest.raises(RuntimeError, match="did not drain"):
        runtime.stop()
    assert runtime._runtime_started and not runtime._runtime_closed
    assert state["owned"] and state["released"] == 0
    runtime.scheduler.stop = lambda **kwargs: True
    runtime.stop()
    runtime.stop()
    assert not state["owned"] and state["released"] == 1


def test_failed_start_does_not_release_owner_if_cleanup_is_still_active():
    runtime, state = _runtime_shell()

    def fail():
        raise RuntimeError("injected startup error")

    runtime._start_services = fail
    runtime.scheduler.stop = lambda **kwargs: False
    with pytest.raises(RuntimeError, match="did not drain"):
        runtime.start()
    assert state["owned"] and state["released"] == 0
    runtime.scheduler.stop = lambda **kwargs: True
    runtime.stop()
    assert not state["owned"] and state["released"] == 1
