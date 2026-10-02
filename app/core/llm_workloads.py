"""Shared foreground/background admission and persistent usage budgets.

The controller holds no prompt content. Admission works across worker threads
and event loops; waiting is asynchronous and foreground callers get priority.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.llm.errors import LLMClientError


class BackgroundBudgetDeferred(LLMClientError):
    """A task should wait for budget or foreground load to clear."""


class BackgroundTaskBudgetExceeded(LLMClientError):
    """A bounded background task has exhausted its total token allowance."""


@dataclass(frozen=True)
class Workload:
    pool: str = "interactive"
    task_id: str | None = None
    max_tokens: int | None = None


_workload: ContextVar[Workload | None] = ContextVar("llm_workload", default=None)


def current_workload() -> Workload:
    return _workload.get() or Workload()


@contextmanager
def workload_scope(pool: str, *, task_id: str | None = None, max_tokens: int | None = None):
    if pool not in {"interactive", "background_memory", "background_io"}:
        raise ValueError("unknown LLM workload pool")
    token = _workload.set(Workload(pool, task_id, max_tokens))
    try:
        yield
    finally:
        _workload.reset(token)


@dataclass
class UsageTicket:
    call_id: str
    workload: Workload
    input_tokens: int
    output_tokens: int
    started: float = field(default_factory=time.perf_counter)
    usage: dict[str, Any] = field(default_factory=dict)
    dispatched: bool = False
    failed: bool = False


class LLMWorkloadController:
    def __init__(
        self, db_path: str | Path, *, max_concurrency: int = 4,
        interactive_reserved: int = 2, memory_concurrency: int = 1,
        io_concurrency: int = 1, hourly_tokens: int = 200_000,
        daily_tokens: int = 1_000_000, daily_cost_limit: float = 0,
        input_cost_per_million: float = 0, output_cost_per_million: float = 0,
    ) -> None:
        if not 1 <= interactive_reserved <= max_concurrency:
            raise ValueError("interactive reservation must fit total concurrency")
        self.db_path = str(db_path)
        self.max_concurrency = max_concurrency
        self.interactive_reserved = interactive_reserved
        self.pool_limits = {"background_memory": memory_concurrency, "background_io": io_concurrency}
        self.hourly_tokens, self.daily_tokens = hourly_tokens, daily_tokens
        self.daily_cost_limit = daily_cost_limit
        self.input_price, self.output_price = input_cost_per_million, output_cost_per_million
        self._guard = threading.Lock()
        self._active: Counter[str] = Counter()
        self._interactive_waiters = 0
        self._cooldown_until = 0.0
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS llm_workload_usage(
                call_id TEXT PRIMARY KEY, pool TEXT NOT NULL, task_id TEXT,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL,
                input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                cost REAL NOT NULL, duration_seconds REAL, count_method TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_time ON llm_workload_usage(created_at,pool)")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_price + output_tokens * self.output_price) / 1_000_000

    def _reserve(self, ticket: UsageTicket) -> None:
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Counters need only rolling windows. Retain three months of
            # payload-free diagnostics, not an unbounded billing ledger.
            conn.execute("DELETE FROM llm_workload_usage WHERE created_at<? AND status!='reserved'",
                         ((now - timedelta(days=90)).isoformat(),))
            # A killed process cannot retain a reservation forever. Expired
            # dispatched calls stay charged conservatively, never free retries.
            conn.execute("UPDATE llm_workload_usage SET status='estimated' WHERE status='reserved' AND expires_at<=?", (now.isoformat(),))
            if ticket.workload.pool != "interactive":
                for interval, limit in ((timedelta(hours=1), self.hourly_tokens), (timedelta(days=1), self.daily_tokens)):
                    used = conn.execute(
                        "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM llm_workload_usage WHERE pool!='interactive' AND created_at>=?",
                        ((now - interval).isoformat(),),
                    ).fetchone()[0]
                    if limit and used + ticket.input_tokens + ticket.output_tokens > limit:
                        raise BackgroundBudgetDeferred("background token budget reached")
                cost_used = conn.execute(
                    "SELECT COALESCE(SUM(cost),0) FROM llm_workload_usage WHERE pool!='interactive' AND created_at>=?",
                    ((now - timedelta(days=1)).isoformat(),),
                ).fetchone()[0]
                if self.daily_cost_limit and cost_used + self._cost(ticket.input_tokens, ticket.output_tokens) > self.daily_cost_limit:
                    raise BackgroundBudgetDeferred("background cost budget reached")
                if ticket.workload.task_id and ticket.workload.max_tokens:
                    used = conn.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM llm_workload_usage WHERE task_id=?", (ticket.workload.task_id,)).fetchone()[0]
                    if used + ticket.input_tokens + ticket.output_tokens > ticket.workload.max_tokens:
                        raise BackgroundTaskBudgetExceeded("background task token budget reached")
            conn.execute("INSERT INTO llm_workload_usage VALUES(?,?,?,?,?,'reserved',?,?,?,NULL,'reserved_estimate')", (
                ticket.call_id, ticket.workload.pool, ticket.workload.task_id,
                now.isoformat(), (now + timedelta(minutes=15)).isoformat(),
                ticket.input_tokens, ticket.output_tokens, self._cost(ticket.input_tokens, ticket.output_tokens),
            ))

    def _finish(self, ticket: UsageTicket) -> None:
        if not ticket.dispatched:
            with self._connect() as conn:
                conn.execute("DELETE FROM llm_workload_usage WHERE call_id=?", (ticket.call_id,))
            return
        usage = ticket.usage
        input_actual = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_actual = usage.get("completion_tokens", usage.get("output_tokens"))
        def valid(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0
        known = valid(input_actual) and valid(output_actual)
        incoming = input_actual if valid(input_actual) else ticket.input_tokens
        outgoing = output_actual if valid(output_actual) else ticket.output_tokens
        with self._connect() as conn:
            conn.execute("UPDATE llm_workload_usage SET status=?,input_tokens=?,output_tokens=?,cost=?,duration_seconds=?,count_method=? WHERE call_id=?", (
                "failed" if ticket.failed else "completed", incoming, outgoing,
                self._cost(incoming, outgoing), time.perf_counter() - ticket.started,
                "provider_usage" if known else "conservative_estimate", ticket.call_id,
            ))

    def provider_limited(self, seconds: float = 30) -> None:
        with self._guard:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    @asynccontextmanager
    async def admit(self, *, input_tokens: int, output_tokens: int):
        workload = _workload.get() or Workload()
        foreground = workload.pool == "interactive"
        acquired = False
        registered_waiter = foreground
        ticket = UsageTicket(uuid4().hex, workload, input_tokens, output_tokens)
        if foreground:
            with self._guard:
                self._interactive_waiters += 1
        try:
            wait_started = time.monotonic()
            while not acquired:
                with self._guard:
                    total = sum(self._active.values())
                    background = total - self._active["interactive"]
                    permitted = total < self.max_concurrency
                    if not foreground:
                        permitted = permitted and not self._interactive_waiters and time.monotonic() >= self._cooldown_until
                        permitted = permitted and background < self.max_concurrency - self.interactive_reserved
                        permitted = permitted and self._active[workload.pool] < self.pool_limits[workload.pool]
                    if permitted:
                        self._active[workload.pool] += 1
                        if foreground:
                            self._interactive_waiters -= 1
                            registered_waiter = False
                        acquired = True
                if not acquired:
                    if not foreground and time.monotonic() - wait_started > 5:
                        raise BackgroundBudgetDeferred("foreground work or provider cooldown takes priority")
                    await asyncio.sleep(0.025)
            reservation = asyncio.create_task(asyncio.to_thread(self._reserve, ticket))
            try:
                await asyncio.shield(reservation)
            except asyncio.CancelledError:
                await reservation
                await asyncio.to_thread(self._finish, ticket)
                raise
            try:
                yield ticket
            finally:
                await asyncio.to_thread(self._finish, ticket)
        finally:
            with self._guard:
                if registered_waiter:
                    self._interactive_waiters -= 1
                if acquired:
                    self._active[workload.pool] -= 1

    def health(self) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with self._connect() as conn:
            rows = conn.execute("SELECT pool,status,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,SUM(cost) AS estimated_cost,COUNT(*) AS calls FROM llm_workload_usage WHERE created_at>=? GROUP BY pool,status", (since,)).fetchall()
        with self._guard:
            active = dict(self._active)
            waiters = self._interactive_waiters
        return {"active_by_pool": active, "interactive_waiters": waiters,
                "last_24h": [dict(row) for row in rows],
                "cost_pricing_configured": bool(self.input_price or self.output_price),
                "limits": {"hourly_background_tokens": self.hourly_tokens, "daily_background_tokens": self.daily_tokens, "daily_background_cost": self.daily_cost_limit}}
