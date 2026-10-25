"""Process-death tests for durable session-input acceptance and dispatch."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.core.runtime import LocalKnowledgeAgentRuntime

_CRASH_ENABLED = False
_CRASH_PIPE = None


def _isolated_config():
    config = LocalAppConfig()
    config.management.enabled = True
    config.agent.unified_entry_enabled = True
    config.agent.checkpoint_backend = "sqlite"
    config.memory.enabled = False
    config.message_history.enabled = False
    return config


def _load_child_config(_settings):
    return _isolated_config()


def _crash_child(stage: str, data_dir: str, child_pipe) -> None:
    global _CRASH_ENABLED, _CRASH_PIPE
    from app.storage.sqlite_timing import TimedConnection, connect_sqlite

    Settings.load_local_config = _load_child_config
    settings = Settings(
        _env_file=None,
        LKA_DATA_DIR=Path(data_dir),
        LKA_WORKSPACE_ROOTS=data_dir,
        LKA_SCHEDULING_MODE="event",
        LKA_SCHEDULING_CAPACITY=8,
        LKA_SCHEDULING_WORKERS=2,
        LKA_SCHEDULING_RECOVERY_PAGE_SIZE=4,
    )
    runtime = LocalKnowledgeAgentRuntime(settings)
    session_id = runtime.management_service.get_default()["session"]["session_id"]
    child_pipe.send(("ready", session_id))

    if stage == "accept_before_commit":

        class CrashBeforeCommitConnection(TimedConnection):
            def __exit__(self, exc_type, exc_value, traceback):
                if _CRASH_ENABLED and self.in_transaction:
                    _CRASH_PIPE.send(("accept_before_commit", session_id))
                    os._exit(73)
                return super().__exit__(exc_type, exc_value, traceback)

        def crash_conn():
            conn = connect_sqlite(runtime.db_path, timeout=5.0, factory=CrashBeforeCommitConnection)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            return conn

        runtime.session_inputs._conn = crash_conn
        _CRASH_PIPE = child_pipe
        _CRASH_ENABLED = True
        runtime.session_inputs.submit(
            session_id,
            client_message_id="crash-key",
            user_input="durable input",
            attachment_ids=[],
        )
    elif stage == "accept_after_commit":

        def crash_after_notification(_sid):
            child_pipe.send(("accept_after_commit", session_id))
            os._exit(73)

        runtime.session_inputs.notify = crash_after_notification
        runtime.session_inputs.submit(
            session_id,
            client_message_id="crash-key",
            user_input="durable input",
            attachment_ids=[],
        )
    elif stage == "bind_after_commit":
        runtime.session_inputs.submit(
            session_id,
            client_message_id="crash-key",
            user_input="durable input",
            attachment_ids=[],
        )

        def crash_before_dispatch(_self=None):
            child_pipe.send(("bind_after_commit", session_id))
            os._exit(73)

        runtime.agent_dispatch.notify = crash_before_dispatch
        runtime.session_inputs.tick()
    else:
        raise ValueError(stage)


def _spawn_crash(stage: str, data_dir: Path) -> tuple[str, int]:
    context = multiprocessing.get_context("spawn")
    parent_pipe, child_pipe = context.Pipe(duplex=False)
    process = context.Process(target=_crash_child, args=(stage, str(data_dir), child_pipe))
    process.start()
    child_pipe.close()
    try:
        ready = parent_pipe.recv()
        assert ready[0] == "ready"
        crashed = parent_pipe.recv()
        assert crashed == (stage, ready[1])
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
            pytest.fail(f"crash injector did not exit at {stage}")
        assert process.exitcode == 73
        return ready[1], process.exitcode
    finally:
        parent_pipe.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()


def _runtime(data_dir: Path, monkeypatch):
    monkeypatch.setattr(Settings, "load_local_config", _load_child_config)
    return LocalKnowledgeAgentRuntime(
        Settings(
            _env_file=None,
            LKA_DATA_DIR=data_dir,
            LKA_WORKSPACE_ROOTS=str(data_dir),
            LKA_SCHEDULING_MODE="event",
            LKA_SCHEDULING_CAPACITY=8,
            LKA_SCHEDULING_WORKERS=2,
            LKA_SCHEDULING_RECOVERY_PAGE_SIZE=4,
        )
    )


def _install_execution_stub(runtime):
    executed = []

    def execute(run, guard):
        guard.check()
        executed.append(run.run_id)
        runtime.agent_run_manager.mark_running(run.run_id)
        runtime.agent_run_manager.complete_run(run.run_id, result_snapshot={"status": "completed"})

    runtime.agent_dispatch.execute = execute
    return executed


def _wait_for_completed(runtime, run_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        run = runtime.agent_run_manager.get_run(run_id)
        if run is not None and run.status.value == "completed":
            return run
        time.sleep(0.01)
    raise AssertionError(f"run did not complete after recovery: {run_id}")


@pytest.mark.parametrize(
    "stage",
    ["accept_before_commit", "accept_after_commit", "bind_after_commit"],
)
def test_process_crash_recovers_input_and_dispatch_from_sqlite(stage, tmp_path, monkeypatch):
    data_dir = tmp_path / "isolated-data"
    session_id, exitcode = _spawn_crash(stage, data_dir)
    assert exitcode == 73

    database = data_dir / "lka.sqlite3"
    with sqlite3.connect(database) as conn:
        inputs = conn.execute(
            "SELECT input_id,message_id,run_id FROM session_inputs "
            "WHERE session_id=? AND client_message_id='crash-key'",
            (session_id,),
        ).fetchall()
        messages = conn.execute(
            "SELECT message_id FROM agent_session_messages WHERE session_id=? AND role='user'",
            (session_id,),
        ).fetchall()
        runs = conn.execute(
            "SELECT run_id FROM agent_runs WHERE session_id=? AND "
            "json_extract(record_payload,'$.metadata.session_input_id') IS NOT NULL",
            (session_id,),
        ).fetchall()
        jobs = conn.execute(
            "SELECT job_id,payload_json FROM background_jobs WHERE kind='agent_dispatch'",
        ).fetchall()

    if stage == "accept_before_commit":
        assert inputs == []
        assert messages == []
        assert runs == []
        assert jobs == []
        return

    assert len(inputs) == len(messages) == 1
    original_input_id, original_message_id, original_run_id = inputs[0]
    runtime = _runtime(data_dir, monkeypatch)
    executed = _install_execution_stub(runtime)
    try:
        if stage == "accept_after_commit":
            receipt = runtime.session_inputs.submit(
                session_id,
                client_message_id="crash-key",
                user_input="durable input",
                attachment_ids=[],
            )
            assert receipt["input"]["input_id"] == original_input_id
            assert receipt["input"]["message_id"] == original_message_id
            assert receipt["input"]["run_id"] is None
            assert runs == []
            assert jobs == []
        else:
            assert original_run_id is not None
            assert len(runs) == 1
            assert runs[0][0] == original_run_id
            assert len(jobs) == 1
            assert json.loads(jobs[0][1])["run_id"] == original_run_id

        runtime.start()
        if stage == "accept_after_commit":
            deadline = time.monotonic() + 5
            recovered_run_id = None
            while time.monotonic() < deadline:
                current = runtime.session_inputs.snapshot(session_id)["inputs"][0]
                recovered_run_id = current["run_id"]
                if recovered_run_id is not None:
                    break
                time.sleep(0.01)
            assert recovered_run_id is not None
            assert recovered_run_id != original_run_id
            _wait_for_completed(runtime, recovered_run_id)
            with sqlite3.connect(database) as conn:
                rows = conn.execute(
                    "SELECT input_id,message_id,run_id FROM session_inputs "
                    "WHERE session_id=? AND client_message_id='crash-key'",
                    (session_id,),
                ).fetchall()
                message_count = conn.execute(
                    "SELECT count(*) FROM agent_session_messages WHERE session_id=? AND role='user'",
                    (session_id,),
                ).fetchone()[0]
                run_count = conn.execute(
                    "SELECT count(*) FROM agent_runs WHERE session_id=? AND "
                    "json_extract(record_payload,'$.metadata.session_input_id')=?",
                    (session_id, original_input_id),
                ).fetchone()[0]
            assert rows == [(original_input_id, original_message_id, recovered_run_id)]
            assert message_count == run_count == 1
        else:
            _wait_for_completed(runtime, original_run_id)
            assert executed == [original_run_id]
            with sqlite3.connect(database) as conn:
                run_count = conn.execute(
                    "SELECT count(*) FROM agent_runs WHERE session_id=? AND "
                    "json_extract(record_payload,'$.metadata.session_input_id')=?",
                    (session_id, original_input_id),
                ).fetchone()[0]
                job_rows = conn.execute(
                    "SELECT payload_json FROM background_jobs WHERE kind='agent_dispatch'",
                ).fetchall()
            assert run_count == 1
            assert len(job_rows) == 1
            assert json.loads(job_rows[0][0])["run_id"] == original_run_id
    finally:
        runtime.stop()
