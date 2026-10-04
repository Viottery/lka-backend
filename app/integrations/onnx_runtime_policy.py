"""Bounded CPU threads for local ONNX sessions, independent of model/domain."""

from __future__ import annotations

import os


def resolve_onnx_threads(threads: int | None = None) -> int:
    """None means AUTO (available CPUs, capped at four), not the library default.

    Explicit positive integers are expert overrides. Windows and environments
    without usable affinity fall back to cpu_count, then one CPU if unknown.
    """
    if threads is not None:
        if type(threads) is not int or threads < 1:
            raise ValueError("threads must be a positive integer or None (AUTO)")
        return threads
    available = 0
    affinity = getattr(os, "sched_getaffinity", None)
    if callable(affinity):
        try:
            available = len(affinity(0))
        except (OSError, NotImplementedError):
            pass
    if not available:
        available = os.cpu_count() or 1
    return max(1, min(4, available))
