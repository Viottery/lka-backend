"""Fail-closed access to operator-provisioned native Linux workspace mounts.

No provider is installed or mounted here. Mount-table and descriptor identities
prevent writes to an empty local mountpoint after unmount. A bounded probe can
detect failed I/O; provider caches and already-running I/O are not a distributed
lease or a promise of immediate disconnect fencing.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.platform.safe_files import open_posix_path_no_follow


class WorkspaceMountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_filesystem: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    expected_source: str = Field(min_length=1, max_length=1024, pattern=r"^[^\x00\r\n]+$")
    read_only: bool = False
    health_probe_timeout_seconds: float = Field(default=2, ge=0.1, le=10)

    @model_validator(mode="after")
    def no_writeback_mount(self):
        if self.expected_filesystem == "fuse.rclone" and not self.read_only:
            raise ValueError("rclone workspace mounts require read_only; writeback caching is not fenced")
        return self


@dataclass(frozen=True)
class MountIdentity:
    mount_id: int
    device: int
    filesystem: str
    source: str
    read_only: bool


def _unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _mountinfo():
    with Path("/proc/self/mountinfo").open(encoding="utf-8") as source:
        value = source.read(4 * 1024 * 1024 + 1)
    if len(value) > 4 * 1024 * 1024:
        raise PermissionError("mount table exceeds inspection limit")
    return value


def _probe(path, timeout):
    # A separate process bounds unavailable native filesystem calls. No shell,
    # profile, provider credential or user-controlled program is executed.
    script = ("import os,sys; p=sys.argv[1]; os.stat(p); "
              "it=os.scandir(p); next(it,None); it.close()")
    try:
        process = subprocess.Popen([sys.executable, "-I", "-c", script, str(path)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    try:
        return process.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        process.kill()
        # A native filesystem syscall can be uninterruptible. Never block the
        # caller indefinitely waiting for it; Popen retains eventual reaping.
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
        return False


class WorkspaceMountGuard:
    def __init__(self, mappings: dict[str, str], mounts: dict[str, WorkspaceMountConfig | dict], *,
                 mountinfo_reader: Callable[[], str] | None = None,
                 probe: Callable[[Path, float], bool] | None = None, platform: str | None = None):
        self.mappings = dict(mappings)
        self.mounts = {key: WorkspaceMountConfig.model_validate(value) for key, value in mounts.items()}
        if self.mounts.keys() - self.mappings.keys():
            raise ValueError("mount requires an explicit workspace mapping")
        self._table = mountinfo_reader or _mountinfo
        self._probe = probe or _probe
        self.platform = sys.platform if platform is None else platform

    def identity(self, workspace_id: str, *, write=False) -> MountIdentity:
        config = self.mounts[workspace_id]
        if self.platform != "linux":
            raise PermissionError("native workspace mounts require the Linux mount identity provider")
        path = Path(self.mappings[workspace_id]).expanduser()
        if not path.is_absolute():
            raise PermissionError("mounted workspace requires an absolute mapping")
        matches = []
        try:
            table = self._table()
            if len(table) > 4 * 1024 * 1024:
                raise ValueError("table too large")
            for line in table.splitlines():
                before, after = line.split(" - ", 1)
                fields, provider = before.split(), after.split()
                if _unescape(fields[4]) != str(path):
                    continue
                major, minor = fields[2].split(":", 1)
                matches.append(MountIdentity(int(fields[0]), os.makedev(int(major), int(minor)),
                                             provider[0], _unescape(provider[1]),
                                             "ro" in fields[5].split(",") or "ro" in provider[2].split(",")))
        except (OSError, ValueError, IndexError) as exc:
            raise PermissionError("workspace mount identity unavailable") from exc
        # Reject stacked/ambiguous mountpoints instead of trusting the wrong layer.
        if (len(matches) != 1 or matches[0].filesystem != config.expected_filesystem
                or matches[0].source != config.expected_source):
            raise PermissionError("workspace mount absent or identity changed")
        if write and (config.read_only or matches[0].read_only):
            raise PermissionError("mounted workspace is read-only")
        return matches[0]

    def require_available(self, workspace_id: str, *, write=False) -> MountIdentity:
        identity = self.identity(workspace_id, write=write)
        config = self.mounts[workspace_id]
        if not self._probe(Path(self.mappings[workspace_id]), config.health_probe_timeout_seconds):
            raise PermissionError("workspace mount health probe failed")
        if self.identity(workspace_id, write=write) != identity:
            raise PermissionError("workspace mount changed during health probe")
        return identity

    def validate_descriptor(self, workspace_id: str, descriptor: int, expected: MountIdentity, *, write=False):
        if self.identity(workspace_id, write=write) != expected:
            raise PermissionError("workspace mount changed during access")
        try:
            text = Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="utf-8")
            mount_id = next(int(line.split(":", 1)[1]) for line in text.splitlines() if line.startswith("mnt_id:"))
            if mount_id != expected.mount_id or os.fstat(descriptor).st_dev != expected.device:
                raise PermissionError("workspace descriptor is outside the approved mount")
        except (OSError, ValueError, StopIteration) as exc:
            raise PermissionError("workspace descriptor mount identity unavailable") from exc

    @contextmanager
    def open_root(self, workspace_id: str, *, write=False):
        expected = self.require_available(workspace_id, write=write)
        descriptor = open_posix_path_no_follow(Path(self.mappings[workspace_id]),
                                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.validate_descriptor(workspace_id, descriptor, expected, write=write)
            yield descriptor, expected
        finally:
            os.close(descriptor)

    def status(self, workspace_id: str):
        if workspace_id not in self.mappings:
            raise KeyError("workspace not registered")
        if workspace_id not in self.mounts:
            path = Path(self.mappings[workspace_id]).expanduser()
            try:
                available = path.is_absolute() and path.resolve(strict=True) == path and path.is_dir()
            except OSError:
                available = False
            return {"kind": "local", "available": available}
        try:
            identity = self.require_available(workspace_id)
            return {"kind": "mounted", "available": True,
                    "read_only": self.mounts[workspace_id].read_only or identity.read_only}
        except (PermissionError, OSError):
            return {"kind": "mounted", "available": False,
                    "read_only": self.mounts[workspace_id].read_only}
