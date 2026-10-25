"""Bounded in-process keyed scheduling signals."""

from __future__ import annotations

import heapq
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class RecoveryPage:
    keys: list[str]
    cursor: object | None = None
    done: bool = True


@dataclass
class _Namespace:
    handler: Callable[[str], None]
    recover: Callable[[object | None, int], RecoveryPage | None] | None
    ready: deque[str]
    queued: set[str]
    inflight: set[str]
    generations: dict[str, int]
    retry: dict[str, tuple[float, int]]
    cursor: object | None = None
    recovery_active: bool = False
    recovery_due: float = 0.0
    recovery_error: int = 0
    scan_generation: int = 0
    rescan_requested: int = 0


class KeyedScheduler:
    def __init__(
        self,
        capacity: int = 1024,
        workers: int = 2,
        reconcile_seconds: float = 30,
        recovery_page_size: int = 128,
        jitter_ratio: float = 0.1,
        *,
        clock: Callable[[], float] = time.monotonic,
        random_source: Callable[[], float] = random.random,
    ):
        if capacity < 1 or workers < 1 or recovery_page_size < 1:
            raise ValueError("capacity, workers and recovery_page_size must be positive")
        self.capacity, self.worker_count = capacity, workers
        self.reconcile_seconds, self.recovery_page_size = reconcile_seconds, recovery_page_size
        self.jitter_ratio, self._clock, self._random = jitter_ratio, clock, random_source
        self._cv = threading.Condition()
        self._spaces: dict[str, _Namespace] = {}
        self._timers: list[tuple[float, int, str, str]] = []
        self._timer_generation: dict[tuple[str, str], int] = {}
        self._ready_count = 0
        self._rescan_generation = 0
        self._rescan_pending = False
        self._rescan_namespaces: set[str] = set()
        self._timer_overflow = 0
        self._timer_overflow_pending = False
        self._timer_serial = 0
        self._handler_failures = 0
        self._recovery_failures = 0
        self._last_error_type: str | None = None
        self._stop = False
        self._started = False
        self._workers: list[threading.Thread] = []
        self._rr: deque[str] = deque()
        self._rr_set: set[str] = set()

    def register(
        self,
        namespace: str,
        handler: Callable[[str], None],
        recover: Callable[[object | None, int], RecoveryPage | None] | None = None,
    ) -> None:
        with self._cv:
            if namespace in self._spaces:
                raise ValueError(f"namespace already registered: {namespace}")
            initial_due = self._clock() if recover else self._next_reconcile()
            self._spaces[namespace] = _Namespace(
                handler, recover, deque(), set(), set(), {}, {}, recovery_due=initial_due
            )
            self._rr_append(namespace)

    def _rr_append(self, ns: str) -> None:
        if ns not in self._rr_set:
            self._rr.append(ns)
            self._rr_set.add(ns)

    def notify(self, namespace: str, key: str = "*") -> bool:
        with self._cv:
            space = self._spaces[namespace]
            if key in space.queued:
                self._cv.notify()
                return True
            if key in space.inflight:
                space.generations[key] = space.generations.get(key, 0) + 1
                self._cv.notify()
                return True
            if self._ready_count >= self.capacity or not self._has_capacity_for_locked(
                namespace, key
            ):
                self._mark_rescan_locked(namespace)
                self._cv.notify_all()
                return False
            self._enqueue(space, key)
            self._cv.notify()
            return True

    def request_rescan(self, namespace: str) -> int:
        """Request a fresh durable namespace scan after external state invalidation."""
        with self._cv:
            self._mark_rescan_locked(namespace)
            self._cv.notify_all()
            return self._spaces[namespace].rescan_requested

    def _enqueue(self, space: _Namespace, key: str) -> None:
        space.ready.append(key)
        space.queued.add(key)
        self._ready_count += 1

    def _tracked_keys_locked(self) -> set[tuple[str, str]]:
        tracked = set(self._timer_generation)
        for ns, space in self._spaces.items():
            tracked.update((ns, key) for key in space.queued | space.inflight | space.retry.keys())
        return tracked

    def _has_capacity_for_locked(self, namespace: str, key: str) -> bool:
        tracked = self._tracked_keys_locked()
        return (namespace, key) in tracked or len(tracked) < self.capacity

    def schedule(self, namespace: str, key: str, delay: float) -> None:
        with self._cv:
            slot = namespace, key
            if slot not in self._timer_generation and (
                len(self._timer_generation) >= self.capacity
                or not self._has_capacity_for_locked(*slot)
            ):
                self._timer_overflow += 1
                self._timer_overflow_pending = True
                self._mark_rescan_locked(namespace)
                self._cv.notify_all()
                return
            self._timer_serial += 1
            gen = self._timer_serial
            self._timer_generation[slot] = gen
            heapq.heappush(self._timers, (self._clock() + max(0, delay), gen, namespace, key))
            if len(self._timers) > self.capacity * 2:
                self._compact_timers_locked()
            self._cv.notify_all()

    def start(self) -> None:
        with self._cv:
            if self._started:
                return
            self._started = True
            self._stop = False
            self._workers = [
                threading.Thread(target=self._worker, name=f"keyed-scheduler-{i}", daemon=True)
                for i in range(self.worker_count)
            ]
            for thread in self._workers:
                thread.start()

    def stop(self, timeout: float | None = None) -> bool:
        with self._cv:
            if not self._started:
                return True
            self._stop = True
            self._cv.notify_all()
            workers = list(self._workers)
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        for worker in workers:
            worker.join(None if deadline is None else max(0, deadline - time.monotonic()))
        done = not any(w.is_alive() for w in workers)
        with self._cv:
            if done:
                self._started = False
                self._workers = []
        return done

    def status(self) -> dict[str, object]:
        with self._cv:
            return {
                "started": self._started,
                "stopping": self._stop,
                "ready": self._ready_count,
                "inflight": sum(len(s.inflight) for s in self._spaces.values()),
                "needs_rescan": self._rescan_pending,
                "rescan_generation": self._rescan_generation,
                "timer_count": len(self._timer_generation),
                "timer_overflow": self._timer_overflow,
                "timer_overflow_pending": self._timer_overflow_pending,
                "rescan_namespaces": len(self._rescan_namespaces),
                "handler_failures": self._handler_failures,
                "recovery_failures": self._recovery_failures,
                "last_error_type": self._last_error_type,
                "recovery_in_progress": any(s.recovery_active for s in self._spaces.values()),
            }

    def _worker(self) -> None:
        while True:
            task = None
            with self._cv:
                while task is None:
                    if self._stop:
                        return
                    now = self._clock()
                    while self._timers and self._timers[0][0] <= now:
                        _, gen, ns, key = heapq.heappop(self._timers)
                        if self._timer_generation.get((ns, key)) == gen:
                            self._timer_generation.pop((ns, key), None)
                            self._offer_locked(ns, key)
                    # Alternate one ready task and one recovery page.
                    for _ in range(len(self._rr)):
                        ns = self._rr.popleft()
                        self._rr_set.discard(ns)
                        space = self._spaces[ns]
                        self._rr_append(ns)
                        if (
                            space.recover
                            and not space.recovery_active
                            and space.recovery_due <= now
                            and len(self._tracked_keys_locked()) < self.capacity
                        ):
                            space.recovery_active = True
                            if space.cursor is None and ns in self._rescan_namespaces:
                                space.scan_generation = space.rescan_requested
                            task = (ns, "", 0, "recover", space.cursor)
                            break
                        if (
                            not space.recover
                            and space.recovery_due <= now
                            and "*" not in space.inflight
                        ):
                            if ns in self._rescan_namespaces:
                                space.scan_generation = space.rescan_requested
                            space.recovery_due = self._next_reconcile()
                            space.inflight.add("*")
                            task = (ns, "*", 0, "handler", None)
                            break
                        for _ in range(len(space.ready)):
                            key = space.ready.popleft()
                            space.queued.discard(key)
                            self._ready_count -= 1
                            due = space.retry.get(key)
                            if due and due[0] > now:
                                self._enqueue(space, key)
                                continue
                            if key in space.inflight:
                                continue
                            space.inflight.add(key)
                            task = (ns, key, space.generations.get(key, 0), "handler", None)
                            break
                        if task:
                            break
                    if task:
                        break
                    wait = None
                    if self._timers:
                        wait = max(0.0, self._timers[0][0] - now)
                    for s in self._spaces.values():
                        if (
                            s.recover
                            and (
                                s.recovery_active
                                or len(self._tracked_keys_locked()) >= self.capacity
                            )
                        ) or (not s.recover and "*" in s.inflight):
                            continue
                        d = max(0.0, s.recovery_due - now)
                        wait = d if wait is None else min(wait, d)
                    self._cv.wait(wait)
            ns, key, generation, kind, cursor = task
            if kind == "handler":
                self._run_handler(ns, key, generation)
            else:
                self._run_recovery(ns, cursor)

    def _offer_locked(self, ns: str, key: str) -> None:
        space = self._spaces.get(ns)
        if not space:
            return
        if key in space.inflight or key in space.queued:
            return
        if self._ready_count >= self.capacity or not self._has_capacity_for_locked(ns, key):
            self._mark_rescan_locked(ns)
            return
        self._enqueue(space, key)

    def _run_handler(self, ns: str, key: str, observed: int) -> None:
        space = self._spaces[ns]
        failed = False
        try:
            space.handler(key)
        except Exception as exc:  # noqa: BLE001 - arbitrary handler failures are retried
            failed = True
            with self._cv:
                self._handler_failures += 1
                self._last_error_type = type(exc).__name__
        with self._cv:
            space.inflight.discard(key)
            current = space.generations.get(key, 0)
            if failed:
                attempts = space.retry.get(key, (0, 0))[1] + 1
                retry_due = self._clock() + min(60.0, 0.25 * (2 ** min(attempts, 8)))
                if self._offer_retry_timer_locked(ns, key, retry_due):
                    space.retry[key] = (retry_due, attempts)
            elif key in space.retry:
                space.retry.pop(key, None)
            if (
                not failed
                and current != observed
                and key not in space.queued
                and self._ready_count < self.capacity
            ):
                self._enqueue(space, key)
            if (
                not failed
                and key == "*"
                and ns in self._rescan_namespaces
                and space.scan_generation == space.rescan_requested
            ):
                self._rescan_namespaces.discard(ns)
                if not self._rescan_namespaces:
                    self._rescan_pending = False
                    self._timer_overflow_pending = False
            space.generations.pop(key, None)
            self._cv.notify_all()

    def _offer_retry_timer_locked(self, ns: str, key: str, due: float) -> bool:
        slot = ns, key
        if slot not in self._timer_generation and (
            len(self._timer_generation) >= self.capacity or not self._has_capacity_for_locked(*slot)
        ):
            if key == "*" and not self._spaces[ns].recover:
                self._spaces[ns].recovery_due = self._next_reconcile()
            else:
                self._mark_rescan_locked(ns)
            return False
        if not self._has_capacity_for_locked(ns, key):
            if key == "*" and not self._spaces[ns].recover:
                self._spaces[ns].recovery_due = self._next_reconcile()
            else:
                self._mark_rescan_locked(ns)
            return False
        self._timer_serial += 1
        gen = self._timer_serial
        self._timer_generation[slot] = gen
        heapq.heappush(self._timers, (due, gen, ns, key))
        if len(self._timers) > self.capacity * 2:
            self._compact_timers_locked()
        return True

    def _compact_timers_locked(self) -> None:
        self._timers = [
            entry
            for entry in self._timers
            if self._timer_generation.get((entry[2], entry[3])) == entry[1]
        ]
        heapq.heapify(self._timers)

    def _mark_rescan_locked(self, namespace: str) -> None:
        self._rescan_generation += 1
        self._rescan_pending = True
        space = self._spaces[namespace]
        space.rescan_requested = self._rescan_generation
        self._rescan_namespaces.add(namespace)
        if space.recover and not space.recovery_active:
            space.cursor = None
        if not space.recovery_active:
            space.scan_generation = space.rescan_requested
        space.recovery_due = min(space.recovery_due, self._clock())

    def _run_recovery(self, ns: str, cursor: object | None) -> None:
        space = self._spaces[ns]
        with self._cv:
            available = self.capacity - len(self._tracked_keys_locked())
            if available <= 0:
                space.recovery_active = False
                space.recovery_due = self._clock()
                self._cv.notify_all()
                return
            limit = min(self.recovery_page_size, available)
        try:
            page = space.recover(cursor, limit) if space.recover else None
        except Exception as exc:  # noqa: BLE001 - recovery provider failures are retried
            page = None
            with self._cv:
                self._recovery_failures += 1
                self._last_error_type = type(exc).__name__
        with self._cv:
            space.recovery_active = False
            if page is None:
                space.recovery_error += 1
                space.recovery_due = self._clock() + min(
                    60.0, self.reconcile_seconds * (2 ** min(space.recovery_error, 5))
                )
            else:
                space.recovery_error = 0
                overflow = False
                for key in page.keys:
                    if key in space.queued or key in space.inflight:
                        continue
                    if self._ready_count >= self.capacity or not self._has_capacity_for_locked(
                        ns, key
                    ):
                        overflow = True
                        break
                    self._enqueue(space, key)
                # Cursor advances only when every page key was retained/coalesced.
                if not overflow:
                    newer_overflow = (
                        ns in self._rescan_namespaces
                        and space.scan_generation != space.rescan_requested
                    )
                    if newer_overflow:
                        space.cursor = None
                        space.recovery_due = self._clock()
                    else:
                        space.cursor = None if page.done else page.cursor
                        space.recovery_due = (
                            self._clock() if not page.done else self._next_reconcile()
                        )
                    if page.done and ns in self._rescan_namespaces and not newer_overflow:
                        self._rescan_namespaces.discard(ns)
                        if not self._rescan_namespaces:
                            self._rescan_pending = False
                            self._timer_overflow_pending = False
                else:
                    space.recovery_due = self._clock() + 0.01
            if self._rescan_pending and not space.recover:
                space.recovery_due = self._next_reconcile()
            self._cv.notify_all()

    def _next_reconcile(self) -> float:
        base = max(0.01, self.reconcile_seconds)
        return self._clock() + base * (1 + (2 * self._random() - 1) * self.jitter_ratio)
