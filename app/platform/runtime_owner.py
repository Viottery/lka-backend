"""Cross-process and same-process exclusive ownership for a runtime data directory."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Self

_registry_lock = threading.Lock()
_held_paths: set[str] = set()


class RuntimeOwner:
    def __init__(self, data_dir: str | os.PathLike[str]):
        self.data_dir = Path(data_dir).resolve()
        self._path = self.data_dir / ".lka-runtime-owner.lock"
        self._file = None
        self._key = os.path.normcase(str(self._path))

    def acquire(self) -> bool:
        with _registry_lock:
            if self._key in _held_paths:
                return False
            self.data_dir.mkdir(parents=True, exist_ok=True)
            handle = open(self._path, "a+b")  # noqa: SIM115 - held for the lock lifetime
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if handle.read(1) == b"":
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                handle.close()
                return False
            self._file = handle
            _held_paths.add(self._key)
            return True

    def release(self) -> None:
        with _registry_lock:
            if self._file is None:
                return
            handle, self._file = self._file, None
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                _held_paths.discard(self._key)

    def __enter__(self) -> Self:
        if not self.acquire():
            raise RuntimeError(f"runtime data directory already owned: {self.data_dir}")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
