"""Canonical, invocation-scoped workspace path approvals for local tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def approved_path(path: Path, context: Any) -> bool:
    """Return whether this exact canonical path was approved for this invocation."""

    canonical = str(path.expanduser().resolve(strict=False))
    return bool(getattr(context, "safety_review_approved", False)) and canonical in tuple(
        getattr(context, "approved_paths", ()) or ()
    )


def require_child_path_scope(path: Path, context: Any) -> None:
    """Recheck a child snapshot's immutable path ceiling."""

    view = getattr(context, "tool_view", None)
    if view is None or getattr(view, "child_run_id", None) is None:
        return
    if getattr(view, "full_workspace_authority", False):
        return
    allowed = tuple(Path(value).expanduser().resolve(strict=False) for value in view.allowed_paths)
    if not allowed or not any(is_path_within(path, root) for root in allowed):
        raise PermissionError("path is outside the child ContextSnapshot workspace scope")


def is_path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False
