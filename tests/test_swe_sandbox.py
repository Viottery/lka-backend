"""Offline isolation contracts, plus a namespace-dependent integration probe."""

import os
import signal
import subprocess
from pathlib import Path

import pytest

from evals.lka_evals import swe_sandbox as sandbox_module
from evals.lka_evals.swe_sandbox import OUTPUT_LIMIT, Sandbox, SandboxError


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: "/usr/bin/bwrap")
    return Sandbox(tmp_path)


def options(argv, option, count):
    return [
        argv[index + 1 : index + 1 + count] for index, value in enumerate(argv) if value == option
    ]


def test_command_confines_mounts_environment_and_shell(sandbox, monkeypatch):
    monkeypatch.setenv("PRIVATE_TOKEN", "secret")
    argv = sandbox.command_argv("printf hello")
    assert "--unshare-all" in argv
    assert "--new-session" in argv
    assert "--die-with-parent" in argv
    assert "--clearenv" in argv
    assert options(argv, "--cap-drop", 1) == [["ALL"]]
    assert options(argv, "--bind", 2) == [[str(sandbox.workspace)] * 2]
    assert all(
        source in {"/usr", "/bin", "/lib", "/lib64"} for source, _ in options(argv, "--ro-bind", 2)
    )
    assert options(argv, "--tmpfs", 1) == [["/tmp"]]
    assert options(argv, "--proc", 1) == [["/proc"]]
    assert options(argv, "--dev", 1) == [["/dev"]]
    env = dict(options(argv, "--setenv", 2))
    assert set(env) == {
        "PATH",
        "HOME",
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        "LANG",
        "workspace_root",
        "WORKSPACE_ROOT",
        "LKA_WORKSPACE_ROOT",
        "LKA_WORKSPACE_ROOTS",
    }
    assert env["HOME"] == "/tmp/home"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["PYTHONPATH"] == f"{sandbox.workspace}/src:{sandbox.workspace}"
    assert "secret" not in str(argv)
    assert argv[-6:] == ["--", "/bin/bash", "--noprofile", "--norc", "-c", "printf hello"]
    for name in ("/bin", "/lib", "/lib64"):
        if Path(name).is_symlink():
            assert [os.readlink(name), name] in options(argv, "--symlink", 2)


def test_dedicated_venv_and_managed_interpreter_are_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: "/usr/bin/bwrap")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = managed\n")
    managed = tmp_path / "managed-python"
    (managed / "bin").mkdir(parents=True)
    interpreter = managed / "bin/python3"
    interpreter.touch()
    python = venv / "bin/python"
    python.symlink_to(interpreter)
    argv = Sandbox(workspace, python).command_argv("python -V")
    mounts = options(argv, "--ro-bind", 2)
    assert [str(venv), str(venv)] in mounts
    assert [str(managed), str(managed)] in mounts
    assert [str(tmp_path), str(tmp_path)] not in mounts
    assert dict(options(argv, "--setenv", 2))["PATH"].startswith(f"{venv}/bin:")
    with pytest.raises(ValueError, match="virtualenv"):
        Sandbox(workspace, interpreter)


def test_cwd_stays_inside_workspace(sandbox, tmp_path):
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    assert options(sandbox.command_argv("true", Path("subdir")), "--chdir", 1) == [[str(subdir)]]
    for cwd in (Path(".."), tmp_path.parent):
        with pytest.raises(ValueError, match="inside"):
            sandbox.command_argv("true", cwd)
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="inside"):
        sandbox.command_argv("true", Path("escape"))


def test_missing_bwrap_fails_closed(sandbox, monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: None)
    assert sandbox.check()["available"] is False
    with pytest.raises(SandboxError, match="bubblewrap"):
        sandbox.run("touch should-never-exist")


@pytest.mark.parametrize("timeout", [False, True])
def test_execution_bounds_output_and_always_kills_process_group(sandbox, monkeypatch, timeout):
    captured = {}
    kills = []

    class Process:
        pid = 991234
        returncode = None

        def __init__(self, argv, **kwargs):
            captured.update(kwargs)
            kwargs["stdout"].write(b"x" * (OUTPUT_LIMIT + 100))
            kwargs["stderr"].write(b"\xff" + b"e" * (OUTPUT_LIMIT + 100))
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if timeout is not None and globals_timeout:
                raise subprocess.TimeoutExpired("bwrap", timeout)
            self.returncode = -9 if globals_timeout else 0
            return self.returncode

    globals_timeout = timeout
    monkeypatch.setattr(sandbox_module.subprocess, "Popen", Process)
    monkeypatch.setattr(sandbox_module.os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    result = sandbox._execute("sleep 100 & true", None, 0.1)
    assert result["timed_out"] is timeout
    assert result["exit_code"] == (-9 if timeout else 0)
    assert len(result["stdout"]) == OUTPUT_LIMIT
    assert len(result["stderr"]) == OUTPUT_LIMIT
    assert kills == [(Process.pid, signal.SIGKILL)]
    assert captured["env"] == {}
    assert captured["start_new_session"] is True
    assert captured["close_fds"] is True
    assert captured["stdin"] == subprocess.DEVNULL


def test_run_requires_successful_probe(sandbox, monkeypatch):
    monkeypatch.setattr(sandbox, "check", lambda: {"available": False, "error": "namespace denied"})
    monkeypatch.setattr(sandbox, "_execute", lambda *args: pytest.fail("Must not execute task"))
    with pytest.raises(SandboxError, match="namespace denied"):
        sandbox.run("true")


def test_bwrap_setup_failure_is_environment_error(sandbox, monkeypatch):
    class Process:
        pid = 991234
        returncode = 1

        def __init__(self, argv, **kwargs):
            kwargs["stderr"].write(b"bwrap: Creating new namespace failed: Operation not permitted")

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(sandbox_module.subprocess, "Popen", Process)
    monkeypatch.setattr(sandbox_module.os, "killpg", lambda *args: None)
    with pytest.raises(SandboxError, match="host execution is disabled"):
        sandbox._execute("true", None, 1)
    assert sandbox.check()["available"] is False


def test_available_sandbox_enforces_runtime_isolation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "oracle-secret"
    outside.write_text("secret")
    (repo / "escape").symlink_to(outside)
    sandbox = Sandbox(repo)
    status = sandbox.check()
    if not status["available"]:
        pytest.skip(status["error"])
    command = (
        f"test ! -e {outside} && test ! -e escape && "
        'test "$HOME" = /tmp/home && test -z "$PRIVATE_TOKEN" && '
        "test ! -e /etc/resolv.conf && printf ok > result && cat result"
    )
    result = sandbox.run(command)
    assert result == {"stdout": "ok", "stderr": "", "exit_code": 0, "timed_out": False}
    assert (repo / "result").read_text() == "ok"
    assert sandbox.run("exit 7")["exit_code"] == 7
