"""Async stdio processes with native process-tree ownership on Windows."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class _PipeReader:
    def __init__(self, chunks: Iterator[bytes], limit: int) -> None:
        self._chunks = chunks
        self._buffer = bytearray()
        self._limit = limit

    async def read(self, size: int) -> bytes:
        if not self._buffer:
            self._buffer.extend(await asyncio.to_thread(next, self._chunks, b""))
        output = bytes(self._buffer[:size])
        del self._buffer[:size]
        return output

    async def readline(self) -> bytes:
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                if end > self._limit:
                    raise ValueError("Subprocess output line exceeds the stream limit.")
                output = bytes(self._buffer[:end + 1])
                del self._buffer[:end + 1]
                return output
            if len(self._buffer) > self._limit:
                raise ValueError("Subprocess output line exceeds the stream limit.")
            chunk = await asyncio.to_thread(next, self._chunks, b"")
            if not chunk:
                output = bytes(self._buffer)
                self._buffer.clear()
                return output
            self._buffer.extend(chunk)


class _PipeWriter:
    def __init__(self, process: Any) -> None:
        self._process = process
        self._buffer = bytearray()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def write(self, data: bytes) -> None:
        if self._closed:
            raise BrokenPipeError("Subprocess stdin is closed.")
        self._buffer.extend(data)

    async def drain(self) -> None:
        data = bytes(self._buffer)
        self._buffer.clear()
        if data:
            await asyncio.to_thread(self._process.write, data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._buffer.clear()
        # An active WriteFile may hold the native write lock while the child
        # stops reading. Never acquire that lock on the event-loop thread.
        self._close_task = asyncio.create_task(asyncio.to_thread(self._process.close_stdin))

    async def wait_closed(self) -> None:
        if self._close_task is not None:
            # A caller's grace timeout must not cancel the pending close: Job
            # termination releases a blocked WriteFile, letting this finish.
            await asyncio.shield(self._close_task)


class WindowsAsyncProcess:
    """Small asyncio Process interface around a suspended-start Windows Job."""

    def __init__(self, process: Any, *, limit: int) -> None:
        self._process = process
        self.pid = process.pid
        self.stdin = _PipeWriter(process)
        self.stdout = _PipeReader(process.chunks(), limit)
        self.stderr = _PipeReader(process.stderr_chunks(), limit)

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    async def wait(self) -> int:
        return await asyncio.to_thread(self._process.wait)

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.terminate()

    def close(self) -> None:
        self._process.close()


async def start_stdio_process(
    *argv: str,
    cwd: str,
    env: dict[str, str],
    stdin: int = asyncio.subprocess.PIPE,
    stdout: int = asyncio.subprocess.PIPE,
    stderr: int = asyncio.subprocess.PIPE,
    limit: int = 1024 * 1024,
) -> asyncio.subprocess.Process | WindowsAsyncProcess:
    """Start an explicit executable without a shell, owning its descendants."""
    if os.name != "nt":
        return await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
            limit=limit, start_new_session=True,
        )
    if any(stream != asyncio.subprocess.PIPE for stream in (stdin, stdout, stderr)):
        raise ValueError("Managed stdio requires three pipe streams.")
    from app.platform.windows_process import WindowsProcess

    # Creation and Job assignment are atomic with respect to child execution:
    # WindowsProcess resumes only after containment has been established.
    process = WindowsProcess(list(argv), cwd=Path(cwd), env=env, terminal=False)
    return WindowsAsyncProcess(process, limit=limit)
