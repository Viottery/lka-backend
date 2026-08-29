from __future__ import annotations

import time
from pathlib import Path

from app.core.config import get_settings
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.tools import ToolContext
from app.tool_packages.bash import is_read_only_command


def _runtime(tmp_path: Path, monkeypatch) -> LocalKnowledgeAgentRuntime:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(workspace))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    return LocalKnowledgeAgentRuntime(get_settings())


def _approved_context() -> ToolContext:
    return ToolContext(
        session_id="session_bash",
        trace_id="trace_bash",
        safety_review_approved=True,
        safety_review_id="test_review_bash",
    )


def test_bash_read_only_classifier_is_conservative():
    assert is_read_only_command("pwd")
    assert is_read_only_command("printenv WORKSPACE_ROOT")
    assert is_read_only_command("ls -la | head -5")
    assert is_read_only_command("git status")
    assert is_read_only_command("rg needle . && git diff")
    assert not is_read_only_command("echo hi > out.txt")
    assert not is_read_only_command("python3 script.py")
    assert not is_read_only_command("git commit -m nope")
    assert not is_read_only_command("ls $(touch x)")
    assert not is_read_only_command("ls `touch x`")
    assert not is_read_only_command("grep needle < input.txt")


def test_bash_sync_read_only_command_runs_without_safety_review(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    (workspace / "notes.txt").write_text("alpha\n", encoding="utf-8")

    result = runtime.tool_executor.execute(
        invocation_id="bash_sync_read_only",
        tool_name="bash.run",
        tool_input={"command": "ls", "cwd": str(workspace), "mode": "sync"},
        context=ToolContext(session_id="session_bash"),
    )

    assert result.status == "completed"
    assert result.output["read_only"] is True
    assert result.output["exit_code"] == 0
    assert "notes.txt" in result.output["stdout"]
    assert runtime.tool_executor.validate_output(
        tool_name="bash.run",
        result=result,
    ) == []


def test_bash_non_read_only_command_requires_safety_review(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"

    rejected = runtime.tool_executor.execute(
        invocation_id="bash_write_rejected",
        tool_name="bash.run",
        tool_input={
            "command": "printf hi > marker.txt",
            "cwd": str(workspace),
            "mode": "sync",
        },
        context=ToolContext(session_id="session_bash"),
    )
    assert rejected.status == "rejected"
    assert rejected.output["safety_review_required"] is True
    assert rejected.output["read_only"] is False
    assert not (workspace / "marker.txt").exists()

    completed = runtime.tool_executor.execute(
        invocation_id="bash_write_approved",
        tool_name="bash.run",
        tool_input={
            "command": "printf hi > marker.txt",
            "cwd": str(workspace),
            "mode": "sync",
        },
        context=_approved_context(),
    )

    assert completed.status == "completed"
    assert completed.output["read_only"] is False
    assert completed.output["exit_code"] == 0
    assert (workspace / "marker.txt").read_text(encoding="utf-8") == "hi"


def test_bash_injects_workspace_variables_and_resolves_relative_cwd(
    tmp_path,
    monkeypatch,
):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    subdir = workspace / "nested"
    subdir.mkdir()

    write_result = runtime.tool_executor.execute(
        invocation_id="bash_write_with_workspace_root",
        tool_name="bash.run",
        tool_input={
            "command": "printf env-ok > \"$workspace_root/nested/from_env.txt\"",
            "cwd": str(workspace),
            "mode": "sync",
        },
        context=_approved_context(),
    )
    assert write_result.status == "completed"
    assert write_result.output["workspace_root"] == workspace.as_posix()
    assert (subdir / "from_env.txt").read_text(encoding="utf-8") == "env-ok"

    read_result = runtime.tool_executor.execute(
        invocation_id="bash_relative_cwd",
        tool_name="bash.run",
        tool_input={
            "command": "pwd && cat from_env.txt && printenv LKA_WORKSPACE_ROOTS",
            "cwd": "nested",
            "mode": "sync",
        },
        context=ToolContext(session_id="session_bash"),
    )
    assert read_result.status == "completed"
    assert read_result.output["read_only"] is True
    assert read_result.output["cwd"] == subdir.as_posix()
    assert read_result.output["workspace_root"] == workspace.as_posix()
    assert "env-ok" in read_result.output["stdout"]
    assert workspace.as_posix() in read_result.output["stdout"]

    cwd_var_result = runtime.tool_executor.execute(
        invocation_id="bash_workspace_variable_cwd",
        tool_name="bash.run",
        tool_input={
            "command": "pwd",
            "cwd": "$workspace_root/nested",
            "mode": "sync",
        },
        context=ToolContext(session_id="session_bash"),
    )
    assert cwd_var_result.status == "completed"
    assert cwd_var_result.output["cwd"] == subdir.as_posix()


def test_bash_background_session_supports_interaction_and_output_polling(
    tmp_path,
    monkeypatch,
):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    context = _approved_context()

    started = runtime.tool_executor.execute(
        invocation_id="bash_background_start",
        tool_name="bash.run",
        tool_input={
            "command": "printf 'ready\\n'; read line; printf 'got:%s\\n' \"$line\"",
            "cwd": str(workspace),
            "mode": "background",
        },
        context=context,
    )

    assert started.status == "completed"
    session_id = started.output["session_id"]
    assert started.output["running"] is True

    ready = _wait_for_output(runtime, session_id, "ready")
    assert ready["running"] is True

    active = runtime.tool_executor.execute(
        invocation_id="bash_list_active",
        tool_name="bash.list_sessions",
        tool_input={"active_only": True},
        context=ToolContext(session_id="session_bash"),
    )
    assert session_id in {session["session_id"] for session in active.output["sessions"]}

    written = runtime.tool_executor.execute(
        invocation_id="bash_write_session",
        tool_name="bash.write_session",
        tool_input={"session_id": session_id, "text": "hello\n"},
        context=context,
    )
    assert written.status == "completed"
    assert written.output["bytes_written"] == len("hello\n")

    done = _wait_for_output(runtime, session_id, "got:hello")
    assert done["running"] is False
    assert done["exit_code"] == 0


def test_bash_background_session_receives_workspace_environment(
    tmp_path,
    monkeypatch,
):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"

    started = runtime.tool_executor.execute(
        invocation_id="bash_background_env",
        tool_name="bash.run",
        tool_input={
            "command": "printenv WORKSPACE_ROOT",
            "cwd": str(workspace),
            "mode": "background",
        },
        context=ToolContext(session_id="session_bash"),
    )

    assert started.status == "completed"
    session_id = started.output["session_id"]
    output = _wait_for_output(runtime, session_id, workspace.as_posix())
    assert output["workspace_root"] == workspace.as_posix()


def test_bash_interrupt_and_terminate_sessions(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    context = _approved_context()

    interrupted_id = _start_sleep_session(runtime, workspace, context, "bash_sleep_int")
    interrupted = runtime.tool_executor.execute(
        invocation_id="bash_interrupt",
        tool_name="bash.interrupt_session",
        tool_input={"session_id": interrupted_id},
        context=context,
    )
    assert interrupted.status == "completed"
    interrupted_status = _wait_for_not_running(runtime, interrupted_id)
    assert interrupted_status["running"] is False

    terminated_id = _start_sleep_session(runtime, workspace, context, "bash_sleep_term")
    terminated = runtime.tool_executor.execute(
        invocation_id="bash_terminate",
        tool_name="bash.terminate_session",
        tool_input={"session_id": terminated_id},
        context=context,
    )
    assert terminated.status == "completed"
    terminated_status = _wait_for_not_running(runtime, terminated_id)
    assert terminated_status["running"] is False


def _start_sleep_session(runtime, workspace: Path, context: ToolContext, invocation_id: str) -> str:
    result = runtime.tool_executor.execute(
        invocation_id=invocation_id,
        tool_name="bash.run",
        tool_input={
            "command": "sleep 10",
            "cwd": str(workspace),
            "mode": "background",
        },
        context=context,
    )
    assert result.status == "completed"
    session_id = result.output["session_id"]
    assert isinstance(session_id, str)
    return session_id


def _wait_for_output(runtime, session_id: str, expected: str) -> dict:
    deadline = time.monotonic() + 5
    offset = 0
    collected = ""
    latest: dict = {}
    while time.monotonic() < deadline:
        result = runtime.tool_executor.execute(
            invocation_id=f"bash_read_{time.monotonic_ns()}",
            tool_name="bash.read_session",
            tool_input={"session_id": session_id, "offset": offset},
            context=ToolContext(session_id="session_bash"),
        )
        assert result.status == "completed"
        latest = result.output
        collected += latest["output"]
        offset = latest["next_offset"]
        if expected in collected:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {expected!r}; saw {collected!r}")


def _wait_for_not_running(runtime, session_id: str) -> dict:
    deadline = time.monotonic() + 5
    latest: dict = {}
    while time.monotonic() < deadline:
        result = runtime.tool_executor.execute(
            invocation_id=f"bash_status_{time.monotonic_ns()}",
            tool_name="bash.read_session",
            tool_input={"session_id": session_id},
            context=ToolContext(session_id="session_bash"),
        )
        assert result.status == "completed"
        latest = result.output
        if latest["running"] is False:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for session to stop: {latest}")
