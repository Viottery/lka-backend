"""Read-only, offline memory view over local memory and an explicit hub replica.

Cloud identities are namespaced; records are never imported into the local
memory tables. Local writes and learning continue to use MemoryService. This
view cannot publish, approve, correct or retract a cloud record.
"""

from __future__ import annotations

import base64
import hashlib
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.domains.memory import MemoryRecord, MemoryService
from app.storage.shared_replica import SharedReplicaStore


class SharedMemoryCache:
    """Bounded recall without network I/O or an implicit project identity merge.

    Project bindings explicitly map local memory project IDs to hub project IDs.
    Equal IDs and equal directory names alone do not establish such a binding.
    The unbound default exposes only global cloud memory. The local and cloud
    records coexist even when their original IDs or content collide.
    """

    def __init__(self, local: MemoryService, replica: SharedReplicaStore, *,
                 project_bindings: dict[str, str] | None = None,
                 stale_after_seconds: float = 60):
        bindings = dict(project_bindings or {})
        if (len(bindings) > 100 or any(
            not isinstance(key, str) or not key or len(key) > 1024
            or not isinstance(value, str) or not value or len(value) > 1024
            for key, value in bindings.items()
        ) or len(set(bindings.values())) != len(bindings)):
            raise ValueError("memory project bindings must be bounded and one-to-one")
        if not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0:
            raise ValueError("invalid cache staleness interval")
        self.local, self.replica = local, replica
        self.replication_filter = None
        self.project_bindings = bindings
        self.stale_after_seconds = stale_after_seconds
        hub_digest = hashlib.sha256(replica.hub_id.encode()).hexdigest()[:24]
        # MemoryService allocates mem_<hex>; this disjoint namespace is never
        # resolved against local memory, even if a local record is inactive.
        self._prefix = f"cloud_{hub_digest}_"

    def cloud_id(self, memory_id: str) -> str:
        encoded = base64.urlsafe_b64encode(memory_id.encode()).decode().rstrip("=")
        return self._prefix + encoded

    def _original_id(self, memory_id: str) -> str | None:
        if not memory_id.startswith(self._prefix):
            return None
        try:
            encoded = memory_id[len(self._prefix):]
            original = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True,
            ).decode()
            return original if original and self.cloud_id(original) == memory_id else None
        except (ValueError, UnicodeError):
            return None

    def resolve_project(self, workspace_path, *, create=True):
        return self.local.resolve_project(workspace_path, create=create)

    def cache_status(self) -> dict[str, Any]:
        """Informational sync observations, never authority or a freshness guarantee."""
        try:
            state = self.replica.memory_cache_status()
            pulled = state["last_pull_at"]
            age = None
            if pulled:
                instant = datetime.fromisoformat(pulled)
                if instant.tzinfo is not None:
                    age = max(0, (datetime.now(UTC) - instant).total_seconds())
            high = state["high_watermark"]
            return {
                **state, "available": True, "age_seconds": age,
                "stale": age is None or age > self.stale_after_seconds,
                "observed_lag": max(0, high - state["cursor"]) if high is not None else None,
                "reads_allow_stale": True,
                "policy": "Local cached observation; does not grant authority or prove current cloud state.",
            }
        except (sqlite3.Error, OSError, ValueError):
            return {"hub_id": self.replica.hub_id, "available": False, "stale": True,
                    "cursor": None, "reads_allow_stale": True}

    def _records(self, *, scope, project_id=None, query=None, memory_id=None,
                 limit=100, offset=0) -> list[MemoryRecord]:
        cloud_project = self.project_bindings.get(project_id) if scope == "project" else None
        if scope == "project" and cloud_project is None:
            return []
        try:
            records = self.replica.read_memories(
                scope=scope, project_id=cloud_project, query=query,
                memory_id=memory_id, limit=limit, offset=offset,
            )
        except (sqlite3.Error, OSError, ValueError):
            # Losing the cache must not make ordinary local work depend on a hub.
            return []
        visible = []
        for record in records:
            try:
                shared_id = record["memory_id"]
                sources = record["source_ids"]
                if not record["content"] or record["version"] < 1:
                    continue
                item = MemoryRecord.model_validate({
                    **record,
                    "memory_id": self.cloud_id(shared_id),
                    "project_id": project_id if scope == "project" else None,
                    "source_ids": [self.cloud_id(source) for source in sources],
                    "supersedes_id": self.cloud_id(record["supersedes_id"])
                    if record["supersedes_id"] else None,
                    "metadata": {
                        "origin": "cloud_replica", "hub_id": self.replica.hub_id,
                        "shared_memory_id": shared_id,
                        "shared_project_id": cloud_project,
                        "sources_truncated": record["sources_truncated"],
                        "read_only": True,
                    },
                })
                visible.append(item)
            except (ValidationError, TypeError, ValueError):
                # A structurally synchronized projection is not necessarily a
                # semantically valid memory; never weaken the local contract.
                continue
        return visible

    @staticmethod
    def _merge(local, cloud, limit):
        # Keep both identities. Existing local records win ties; neither store
        # is changed or promoted by recall.
        return sorted([*local, *cloud], key=lambda item: item.updated_at, reverse=True)[:limit]

    def _local_visible(self, record):
        if self.replication_filter is None:
            return True
        try:
            return self.replication_filter(record)
        except (sqlite3.Error, OSError):
            # A lost ownership journal cannot revive a centrally withdrawn copy.
            return False

    def list(self, *, scope=None, project_id=None, statuses=("active",),
             include_expired=False, limit=100, offset=0):
        """Page within a merged recall window of at most 100 candidates."""
        bounded = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        if offset >= 100:
            return []
        pool_size = min(100, offset + bounded)
        local = self.local.list(
            scope=scope if scope is not None or project_id else "global",
            project_id=project_id, statuses=statuses,
            include_expired=include_expired, limit=pool_size,
        )
        local = [record for record in local if self._local_visible(record)]
        # This is a recall view, not an administrative/history API. Scope=None
        # never widens cloud access to other projects.
        cloud = []
        if "active" in statuses:
            cloud.extend(self._records(scope=scope or "global", project_id=project_id,
                                       limit=pool_size))
            if scope is None and project_id:
                cloud.extend(self._records(scope="project", project_id=project_id,
                                           limit=pool_size))
        return self._merge(local, cloud, pool_size)[offset:offset + bounded]

    def search(self, query, *, scope=None, project_id=None, limit=20):
        bounded = max(1, min(int(limit), 100))
        scope = scope or ("project" if project_id else "global")
        local = self.local.search(query, scope=scope, project_id=project_id, limit=bounded)
        local = [record for record in local if self._local_visible(record)]
        cloud = self._records(scope=scope, project_id=project_id, query=query, limit=bounded)
        return self._merge(local, cloud, bounded)

    def get_active(self, memory_id: str) -> MemoryRecord | None:
        if not memory_id.startswith("cloud_"):
            record = self.local.get_active(memory_id)
            return record if record is not None and self._local_visible(record) else None
        original = self._original_id(memory_id)
        if original is None:
            return None
        global_match = self._records(scope="global", memory_id=original, limit=1)
        if global_match:
            return global_match[0]
        for project_id in self.project_bindings:
            project_match = self._records(scope="project", project_id=project_id,
                                          memory_id=original, limit=1)
            if project_match:
                return project_match[0]
        return None

    def get_recallable(self, memory_id: str) -> MemoryRecord | None:
        return self.get_active(memory_id)

    def get(self, memory_id: str) -> MemoryRecord:
        if not memory_id.startswith("cloud_"):
            record = self.local.get(memory_id)
            if not self._local_visible(record):
                raise KeyError(f"Memory is owned by the shared authority: {memory_id}")
            return record
        record = self.get_active(memory_id)
        if record is None:
            raise KeyError(f"Cached memory is unavailable: {memory_id}")
        return record
