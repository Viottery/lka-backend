"""Create disposable, bounded working copies for an external Codex worker.

This module only stages changes. It never writes them back to the source
workspace. A caller must review the returned manifest and perform any later
apply operation through its own approval and conflict-checking workflow.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Literal

from app.platform.safe_files import is_link_or_reparse, open_file_no_follow, scandir_no_follow


class CodexWorkspaceError(RuntimeError):
    """Base error for workspaces that cannot be safely snapshotted or staged."""


class CodexWorkspaceUnsupportedError(CodexWorkspaceError):
    """Raised when a repository contains an unsupported or unsafe file layout."""


@dataclass(frozen=True, slots=True)
class CodexFileSnapshot:
    """Identity of one regular file copied into an isolated workspace."""

    sha256: str
    size_bytes: int
    mode: int
    workspace_mode: int


@dataclass(frozen=True, slots=True)
class CodexWorkspaceChange:
    """A staged file-level change; it is not applied to the source tree."""

    path: str
    status: Literal["added", "modified", "deleted"]
    original_sha256: str | None
    result_sha256: str | None
    original_size_bytes: int | None
    result_size_bytes: int | None
    original_mode: int | None
    result_mode: int | None
    diff: str | None
    diff_reason: str | None
    added_lines: int | None
    removed_lines: int | None
    source_conflict: bool


@dataclass(frozen=True, slots=True)
class CodexWorkspaceLease:
    """A lease over a copied workspace and its creation-time file manifest."""

    source_root: Path
    cwd: Path
    lease_root: Path
    snapshot: Mapping[str, CodexFileSnapshot]
    max_files: int
    max_total_bytes: int
    max_file_bytes: int

    def collect_changes(self) -> tuple[CodexWorkspaceChange, ...]:
        """Return a bounded manifest of worker changes, without applying them."""

        current = _scan_working_copy(
            self.cwd,
            max_files=self.max_files,
            max_total_bytes=self.max_total_bytes,
            max_file_bytes=self.max_file_bytes,
        )
        changes: list[CodexWorkspaceChange] = []
        all_paths = sorted(set(self.snapshot) | set(current))
        for relative_path in all_paths:
            before = self.snapshot.get(relative_path)
            after = current.get(relative_path)
            if before is None:
                status: Literal["added", "modified", "deleted"] = "added"
            elif after is None:
                status = "deleted"
            elif before.sha256 != after.sha256 or before.workspace_mode != after.mode:
                status = "modified"
            else:
                continue

            diff, diff_reason, added_lines, removed_lines, source_conflict = self._diff(
                relative_path, status, before, after
            )
            changes.append(
                CodexWorkspaceChange(
                    path=relative_path,
                    status=status,
                    original_sha256=before.sha256 if before else None,
                    result_sha256=after.sha256 if after else None,
                    original_size_bytes=before.size_bytes if before else None,
                    result_size_bytes=after.size_bytes if after else None,
                    original_mode=before.mode if before else None,
                    result_mode=after.mode if after else None,
                    diff=diff,
                    diff_reason=diff_reason,
                    added_lines=added_lines,
                    removed_lines=removed_lines,
                    source_conflict=source_conflict,
                )
            )
        return tuple(changes)

    def source_conflicts(
        self, changes: Iterable[CodexWorkspaceChange] | None = None
    ) -> tuple[str, ...]:
        """Recheck only changed paths before a separately approved apply step.

        The check catches concurrent edits, deletions, and newly occupied paths.
        It does not apply changes and does not make a later write atomic; callers
        must recheck immediately before writing and handle filesystem races.
        """

        if changes is None:
            changes = self.collect_changes()
        paths = sorted({change.path for change in changes})
        conflicts: list[str] = []
        for relative_path in paths:
            snapshot = self.snapshot.get(relative_path)
            source_path = _safe_join(self.source_root, relative_path)
            try:
                current = _read_file_identity(source_path)
            except FileNotFoundError:
                current = None
            except (OSError, CodexWorkspaceUnsupportedError):
                conflicts.append(relative_path)
                continue

            if snapshot is None:
                if current is not None:
                    conflicts.append(relative_path)
            elif (
                current is None
                or current.sha256 != snapshot.sha256
                or current.size_bytes != snapshot.size_bytes
                or current.mode != snapshot.mode
            ):
                conflicts.append(relative_path)
        return tuple(conflicts)

    def _diff(
        self,
        relative_path: str,
        status: Literal["added", "modified", "deleted"],
        before: CodexFileSnapshot | None,
        after: CodexFileSnapshot | None,
    ) -> tuple[str | None, str | None, int | None, int | None, bool]:
        source_path = _safe_join(self.source_root, relative_path)
        try:
            source_data = _read_regular_bytes(source_path, self.max_file_bytes)
        except FileNotFoundError:
            source_data = None
        except (OSError, CodexWorkspaceUnsupportedError):
            source_data = None

        if before is not None:
            if source_data is None:
                return None, "source_changed_since_snapshot", None, None, True
            source_identity = _identity_from_bytes(source_data, source_path)
            if (
                source_identity.sha256 != before.sha256
                or source_identity.size_bytes != before.size_bytes
                or source_identity.mode != before.mode
            ):
                return None, "source_changed_since_snapshot", None, None, True
        elif source_data is not None:
            return None, "source_path_was_created_concurrently", None, None, True

        result_path = _safe_join(self.cwd, relative_path)
        try:
            result_data = _read_regular_bytes(result_path, self.max_file_bytes)
        except FileNotFoundError:
            result_data = None
        except (OSError, CodexWorkspaceUnsupportedError):
            return None, "result_unavailable", None, None, False

        if after is not None:
            if result_data is None:
                return None, "result_changed_during_collection", None, None, False
            result_identity = _identity_from_bytes(result_data, result_path)
            if (
                result_identity.sha256 != after.sha256
                or result_identity.size_bytes != after.size_bytes
                or result_identity.mode != after.mode
            ):
                return None, "result_changed_during_collection", None, None, False

        if status == "deleted":
            result_data = None
        if source_data is not None and (b"\0" in source_data or len(source_data) > 1_000_000):
            return None, "binary_or_large_file", None, None, False
        if result_data is not None and (b"\0" in result_data or len(result_data) > 1_000_000):
            return None, "binary_or_large_file", None, None, False

        try:
            old_text = source_data.decode("utf-8") if source_data is not None else ""
            new_text = result_data.decode("utf-8") if result_data is not None else ""
        except UnicodeDecodeError:
            return None, "non_utf8_file", None, None, False

        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        patch_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{relative_path}" if source_data is not None else "/dev/null",
                tofile=f"b/{relative_path}" if result_data is not None else "/dev/null",
                n=3,
            )
        )
        added = sum(1 for line in patch_lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in patch_lines if line.startswith("-") and not line.startswith("---"))
        patch = "".join(patch_lines)
        if len(patch) > 32_000:
            patch = patch[:32_000] + "\n... diff preview truncated ...\n"
            diff_reason = "diff_preview_truncated"
        else:
            diff_reason = None
        return patch, diff_reason, added, removed, False


class CodexWorkspaceFactory:
    """Build disposable snapshots from a Git repository's current files."""

    DEFAULT_MAX_FILES = 20_000
    DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
    DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        base_dir: str | Path,
        *,
        max_files: int = DEFAULT_MAX_FILES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        if min(max_files, max_total_bytes, max_file_bytes) <= 0:
            raise ValueError("Codex workspace limits must be positive.")
        self.base_dir = Path(base_dir).expanduser().absolute()
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.max_file_bytes = max_file_bytes

    def create(self, source_root: str | Path) -> CodexWorkspaceLease:
        """Copy tracked and nonignored untracked regular files into a fresh lease."""

        source = Path(source_root).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise CodexWorkspaceUnsupportedError("The source workspace must be a directory.")
        git_root = _git_root(source)
        if git_root != source:
            raise CodexWorkspaceUnsupportedError(
                "The source workspace must be the Git worktree root; subdirectory snapshots are unsupported."
            )

        base_candidate = self.base_dir.resolve(strict=False)
        if _paths_overlap(source, base_candidate):
            raise CodexWorkspaceUnsupportedError(
                "The configured Codex workspace base must be outside and separate from the source workspace."
            )
        self.base_dir.mkdir(parents=True, exist_ok=True)
        base = self.base_dir.resolve(strict=True)
        if _paths_overlap(source, base):
            raise CodexWorkspaceUnsupportedError(
                "The configured Codex workspace base must be outside and separate from the source workspace."
            )

        paths = _git_file_paths(source, max_paths=self.max_files + 10_000)
        eligible: list[tuple[str, Path, os.stat_result]] = []
        total_bytes = 0
        for relative_path in paths:
            if _is_sensitive_path(relative_path):
                continue
            source_path = _safe_join(source, relative_path)
            try:
                info = source_path.lstat()
            except FileNotFoundError:
                # A tracked path deleted in the source is not part of its snapshot.
                continue
            if is_link_or_reparse(info):
                # Links are intentionally absent from both the copy and manifest.
                continue
            if not stat.S_ISREG(info.st_mode):
                raise CodexWorkspaceUnsupportedError(
                    f"Unsupported non-regular Git path: {relative_path}"
                )
            size = info.st_size
            if size > self.max_file_bytes:
                raise CodexWorkspaceUnsupportedError(
                    f"File exceeds the Codex snapshot size limit: {relative_path}"
                )
            total_bytes += size
            if total_bytes > self.max_total_bytes:
                raise CodexWorkspaceUnsupportedError("The source workspace exceeds the byte limit.")
            eligible.append((relative_path, source_path, info))
            if len(eligible) > self.max_files:
                raise CodexWorkspaceUnsupportedError("The source workspace exceeds the file limit.")

        lease_root = Path(tempfile.mkdtemp(prefix="codex-lease-", dir=base))
        cwd = lease_root / "workspace"
        cwd.mkdir()
        copied: dict[str, CodexFileSnapshot] = {}
        try:
            copied_bytes = 0
            for relative_path, source_path, initial_info in eligible:
                destination = _safe_join(cwd, relative_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                remaining_bytes = self.max_total_bytes - copied_bytes
                file_hash, size, source_mode, workspace_mode = _copy_regular_file(
                    source_path,
                    destination,
                    max_bytes=min(self.max_file_bytes, remaining_bytes),
                )
                post_copy = _read_file_identity(source_path)
                if (
                    post_copy.sha256 != file_hash
                    or post_copy.size_bytes != size
                    or post_copy.mode != (initial_info.st_mode & 0o777)
                ):
                    raise CodexWorkspaceUnsupportedError(
                        f"Source file changed while snapshotting: {relative_path}"
                    )
                copied[relative_path] = CodexFileSnapshot(
                    sha256=file_hash,
                    size_bytes=size,
                    mode=source_mode,
                    workspace_mode=workspace_mode,
                )
                copied_bytes += size
                if copied_bytes > self.max_total_bytes:
                    raise CodexWorkspaceUnsupportedError("The copied workspace exceeds the byte limit.")

            # Catch additions/deletions during enumeration. Per-file content is
            # separately verified after copying above.
            if _git_file_paths(source, max_paths=self.max_files + 10_000) != paths:
                raise CodexWorkspaceUnsupportedError("Git file list changed while snapshotting.")
        except Exception:
            shutil.rmtree(lease_root)
            raise

        return CodexWorkspaceLease(
            source_root=source,
            cwd=cwd,
            lease_root=lease_root,
            snapshot=MappingProxyType(copied),
            max_files=self.max_files,
            max_total_bytes=self.max_total_bytes,
            max_file_bytes=self.max_file_bytes,
        )


def _git_root(source: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexWorkspaceUnsupportedError("The source workspace must be a readable Git worktree.") from exc
    return Path(result.stdout.strip()).resolve(strict=True)


def _git_file_paths(source: Path, *, max_paths: int) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexWorkspaceUnsupportedError("Unable to enumerate Git workspace files.") from exc
    raw_paths = result.stdout.split(b"\0")
    if len(raw_paths) > max_paths + 1:
        raise CodexWorkspaceUnsupportedError("The Git workspace exceeds the path enumeration limit.")
    normalized: set[str] = set()
    for raw_path in raw_paths:
        if not raw_path:
            continue
        relative_path = os.fsdecode(raw_path)
        _validate_relative_path(relative_path)
        normalized.add(relative_path)
    if len(normalized) > max_paths:
        raise CodexWorkspaceUnsupportedError("The Git workspace exceeds the path enumeration limit.")
    return tuple(sorted(normalized))


def _validate_relative_path(relative_path: str) -> None:
    pure = PurePosixPath(relative_path)
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or "\0" in relative_path
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        or pure.is_absolute()
        or (os.name == "nt" and any(
            ":" in part or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()
            for part in relative_path.split("/")
        ))
    ):
        raise CodexWorkspaceUnsupportedError("Git returned an unsafe relative path.")


def _safe_join(root: Path, relative_path: str) -> Path:
    _validate_relative_path(relative_path)
    root_resolved = root.resolve(strict=True)
    candidate = root_resolved.joinpath(*relative_path.split("/"))
    try:
        candidate.parent.resolve(strict=False).relative_to(root_resolved)
    except ValueError as exc:
        raise CodexWorkspaceUnsupportedError("A workspace path escapes its root.") from exc
    return candidate


def _is_sensitive_path(relative_path: str) -> bool:
    parts = tuple(part.casefold() for part in relative_path.split("/"))
    name = parts[-1]
    if any(part == ".codex" for part in parts):
        return True
    if any(part in {".aws", ".ssh", ".gnupg", "secrets", "credentials"} for part in parts):
        return True
    if any(part.startswith(".env") for part in parts):
        return True
    if parts[:2] == ("data", "agent_logs"):
        return True
    if len(parts) == 2 and parts[0] == "config" and (
        name == "local.toml" or (name.startswith("local.") and name.endswith(".toml"))
    ):
        return True
    if name in {".netrc", ".git-credentials", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}:
        return True
    return name.endswith((".pem", ".p12", ".pfx", ".key", ".keystore"))


def _copy_regular_file(
    source: Path, destination: Path, *, max_bytes: int
) -> tuple[str, int, int, int]:
    fd = open_file_no_follow(source)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CodexWorkspaceUnsupportedError(f"Unsupported source file: {source.name}")
        source_mode = info.st_mode & 0o777
        workspace_mode = source_mode | stat.S_IWUSR
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb", closefd=False) as reader, destination.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise CodexWorkspaceUnsupportedError(
                        "A source file exceeds the configured snapshot byte limit."
                    )
                digest.update(chunk)
                writer.write(chunk)
        os.chmod(destination, workspace_mode)
        return digest.hexdigest(), size, source_mode, workspace_mode
    finally:
        os.close(fd)


def _read_file_identity(path: Path) -> CodexFileSnapshot:
    fd = open_file_no_follow(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CodexWorkspaceUnsupportedError(f"Unsupported non-regular file: {path.name}")
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb", closefd=False) as reader:
            while chunk := reader.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        return CodexFileSnapshot(
            sha256=digest.hexdigest(),
            size_bytes=size,
            mode=info.st_mode & 0o777,
            workspace_mode=info.st_mode & 0o777,
        )
    finally:
        os.close(fd)


def _read_regular_bytes(path: Path, max_bytes: int) -> bytes:
    identity = _read_file_identity(path)
    if identity.size_bytes > max_bytes:
        raise CodexWorkspaceUnsupportedError("A workspace file exceeds the configured size limit.")
    fd = open_file_no_follow(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise CodexWorkspaceUnsupportedError("A workspace file changed during diff collection.")
        with os.fdopen(fd, "rb", closefd=False) as reader:
            data = reader.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise CodexWorkspaceUnsupportedError("A workspace file exceeds the configured size limit.")
        return data
    finally:
        os.close(fd)


def _identity_from_bytes(data: bytes, path: Path) -> CodexFileSnapshot:
    info = path.lstat()
    if is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise CodexWorkspaceUnsupportedError("A source path changed to a non-regular file.")
    return CodexFileSnapshot(
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        mode=info.st_mode & 0o777,
        workspace_mode=info.st_mode & 0o777,
    )


def _scan_working_copy(
    root: Path,
    *,
    max_files: int,
    max_total_bytes: int,
    max_file_bytes: int,
) -> dict[str, CodexFileSnapshot]:
    # Keep the lease's pathname: resolve() could silently follow a replaced root.
    scan_root = root.absolute()
    output: dict[str, CodexFileSnapshot] = {}
    total_bytes = 0

    def visit(directory: Path, prefix: str) -> None:
        nonlocal total_bytes
        with scandir_no_follow(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
            for entry in ordered:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                _validate_relative_path(relative)
                path = directory / entry.name
                info = entry.stat(follow_symlinks=False)
                if is_link_or_reparse(info):
                    raise CodexWorkspaceUnsupportedError(f"Codex created a symlink or reparse point: {relative}")
                if _is_sensitive_path(relative):
                    # Current Codex versions create an empty top-level .codex
                    # directory when starting a thread. It is not an artifact;
                    # any content under it remains forbidden.
                    if relative == ".codex" and stat.S_ISDIR(info.st_mode):
                        with scandir_no_follow(path) as children:
                            if not any(children):
                                continue
                    raise CodexWorkspaceUnsupportedError(
                        f"Codex created a path excluded from workspace snapshots: {relative}"
                    )
                if stat.S_ISDIR(info.st_mode):
                    visit(path, relative)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise CodexWorkspaceUnsupportedError(f"Codex created a non-regular file: {relative}")
                if info.st_size > max_file_bytes:
                    raise CodexWorkspaceUnsupportedError(f"Codex output exceeds the file size limit: {relative}")
                if len(output) >= max_files:
                    raise CodexWorkspaceUnsupportedError("Codex output exceeds the file count limit.")
                snapshot = _read_file_identity(path)
                if snapshot.size_bytes > max_file_bytes:
                    raise CodexWorkspaceUnsupportedError(f"Codex output exceeds the file size limit: {relative}")
                output[relative] = snapshot
                total_bytes += snapshot.size_bytes
                if total_bytes > max_total_bytes:
                    raise CodexWorkspaceUnsupportedError("Codex output exceeds the total byte limit.")

    try:
        visit(scan_root, "")
    except OSError as exc:
        raise CodexWorkspaceUnsupportedError("Codex workspace cannot be scanned safely.") from exc
    return output


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
