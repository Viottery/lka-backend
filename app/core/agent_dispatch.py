"""Durable dispatch of already persisted root runs through the existing harness."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from app.core.agent_runs import AgentRunRecord, InMemoryAgentRunManager
from app.core.background_jobs import BackgroundJobFailure, BackgroundJobStore, BackgroundJobWorker

_logger = logging.getLogger(__name__)


class AgentDispatchFenced(RuntimeError):
    """This dispatcher lost its lease and must stop without changing run state."""


# Compatibility name for callers/tests that only need to identify the fence.
AgentDispatchLeaseLost = AgentDispatchFenced


class DispatchGuard:
    def __init__(self, dispatcher: AgentDispatcher, job: dict[str, Any], run_id: str):
        self.dispatcher = dispatcher
        self.job = job
        self.run_id = run_id
        self.lost = threading.Event()
        self.pause_requested = False
        self._lock = threading.Lock()

    def _lose(self):
        with self._lock:
            if self.lost.is_set():
                return
            self.lost.set()

    def check(self, conn: sqlite3.Connection | None = None):
        if self.lost.is_set():
            raise AgentDispatchFenced("dispatch_lease_lost")
        if conn is not None:
            try:
                row = conn.execute("SELECT status,lease_owner,lease_epoch,lease_expires_at,deadline "
                                   "FROM background_jobs WHERE job_id=?", (self.job["job_id"],)).fetchone()
                current = dict(zip(("status", "lease_owner", "lease_epoch", "lease_expires_at", "deadline"), row, strict=True)) if row else None
            except Exception:  # noqa: BLE001 - caller transaction cannot safely open another connection.
                current = None
        else:
            current = None
            while True:
                try:
                    current = self.dispatcher.store.get(self.job["job_id"])
                except Exception:  # noqa: BLE001 - allow brief read contention within persisted lease.
                    current = None
                if current is not None:
                    break
                expires = self.job.get("lease_expires_at")
                remaining = ((datetime.fromisoformat(expires) - datetime.now(UTC)).total_seconds()
                             if expires else 0)
                if remaining <= 0:
                    break
                threading.Event().wait(min(0.05, remaining))
        try:
            valid = (current is not None and current["status"] == "running"
                     and current["lease_owner"] == self.job["lease_owner"]
                     and current["lease_epoch"] == self.job["lease_epoch"]
                     and current["lease_expires_at"] is not None
                     and datetime.fromisoformat(current["lease_expires_at"]) > datetime.now(UTC)
                     and (current["deadline"] is None or datetime.fromisoformat(current["deadline"]) > datetime.now(UTC)))
        except Exception:  # noqa: BLE001 - malformed or missing lease fails closed.
            valid = False
        if not valid:
            # A transaction-local observation may include uncommitted changes.
            # Fence that operation, but do not poison the shared guard unless
            # the committed lease check outside the transaction also fails.
            if conn is None:
                self._lose()
            raise AgentDispatchFenced("dispatch_lease_lost")


class _ClaimedJobStore:
    """Feed one existing lease through the shared worker outcome handling."""

    def __init__(self, store, job):
        self.store, self.job = store, job

    def claim(self, *args, **kwargs):
        job, self.job = self.job, None
        return job

    def __getattr__(self, name):
        return getattr(self.store, name)


class _DispatchWorker(BackgroundJobWorker):
    """Claim durable jobs continuously, with an independent thread per lease."""

    def __init__(self, *args, reconcile=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.reconcile = reconcile
        self._executions: set[threading.Thread] = set()
        self._executions_lock = threading.Lock()

    def start(self):
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self.store.compact_history()
        thread = threading.Thread(target=self._loop, daemon=True, name="lka-dispatch-claims")
        self._threads = [thread]
        thread.start()

    def _loop(self):
        next_reconcile = 0.0
        while not self._stop.is_set():
            try:
                if self.reconcile is not None:
                    current_time = time.monotonic()
                    if current_time >= next_reconcile:
                        self.reconcile()
                        next_reconcile = current_time + 30
                job = self.store.claim(self._owner, self.lease_seconds, kinds=tuple(self.handlers))
                if job is None:
                    self._stop.wait(self.poll_seconds)
                    continue
                # Claiming and launching belong to the same pump. Shutdown joins
                # it before draining executions, including its final claim.
                thread = threading.Thread(target=self._execute_claimed, args=(job,), daemon=True,
                                          name=f"lka-dispatch-{job['job_id']}")
                with self._executions_lock:
                    self._executions.add(thread)
                try:
                    thread.start()
                except RuntimeError:
                    with self._executions_lock:
                        self._executions.discard(thread)
                    # The same lease still gets the standard failure/retry path.
                    self._execute_claimed(job)
            except Exception:  # noqa: BLE001 - keep claiming after transient store errors.
                self._stop.wait(self.poll_seconds)

    def _execute_claimed(self, job):
        try:
            worker = BackgroundJobWorker(_ClaimedJobStore(self.store, job), self.handlers,
                                         lease_seconds=self.lease_seconds)
            worker.run_one(job["lease_owner"])
        finally:
            if self.reconcile is not None:
                try:
                    self.reconcile()
                except Exception as exc:  # noqa: BLE001 - a later pump pass retries reconciliation.
                    _logger.warning("Dispatch reconciliation failed type=%s", type(exc).__name__)
            with self._executions_lock:
                self._executions.discard(threading.current_thread())

    def stop(self, timeout=None):
        """Wait for claims and every active handler before stores may close."""
        self._stop.set()
        with self._executions_lock:
            executions = list(self._executions)
        if threading.current_thread() in [*self._threads, *executions]:
            raise RuntimeError("Dispatcher cannot stop from its own execution callback")
        for thread in self._threads:
            thread.join()
        self._threads = []
        with self._executions_lock:
            executions = list(self._executions)
        for thread in executions:
            thread.join()


class AgentDispatcher:
    """Callbacks execute the original run; this service never creates a new turn."""

    def __init__(self, store: BackgroundJobStore, run_manager: InMemoryAgentRunManager, *,
                 execute: Callable[[AgentRunRecord, DispatchGuard], Any],
                 resume: Callable[[AgentRunRecord, DispatchGuard], Any],
                 has_checkpoint: Callable[[str], bool],
                 request_pause: Callable[[AgentRunRecord], Any] | None = None,
                 lease_seconds: float = 60, poll_seconds: float = 1, worker_count: int = 1):
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.store, self.run_manager = store, run_manager
        self.execute, self.resume, self.has_checkpoint = execute, resume, has_checkpoint
        self.request_pause = request_pause
        self.lease_seconds = lease_seconds
        self.guards: dict[str, DispatchGuard] = {}
        self._guards_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stopping = threading.Event()
        self._condition = threading.Condition()
        self._active_handlers = 0
        self._manual_calls = 0
        self.worker = _DispatchWorker(store, {"agent_dispatch": self._handle},
                                      lease_seconds=lease_seconds, poll_seconds=poll_seconds,
                                      reconcile=self.reconcile_failed_runs,
                                      # Deprecated compatibility argument; dispatch has no worker pool.
                                      worker_count=1)

    def reconcile_failed_runs(self, *, limit: int = 20) -> int:
        durable_store = self.run_manager.durable_store
        if durable_store is None:
            return 0
        records = durable_store.reconcile_failed_dispatch_runs(limit=limit)
        for record in records:
            self.run_manager.refresh_after_transaction(record)
        return len(records)

    def enqueue(self, run_id: str, *, mode: Literal["execute", "resume"] = "execute",
                command_id: str | None = None, conn: sqlite3.Connection | None = None):
        if mode not in {"execute", "resume"}:
            raise ValueError("Dispatch mode must be execute or resume")
        row = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (run_id,)).fetchone() if conn else None
        run = AgentRunRecord.model_validate_json(row[0]) if row else self.run_manager.get_run(run_id)
        if run is None or run.parent_run_id is not None:
            raise ValueError("Dispatch requires an existing root run")
        key = command_id if command_id is not None else f"{mode}:{run_id}"
        payload = {"run_id": run_id, "mode": mode}
        job = self.store.enqueue("agent_dispatch", run.session_id, key, payload, conn=conn)
        if conn is not None:
            row = conn.execute("SELECT payload_json FROM background_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            previous = json.loads(row[0])
        else:
            previous = self.store.get(job["job_id"], include_payload=True)["payload"]
        if previous != payload:
            raise ValueError("Dispatch command ID was already used for a different request")
        return job

    def check_run(self, run_id: str, conn: sqlite3.Connection | None = None):
        visited = set()
        while run_id and run_id not in visited:
            visited.add(run_id)
            with self._guards_lock:
                guard = self.guards.get(run_id)
            if guard is not None:
                guard.check(conn=conn)
            # Never hold the guards lock while reading the manager, and never
            # acquire its lock from a caller-owned SQLite write transaction.
            if conn is not None:
                row = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
                run_id = json.loads(row[0]).get("parent_run_id") if row else None
            else:
                run = self.run_manager.get_run(run_id)
                run_id = run.parent_run_id if run is not None else None

    def run_one(self, owner: str | None = None):
        with self._condition:
            if self._stopping.is_set():
                return False
            self._manual_calls += 1
        try:
            return self.worker.run_one(owner)
        finally:
            try:
                self.reconcile_failed_runs()
            except Exception as exc:  # noqa: BLE001 - later invocation retries durable reconciliation.
                _logger.warning("Dispatch reconciliation failed type=%s", type(exc).__name__)
            with self._condition:
                self._manual_calls -= 1
                self._condition.notify_all()

    def start(self):
        with self._lifecycle_lock:
            self._stopping.clear()
            self.worker.start()

    def stop(self):
        with self._lifecycle_lock:
            self._stopping.set()
            self.worker._stop.set()
            with self._guards_lock:
                guards = list(self.guards.values())
            try:
                for guard in guards:
                    self._pause_for_shutdown(guard)
            finally:
                self.worker.stop()
                # Also drain synchronous run_one calls, including an in-flight
                # claim that has not entered its handler yet.
                with self._condition:
                    self._condition.wait_for(lambda: self._active_handlers == 0 and self._manual_calls == 0)

    def _pause_for_shutdown(self, guard):
        if self.request_pause is None:
            return
        with guard._lock:
            if guard.pause_requested:
                return
            guard.pause_requested = True
        run = self.run_manager.get_run(guard.run_id)
        if run is not None and run.status.value in {"queued", "running"}:
            try:
                self.request_pause(run)
            except ValueError:
                latest = self.run_manager.get_run(run.run_id)
                if latest is None or latest.status.value not in {
                    "completed", "failed", "cancelled", "timed_out",
                }:
                    raise

    def _heartbeat(self, guard: DispatchGuard, finished: threading.Event):
        while not finished.wait(max(0.01, self.lease_seconds / 3)):
            while not finished.is_set():
                try:
                    renewed = self.store.heartbeat(
                        guard.job["job_id"], guard.job["lease_owner"],
                        guard.job["lease_epoch"], self.lease_seconds,
                    )
                except Exception:  # noqa: BLE001 - distinguish DB pressure from lease revocation.
                    renewed = False
                if renewed:
                    try:
                        persisted = self.store.get(guard.job["job_id"])
                        if (persisted is not None
                                and persisted.get("lease_owner") == guard.job["lease_owner"]
                                and persisted.get("lease_epoch") == guard.job["lease_epoch"]
                                and persisted.get("status") == "running"):
                            guard.job["lease_expires_at"] = persisted["lease_expires_at"]
                    except Exception as exc:  # noqa: BLE001 - keep only the last confirmed expiry.
                        _logger.debug("Could not refresh cached dispatch lease type=%s", type(exc).__name__)
                    break
                read_failed = False
                try:
                    current = self.store.get(guard.job["job_id"])
                except Exception:  # noqa: BLE001 - distinguish read contention from revoked ownership.
                    current = None
                    read_failed = True
                now = datetime.now(UTC)
                if read_failed:
                    expiry = datetime.fromisoformat(guard.job["lease_expires_at"])
                    remaining = (expiry - now).total_seconds()
                    if remaining > 0:
                        if finished.wait(min(0.05, remaining)):
                            return
                        continue
                same_owner = (current is not None and current.get("status") == "running"
                              and current.get("lease_owner") == guard.job["lease_owner"]
                              and current.get("lease_epoch") == guard.job["lease_epoch"])
                unexpired = bool(same_owner and current.get("lease_expires_at")
                                 and datetime.fromisoformat(current["lease_expires_at"]) > now
                                 and (not current.get("deadline")
                                      or datetime.fromisoformat(current["deadline"]) > now))
                if not unexpired:
                    guard._lose()
                    return
                # Do not assume a renewal that SQLite did not commit. Retry only
                # until the already-persisted lease expires.
                remaining = (datetime.fromisoformat(current["lease_expires_at"]) - now).total_seconds()
                if finished.wait(min(0.1, max(0.01, remaining / 4))):
                    return

    def _handle(self, job):
        with self._condition:
            self._active_handlers += 1
        guard = None
        finished = threading.Event()
        heartbeat = None
        try:
            run_id = job["payload"]["run_id"]
            run = self.run_manager.get_run(run_id)
            if run is None or run.parent_run_id is not None:
                raise BackgroundJobFailure("dispatch_run_unavailable")
            status = run.status.value
            mode = job["payload"].get("mode", "execute")
            if status in {"waiting_confirmation", "waiting_user", "completed", "failed", "cancelled", "timed_out"}:
                return
            if status == "paused" and mode != "resume":
                return
            if status == "running" and not self.has_checkpoint(run_id):
                self.run_manager.fail_run(run_id, error_type="execution_uncertain",
                                          error="Interrupted execution has no safe checkpoint; replay is disabled.")
                return
            if status == "paused" and not self.has_checkpoint(run_id):
                raise BackgroundJobFailure("dispatch_checkpoint_missing")
            guard = DispatchGuard(self, job, run_id)
            with self._guards_lock:
                if run_id in self.guards:
                    raise BackgroundJobFailure("dispatch_execution_busy")
                self.guards[run_id] = guard
            heartbeat = threading.Thread(target=self._heartbeat, args=(guard, finished),
                                         daemon=True, name="lka-dispatch-heartbeat")
            heartbeat.start()
            guard.check()
            if self._stopping.is_set():
                self._pause_for_shutdown(guard)
            callback = self.execute if status == "queued" else self.resume
            try:
                callback(run, guard)
            except Exception:
                # Harness pause/confirmation/user interrupts have already saved
                # their durable state; they are successful dispatch outcomes.
                current = self.run_manager.get_run(run_id)
                if current is None or current.status.value not in {
                    "paused", "waiting_confirmation", "waiting_user", "completed", "failed", "cancelled", "timed_out",
                }:
                    raise
            guard.check()
        finally:
            finished.set()
            if heartbeat is not None:
                heartbeat.join()
            if guard is not None:
                with self._guards_lock:
                    if self.guards.get(guard.run_id) is guard:
                        del self.guards[guard.run_id]
            with self._condition:
                self._active_handlers -= 1
                self._condition.notify_all()
