"""Agent-visible bash command and terminal session tools."""

from __future__ import annotations

import os
import re
import select
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import pty
except ImportError:  # pragma: no cover - Windows fallback path.
    pty = None  # type: ignore[assignment]

from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec


DEFAULT_SYNC_TIMEOUT_SECONDS = 30
MAX_SYNC_TIMEOUT_SECONDS = 300
DEFAULT_OUTPUT_BYTES = 32_768
MAX_OUTPUT_BYTES = 131_072
SESSION_BUFFER_BYTES = 1_000_000
READ_ONLY_COMMANDS = {
    "cat",
    "cut",
    "df",
    "du",
    "file",
    "find",
    "grep",
    "head",
    "ls",
    "nl",
    "printenv",
    "pwd",
    "rg",
    "sed",
    "sort",
    "stat",
    "tail",
    "tr",
    "uniq",
    "wc",
    "whoami",
}
READ_ONLY_GIT_SUBCOMMANDS = {
    "branch",
    "diff",
    "log",
    "show",
    "status",
}
SHELL_CONTROL_TOKENS = {"&&", "||", ";", "|"}


BASH_PACKAGE = ToolPackageSpec(
    name="bash",
    description=(
        "Run bash commands in configured workspace roots. Supports synchronous commands, "
        "background terminal sessions, output polling, stdin writes, Ctrl-C, termination, "
        "active session listing, workspace-relative cwd values, and injected workspace "
        "environment variables."
    ),
    risk="high",
    requires_expansion=True,
    routing_hints=[
        "Use this package for directory listing, text search, command execution, tests, builds, and scripts.",
        "Use filesystem.read_file/edit_file for precise file reading or targeted file edits when possible.",
    ],
    decision_hints=[
        "Prefer read-only commands first, such as pwd, printenv, ls, find, rg, grep, cat, sed, head, tail, wc, git status, git diff, git log, and git show.",
        "Commands outside the read-only whitelist are treated as non-read-only and must pass safety review.",
        "Read-only classification also checks arguments and shell expansion. Writing options, process hooks, arbitrary programs and executable paths require review; use literal quoted patterns and quoted workspace-root variables for inspection.",
        "Use mode=background for long-running or interactive commands, then poll with bash.read_session.",
        "Use bash.write_session to send stdin to a background terminal, bash.interrupt_session for Ctrl-C, and bash.terminate_session to stop it.",
        "The default cwd is the first configured workspace root. Relative cwd values are resolved inside that workspace root.",
        "Commands receive workspace_root, WORKSPACE_ROOT, LKA_WORKSPACE_ROOT, and LKA_WORKSPACE_ROOTS environment variables.",
        "Use relative paths from cwd or the injected $workspace_root variable; do not invent placeholder paths.",
        "Always inspect command status and output before claiming completion.",
    ],
)


@dataclass(frozen=True)
class BashAccessPolicy:
    roots: list[Path]
    allow_session_root_override: bool = False

    @classmethod
    def from_workspace_roots(cls, roots: list[Path] | None) -> "BashAccessPolicy":
        configured = [root.expanduser().resolve(strict=False) for root in roots or []]
        if not configured:
            configured = [Path.cwd().resolve(strict=False)]
        return cls(roots=configured, allow_session_root_override=not bool(roots))

    def for_session_workspace(self, workspace_root: str | None) -> "BashAccessPolicy":
        if not workspace_root:
            return self
        root = Path(workspace_root).expanduser().resolve(strict=False)
        if not self.allow_session_root_override and not self._is_allowed(root):
            raise PermissionError("session workspace is outside allowed workspace roots")
        return BashAccessPolicy(roots=[root])

    def resolve_cwd(self, cwd_value: str | None) -> Path:
        if cwd_value and cwd_value.strip():
            path = Path(self.expand_workspace_variables(cwd_value)).expanduser()
            if not path.is_absolute():
                path = self.default_root / path
            resolved = path.resolve(strict=False)
        else:
            resolved = self.default_root
        if not self._is_allowed(resolved):
            allowed = ", ".join(root.as_posix() for root in self.roots)
            raise PermissionError(f"cwd is outside allowed workspace roots: {allowed}")
        if not resolved.exists():
            raise FileNotFoundError(f"cwd does not exist: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"cwd is not a directory: {resolved}")
        return resolved

    @property
    def default_root(self) -> Path:
        return self.roots[0]

    def env(self) -> dict[str, str]:
        root = self.default_root.as_posix()
        return {
            "workspace_root": root,
            "WORKSPACE_ROOT": root,
            "LKA_WORKSPACE_ROOT": root,
            "LKA_WORKSPACE_ROOTS": ";".join(item.as_posix() for item in self.roots),
        }

    def expand_workspace_variables(self, value: str) -> str:
        root = self.default_root.as_posix()
        return (
            value.replace("$workspace_root", root)
            .replace("${workspace_root}", root)
            .replace("$WORKSPACE_ROOT", root)
            .replace("${WORKSPACE_ROOT}", root)
            .replace("$LKA_WORKSPACE_ROOT", root)
            .replace("${LKA_WORKSPACE_ROOT}", root)
        )

    def _is_allowed(self, path: Path) -> bool:
        for root in self.roots:
            try:
                path.relative_to(root)
            except ValueError:
                continue
            return True
        return False


@dataclass
class BashSession:
    session_id: str
    command: str
    cwd: Path
    workspace_root: Path
    read_only: bool
    process: subprocess.Popen[bytes]
    master_fd: int
    started_at: float
    owner_run_id: str | None = None
    output: bytearray = field(default_factory=bytearray)
    output_start_offset: int = 0
    reader_error: str | None = None


class BashSessionManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, BashSession] = {}
        self._next_id = 1

    def start(
        self,
        *,
        command: str,
        cwd: Path,
        workspace_root: Path,
        read_only: bool,
        env: dict[str, str],
        owner_run_id: str | None = None,
    ) -> BashSession:
        if pty is None:
            raise RuntimeError("background bash sessions require POSIX pty support.")
        master_fd, slave_fd = pty.openpty()
        try:
            process = subprocess.Popen(
                ["/bin/bash", "-lc", command],
                cwd=cwd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)
        with self._lock:
            session_id = f"bash_session_{self._next_id:06d}"
            self._next_id += 1
            session = BashSession(
                session_id=session_id,
                command=command,
                cwd=cwd,
                workspace_root=workspace_root,
                read_only=read_only,
                process=process,
                master_fd=master_fd,
                started_at=time.time(),
                owner_run_id=owner_run_id,
            )
            self._sessions[session_id] = session
        threading.Thread(
            target=self._reader_loop,
            args=(session,),
            name=f"lka-bash-reader-{session_id}",
            daemon=True,
        ).start()
        return session

    def get(self, session_id: str, *, owner_run_id: str | None = None) -> BashSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"bash session not found: {session_id}")
            if owner_run_id is not None and session.owner_run_id != owner_run_id:
                raise KeyError(f"bash session not found: {session_id}")
            return session

    def list(
        self, *, active_only: bool = False, owner_run_id: str | None = None
    ) -> list[BashSession]:
        with self._lock:
            sessions = list(self._sessions.values())
        if active_only:
            sessions = [session for session in sessions if session.process.poll() is None]
        if owner_run_id is not None:
            sessions = [session for session in sessions if session.owner_run_id == owner_run_id]
        return sessions

    def read(
        self,
        *,
        session_id: str,
        offset: int = 0,
        max_bytes: int = DEFAULT_OUTPUT_BYTES,
        owner_run_id: str | None = None,
    ) -> dict[str, Any]:
        session = self.get(session_id, owner_run_id=owner_run_id)
        with self._lock:
            start = max(offset, session.output_start_offset)
            local_start = start - session.output_start_offset
            byte_limit = min(max(1, max_bytes), MAX_OUTPUT_BYTES)
            raw = bytes(session.output[local_start : local_start + byte_limit])
            next_offset = start + len(raw)
            end_offset = session.output_start_offset + len(session.output)
            truncated = next_offset < end_offset
            output_start_offset = session.output_start_offset
            reader_error = session.reader_error
        return {
            **self._session_status_payload(session),
            "offset": start,
            "next_offset": next_offset,
            "output_start_offset": output_start_offset,
            "output": raw.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "reader_error": reader_error,
        }

    def write(self, *, session_id: str, text: str, owner_run_id: str | None = None) -> int:
        session = self.get(session_id, owner_run_id=owner_run_id)
        if session.process.poll() is not None:
            raise RuntimeError(f"bash session is not running: {session_id}")
        return os.write(session.master_fd, text.encode("utf-8"))

    def interrupt(self, *, session_id: str, owner_run_id: str | None = None) -> None:
        session = self.get(session_id, owner_run_id=owner_run_id)
        if session.process.poll() is None:
            os.killpg(session.process.pid, signal.SIGINT)

    def terminate(self, *, session_id: str, owner_run_id: str | None = None) -> None:
        session = self.get(session_id, owner_run_id=owner_run_id)
        if session.process.poll() is None:
            os.killpg(session.process.pid, signal.SIGTERM)

    def _reader_loop(self, session: BashSession) -> None:
        try:
            while True:
                ready, _, _ = select.select([session.master_fd], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(session.master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    with self._lock:
                        session.output.extend(chunk)
                        if len(session.output) > SESSION_BUFFER_BYTES:
                            overflow = len(session.output) - SESSION_BUFFER_BYTES
                            del session.output[:overflow]
                            session.output_start_offset += overflow
                if session.process.poll() is not None:
                    ready, _, _ = select.select([session.master_fd], [], [], 0)
                    if not ready:
                        break
        except Exception as exc:  # pragma: no cover - defensive reader path.
            with self._lock:
                session.reader_error = str(exc)
        finally:
            try:
                os.close(session.master_fd)
            except OSError:
                pass

    def _session_status_payload(self, session: BashSession) -> dict[str, Any]:
        exit_code = session.process.poll()
        return {
            "session_id": session.session_id,
            "command": session.command,
            "cwd": session.cwd.as_posix(),
            "workspace_root": session.workspace_root.as_posix(),
            "read_only": session.read_only,
            "status": "running" if exit_code is None else "exited",
            "running": exit_code is None,
            "exit_code": exit_code,
        }


class BashRunTool:
    def __init__(self, *, policy: BashAccessPolicy, session_manager: BashSessionManager) -> None:
        self.policy = policy
        self.session_manager = session_manager

    spec = ToolSpec(
        name="bash.run",
        package="bash",
        type="local_tool",
        description=(
            "Run a bash command in sync or background mode. The command is classified "
            "per invocation as read_only only for whitelisted commands with safe arguments; "
            "otherwise it is non-read-only and requires safety review."
        ),
        risk="high",
        requires_confirmation=False,
        read_only=False,
        supports_read_only_invocations=True,
        side_effects=["execute_local_process", "read_local_files", "write_local_files"],
        scope_uses_workspace=True,
        input_schema={
            "type": "object",
            "required": ["command"],
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "mode": {"type": "string", "allowed_values": ["sync", "background"]},
                "timeout_seconds": {"type": "integer", "minimum": 1},
                "max_output_bytes": {"type": "integer", "minimum": 1},
            },
        },
        output_schema={
            "mode": "string",
            "command": "string",
            "cwd": "string",
            "workspace_root": "string",
            "read_only": "boolean",
            "status": "string",
            "running": "boolean",
            "exit_code": ["integer", "null"],
            "timed_out": "boolean",
            "stdout": "string",
            "stderr": "string",
            "output": "string",
            "session_id": ["string", "null"],
            "next_offset": ["integer", "null"],
        },
    )

    def is_read_only_invocation(self, tool_input: dict[str, Any]) -> bool:
        return is_read_only_command(str(tool_input.get("command") or ""))

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        if (
            _child_run_id(context) is not None
            and context.tool_view is not None
            and not context.tool_view.full_workspace_authority
        ):
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="rejected",
                error=(
                    "bash.run is unavailable because arbitrary shell paths cannot be safely "
                    "constrained to a narrowed child workspace scope."
                ),
            )
        command = str(invocation.input.get("command") or "")
        mode = str(invocation.input.get("mode") or "sync")
        read_only = self.is_read_only_invocation(invocation.input)
        try:
            if not command.strip():
                raise ValueError("command is required.")
            policy = self.policy.for_session_workspace(context.workspace_root)
            cwd = policy.resolve_cwd(invocation.input.get("cwd"))
            if mode == "background":
                payload = self._run_background(
                    command=command,
                    cwd=cwd,
                    read_only=read_only,
                    policy=policy,
                    owner_run_id=_child_run_id(context),
                )
            else:
                payload = self._run_sync(
                    command=command,
                    cwd=cwd,
                    read_only=read_only,
                    policy=policy,
                    timeout_seconds=int(
                        invocation.input.get("timeout_seconds")
                        or DEFAULT_SYNC_TIMEOUT_SECONDS
                    ),
                    max_output_bytes=int(
                        invocation.input.get("max_output_bytes")
                        or DEFAULT_OUTPUT_BYTES
                    ),
                )
        except (OSError, RuntimeError, ValueError, PermissionError) as exc:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="failed",
                error=str(exc),
            )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=payload,
        )

    def _run_sync(
        self,
        *,
        command: str,
        cwd: Path,
        read_only: bool,
        policy: BashAccessPolicy,
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        timeout = min(max(1, timeout_seconds), MAX_SYNC_TIMEOUT_SECONDS)
        byte_limit = min(max(1, max_output_bytes), MAX_OUTPUT_BYTES)
        timed_out = False
        try:
            completed = subprocess.run(
                ["/bin/bash", "-lc", command],
                cwd=cwd,
                env=self._env(policy),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            stdout_raw = completed.stdout
            stderr_raw = completed.stderr
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout_raw = exc.stdout or b""
            stderr_raw = exc.stderr or b""
            exit_code = None
        stdout = _decode_limited(stdout_raw, byte_limit)
        stderr = _decode_limited(stderr_raw, byte_limit)
        return {
            "mode": "sync",
            "command": command,
            "cwd": cwd.as_posix(),
            "workspace_root": policy.default_root.as_posix(),
            "read_only": read_only,
            "status": "timed_out" if timed_out else "exited",
            "running": False,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": stdout,
            "stderr": stderr,
            "output": stdout + stderr,
            "session_id": None,
            "next_offset": None,
        }

    def _run_background(
        self,
        *,
        command: str,
        cwd: Path,
        read_only: bool,
        policy: BashAccessPolicy,
        owner_run_id: str | None = None,
    ) -> dict[str, Any]:
        session = self.session_manager.start(
            command=command,
            cwd=cwd,
            workspace_root=policy.default_root,
            read_only=read_only,
            env=self._env(policy),
            owner_run_id=owner_run_id,
        )
        return {
            "mode": "background",
            "command": command,
            "cwd": cwd.as_posix(),
            "workspace_root": policy.default_root.as_posix(),
            "read_only": read_only,
            "status": "running",
            "running": True,
            "exit_code": None,
            "timed_out": False,
            "stdout": "",
            "stderr": "",
            "output": "",
            "session_id": session.session_id,
            "next_offset": 0,
        }

    def _env(self, policy: BashAccessPolicy) -> dict[str, str]:
        env = dict(os.environ)
        env.update(policy.env())
        return env


class BashListSessionsTool:
    def __init__(self, session_manager: BashSessionManager) -> None:
        self.session_manager = session_manager

    spec = ToolSpec(
        name="bash.list_sessions",
        package="bash",
        type="local_tool",
        description="List bash terminal sessions, optionally only active/running sessions.",
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_terminal_state"],
        input_schema={
            "type": "object",
            "properties": {"active_only": {"type": "boolean"}},
        },
        output_schema={"sessions": "array"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        _ = context
        sessions = [
            self.session_manager._session_status_payload(session)
            for session in self.session_manager.list(
                active_only=bool(invocation.input.get("active_only")),
                owner_run_id=_child_run_id(context),
            )
        ]
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"sessions": sessions},
        )


class BashReadSessionTool:
    def __init__(self, session_manager: BashSessionManager) -> None:
        self.session_manager = session_manager

    spec = ToolSpec(
        name="bash.read_session",
        package="bash",
        type="local_tool",
        description="Read buffered output and status from a background bash terminal session.",
        risk="low",
        requires_confirmation=False,
        read_only=True,
        side_effects=["read_terminal_output"],
        input_schema={
            "type": "object",
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "max_bytes": {"type": "integer", "minimum": 1},
            },
        },
        output_schema={
            "session_id": "string",
            "command": "string",
            "cwd": "string",
            "workspace_root": "string",
            "read_only": "boolean",
            "status": "string",
            "running": "boolean",
            "exit_code": ["integer", "null"],
            "offset": "integer",
            "next_offset": "integer",
            "output_start_offset": "integer",
            "output": "string",
            "truncated": "boolean",
            "reader_error": ["string", "null"],
        },
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        _ = context
        try:
            payload = self.session_manager.read(
                session_id=str(invocation.input.get("session_id") or ""),
                offset=int(invocation.input.get("offset") or 0),
                max_bytes=int(invocation.input.get("max_bytes") or DEFAULT_OUTPUT_BYTES),
                owner_run_id=_child_run_id(context),
            )
        except (KeyError, ValueError) as exc:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="failed",
                error=str(exc),
            )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output=payload,
        )


class BashWriteSessionTool:
    def __init__(self, session_manager: BashSessionManager) -> None:
        self.session_manager = session_manager

    spec = ToolSpec(
        name="bash.write_session",
        package="bash",
        type="local_tool",
        description="Write stdin text to a running background bash terminal session.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["write_terminal_input", "execute_local_process"],
        input_schema={
            "type": "object",
            "required": ["session_id", "text"],
            "properties": {
                "session_id": {"type": "string"},
                "text": {"type": "string"},
            },
        },
        output_schema={"session_id": "string", "bytes_written": "integer", "status": "string"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        _ = context
        session_id = str(invocation.input.get("session_id") or "")
        try:
            bytes_written = self.session_manager.write(
                session_id=session_id,
                text=str(invocation.input.get("text") or ""),
                owner_run_id=_child_run_id(context),
            )
        except (KeyError, OSError, RuntimeError) as exc:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="failed",
                error=str(exc),
            )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"session_id": session_id, "bytes_written": bytes_written, "status": "written"},
        )


class BashInterruptSessionTool:
    def __init__(self, session_manager: BashSessionManager) -> None:
        self.session_manager = session_manager

    spec = ToolSpec(
        name="bash.interrupt_session",
        package="bash",
        type="local_tool",
        description="Send Ctrl-C/SIGINT to a running background bash terminal session.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["signal_local_process"],
        input_schema={
            "type": "object",
            "required": ["session_id"],
            "properties": {"session_id": {"type": "string"}},
        },
        output_schema={"session_id": "string", "status": "string"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        _ = context
        session_id = str(invocation.input.get("session_id") or "")
        try:
            self.session_manager.interrupt(session_id=session_id, owner_run_id=_child_run_id(context))
        except (KeyError, OSError) as exc:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="failed",
                error=str(exc),
            )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"session_id": session_id, "status": "interrupted"},
        )


class BashTerminateSessionTool:
    def __init__(self, session_manager: BashSessionManager) -> None:
        self.session_manager = session_manager

    spec = ToolSpec(
        name="bash.terminate_session",
        package="bash",
        type="local_tool",
        description="Send SIGTERM to a running background bash terminal session.",
        risk="medium",
        requires_confirmation=False,
        read_only=False,
        side_effects=["signal_local_process"],
        input_schema={
            "type": "object",
            "required": ["session_id"],
            "properties": {"session_id": {"type": "string"}},
        },
        output_schema={"session_id": "string", "status": "string"},
    )

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        _ = context
        session_id = str(invocation.input.get("session_id") or "")
        try:
            self.session_manager.terminate(session_id=session_id, owner_run_id=_child_run_id(context))
        except (KeyError, OSError) as exc:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="failed",
                error=str(exc),
            )
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"session_id": session_id, "status": "terminated"},
        )


def is_read_only_command(command: str) -> bool:
    if not command.strip():
        return False
    if _has_unsafe_shell_syntax(command):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    if not tokens:
        return False
    if any(token in {"&", "tee"} or "(" in token or ")" in token for token in tokens):
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in SHELL_CONTROL_TOKENS:
            if not segments[-1]:
                return False
            segments.append([])
            continue
        segments[-1].append(token)
    return all(_segment_is_read_only(segment) for segment in segments if segment)


def _has_unsafe_shell_syntax(command: str) -> bool:
    # TODO: Replace this conservative classifier when read-only parallel execution has
    # a richer command analysis model. It intentionally over-reviews safe shell idioms
    # such as stderr/input redirection for now.
    return _has_dynamic_shell_expansion(command) or any(
        marker in command
        for marker in [
            "$(",
            "`",
            "<(",
            ">",
            "<",
            "\n",
            "\r",
        ]
    )


def _has_dynamic_shell_expansion(command: str) -> bool:
    """Do not classify arguments before shell expansion as proven safe.

    Quoted, server-injected workspace roots are path values, not options. Other
    variables and unquoted globs/braces can expand into hidden writing flags.
    Unknown syntax requires review; this is not a general shell sandbox.
    """
    root_var = re.compile(r"\$(?:\{(?:workspace_root|WORKSPACE_ROOT|LKA_WORKSPACE_ROOT)\}"
                          r"|(?:workspace_root|WORKSPACE_ROOT|LKA_WORKSPACE_ROOT)(?![\w]))")
    quote = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char == "\\":
            escaped = True
        elif char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == "$":
            if quote != '"' or root_var.match(command, index) is None:
                return True
        elif quote is None and char in "*?[]{}":
            return True
    return False


def _matches_long_option(arg: str, options: tuple[str, ...]) -> bool:
    name = arg.split("=", 1)[0]
    # GNU-style option abbreviations can carry the same effect as the full name.
    return name.startswith("--") and any(option.startswith(name) for option in options)


def _segment_is_read_only(segment: list[str]) -> bool:
    command = Path(segment[0]).name
    if segment[0] != command:
        # A workspace executable named "cat" is not the standard read utility.
        return False
    args = segment[1:]
    if command == "git":
        if not args or args[0] not in READ_ONLY_GIT_SUBCOMMANDS:
            return False
        if any(_matches_long_option(arg, ("--output", "--ext-diff", "--textconv")) for arg in args[1:]):
            return False
        if args[0] == "branch":
            listing = {"-a", "-r", "-v", "-vv", "-l", "--all", "--remotes", "--verbose",
                       "--list", "--show-current", "--no-color", "--color=never"}
            return all(arg in listing or ("--list" in args and not arg.startswith("-"))
                       for arg in args[1:])
        return True
    if command == "sed":
        # Programmable sed can execute commands or write without -i. Only a
        # numeric print selection is proven read-only; other programs get review.
        scripts = args[1:] if args and args[0] == "-n" else args
        return bool(scripts and re.fullmatch(r"(?:(?:\d+|\$)(?:,(?:\d+|\$))?)?p", scripts[0])
                    and all(not arg.startswith("-") for arg in scripts[1:]))
    if command == "find":
        return not any(arg in {"-delete", "-exec", "-execdir", "-ok", "-okdir",
                               "-fprint", "-fprint0", "-fprintf", "-fls"} for arg in args)
    if command == "rg":
        return not any(_matches_long_option(arg, ("--pre", "--pre-glob", "--hostname-bin")) for arg in args)
    if command == "sort":
        return not any(_matches_long_option(arg, ("--output", "--compress-program"))
                       or (arg.startswith("-") and not arg.startswith("--") and "o" in arg)
                       for arg in args)
    if command == "file":
        return not any(_matches_long_option(arg, ("--compile",))
                       or (arg.startswith("-") and not arg.startswith("--") and "C" in arg)
                       for arg in args)
    if command == "uniq":
        operands = []
        index = 0
        options = True
        while index < len(args):
            arg = args[index]
            if options and arg == "--":
                options = False
            elif options and arg in {"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"}:
                index += 1
                if index >= len(args) or not args[index].isdigit():
                    return False
            elif options and arg.startswith("-") and arg != "-":
                if not (arg in {"-c", "-d", "-u", "-i", "-z", "--count", "--repeated", "--unique",
                                "--ignore-case", "--zero-terminated"}
                        or re.fullmatch(r"-(?:f|s|w)\d+", arg)
                        or re.fullmatch(r"--(?:skip-fields|skip-chars|check-chars)=\d+", arg)):
                    return False
            else:
                operands.append(arg)
            index += 1
        # uniq's second positional operand is an output file, not another input.
        return len(operands) <= 1
    return command in READ_ONLY_COMMANDS


def _decode_limited(raw: bytes, max_bytes: int) -> str:
    if len(raw) <= max_bytes:
        return raw.decode("utf-8", errors="replace")
    suffix = f"\n...[truncated {len(raw) - max_bytes} bytes]"
    return raw[:max_bytes].decode("utf-8", errors="replace") + suffix


def _child_run_id(context: ToolContext) -> str | None:
    view = context.tool_view
    return view.child_run_id if view is not None else None
