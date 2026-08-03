"""Runtime platform detection."""

from __future__ import annotations

import platform as runtime_platform

from app.platform.base import PlatformInfo


def detect_platform(configured_platform: str = "auto") -> PlatformInfo:
    """Return normalized platform metadata for the current backend process."""

    requested = configured_platform.strip().lower()
    if requested == "auto":
        system_name = runtime_platform.system().lower()
    else:
        system_name = requested

    if system_name.startswith("win"):
        return PlatformInfo(
            name="windows",
            path_style="windows",
            default_shell="powershell",
            case_sensitive_paths=False,
        )
    if system_name == "darwin" or system_name == "macos":
        return PlatformInfo(
            name="macos",
            path_style="posix",
            default_shell="sh",
            case_sensitive_paths=False,
        )
    return PlatformInfo(
        name="linux",
        path_style="posix",
        default_shell="sh",
        case_sensitive_paths=True,
    )
