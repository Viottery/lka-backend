"""Bounded FIFO admission before borrowing connections for a hot write key.

This is only process-local backpressure. Row locks, idempotency, authorization
and commit remain mandatory in the following database transaction.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from contextlib import contextmanager


class KeyedWriteAdmissionError(RuntimeError):
    """No write was started by this admission attempt."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__('write_admission_'+reason)


class KeyedWriteAdmission:
    def __init__(self, *, capacity=128, wait_seconds=5.0):
        if type(capacity) is not int or capacity < 1 or not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError('Write admission limits must be positive and finite')
        self.capacity, self.wait_seconds = capacity, wait_seconds
        self._lock = threading.Lock()
        self._queues = {}
        self._tickets = 0

    @contextmanager
    def admit(self, key):
        ticket = threading.Event()
        deadline = time.monotonic() + self.wait_seconds
        registered = False
        try:
            with self._lock:
                if self._tickets >= self.capacity:
                    raise KeyedWriteAdmissionError('full')
                queue = self._queues.setdefault(key, deque())
                queue.append(ticket)
                self._tickets += 1
                registered = True
                if len(queue) == 1:
                    ticket.set()
            signalled = ticket.wait(max(0.0, deadline - time.monotonic()))
            with self._lock:
                # The old owner can hand off just as wait() times out. The
                # authoritative FIFO head decides who owns the position.
                if queue[0] is not ticket:
                    if not signalled:
                        raise KeyedWriteAdmissionError('timeout')
                    raise RuntimeError('Write admission signalled a non-owner')
            yield
        finally:
            if registered:
                next_ticket = None
                # The caller's DB context unwinds first. Never time-release a
                # live owner: it must commit/rollback/close before handoff.
                with self._lock:
                    was_owner = queue[0] is ticket
                    queue.remove(ticket)
                    self._tickets -= 1
                    if not queue:
                        self._queues.pop(key, None)
                    elif was_owner:
                        next_ticket = queue[0]
                # Wake only this key's next waiter; notify_all would wake every
                # waiting HTTP thread on each commit and create a CPU herd.
                if next_ticket is not None:
                    next_ticket.set()

    def snapshot(self):
        with self._lock:
            return {'keys': len(self._queues), 'tickets': self._tickets}
