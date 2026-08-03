"""Cross-platform filesystem scanning helpers."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from app.platform.base import PlatformInfo
from app.platform.paths import ResolvedWorkspacePath


@dataclass(frozen=True)
class ScanOptions:
    recursive: bool = True
    skip_hidden: bool = True
    allow_symlinks: bool = False
    max_files: int = 50_000
    sample_limit: int = 10


@dataclass(frozen=True)
class ScannedFile:
    relative_path: str
    name: str
    role: str


@dataclass(frozen=True)
class WorkspaceScanResult:
    workspace: ResolvedWorkspacePath
    indexed_files: int = 0
    indexed_chunks: int = 0
    sampled_files: list[ScannedFile] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    status: str = "completed"


class FilesystemScanner:
    """Scan workspace file metadata without reading file contents."""

    def __init__(self, platform: PlatformInfo) -> None:
        self.platform = platform

    def scan_workspace(
        self,
        workspace: ResolvedWorkspacePath,
        *,
        options: ScanOptions | None = None,
    ) -> WorkspaceScanResult:
        scan_options = options or ScanOptions()
        root = workspace.resolved_path
        if not workspace.exists or not root.is_dir():
            return WorkspaceScanResult(workspace=workspace)

        indexed_files = 0
        sampled_files: list[ScannedFile] = []
        skipped_paths: list[str] = []

        def onerror(error: OSError) -> None:
            skipped_paths.append(getattr(error, "filename", str(error)))

        for current_root, dirs, files in os.walk(
            root,
            topdown=True,
            onerror=onerror,
            followlinks=scan_options.allow_symlinks,
        ):
            current_path = Path(current_root)
            if scan_options.skip_hidden:
                dirs[:] = [name for name in sorted(dirs) if not self._is_hidden(current_path / name)]
                files = [name for name in sorted(files) if not self._is_hidden(current_path / name)]
            else:
                dirs[:] = sorted(dirs)
                files = sorted(files)

            for filename in files:
                if indexed_files >= scan_options.max_files:
                    return WorkspaceScanResult(
                        workspace=workspace,
                        indexed_files=indexed_files,
                        indexed_chunks=indexed_files * 4,
                        sampled_files=sampled_files,
                        skipped_paths=skipped_paths,
                    )

                indexed_files += 1
                if len(sampled_files) < scan_options.sample_limit:
                    full_path = current_path / filename
                    sampled_files.append(
                        ScannedFile(
                            relative_path=self._relative_path(full_path, root),
                            name=filename,
                            role=self._role_for(filename),
                        )
                    )

            if not scan_options.recursive:
                dirs[:] = []

        return WorkspaceScanResult(
            workspace=workspace,
            indexed_files=indexed_files,
            indexed_chunks=indexed_files * 4,
            sampled_files=sampled_files,
            skipped_paths=skipped_paths,
        )

    def _relative_path(self, path: Path, root: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()

    def _role_for(self, filename: str) -> str:
        lowered = filename.lower()
        if lowered.startswith("readme"):
            return "readme"
        if lowered in {"pyproject.toml", "package.json", "cargo.toml", "docker-compose.yml"}:
            return "config"
        return "workspace_file"

    def _is_hidden(self, path: Path) -> bool:
        if path.name.startswith("."):
            return True
        if self.platform.name != "windows":
            return False

        try:
            attrs = path.stat().st_file_attributes
        except (AttributeError, OSError):
            return False
        return bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN)
