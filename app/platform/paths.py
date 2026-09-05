"""Workspace path resolution for native platform execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from app.platform.base import PlatformInfo


@dataclass(frozen=True)
class ResolvedWorkspacePath:
    """Resolved representation of a frontend-provided workspace path."""

    original_path: str
    normalized_path: str
    resolved_path: Path
    platform: str
    source_frontend: str | None
    exists: bool


class PathResolver:
    """Resolve workspace paths for the backend's native platform."""

    def __init__(
        self,
        platform: PlatformInfo,
        workspace_roots: list[Path] | None = None,
        wsl_windows_mount_root: Path = Path("/mnt"),
    ) -> None:
        self.platform = platform
        self.workspace_roots = workspace_roots or []
        self.wsl_windows_mount_root = wsl_windows_mount_root

    def resolve_workspace(
        self,
        workspace: str,
        *,
        source_frontend: str | None = None,
    ) -> ResolvedWorkspacePath:
        path = Path(workspace).expanduser()
        resolved = path.resolve(strict=False)
        normalized = resolved.as_posix() if self.platform.path_style == "posix" else str(resolved)
        return ResolvedWorkspacePath(
            original_path=workspace,
            normalized_path=normalized,
            resolved_path=resolved,
            platform=self.platform.name,
            source_frontend=source_frontend,
            exists=resolved.exists(),
        )

    def resolve_windows_workspace_from_wsl(self, workspace: str) -> ResolvedWorkspacePath:
        """Resolve a Windows drive path through the standard WSL mount layout."""

        if self.platform.name != "linux":
            raise ValueError("Windows-to-WSL path mapping requires a Linux backend.")
        windows_path = PureWindowsPath(workspace)
        if not windows_path.is_absolute() or not windows_path.drive:
            raise ValueError("workspace path must be an absolute Windows drive path")
        if windows_path.drive.startswith("\\\\"):
            raise ValueError("Windows UNC paths are not supported by the default WSL mount mapping")
        drive = windows_path.drive.rstrip(":").lower()
        mapped = self.wsl_windows_mount_root / drive
        mapped = mapped.joinpath(*windows_path.parts[1:])
        return self.resolve_workspace(str(mapped), source_frontend="windows-via-wsl")

    def is_allowed_workspace(self, resolved: ResolvedWorkspacePath) -> bool:
        """Return whether the path is under configured roots.

        An empty root list means "no root restriction" for the current MVP.
        """

        if not self.workspace_roots:
            return True

        for root in self.workspace_roots:
            try:
                resolved.resolved_path.relative_to(root.resolve(strict=False))
            except ValueError:
                continue
            return True
        return False
