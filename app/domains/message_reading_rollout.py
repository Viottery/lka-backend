"""Deterministic new-family rollout, independent of model/source authority."""
from __future__ import annotations

import hashlib
from typing import Any


def choose_selector_algorithm(config: Any, family_id: str, checkpoint: dict | None = None) -> str:
    """Never change the selector of a frozen work family, including old records."""
    if checkpoint:
        selected = checkpoint.get("selector_algorithm", "current")
        if selected not in ("current", "multi_lane"):
            raise ValueError("unsupported_checkpoint_selector")
        return selected
    if getattr(config, "selector_algorithm", "current") != "multi_lane":
        return "current"
    percent = getattr(config, "selector_rollout_percent", 0)
    bucket = int(hashlib.sha256(f"message-selector-v1\0{family_id}".encode()).hexdigest()[:8], 16) % 100
    return "multi_lane" if bucket < percent else "current"
