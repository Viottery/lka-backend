"""Process-local execution capacity, separate from durable session ownership.

Only active execution holds a permit. Durable dispatch jobs remain unclaimed
when full; callers cannot use these counters to authorize a tool or replay work.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Literal

ExecutionKind = Literal['interactive', 'background', 'child']
_current: ContextVar[ExecutionLease | None] = ContextVar('lka_execution_lease', default=None)


class ExecutionCapacityError(RuntimeError):
    """The request has not been accepted into execution."""


class ExecutionLease:
    def __init__(self, governor: ExecutionAdmission, kind: ExecutionKind, key: str | None):
        self.governor, self.kind, self.key = governor, kind, key
        self.held = True
        self.references = 0
        self.closed = False

    def retain(self):
        with self.governor._condition:
            if self.closed:
                raise RuntimeError('Execution permit is already closed')
            self.references += 1

    def release(self):
        with self.governor._condition:
            if self.references <= 0:
                raise RuntimeError('Execution reference released twice')
            self.references -= 1
            last = self.references == 0
            if last:
                # Closing and the last-reference transition must be atomic with
                # a worker's activate(), or a late worker could enter a permit
                # whose capacity has already been returned to another request.
                self.closed = True
                if self.held:
                    self.governor._release(self.kind)
                    self.held = False
        if last:
            self.governor._notify()

    @contextmanager
    def activate(self):
        with self.governor._condition:
            if self.closed:
                raise RuntimeError('Execution permit is already closed')
            self.references += 1
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)
            self.release()

    def close(self):
        with self.governor._condition:
            if self.closed:
                return
            self.closed = True
            if self.held:
                self.governor._release(self.kind)
                self.held = False
        self.governor._notify()


class ExecutionAdmission:
    def __init__(self, *, total: int = 4, interactive_reserved: int = 2, children: int = 2):
        if total < 1 or not 0 <= interactive_reserved < total or children < 1:
            raise ValueError('Invalid execution capacity')
        self.total, self.background_limit, self.children = total, total - interactive_reserved, children
        self._counts = dict.fromkeys(('interactive', 'background', 'child'), 0)
        self._condition = threading.Condition()
        self.on_available = None

    def _available(self, kind):
        if kind == 'child':
            return self._counts[kind] < self.children
        return (self._counts['interactive'] + self._counts['background'] < self.total
                and (kind != 'background' or self._counts[kind] < self.background_limit))

    def try_acquire(self, kind: ExecutionKind = 'interactive', *, key: str | None = None):
        if kind not in self._counts:
            raise ValueError('Unknown execution kind')
        with self._condition:
            if not self._available(kind):
                return None
            self._counts[kind] += 1
            return ExecutionLease(self, kind, key)

    def _release(self, kind):
        self._counts[kind] -= 1
        self._condition.notify_all()

    def _notify(self):
        if self.on_available is not None:
            self.on_available()

    def snapshot(self):
        with self._condition:
            return {**self._counts, 'total_limit': self.total, 'child_limit': self.children}

    @contextmanager
    def scope(self, kind: ExecutionKind = 'interactive', *, key: str | None = None):
        current = _current.get()
        if (current is not None and current.governor is self and not current.closed
                and (current.kind == kind or {current.kind, kind} <= {'interactive', 'background'})
                and (key is None or current.key in {None, key})):
            lease = current
            if key is not None:
                lease.key = key
        else:
            lease = self.try_acquire(kind, key=key)
            if lease is None:
                raise ExecutionCapacityError('Agent execution capacity is full')
        with lease.activate():
            yield lease


@asynccontextmanager
async def suspend_execution():
    """Release a parent's execution position while its persisted children run.

    Session admission, checkpoint ownership and child outcome audits are retained.
    Reacquisition is async and holds neither a DB connection nor a thread lock.
    """
    lease = _current.get()
    if lease is None or lease.closed or not lease.held:
        yield
        return
    governor = lease.governor
    with governor._condition:
        governor._release(lease.kind)
        lease.held = False
    governor._notify()
    # An exception or cancellation exits before this reacquisition code. The
    # enclosing execution is then unwinding and must not wait for capacity.
    yield
    while True:
        with governor._condition:
            if lease.closed:
                raise RuntimeError('Parent execution closed while suspended')
            if governor._available(lease.kind):
                governor._counts[lease.kind] += 1
                lease.held = True
                break
        await asyncio.sleep(0.02)


def admitted_execution(function):
    """Apply runtime admission before a turn can create a durable root record."""
    def scope(runtime, args, kwargs):
        admission = getattr(runtime, 'execution_admission', None)
        from contextlib import nullcontext
        if admission is None:  # Lightweight runtime doubles keep their contract.
            return nullcontext()
        run_id = kwargs.get('existing_run_id') or kwargs.get('run_id') or (args[0] if args else None)
        run = runtime.agent_run_manager.get_run(run_id) if run_id else None
        kind = 'child' if run is not None and run.parent_run_id else 'interactive'
        return admission.scope(kind, key=run_id)

    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def async_call(self, *args, **kwargs):
            with scope(self, args, kwargs) as lease:
                try:
                    return await function(self, *args, **kwargs)
                except Exception as exc:
                    cleanup = getattr(self, "_execution_failed_before_start", None)
                    if cleanup is not None and lease is not None and lease.key is not None:
                        await asyncio.to_thread(cleanup, lease.key, exc)
                    raise
        return async_call

    @wraps(function)
    def sync_call(self, *args, **kwargs):
        with scope(self, args, kwargs) as lease:
            try:
                return function(self, *args, **kwargs)
            except Exception as exc:
                cleanup = getattr(self, "_execution_failed_before_start", None)
                if cleanup is not None and lease is not None and lease.key is not None:
                    cleanup(lease.key, exc)
                raise
    return sync_call


@asynccontextmanager
async def child_execution_scope(governor, run_id, check_active):
    if governor is None:
        yield
        return
    lease = None
    while lease is None:
        check_active()
        lease = governor.try_acquire('child', key=run_id)
        if lease is None:
            await asyncio.sleep(0.02)
    with lease.activate():
        yield


def bind_execution_run(run_id: str) -> None:
    """Attach an accepted ID to the current entry permit for failure cleanup."""
    lease = _current.get()
    if lease is not None and not lease.closed and lease.key is None:
        lease.key = run_id


def retain_execution_for_worker() -> ExecutionLease | None:
    """Transfer a reference before thread.start, including its startup gap.

    Cancelling the awaiting coroutine must not return execution capacity while
    its daemon thread still exists, even if that thread has not entered run().
    """
    lease = _current.get()
    if lease is not None and not lease.closed:
        lease.retain()
        return lease
    return None
