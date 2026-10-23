"""Bounded delivery of trusted-backend memory snapshots over pinned node TLS."""
from __future__ import annotations

import os
import threading

from app.integrations.cloud_sync import CloudReplicaSynchronizer, CloudSyncError


class MemoryReplicationSynchronizer:
    def __init__(self, config, journal, *, transport=None):
        self.config, self.journal = config, journal
        self._transport = transport or CloudReplicaSynchronizer._request.__get__(self)
        self._lock = threading.Lock()
        self._last_error = None

    def flush_once(self, *, node_id=None):
        if not (self.config.enabled and self.config.role == "replica"
                and getattr(self.config, "memory_aggregation_enabled", False)):
            return {"enabled": False, "delivered": 0}
        if node_id is not None and node_id != self.journal.node_id:
            raise CloudSyncError("cloud_memory_replication_node_mismatch")
        if not self._lock.acquire(blocking=False):
            raise CloudSyncError("cloud_memory_replication_already_running")
        delivered = rejected = 0
        try:
            token = os.environ.get(self.config.token_env, "")
            if not token or "\r" in token or "\n" in token:
                raise CloudSyncError("cloud_node_token_missing")
            collected = self.journal.collect()
            for payload in self.journal.pending():
                try:
                    receipt = self._transport("POST", "/node/sync/memory-records", payload, token)
                except CloudSyncError as exc:
                    if str(exc) not in {"cloud_http_403", "cloud_http_409", "cloud_http_422"}:
                        raise
                    self.journal.reject(payload, str(exc))
                    rejected += 1
                    continue
                self.journal.acknowledge(payload, receipt)
                delivered += 1
            self._last_error = None
            return {"enabled": True, "collected": collected, "delivered": delivered,
                    "rejected": rejected, "journal": self.journal.status()}
        except Exception as exc:
            self._last_error = str(exc) if isinstance(exc, CloudSyncError) else type(exc).__name__
            raise
        finally:
            self._lock.release()

    def status(self):
        return {"last_error": self._last_error, "journal": self.journal.status()}
