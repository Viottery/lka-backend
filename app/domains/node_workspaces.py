"""Direct node workspace operations on explicit local mappings."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

from app.domains.workspace_mounts import WorkspaceMountGuard
from app.platform.safe_files import (
    is_link_or_reparse,
    locked_windows_directory,
    open_file_no_follow,
    open_posix_path_no_follow,
    scandir_no_follow,
)

MAX_TRANSFER_BYTES = 32 * 1024 * 1024


class NodeWorkspaceService:
    def __init__(self, mappings: dict[str, str], mounts=None):
        self.mappings = dict(mappings)
        self.mount_guard = WorkspaceMountGuard(self.mappings, mounts or {})

    def _mount_for_path(self, path):
        target = Path(os.path.abspath(Path(path).expanduser()))
        matches = [identity for identity in self.mount_guard.mounts
                   if target.is_relative_to(Path(self.mappings[identity]).expanduser())]
        return max(matches, key=lambda identity: len(self.mappings[identity])) if matches else None

    def mounted_workspace_for_path(self, path):
        return self._mount_for_path(path)

    def _validate_write_descriptor(self, path, descriptor):
        mount_id = self._mount_for_path(path)
        if mount_id is not None:
            expected = self.mount_guard.identity(mount_id, write=True)
            self.mount_guard.validate_descriptor(mount_id, descriptor, expected, write=True)

    def check_path(self, path, *, write=False):
        identity = self._mount_for_path(path)
        if identity is not None:
            self.mount_guard.require_available(identity, write=write)

    def status(self, workspace_id):
        if workspace_id not in self.mappings:
            raise KeyError("workspace not registered")
        mount_id = self._mount_for_path(self.mappings[workspace_id])
        return self.mount_guard.status(mount_id or workspace_id)

    def is_mounted(self, workspace_id: str) -> bool:
        """Conservatively identify mounted roots before native host execution.

        An unrestricted process cannot honor a mount's disconnect/write fence.
        Mounted workspaces use the scoped filesystem tools instead.
        """
        if workspace_id not in self.mappings:
            return False
        path = Path(self.mappings[workspace_id]).expanduser()
        if self._mount_for_path(path) is not None:
            return True
        return any(os.path.ismount(parent) for parent in (path, *path.parents)
                   if parent != Path(parent.anchor))

    def root(self, workspace_id: str) -> Path:
        if workspace_id not in self.mappings:
            raise KeyError("workspace not registered")
        path = Path(self.mappings[workspace_id]).expanduser()
        # Check native mount identity before touching the mountpoint. Directory
        # existence alone would silently fall back to the underlying local disk.
        self.check_path(path)
        if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
            raise PermissionError("workspace mapping unavailable or changed")
        return path

    def path(self, workspace_id: str, relative: str = "") -> Path:
        root = self.root(workspace_id)
        if (not isinstance(relative, str) or len(relative) > 4096 or "\\" in relative
                or ":" in relative or "\x00" in relative or relative.startswith("/")):
            raise ValueError("invalid relative workspace path")
        parts = relative.split("/") if relative else []
        for part in parts:
            if (not part or part in {".", ".."} or part.endswith((".", " "))
                    or PureWindowsPath(part).is_reserved()
                    or any(ord(c) < 32 or c in '*?<>|"' for c in part)):
                raise ValueError("invalid workspace path component")
        path = root.joinpath(*parts)
        if not path.resolve(strict=False).is_relative_to(root):
            raise PermissionError("workspace path escapes mapped root")
        return path

    @contextmanager
    def parent(self, path: Path):
        mount_id = self._mount_for_path(path)
        if mount_id is not None:
            with self._mounted_directory(mount_id, path.parent, write=True) as (descriptor, _):
                yield descriptor
        elif os.name == "nt":
            with locked_windows_directory(path.parent):
                yield None
        else:
            descriptor = open_posix_path_no_follow(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                yield descriptor
            finally:
                os.close(descriptor)

    @contextmanager
    def _mounted_directory(self, mount_id, path, *, write=False):
        """Walk relative to a pinned mount descriptor, never its empty backing directory."""
        root = Path(self.mappings[mount_id]).expanduser()
        with self.mount_guard.open_root(mount_id, write=write) as (root_fd, expected):
            descriptor = os.dup(root_fd)
            try:
                for part in path.relative_to(root).parts:
                    if part in {".", ".."}:
                        raise PermissionError("invalid mounted workspace path")
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                    self.mount_guard.validate_descriptor(mount_id, descriptor, expected, write=write)
                self.mount_guard.validate_descriptor(mount_id, descriptor, expected, write=write)
                yield descriptor, expected
            finally:
                os.close(descriptor)

    def entries(self, workspace_id: str, relative: str = "") -> dict:
        path = self.path(workspace_id, relative)
        result = []
        mount_id = self._mount_for_path(path)
        if mount_id is not None:
            with self._mounted_directory(mount_id, path) as (descriptor, expected), os.scandir(descriptor) as entries:
                for entry in entries:
                    if len(result) >= 200:
                        break
                    metadata = entry.stat(follow_symlinks=False)
                    if not is_link_or_reparse(metadata):
                        result.append({"name": entry.name, "is_directory": stat.S_ISDIR(metadata.st_mode),
                                       "size": metadata.st_size})
                self.mount_guard.validate_descriptor(mount_id, descriptor, expected)
            return {"workspace_id": workspace_id, "path": relative,
                    "items": sorted(result, key=lambda item: (not item["is_directory"], item["name"]))}
        with scandir_no_follow(path) as entries:
            for entry in entries:
                if len(result) >= 200:
                    break
                metadata = entry.stat(follow_symlinks=False)
                if is_link_or_reparse(metadata):
                    continue
                result.append({"name": entry.name, "is_directory": stat.S_ISDIR(metadata.st_mode),
                               "size": metadata.st_size})
        return {"workspace_id": workspace_id, "path": relative,
                "items": sorted(result, key=lambda item: (not item["is_directory"], item["name"]))}

    def read(self, workspace_id: str, relative: str) -> bytes:
        path = self.path(workspace_id, relative)
        mount_id = self._mount_for_path(path)
        if mount_id is not None:
            with self._mounted_directory(mount_id, path.parent) as (directory, expected):
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    self.mount_guard.validate_descriptor(mount_id, descriptor, expected)
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise PermissionError("mounted workspace target must be a regular file")
                except BaseException:
                    os.close(descriptor)
                    raise
        else:
            descriptor = open_file_no_follow(path)
        with os.fdopen(descriptor, "rb") as source:
            if os.fstat(source.fileno()).st_size > MAX_TRANSFER_BYTES:
                raise ValueError("file exceeds direct transfer limit")
            raw = source.read(MAX_TRANSFER_BYTES + 1)
            if len(raw) > MAX_TRANSFER_BYTES:
                raise ValueError("file exceeds direct transfer limit")
            if mount_id is not None:
                self.mount_guard.validate_descriptor(mount_id, source.fileno(), expected)
            return raw

    def create_file(self, workspace_id: str, relative: str, raw: bytes) -> dict:
        if len(raw) > MAX_TRANSFER_BYTES:
            raise ValueError("file exceeds direct transfer limit")
        path = self.path(workspace_id, relative)
        if path == self.root(workspace_id):
            raise ValueError("file name required")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        with self.parent(path) as directory:
            descriptor = os.open(path.name if directory is not None else path, flags, 0o600,
                                 **({"dir_fd": directory} if directory is not None else {}))
            try:
                with os.fdopen(descriptor, "wb") as target:
                    self._validate_write_descriptor(path, target.fileno())
                    target.write(raw)
                    target.flush()
                    os.fsync(target.fileno())
                    self._validate_write_descriptor(path, target.fileno())
            except BaseException:
                os.unlink(path.name if directory is not None else path,
                          **({"dir_fd": directory} if directory is not None else {}))
                raise
        return {"workspace_id": workspace_id, "path": relative, "size": len(raw)}

    def create_directory(self, workspace_id: str, relative: str) -> dict:
        path = self.path(workspace_id, relative)
        if path == self.root(workspace_id):
            raise ValueError("directory name required")
        with self.parent(path) as directory:
            os.mkdir(path.name if directory is not None else path, mode=0o700,
                     **({"dir_fd": directory} if directory is not None else {}))
            try:
                if directory is not None:
                    self._validate_write_descriptor(path.parent, directory)
            except BaseException:
                os.rmdir(path.name, dir_fd=directory)
                raise
        return {"workspace_id": workspace_id, "path": relative}
