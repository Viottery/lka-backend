"""Async newline-delimited stdio transport for a Codex app-server process."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import stat
import subprocess
from pathlib import Path


class CodexTransportError(RuntimeError):
    """The Codex subprocess transport could not read, write, or shut down."""


def _codex_process_env() -> dict[str, str]:
    """Keep backend credentials and instrumentation out of the expert process."""
    allowed = {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TEMP", "TMP",
        "TERM", "SystemRoot", "WINDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith("LC_")
    }


def _prepare_codex_state_home(path: str | Path) -> Path:
    """Create/validate a private Codex state directory owned by this user."""
    state_home = Path(path).expanduser()
    if not state_home.is_absolute():
        raise ValueError("Codex state home must be an absolute configured path.")
    try:
        state_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = state_home.lstat()
    except OSError as exc:
        raise ValueError("Codex state home could not be created or inspected.") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Codex state home must be a real directory, not a symlink.")
    if os.name != "nt":
        if info.st_uid != os.getuid():
            raise ValueError("Codex state home must be owned by the current user.")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Codex state home permissions must be private (0700).")
    if not os.access(state_home, os.R_OK | os.W_OK | os.X_OK):
        raise ValueError("Codex state home must be readable, writable, and searchable.")
    return state_home.resolve()


class CodexSubprocessTransport:
    """Own one Codex app-server subprocess and its line-oriented stdio pipes."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        stderr_limit_bytes: int,
        terminate_grace_seconds: float,
    ) -> None:
        self._process = process
        self._stderr_limit_bytes = stderr_limit_bytes
        self._terminate_grace_seconds = terminate_grace_seconds
        self._stderr_tail = bytearray()
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._closed = False
        self._write_lock = asyncio.Lock()

    @classmethod
    async def start(
        cls,
        *,
        binary_path: str | Path,
        cwd: str | Path,
        codex_state_home: str | Path | None = None,
        codex_home: str | Path | None = None,
        permission_profile_id: str | None = None,
        stderr_limit_bytes: int = 64 * 1024,
        terminate_grace_seconds: float = 2.0,
    ) -> CodexSubprocessTransport:
        """Start the configured executable as ``<binary> app-server``.

        The executable and working directory must be explicit existing paths.
        Only a small OS environment allowlist is inherited. Codex may still
        read its own local authentication files through the user's home path.
        A configured state home overrides app-server's SQLite state location
        without changing the Codex home used for local authentication. No shell
        is involved.
        """
        executable = Path(binary_path).expanduser()
        workdir = Path(cwd).expanduser()
        if not executable.is_absolute():
            raise ValueError("Codex binary_path must be an absolute configured path.")
        if not executable.is_file():
            raise ValueError("Configured Codex binary does not exist or is not a file.")
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise ValueError("Configured Codex binary is not executable.")
        if not workdir.is_absolute() or not workdir.is_dir():
            raise ValueError("Codex cwd must be an existing absolute directory.")
        if stderr_limit_bytes < 0:
            raise ValueError("stderr_limit_bytes must be non-negative.")
        if terminate_grace_seconds < 0:
            raise ValueError("terminate_grace_seconds must be non-negative.")

        process_env = _codex_process_env()
        arguments = [str(executable.resolve()), "app-server"]
        if codex_home is not None:
            process_env["CODEX_HOME"] = str(_prepare_codex_state_home(codex_home))
        if codex_state_home is not None:
            state_home = _prepare_codex_state_home(codex_state_home)
            arguments.extend(("-c", f"sqlite_home={json.dumps(str(state_home))}"))
        if permission_profile_id is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", permission_profile_id):
                raise ValueError("Codex permission profile id must be a custom profile name.")
            # Define this profile only for the child process, leaving the user's
            # Codex configuration untouched. Runtime workspace roots are sent
            # by the protocol client when each thread/turn is started.
            arguments.extend(("-c", f"default_permissions={json.dumps(permission_profile_id)}"))
            profile = f"permissions.{permission_profile_id}"
            arguments.extend(("-c", f'{profile}.extends=":workspace"'))
            # The CLI's dotted -c parser treats quotes in a key as literal
            # characters. An inline TOML table preserves absolute paths with
            # dots and grants read access to exactly the trusted Codex binary,
            # which its command sandbox must execute to launch bubblewrap.
            binary_rule = f"{json.dumps(str(executable.resolve()))}=\"read\""
            rules = (
                '{":root"="deny",":minimal"="read",'
                f'":tmpdir"="deny",":slash_tmp"="deny",{binary_rule}}}'
            )
            arguments.extend(("-c", f"{profile}.filesystem={rules}"))
        kwargs: dict[str, object] = {
            "cwd": str(workdir.resolve()),
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "limit": 1024 * 1024,
            "env": process_env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments, **kwargs
            )
        except (OSError, ValueError) as exc:
            raise CodexTransportError("Could not start the configured Codex app-server.") from exc
        return cls(
            process,
            stderr_limit_bytes=stderr_limit_bytes,
            terminate_grace_seconds=terminate_grace_seconds,
        )

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def stderr_tail(self) -> str:
        """A bounded UTF-8 replacement-decoded tail for diagnostics."""
        return self._stderr_tail.decode("utf-8", errors="replace")

    async def write_line(self, line: str) -> None:
        if self._closed or self._process.stdin is None:
            raise CodexTransportError("Codex app-server stdin is closed.")
        if not isinstance(line, str) or line.count("\n") > (1 if line.endswith("\n") else 0):
            raise ValueError("Transport accepts exactly one line at a time.")
        encoded = line.encode("utf-8")
        if not encoded.endswith(b"\n"):
            encoded += b"\n"
        async with self._write_lock:
            try:
                self._process.stdin.write(encoded)
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                raise CodexTransportError("Codex app-server stdin write failed.") from exc

    async def read_line(self) -> str | None:
        if self._process.stdout is None:
            raise CodexTransportError("Codex app-server stdout is unavailable.")
        try:
            line = await self._process.stdout.readline()
        except (ValueError, OSError) as exc:
            raise CodexTransportError("Codex app-server stdout read failed.") from exc
        if not line:
            return None
        try:
            return line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CodexTransportError("Codex app-server emitted non-UTF-8 output.") from exc

    async def close(self) -> None:
        """Close stdin and stop the process, escalating after a short grace."""
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            self._process.stdin.close()
            try:
                await self._process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        try:
            await asyncio.wait_for(
                asyncio.shield(self._process.wait()),
                timeout=self._terminate_grace_seconds,
            )
        except TimeoutError:
            await self._terminate_process_group()
        if not self._stderr_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stderr_task),
                    timeout=max(0.1, self._terminate_grace_seconds),
                )
            except TimeoutError:
                self._stderr_task.cancel()
                await asyncio.gather(self._stderr_task, return_exceptions=True)

    async def _terminate_process_group(self) -> None:
        if self._process.returncode is not None:
            return
        try:
            if os.name == "nt":
                self._process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(self._process.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError, ValueError):
            if os.name == "nt" and self._process.returncode is None:
                self._process.terminate()
        try:
            await asyncio.wait_for(
                asyncio.shield(self._process.wait()),
                timeout=self._terminate_grace_seconds,
            )
            return
        except TimeoutError:
            pass
        try:
            if os.name == "nt":
                self._process.kill()
            else:
                os.killpg(self._process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await self._process.wait()

    async def _drain_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            if self._stderr_limit_bytes == 0:
                continue
            self._stderr_tail.extend(chunk)
            overflow = len(self._stderr_tail) - self._stderr_limit_bytes
            if overflow > 0:
                del self._stderr_tail[:overflow]


__all__ = ["CodexSubprocessTransport", "CodexTransportError"]
