"""Deterministic runtime context helpers for the Agent Harness."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def current_time_payload(*, timezone_name: str = "Asia/Shanghai") -> dict[str, str]:
    utc_now = datetime.now(UTC)
    try:
        local_zone = ZoneInfo(timezone_name)
    except Exception:
        local_zone = ZoneInfo("Asia/Shanghai")
        timezone_name = "Asia/Shanghai"
    local_now = utc_now.astimezone(local_zone)
    return {
        "utc": utc_now.isoformat(),
        "local": local_now.isoformat(),
        "timezone": timezone_name,
        "date": local_now.date().isoformat(),
    }
