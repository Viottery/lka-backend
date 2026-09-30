"""Focused tests for the managed Codex stdio subprocess transport."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.core.codex_transport import CodexSubprocessTransport


class _Reader:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = asyncio.Queue()
        for chunk in chunks or []:
            self.chunks.put_nowait(chunk)

    async def read(self, _size: int) -> bytes:
        return await self.chunks.get()

    async def readline(self) -> bytes:
        return await self.chunks.get()


class _Writer:
    def __init__(self) -> None:
        self.data: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _Process:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode = 0
        self.stdin = _Writer()
        self.stdout = _Reader([b'{"method":"turn/completed"}\n', b""])
        self.stderr = _Reader([b"diagnostic output longer than limit"])

    async def wait(self) -> int:
        return self.returncode or 0


def test_start_uses_explicit_executable_and_app_server_without_shell(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    process = _Process()

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setenv("BACKEND_SECRET", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    async def exercise() -> None:
        transport = await CodexSubprocessTransport.start(
            binary_path=sys.executable,
            cwd=tmp_path,
            stderr_limit_bytes=12,
        )
        assert await transport.read_line() == '{"method":"turn/completed"}\n'
        assert await transport.read_line() is None
        await asyncio.sleep(0)
        assert len(transport.stderr_tail.encode("utf-8")) <= 12
        await transport.write_line('{"method":"initialized","params":{}}')
        assert process.stdin.data == [b'{"method":"initialized","params":{}}\n']
        await transport.close()
        assert process.stdin.closed

    asyncio.run(exercise())
    assert captured["args"] == (str(Path(sys.executable).resolve()), "app-server")
    kwargs = captured["kwargs"]
    assert captured["args"][:2] == (str(Path(sys.executable).resolve()), "app-server")
    assert isinstance(kwargs, dict)
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE
    assert "shell" not in kwargs
    assert isinstance(kwargs["env"], dict)
    assert "BACKEND_SECRET" not in kwargs["env"]
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert "CODEX_HOME" not in kwargs["env"]


def test_configured_sqlite_home_is_private_and_passed_explicitly(tmp_path, monkeypatch) -> None:
    state_home = tmp_path / "codex-state"
    captured: dict[str, object] = {}
    process = _Process()

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    async def exercise() -> None:
        transport = await CodexSubprocessTransport.start(
            binary_path=sys.executable,
            cwd=tmp_path,
            codex_state_home=state_home,
        )
        await transport.close()

    asyncio.run(exercise())
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert captured["args"] == (
        str(Path(sys.executable).resolve()),
        "app-server",
        "-c",
        f'sqlite_home="{state_home.resolve()}"',
    )
    assert "CODEX_HOME" not in kwargs["env"]
    assert "OPENAI_API_KEY" not in kwargs["env"]
    if sys.platform != "win32":
        assert state_home.stat().st_mode & 0o777 == 0o700


def test_named_permission_profile_is_defined_with_safe_child_config_overrides(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    async def exercise() -> None:
        transport = await CodexSubprocessTransport.start(
            binary_path=sys.executable,
            cwd=tmp_path,
            permission_profile_id="lka_codex_abc123",
        )
        await transport.close()

    asyncio.run(exercise())
    arguments = captured["args"]
    assert isinstance(arguments, tuple)
    overrides = [arguments[index + 1] for index, value in enumerate(arguments) if value == "-c"]
    assert overrides == [
        'default_permissions="lka_codex_abc123"',
        'permissions.lka_codex_abc123.extends=":workspace"',
        (
            'permissions.lka_codex_abc123.filesystem={'
            '":root"="deny",":minimal"="read",":tmpdir"="deny",'
            '":slash_tmp"="deny",'
            f'{json.dumps(str(Path(sys.executable).resolve()))}="read"'
            '}'
        ),
    ]


def test_named_permission_profile_rejects_config_injection(tmp_path) -> None:
    async def exercise() -> None:
        with pytest.raises(ValueError, match="custom profile name"):
            await CodexSubprocessTransport.start(
                binary_path=sys.executable,
                cwd=tmp_path,
                permission_profile_id='safe".default_permissions',
            )

    asyncio.run(exercise())


def test_configured_codex_home_is_private_and_passed_without_changing_default(tmp_path, monkeypatch) -> None:
    codex_home = tmp_path / "codex-profile"
    captured: dict[str, object] = {}
    process = _Process()

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setenv("HOME", "/normal/user/home")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    async def exercise() -> None:
        transport = await CodexSubprocessTransport.start(
            binary_path=sys.executable,
            cwd=tmp_path,
            codex_home=codex_home,
        )
        await transport.close()

    asyncio.run(exercise())
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["env"]["CODEX_HOME"] == str(codex_home.resolve())
    assert kwargs["env"]["HOME"] == "/normal/user/home"
    assert "OPENAI_API_KEY" not in kwargs["env"]
    if sys.platform != "win32":
        assert codex_home.stat().st_mode & 0o777 == 0o700


def test_configured_codex_directories_reject_relative_and_non_private_paths(tmp_path) -> None:
    async def exercise() -> None:
        with pytest.raises(ValueError, match="absolute configured path"):
            await CodexSubprocessTransport.start(
                binary_path=sys.executable, cwd=tmp_path, codex_home="relative"
            )
        if sys.platform != "win32":
            broad = tmp_path / "broad"
            broad.mkdir(mode=0o755)
            with pytest.raises(ValueError, match="permissions must be private"):
                await CodexSubprocessTransport.start(
                    binary_path=sys.executable, cwd=tmp_path, codex_home=broad
                )

    asyncio.run(exercise())


def test_start_rejects_non_absolute_binary_and_invalid_workspace(tmp_path) -> None:
    async def exercise() -> None:
        with pytest.raises(ValueError, match="absolute configured path"):
            await CodexSubprocessTransport.start(binary_path="codex", cwd=tmp_path)
        with pytest.raises(ValueError, match="existing absolute directory"):
            await CodexSubprocessTransport.start(binary_path=sys.executable, cwd=tmp_path / "missing")

    asyncio.run(exercise())
