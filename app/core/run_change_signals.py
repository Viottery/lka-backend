"""Bounded, process-local hints for run readers; persistence remains authoritative."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

_logger = logging.getLogger(__name__)


class RunChangeSignals:
    def __init__(self, limit: int = 512) -> None:
        self._lock = threading.Lock()
        self._listeners: dict[str, dict[object, Callable[[], None]]] = {}
        self._count = 0
        self._limit = limit
        self.closed = False

    def subscribe(self, run_id: str, wake: Callable[[], None]) -> Callable[[], None] | None:
        token = object()
        with self._lock:
            if self.closed or self._count >= self._limit:
                return None
            self._listeners.setdefault(run_id, {})[token] = wake
            self._count += 1

        def unsubscribe() -> None:
            with self._lock:
                listeners = self._listeners.get(run_id)
                if listeners is not None and token in listeners:
                    del listeners[token]
                    self._count -= 1
                    if not listeners:
                        del self._listeners[run_id]

        return unsubscribe

    def notify(self, run_id: str) -> None:
        with self._lock:
            listeners = tuple(self._listeners.get(run_id, {}).values())
        self._wake(listeners)

    def close(self) -> None:
        with self._lock:
            self.closed = True
            listeners = tuple(wake for group in self._listeners.values() for wake in group.values())
            self._listeners.clear()
            self._count = 0
        self._wake(listeners)

    @staticmethod
    def _wake(listeners) -> None:
        for wake in listeners:
            try:
                wake()
            except Exception as exc:  # noqa: BLE001 - hints cannot invalidate committed writes.
                _logger.warning("Run change hint failed type=%s", type(exc).__name__)
