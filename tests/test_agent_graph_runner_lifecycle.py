from __future__ import annotations

import gc
import threading

import pytest

from app.api.main import create_app
from app.core.agent_graph import AgentGraphRunner
from app.core.config import get_settings
from app.core.llm import LLMResponseMode


def _runner(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text('[agent]\norchestrator = "langgraph"\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    runner = create_app().state.runtime.agent_turn_runner
    assert isinstance(runner, AgentGraphRunner)
    return runner


def test_request_snapshot_is_released_after_completion_and_pause_resume(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    completed = runner.create_run_for_turn(
        session_id="lifecycle-completed", user_input="Answer directly."
    )

    result = runner.run(
        existing_run_id=completed.run_id,
        session_id=completed.session_id,
        user_input=completed.user_input,
        llm_response_mode=LLMResponseMode.TEXT,
    )
    assert result.run_id == completed.run_id
    assert completed.run_id not in runner._requests

    paused = runner.create_run_for_turn(
        session_id="lifecycle-paused", user_input="Answer after resuming."
    )
    runner.interrupt_after = ["initialize_run"]
    # A checkpoint boundary leaves the run resumable while the first public
    # invocation returns without a result.
    with pytest.raises(RuntimeError, match="produced no in-memory result"):
        runner.run(
            existing_run_id=paused.run_id,
            session_id=paused.session_id,
            user_input=paused.user_input,
            llm_response_mode=LLMResponseMode.TEXT,
        )
    assert paused.run_id not in runner._requests

    runner.interrupt_after = []
    resumed = runner.resume(paused.run_id)
    assert resumed.run_id == paused.run_id
    assert paused.run_id not in runner._requests


def test_execution_leases_are_shared_while_contenders_hold_them(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    run_id = "concurrent-lease"
    first = runner._execution_lease(run_id)
    lease_id = id(first)
    first.acquire()
    fetched = threading.Event()
    acquired = threading.Event()

    def contender():
        second = runner._execution_lease(run_id)
        assert id(second) == lease_id
        fetched.set()
        with second:
            acquired.set()

    thread = threading.Thread(target=contender)
    thread.start()
    assert fetched.wait(2)
    assert not acquired.is_set()
    first.release()
    thread.join(2)
    assert not thread.is_alive()
    assert acquired.is_set()

    del first
    gc.collect()
    assert run_id not in runner._execution_locks
