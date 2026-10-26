"""Schedule bounded, local history embedding work on the existing durable worker."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from contextlib import closing
from threading import RLock
from time import monotonic
from typing import Any
from uuid import uuid4


class ConversationHistoryIndexer:
    kind = "conversation_history_index"

    def __init__(self, *, index, store, conn_factory, enabled: bool = True) -> None:
        self.index = index
        self.store = store
        self._conn = conn_factory
        self.enabled = enabled
        self._lock = RLock()
        self._next_request: OrderedDict[str, float] = OrderedDict()
        self._provider_retry_at = 0.0

    def request_index(self, session_id: str, *, force: bool = False) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            now = monotonic()
            if now < self._provider_retry_at:
                return False
            if not force and now < self._next_request.get(session_id, 0):
                return False
            self._next_request[session_id] = now + 1
            self._next_request.move_to_end(session_id)
            while len(self._next_request) > 128:
                self._next_request.popitem(last=False)
        try:
            with closing(self._conn()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                self.store.enqueue_latest_watermark(
                    self.kind, session_id, uuid4().hex,
                    {"session_id": session_id}, conn=conn, priority=-10,
                )
                conn.commit()
            self.store.notify_committed()
            return True
        except sqlite3.OperationalError:
            # A derived cache must not turn an otherwise valid read into a
            # failed user turn. The next explicit search may request it again.
            return False

    def handle(self, job: dict[str, Any]) -> None:
        if not self.enabled:
            return
        if not self.store.heartbeat(job["job_id"], job["lease_owner"], job["lease_epoch"], lease_seconds=600):
            return
        result = self.index.sync_batch(job["scope_id"])
        if result.get("state") == "unavailable":
            with self._lock:
                self._next_request[job["scope_id"]] = monotonic() + 60
                if result.get("pending"):
                    self._provider_retry_at = monotonic() + 60
            return
        if result.get("pending"):
            # Coalesce a continuation behind the running job. It is claimed
            # only after this lease finishes, rather than looping inside a job.
            self.request_index(job["scope_id"], force=True)

    def search(self, session_id: str, query: str, *, limit: int = 8, offset: int = 0) -> dict[str, Any]:
        with self._lock:
            if monotonic() < self._provider_retry_at:
                return {"messages": [], "status": {
                    "state": "unavailable", "retry_after_seconds": 60, "pending": True,
                }}
        result = self.index.search(session_id, query, limit=limit, offset=offset)
        status = result.get("status", {})
        if status.get("state") == "unavailable" and "indexed_count" in status:
            with self._lock:
                self._provider_retry_at = monotonic() + 60
        if result.get("pending") or status.get("pending"):
            scheduled = self.request_index(session_id)
            result["index_scheduled"] = scheduled
        if not self.enabled:
            result["background_enabled"] = False
        return result
