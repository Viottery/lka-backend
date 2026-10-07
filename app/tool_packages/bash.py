"""Agent-visible bash command and terminal session tools."""

from __future__ import annotations

import atexit
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.tools import ToolContext, ToolInvocation, ToolPackageSpec, ToolResult, ToolSpec
from app.platform.commands import IS_WINDOWS, SHELL_NAME, run_sync, start_terminal
from app.platform.workspace_access import approved_path, require_child_path_scope

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
        f"Run native {SHELL_NAME} commands in configured workspace roots (bash.* API names remain compatible). Supports synchronous commands, "
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
        ("The actual shell is Windows PowerShell 5.1. Use PowerShell syntax (Get-Location, Get-ChildItem, Get-Content, Select-String); environment variables use $env:WORKSPACE_ROOT. Bash scripts and Unix command options are not supported." if IS_WINDOWS else "The actual shell is bash. Prefer read-only commands first, such as pwd, printenv, ls, find, rg, grep, cat, sed, head, tail, wc, git status, git diff, git log, and git show."),
        "Every invocation passes the configured safety review. The read-only whitelist classifies command risk; it does not bypass review.",
        "Read-only classification also checks arguments and shell expansion. Writing options, process hooks, arbitrary programs and executable paths require review; use literal quoted patterns and quoted workspace-root variables for inspection.",
        "Use mode=background for long-running or interactive commands, then poll with bash.read_session.",
        "Use bash.write_session to send stdin to a background terminal, bash.interrupt_session for Ctrl-C, and bash.terminate_session to stop it.",
        "The default cwd is the session workspace, otherwise the first configured workspace root. Relative cwd values resolve inside that root.",
        "Commands receive workspace_root, WORKSPACE_ROOT, LKA_WORKSPACE_ROOT, and LKA_WORKSPACE_ROOTS environment variables.",
        ("Use relative paths from cwd or $env:WORKSPACE_ROOT; do not invent placeholder paths." if IS_WINDOWS else "Use relative paths from cwd or the injected $workspace_root variable; do not invent placeholder paths."),
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

    def resolve_cwd(self, cwd_value: str | None, *, allow_outside: bool = False) -> Path:
        if cwd_value and cwd_value.strip():
            path = Path(self.expand_workspace_variables(cwd_value)).expanduser()
            if not path.is_absolute():
                path = self.default_root / path
            resolved = path.resolve(strict=False)
        else:
            resolved = self.default_root
        if not self._is_allowed(resolved) and not allow_outside:
            allowed = ", ".join(root.as_posix() for root in self.roots)
            raise PermissionError(f"cwd is outside allowed workspace roots: {allowed}")
        if not resolved.exists():
            raise FileNotFoundError(f"cwd does not exist: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"cwd is not a directory: {resolved}")
        return resolved

    def is_allowed(self, path: Path) -> bool:
        return self._is_allowed(path.resolve(strict=False))

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
        for name in ("workspace_root", "WORKSPACE_ROOT", "LKA_WORKSPACE_ROOT"):
            value = value.replace(f"$env:{name}", root).replace("${env:" + name + "}", root)
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
    process: Any
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
        atexit.register(self.close)

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
        process = start_terminal(command, cwd=cwd, env=env)
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
        return session.process.write(text.encode("utf-8"))

    def interrupt(self, *, session_id: str, owner_run_id: str | None = None) -> None:
        session = self.get(session_id, owner_run_id=owner_run_id)
        if session.process.poll() is None:
            session.process.interrupt()

    def terminate(self, *, session_id: str, owner_run_id: str | None = None) -> None:
        session = self.get(session_id, owner_run_id=owner_run_id)
        if session.process.poll() is None:
            session.process.terminate()

    def close(self) -> None:
        for session in self.list():
            session.process.terminate()
        atexit.unregister(self.close)

    def _reader_loop(self, session: BashSession) -> None:
        try:
            for chunk in session.process.chunks():
                with self._lock:
                    session.output.extend(chunk)
                    if len(session.output) > SESSION_BUFFER_BYTES:
                        overflow = len(session.output) - SESSION_BUFFER_BYTES
                        del session.output[:overflow]
                        session.output_start_offset += overflow
        except Exception as exc:  # pragma: no cover - defensive reader path.
            with self._lock:
                session.reader_error = str(exc)
        finally:
            session.process.close()

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
        unrestricted_execution=True,
        package="bash",
        type="local_tool",
        description=(
            f"Run a native {SHELL_NAME} command in sync or background mode. The command is classified "
            "per invocation as read_only only for whitelisted commands with safe arguments; "
            "otherwise it is non-read-only. Every invocation requires the configured safety review."
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

    def workspace_access_request(self, tool_input: dict[str, Any], context: ToolContext) -> list[str]:
        """Return an out-of-root cwd for review; command operands are not fenced."""
        try:
            policy = self.policy.for_session_workspace(context.workspace_root)
            cwd = policy.resolve_cwd(tool_input.get("cwd"), allow_outside=True)
            require_child_path_scope(cwd, context)
        except (OSError, ValueError, PermissionError):
            return []
        return [] if policy.is_allowed(cwd) else [str(cwd)]

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
            cwd = policy.resolve_cwd(invocation.input.get("cwd"), allow_outside=True)
            require_child_path_scope(cwd, context)
            if not policy.is_allowed(cwd):
                canonical = cwd.expanduser().resolve(strict=False)
                if canonical != cwd or not approved_path(canonical, context):
                    raise PermissionError("cwd is outside allowed workspace roots and was not approved for this invocation")
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
        stdout_raw, stderr_raw, exit_code, timed_out = run_sync(
            command, cwd=cwd, env=self._env(policy, read_only=read_only), timeout=timeout
        )
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
            env=self._env(policy, read_only=read_only),
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

    def _env(self, policy: BashAccessPolicy, *, read_only: bool = False) -> dict[str, str]:
        # Human control credentials must never reach an Agent-owned process.
        env = {key: value for key, value in os.environ.items() if key not in {
            "LKA_MESSAGES_CONTROL_TOKEN", "LKA_MESSAGES_API_TOKEN", "LKA_MESSAGES_IMPORT_TOKEN"
        }}
        if read_only:
            # Startup files, exported functions and rg --pre can run code even
            # when argv is read-only. Reviewed commands retain those semantics.
            env = {key: value for key, value in env.items()
                   if key not in {"RIPGREP_CONFIG_PATH", "BASH_ENV", "ENV"}
                   and not key.startswith("BASH_FUNC_")}
        env.update(policy.env())
        return env


class BashListSessionsTool:
    def __init__(self, session_manager: BashSessionManager) -> None:
        self.session_manager = session_manager

    spec = ToolSpec(
        name="bash.list_sessions",
        unrestricted_execution=True,
        package="bash",
        type="local_tool",
        description=f"List native {SHELL_NAME} terminal sessions, optionally only active/running sessions.",
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
        unrestricted_execution=True,
        package="bash",
        type="local_tool",
        description=f"Read buffered output and status from a background {SHELL_NAME} terminal session.",
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
        unrestricted_execution=True,
        package="bash",
        type="local_tool",
        description=f"Write stdin text to a running background {SHELL_NAME} terminal session.",
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
        unrestricted_execution=True,
        package="bash",
        type="local_tool",
        description=f"Send Ctrl-C to a running background {SHELL_NAME} terminal session (SIGINT on POSIX).",
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
        unrestricted_execution=True,
        package="bash",
        type="local_tool",
        description=f"Terminate a background {SHELL_NAME} process tree (Job termination on Windows, SIGTERM then SIGKILL on POSIX).",
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
    if IS_WINDOWS:
        return is_read_only_powershell_command(command)
    segments = _shell_command_segments(command)
    if not segments:
        return False
    for segment in segments:
        if _has_unsafe_shell_syntax(segment):
            return False
        try:
            # Comments/operators were identified while quotes were still present.
            # shlex's default comments also erase '#' inside ordinary shell words.
            tokens = shlex.split(segment, comments=False, posix=True)
        except ValueError:
            return False
        if not tokens or any(token in {"&", "tee"} or "(" in token or ")" in token for token in tokens):
            return False
        if not _segment_is_read_only(tokens):
            return False
    return True


def _shell_command_segments(command: str) -> list[str] | None:
    """Split only unquoted shell controls, retaining raw argv for safety checks.

    This is deliberately a conservative subset, not a shell parser/sandbox.
    In particular, '#' begins a comment only at the start of a shell word.
    """
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    in_word = False
    requires_command = False
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            current.append(char)
            if char == quote:
                quote = None
        elif char == "\\":
            if index + 1 == len(command) or command[index + 1] in "\n\r":
                return None
            current.extend((char, command[index + 1]))
            index += 1
            in_word = True
        elif quote == '"':
            current.append(char)
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
            current.append(char)
            in_word = True
        elif char == "#" and not in_word:
            # Leave the newline to be processed as a real command boundary.
            newline = command.find("\n", index)
            index = len(command) if newline < 0 else newline
            continue
        elif char in ";|&\n":
            operator = char
            if char in "|&" and command[index:index + 2] == char * 2:
                operator = char * 2
                index += 1
            if operator == "&":
                return None
            raw = "".join(current).strip(" \t")
            if raw:
                segments.append(raw)
                current = []
                requires_command = operator in {"|", "||", "&&"}
            elif char != "\n":
                return None
            in_word = False
        else:
            current.append(char)
            # Shell blanks are ASCII space/tab, not Unicode whitespace. Treating
            # NBSP as a blank could hide real argv behind a false '#' comment.
            in_word = char not in " \t"
        index += 1
    if quote is not None:
        return None
    raw = "".join(current).strip(" \t")
    if raw:
        segments.append(raw)
    elif requires_command:
        return None
    return segments


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


def is_read_only_powershell_command(command: str) -> bool:
    """A literal-only subset, deliberately rejecting PowerShell expression syntax.

    Do not reuse POSIX shlex: PowerShell interprets script blocks, expandable
    strings, escape characters, providers and invocation operators differently.
    Anything outside this subset must pass the normal safety review gate.
    """
    if not command.strip() or any(char in command for char in "`$(){}@&><;|\n\r\x00"):
        return False
    # Only ordinary literal arguments and non-interpolated quoted strings.
    token_pattern = r"(?:'[^']*'|\"[^\"]*\"|[^\s'\"]+)"
    tokens = re.findall(token_pattern, command)
    if " ".join(tokens).split() != command.split():
        return False
    if not tokens:
        return False
    return tokens[0].lower() in {
        "get-location", "pwd", "get-childitem", "ls", "dir", "gci",
        "get-content", "cat", "gc", "get-item", "gi", "test-path",
        "select-string", "get-process", "get-date",
    }
