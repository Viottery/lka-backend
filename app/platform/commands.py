"""Native shell execution; platform process and terminal details stay here."""
from __future__ import annotations

import base64
import os
import select
import signal
import subprocess
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

IS_WINDOWS = os.name == "nt"
SHELL_NAME = "powershell" if IS_WINDOWS else "bash"


def shell_argv(command: str, *, terminal: bool = False, ready_marker: str | None = None) -> list[str]:
    if IS_WINDOWS:
        # EncodedCommand avoids command-line quoting and preserves Unicode on PS 5.1.
        script = (
            "$ProgressPreference = 'SilentlyContinue'; "
            "$utf8 = New-Object System.Text.UTF8Encoding($false); "
            "[Console]::InputEncoding = $utf8; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8; "
            "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
            "try { & { " + command + "\n }; "
            "if (-not $?) { exit 1 }; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE } } "
            "catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }"
        )
        if terminal:
            # CREATE_NEW_PROCESS_GROUP/host shells may pass an inherited ignore-
            # Ctrl-C attribute. Explicitly clear it in this isolated child only.
            reset = (
                "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
                'public static class LkaConsole { [DllImport("kernel32.dll")] '
                "public static extern bool SetConsoleCtrlHandler(IntPtr h, bool add); }'; "
                "[LkaConsole]::SetConsoleCtrlHandler([IntPtr]::Zero, $false) | Out-Null; "
            )
            ready = "[Console]::Write('" + ready_marker + "'); " if ready_marker else ""
            script = "$ProgressPreference = 'SilentlyContinue'; $ErrorActionPreference = 'Stop'; " + reset + ready + script
        executable = str(Path(os.environ.get("SystemRoot", "C:/Windows")) /
                         "System32/WindowsPowerShell/v1.0/powershell.exe")
        return [executable, "-NoLogo", "-NoProfile", "-OutputFormat", "Text", "-EncodedCommand",
                base64.b64encode(script.encode("utf-16le")).decode("ascii")]
    return ["/bin/bash", "-lc", command]


def run_sync(command: str, *, cwd: Path, env: dict[str, str], timeout: int) -> tuple[bytes, bytes, int | None, bool]:
    if IS_WINDOWS:
        from app.platform.windows_process import WindowsProcess
        process = WindowsProcess(shell_argv(command), cwd=cwd, env=env, terminal=False)
        try:
            return process.communicate(timeout)
        finally:
            process.close()
    process = subprocess.Popen(shell_argv(command), cwd=cwd, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return stdout, stderr, process.returncode, False
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        return stdout, stderr, None, True
    finally:
        # Detached children in this process group must not outlive a sync command.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class PosixTerminal:
    def __init__(self, command: str, *, cwd: Path, env: dict[str, str]) -> None:
        import pty
        self._closed = False
        self.fd, slave = pty.openpty()
        try:
            self.process = subprocess.Popen(shell_argv(command), cwd=cwd, env=env,
                                            stdin=slave, stdout=slave, stderr=slave,
                                            start_new_session=True, close_fds=True)
        except BaseException:
            os.close(self.fd)
            raise
        finally:
            os.close(slave)

    def poll(self) -> int | None:
        return self.process.poll()

    def chunks(self) -> Iterator[bytes]:
        while True:
            ready, _, _ = select.select([self.fd], [], [], .1)
            if ready:
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                yield chunk
            if self.poll() is not None and not select.select([self.fd], [], [], 0)[0]:
                break

    def write(self, raw: bytes) -> int:
        return os.write(self.fd, raw)

    def interrupt(self) -> None:
        if self.poll() is None:
            os.killpg(self.process.pid, signal.SIGINT)

    def terminate(self) -> None:
        if self._closed:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self.process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            pass
        # Also kill descendants that ignored SIGTERM after their shell exited.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait()

    def close(self) -> None:
        if self._closed:
            return
        self.terminate()
        self._closed = True
        try:
            os.close(self.fd)
        except OSError:
            pass


class WindowsTerminal:
    """ConPTY with a private shell-readiness handshake for early interrupts."""
    def __init__(self, command: str, *, cwd: Path, env: dict[str, str]):
        from app.platform.windows_process import WindowsProcess
        self._marker = ("LKA_READY_" + uuid.uuid4().hex).encode("ascii")
        self._ready = threading.Event()
        self._interrupt_lock = threading.Lock()
        self._pending_interrupt = False
        self.process = WindowsProcess(
            shell_argv(command, terminal=True, ready_marker=self._marker.decode("ascii")),
            cwd=cwd, env=env, terminal=True,
        )

    def poll(self):
        return self.process.poll()

    def chunks(self):
        prefix = bytearray()
        for chunk in self.process.chunks():
            if self._ready.is_set():
                yield chunk
                continue
            prefix.extend(chunk)
            location = prefix.find(self._marker)
            if location >= 0:
                self._ready.set()
                with self._interrupt_lock:
                    pending, self._pending_interrupt = self._pending_interrupt, False
                if pending:
                    self.process.interrupt()
                clean = bytes(prefix[:location] + prefix[location + len(self._marker):])
                if clean:
                    yield clean
                prefix.clear()
        if prefix:
            yield bytes(prefix)

    def write(self, raw):
        return self.process.write(raw)

    def interrupt(self):
        with self._interrupt_lock:
            if not self._ready.is_set():
                self._pending_interrupt = True
                return
        self.process.interrupt()

    def terminate(self):
        self.process.terminate()

    def close(self):
        self.process.close()


def start_terminal(command: str, *, cwd: Path, env: dict[str, str]):
    if IS_WINDOWS:
        return WindowsTerminal(command, cwd=cwd, env=env)
    return PosixTerminal(command, cwd=cwd, env=env)
