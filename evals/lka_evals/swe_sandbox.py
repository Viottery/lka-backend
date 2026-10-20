"""Fail-closed, network-free Linux command sandbox for disposable SWE workspaces."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

OUTPUT_LIMIT = 128 * 1024


class SandboxError(RuntimeError):
    """The host cannot provide the required benchmark isolation."""


class Sandbox:
    """Expose one disposable checkout and an optional dedicated Python environment."""

    def __init__(self, workspace: Path, python: Path | None = None):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir() or self.workspace == Path("/"):
            raise ValueError("workspace must be a disposable repository directory")
        self.python = Path(os.path.abspath(python)) if python is not None else None
        if self.python is not None:
            root = self.python.parent.parent
            if self.python.parent.name != "bin" or not (root / "pyvenv.cfg").is_file():
                raise ValueError("python must point to a dedicated virtualenv's bin/python")
            if not self.python.is_file():
                raise ValueError(f"Python interpreter does not exist: {self.python}")

    def command_argv(self, command: str, cwd: Path | None = None) -> list[str]:
        """Build the complete isolated invocation; never invoke a host shell."""
        if sys.platform != "linux" or not (bwrap := shutil.which("bwrap")):
            raise SandboxError(
                "SWE isolation requires Linux bubblewrap (bwrap). Install bubblewrap and "
                "enable unprivileged user namespaces; host execution is disabled."
            )
        directory = Path(cwd) if cwd is not None else self.workspace
        if not directory.is_absolute():
            directory = self.workspace / directory
        directory = directory.resolve(strict=True)
        if not directory.is_relative_to(self.workspace) or not directory.is_dir():
            raise ValueError("cwd must be a directory inside the disposable workspace")
        argv = [
            bwrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
        ]
        for name in ("/usr", "/bin", "/lib", "/lib64"):
            path = Path(name)
            if path.is_symlink():
                argv += ["--symlink", os.readlink(path), name]
            elif path.is_dir():
                argv += ["--ro-bind", name, name]
        argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/tmp/home"]
        if self.python is not None:
            # uv environments link to a separate managed interpreter, including its stdlib.
            roots = (self.python.parent.parent.resolve(), self.python.resolve().parent.parent)
            for root in dict.fromkeys(roots):
                if (
                    root == Path("/")
                    or self.workspace.is_relative_to(root)
                    or root.is_relative_to(self.workspace)
                ):
                    raise ValueError(
                        "Python environment must be separate from the disposable workspace"
                    )
                if not root.is_relative_to(Path("/usr")):
                    argv += ["--ro-bind", str(root), str(root)]
            # Keep the lexical venv path usable even if its directory is a symlink.
            lexical_root = self.python.parent.parent
            if lexical_root != lexical_root.resolve():
                argv += ["--symlink", str(lexical_root.resolve()), str(lexical_root)]
        argv += ["--bind", str(self.workspace), str(self.workspace)]
        environment = {
            "PATH": f"{self.python.parent}:/usr/bin:/bin" if self.python else "/usr/bin:/bin",
            "HOME": "/tmp/home",
            "PYTHONPATH": f"{self.workspace / 'src'}:{self.workspace}",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
            "workspace_root": str(self.workspace),
            "WORKSPACE_ROOT": str(self.workspace),
            "LKA_WORKSPACE_ROOT": str(self.workspace),
            "LKA_WORKSPACE_ROOTS": str(self.workspace),
        }
        for key, value in environment.items():
            argv += ["--setenv", key, value]
        return argv + [
            "--chdir",
            str(directory),
            "--",
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            command,
        ]

    def _execute(self, command: str, cwd: Path | None, timeout: float) -> dict:
        argv = self.command_argv(command, cwd)
        timed_out = False
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    env={},
                    close_fds=True,
                )
            except OSError as exc:
                raise SandboxError(f"Cannot start bubblewrap: {exc}") from exc
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                # Also clean up background children when the shell exits successfully.
                # The isolated PID namespace kills descendants that start new sessions.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            stdout.seek(0)
            stderr.seek(0)
            result = {
                "stdout": stdout.read(OUTPUT_LIMIT).decode("utf-8", errors="replace"),
                "stderr": stderr.read(OUTPUT_LIMIT).decode("utf-8", errors="replace"),
                "exit_code": process.returncode,
                "timed_out": timed_out,
            }
        if not timed_out and result["exit_code"] and result["stderr"].startswith("bwrap:"):
            raise SandboxError(
                f"Bubblewrap could not provide isolation: {result['stderr'].strip()}. "
                "Check user/mount namespace support and host security policy; "
                "host execution is disabled."
            )
        return result

    def check(self) -> dict:
        """Probe actual namespace creation, without network or model calls."""
        try:
            result = self._execute("true", None, 10)
            if result["timed_out"] or result["exit_code"] != 0:
                raise SandboxError(f"Bubblewrap isolation probe failed: {result['stderr']}")
        except (SandboxError, OSError) as exc:
            return {"available": False, "error": str(exc)}
        return {"available": True, "error": None}

    def run(self, command: str, cwd: Path | None = None, timeout: float = 60) -> dict:
        """Run only after a real isolation probe succeeds; return bounded output."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        # Validate cwd before attempting any subprocess.
        self.command_argv(command, cwd)
        status = self.check()
        if not status["available"]:
            raise SandboxError(status["error"])
        return self._execute(command, cwd, timeout)
