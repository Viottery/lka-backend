"""Bounded node outbox delivery using the existing pinned HTTPS transport."""
from __future__ import annotations

import os
import threading

from app.integrations.cloud_sync import CloudReplicaSynchronizer, CloudSyncError


class SharedMemoryWriteSynchronizer:
    def __init__(self, config, outbox, *, transport=None):
        self.config, self.outbox = config, outbox
        # Reuse the existing TLS/proxy/redirect/size policy without another
        # lifecycle thread or a second storage copy.
        self._transport = transport or CloudReplicaSynchronizer._request.__get__(self)
        self._lock = threading.Lock()
        self._last_error = None

    def flush_once(self, *, node_id: str | None = None) -> dict:
        if not (self.config.enabled and self.config.role == "replica"
                and getattr(self.config, "memory_write_enabled", False)):
            return {"enabled": False, "delivered": 0}
        if node_id is not None and node_id != self.outbox.node_id:
            raise CloudSyncError("cloud_memory_node_mismatch")
        if not self._lock.acquire(blocking=False):
            raise CloudSyncError("cloud_memory_delivery_already_running")
        delivered = rejected = 0
        try:
            token = os.environ.get(self.config.token_env, "")
            if not token or "\r" in token or "\n" in token:
                raise CloudSyncError("cloud_node_token_missing")
            for payload in self.outbox.pending():
                try:
                    receipt = self._transport("POST", "/node/sync/memory-mutations", payload, token)
                except CloudSyncError as exc:
                    if str(exc) not in {"cloud_http_403", "cloud_http_409", "cloud_http_422"}:
                        raise
                    self.outbox.reject(payload, str(exc))
                    rejected += 1
                    continue
                self.outbox.acknowledge(payload, receipt)
                delivered += 1
            self._last_error = None
            return {"enabled": True, "delivered": delivered, "rejected": rejected,
                    "outbox": self.outbox.status()}
        except Exception as exc:
            self._last_error = str(exc) if isinstance(exc, CloudSyncError) else type(exc).__name__
            raise
        finally:
            self._lock.release()

    def status(self):
        return {"enabled": bool(getattr(self.config, "memory_write_enabled", False)),
                "last_error": self._last_error, "outbox": self.outbox.status()}
