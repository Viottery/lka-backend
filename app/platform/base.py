"""Shared platform contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformInfo:
    """Runtime platform metadata used by platform-specific adapters."""

    name: str
    path_style: str
    default_shell: str
    case_sensitive_paths: bool
