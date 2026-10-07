"""Shared foreground/background admission and persistent usage budgets.

The controller holds no prompt content. Admission works across worker threads
and event loops; waiting is asynchronous and foreground callers get priority.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.llm.errors import LLMClientError

_BACKGROUND_POOLS = {"background_memory", "background_message", "background_io"}
_logger = logging.getLogger(__name__)


class BackgroundBudgetDeferred(LLMClientError):
    """A task should wait for budget or foreground load to clear."""

    category = error_category = "budget_deferred"


class BackgroundCircuitOpen(BackgroundBudgetDeferred):
    """Persistent emergency stop; only an explicit control reset reopens it."""

    category = error_category = "background_circuit_open"


class BackgroundTaskBudgetExceeded(LLMClientError):
    """A bounded background task has exhausted its total token allowance."""


class WorkBudgetExceeded(BackgroundTaskBudgetExceeded):
    """A persistent work allowance requires an explicit limit increase."""

    category = error_category = "work_budget_exhausted"


@dataclass(frozen=True)
class BudgetQuota:
    scope_id: str
    hourly_tokens: int | None = None
    daily_tokens: int | None = None
    hourly_calls: int | None = None
    daily_calls: int | None = None
    max_tokens: int | None = None
    max_calls: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope_id, str) or not self.scope_id:
            raise ValueError("quota scope_id must be nonempty")
        for name in ("hourly_tokens", "daily_tokens", "hourly_calls", "daily_calls", "max_tokens", "max_calls"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or None")


@dataclass(frozen=True)
class BudgetPricing:
    client_name: str
    model: str
    revision: str
    input_cost_per_million: float
    output_cost_per_million: float

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.client_name, self.model, self.revision)):
            raise ValueError("pricing identity must be nonempty")
        for value in (self.input_cost_per_million, self.output_cost_per_million):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("pricing must be finite and nonnegative")


@dataclass(frozen=True)
class Workload:
    pool: str = "interactive"
    task_id: str | None = None
    max_tokens: int | None = None
    quotas: tuple[BudgetQuota, ...] = ()
    before_dispatch: Callable[[], None | Awaitable[None]] | None = None
    pricing: BudgetPricing | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.quotas, tuple) or any(not isinstance(quota, BudgetQuota) for quota in self.quotas):
            raise ValueError("quotas must be a tuple of BudgetQuota values")
        if len({quota.scope_id for quota in self.quotas}) != len(self.quotas):
            raise ValueError("quota scopes must be unique")

    async def check_dispatch(self) -> None:
        if self.before_dispatch is not None:
            if inspect.iscoroutinefunction(self.before_dispatch):
                result = self.before_dispatch()
            else:
                result = await asyncio.to_thread(self.before_dispatch)
            if inspect.isawaitable(result):
                await result


_workload: ContextVar[Workload | None] = ContextVar("llm_workload", default=None)


def current_workload() -> Workload:
    return _workload.get() or Workload()


@contextmanager
def workload_scope(
    pool: str, *, task_id: str | None = None, max_tokens: int | None = None,
    quotas: tuple[BudgetQuota, ...] = (),
    before_dispatch: Callable[[], None | Awaitable[None]] | None = None,
    pricing: BudgetPricing | None = None,
):
    if pool not in {"interactive", *_BACKGROUND_POOLS}:
        raise ValueError("unknown LLM workload pool")
    token = _workload.set(Workload(pool, task_id, max_tokens, quotas, before_dispatch, pricing))
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
        io_concurrency: int = 1, message_concurrency: int = 1, hourly_tokens: int = 200_000,
        daily_tokens: int = 1_000_000, daily_cost_limit: float = 0,
        input_cost_per_million: float = 0, output_cost_per_million: float = 0,
        memory_task_fuse_tokens: int = 262_144,
        memory_hourly_fuse_tokens: int = 2_000_000,
        memory_daily_fuse_tokens: int = 10_000_000,
    ) -> None:
        if not 1 <= interactive_reserved <= max_concurrency:
            raise ValueError("interactive reservation must fit total concurrency")
        self.db_path = str(db_path)
        self.max_concurrency = max_concurrency
        self.interactive_reserved = interactive_reserved
        self.pool_limits = {"background_memory": memory_concurrency, "background_message": message_concurrency,
                            "background_io": io_concurrency}
        self.memory_fuses = {"task_tokens": memory_task_fuse_tokens, "hourly_tokens": memory_hourly_fuse_tokens,
                             "daily_tokens": memory_daily_fuse_tokens}
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in self.memory_fuses.values()):
            raise ValueError("memory emergency thresholds must be positive integers")
        self.hourly_tokens, self.daily_tokens = hourly_tokens, daily_tokens
        self.daily_cost_limit = daily_cost_limit
        self.input_price, self.output_price = input_cost_per_million, output_cost_per_million
        self._guard = threading.Lock()
        self._active: Counter[str] = Counter()
        self._interactive_waiters = 0
        self._cooldown_until = 0.0
        self._inflight: dict[str, tuple[str, asyncio.AbstractEventLoop, asyncio.Task]] = {}
        self._interrupted: set[str] = set()
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS llm_workload_usage(
                call_id TEXT PRIMARY KEY, pool TEXT NOT NULL, task_id TEXT,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL,
                input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                cost REAL NOT NULL, duration_seconds REAL, count_method TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_time ON llm_workload_usage(created_at,pool)")
            conn.execute("""CREATE TABLE IF NOT EXISTS llm_workload_pool_state(
                pool TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
                reset_at TEXT NOT NULL DEFAULT '', opened_at TEXT, reason TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS llm_workload_budget_events(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, pool TEXT NOT NULL,
                created_at TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL,
                revision INTEGER NOT NULL)""")
            for pool in sorted(_BACKGROUND_POOLS):
                conn.execute("INSERT OR IGNORE INTO llm_workload_pool_state(pool) VALUES(?)", (pool,))
            columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_workload_usage)")}
            for name, definition in (("cost_known", "INTEGER NOT NULL DEFAULT 1"),
                                     ("pricing_client", "TEXT"), ("pricing_model", "TEXT"), ("pricing_revision", "TEXT")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE llm_workload_usage ADD COLUMN {name} {definition}")
            conn.execute("""CREATE TABLE IF NOT EXISTS llm_workload_quota_usage(
                call_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                PRIMARY KEY(call_id,scope_id))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_quota_scope ON llm_workload_quota_usage(scope_id,call_id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS llm_workload_quota_totals(
                scope_id TEXT PRIMARY KEY, total_tokens INTEGER NOT NULL,
                total_calls INTEGER NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS llm_workload_quota_adoptions(
                task_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                compacted_tokens INTEGER NOT NULL, compacted_calls INTEGER NOT NULL,
                PRIMARY KEY(task_id,scope_id))""")
            # Seed legacy task allowances once; future compaction never resets
            # these durable counters. New scoped usage uses the same counters.
            conn.execute("""INSERT OR IGNORE INTO llm_workload_quota_totals
                SELECT 'task:' || task_id,SUM(input_tokens+output_tokens),COUNT(*)
                FROM llm_workload_usage WHERE task_id IS NOT NULL GROUP BY task_id""")

    @staticmethod
    def _quotas(workload: Workload) -> tuple[BudgetQuota, ...]:
        quotas = workload.quotas
        if workload.task_id:
            scope_id = "task:" + workload.task_id
            if any(quota.scope_id == scope_id for quota in quotas):
                raise ValueError("explicit quota collides with legacy task scope")
            quotas += (BudgetQuota(scope_id, max_tokens=workload.max_tokens or None),)
        return quotas

    @staticmethod
    def _quota_usage(conn: sqlite3.Connection, scope_id: str, now: datetime) -> dict[str, int]:
        total = conn.execute("SELECT total_tokens,total_calls FROM llm_workload_quota_totals WHERE scope_id=?", (scope_id,)).fetchone()
        result = {"total_tokens": total[0] if total else 0, "total_calls": total[1] if total else 0}
        for name, interval in (("hourly", timedelta(hours=1)), ("daily", timedelta(days=1))):
            row = conn.execute("""SELECT COALESCE(SUM(u.input_tokens+u.output_tokens),0),COUNT(*)
                FROM llm_workload_usage u JOIN llm_workload_quota_usage q ON q.call_id=u.call_id
                LEFT JOIN llm_workload_pool_state s ON s.pool=u.pool
                WHERE q.scope_id=? AND u.created_at>=? AND u.created_at>=COALESCE(s.reset_at,'')""",
                (scope_id, (now - interval).isoformat())).fetchone()
            result[name + "_tokens"], result[name + "_calls"] = row
        return result

    def quota_usage(self, scope_id: str) -> dict[str, int]:
        """Return payload-free durable totals and rolling usage."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            return self._quota_usage(conn, scope_id, datetime.now(UTC))

    def adopt_task_usage(self, task_ids: tuple[str, ...], scope_ids: tuple[str, ...]) -> None:
        """Adopt legacy lifetime usage and retained rolling usage idempotently.

        This preserves the original shared ledger, statuses and task counters.
        Compacted calls are adopted from the durable task allowance, with
        separate source counters so contributions from other tasks are safe.
        """
        for values in (task_ids, scope_ids):
            if not isinstance(values, tuple) or any(not isinstance(value, str) or not value for value in values):
                raise ValueError("task_ids and scope_ids must be tuples of nonempty strings")
        if not task_ids or not scope_ids:
            return
        scopes = tuple(dict.fromkeys(scope_ids))
        tasks = tuple(dict.fromkeys(task_ids))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in tasks)
            rows = conn.execute(
                f"SELECT call_id,task_id,input_tokens+output_tokens AS tokens FROM llm_workload_usage WHERE task_id IN ({placeholders})",
                tasks,
            ).fetchall()
            for row in rows:
                for scope_id in scopes:
                    added = conn.execute("INSERT OR IGNORE INTO llm_workload_quota_usage VALUES(?,?)", (row["call_id"], scope_id))
                    if added.rowcount:
                        conn.execute("""INSERT INTO llm_workload_quota_totals VALUES(?,?,1)
                            ON CONFLICT(scope_id) DO UPDATE SET total_tokens=total_tokens+excluded.total_tokens,
                            total_calls=total_calls+1""", (scope_id, row["tokens"]))
            for task_id in tasks:
                total = conn.execute("SELECT total_tokens,total_calls FROM llm_workload_quota_totals WHERE scope_id=?", ("task:" + task_id,)).fetchone()
                if total is None:
                    continue
                retained = [row for row in rows if row["task_id"] == task_id]
                compacted_tokens = max(0, total[0] - sum(row["tokens"] for row in retained))
                compacted_calls = max(0, total[1] - len(retained))
                for scope_id in scopes:
                    adopted = conn.execute("""SELECT compacted_tokens,compacted_calls
                        FROM llm_workload_quota_adoptions WHERE task_id=? AND scope_id=?""", (task_id, scope_id)).fetchone()
                    tokens = max(0, compacted_tokens - (adopted[0] if adopted else 0))
                    calls = max(0, compacted_calls - (adopted[1] if adopted else 0))
                    conn.execute("""INSERT INTO llm_workload_quota_totals VALUES(?,?,?)
                        ON CONFLICT(scope_id) DO UPDATE SET total_tokens=total_tokens+excluded.total_tokens,
                        total_calls=total_calls+excluded.total_calls""", (scope_id, tokens, calls))
                    conn.execute("""INSERT INTO llm_workload_quota_adoptions VALUES(?,?,?,?)
                        ON CONFLICT(task_id,scope_id) DO UPDATE
                        SET compacted_tokens=MAX(compacted_tokens,excluded.compacted_tokens),
                        compacted_calls=MAX(compacted_calls,excluded.compacted_calls)""",
                        (task_id, scope_id, compacted_tokens, compacted_calls))

    def work_remaining(self, scope_id: str, *, max_tokens: int | None = None, max_calls: int | None = None) -> dict[str, int | None]:
        used = self.quota_usage(scope_id)
        return {"tokens": None if max_tokens is None else max(0, max_tokens - used["total_tokens"]),
                "calls": None if max_calls is None else max(0, max_calls - used["total_calls"])}

    def reclassify_task_usage(self, task_ids: tuple[str, ...], pool: str) -> None:
        """Domain-owned migration of legacy attribution; no usage is removed."""
        if pool not in _BACKGROUND_POOLS or not isinstance(task_ids, tuple) or any(not isinstance(t, str) or not t for t in task_ids):
            raise ValueError("invalid workload attribution")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for task_id in dict.fromkeys(task_ids):
                conn.execute("UPDATE llm_workload_usage SET pool=? WHERE task_id=? AND pool!='interactive'", (pool, task_id))

    def reset_budgets(self, expected_revisions: dict[str, int], *, reason: str) -> dict[str, Any]:
        """Start new rolling accounting windows; retain history and work caps.

        Reset is explicit, CAS-checked and only allowed without active/reserved
        calls in the selected pools. It also acknowledges an emergency stop.
        """
        if not expected_revisions or set(expected_revisions) - _BACKGROUND_POOLS:
            raise ValueError("select known background pools")
        if not reason.strip() or len(reason) > 200:
            raise ValueError("reset reason must contain 1..200 characters")
        if any(type(revision) is not int or revision < 0 for revision in expected_revisions.values()):
            raise ValueError("invalid budget revision")
        now = datetime.now(UTC).isoformat()
        with self._guard, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for pool, revision in expected_revisions.items():
                state = conn.execute("SELECT revision FROM llm_workload_pool_state WHERE pool=?", (pool,)).fetchone()
                reserved = conn.execute("SELECT 1 FROM llm_workload_usage WHERE pool=? AND status='reserved' AND expires_at>? LIMIT 1", (pool, now)).fetchone()
                if self._active[pool] or reserved:
                    raise RuntimeError("selected workload pool is active")
                if state[0] != revision:
                    raise RuntimeError("budget revision changed")
            for pool, revision in expected_revisions.items():
                conn.execute("UPDATE llm_workload_pool_state SET revision=revision+1,reset_at=?,opened_at=NULL,reason=NULL WHERE pool=?", (now, pool))
                conn.execute("INSERT INTO llm_workload_budget_events(pool,created_at,kind,reason,revision) VALUES(?,?,'reset',?,?)",
                             (pool, now, reason, revision + 1))
        return self.health()

    @staticmethod
    def _pool_usage(conn: sqlite3.Connection, pool: str, since: str, *, task_id: str | None = None):
        sql = """SELECT COALESCE(SUM(u.input_tokens+u.output_tokens),0) AS tokens,
            COALESCE(SUM(u.cost),0) AS cost,COALESCE(SUM(u.cost_known=0),0) AS unknown_costs,COUNT(*) AS calls
            FROM llm_workload_usage u JOIN llm_workload_pool_state s ON s.pool=u.pool
            WHERE u.pool=? AND u.created_at>=? AND u.created_at>=s.reset_at"""
        values: tuple = (pool, since)
        if task_id is not None:
            sql += " AND u.task_id=?"
            values += (task_id,)
        return conn.execute(sql, values).fetchone()

    def _circuit_reason(self, conn: sqlite3.Connection, ticket: UsageTicket, now: datetime, extra: int) -> str | None:
        if ticket.workload.pool != "background_memory":
            return None
        state = conn.execute("SELECT opened_at,reason FROM llm_workload_pool_state WHERE pool=?", (ticket.workload.pool,)).fetchone()
        if state["opened_at"]:
            return state["reason"]
        checks = [("hourly_tokens", (now - timedelta(hours=1)).isoformat(), None),
                  ("daily_tokens", (now - timedelta(days=1)).isoformat(), None)]
        if ticket.workload.task_id:
            checks.append(("task_tokens", "", ticket.workload.task_id))
        for name, since, task_id in checks:
            used = self._pool_usage(conn, ticket.workload.pool, since, task_id=task_id)["tokens"]
            if used + extra > self.memory_fuses[name]:
                reason = f"{name}: {used + extra} > {self.memory_fuses[name]}"
                conn.execute("UPDATE llm_workload_pool_state SET opened_at=?,reason=?,revision=revision+1 WHERE pool=?", (now.isoformat(), reason, ticket.workload.pool))
                revision = conn.execute("SELECT revision FROM llm_workload_pool_state WHERE pool=?", (ticket.workload.pool,)).fetchone()[0]
                conn.execute("INSERT INTO llm_workload_budget_events(pool,created_at,kind,reason,revision) VALUES(?,?,'circuit_open',?,?)",
                             (ticket.workload.pool, now.isoformat(), reason, revision))
                _logger.error("Background token circuit opened: pool=%s %s", ticket.workload.pool, reason)
                return reason
        return None

    def _interrupt_pool(self, pool: str, *, exclude: str) -> None:
        with self._guard:
            targets = [(call_id, loop, task) for call_id, (active_pool, loop, task) in self._inflight.items()
                       if active_pool == pool and call_id != exclude and call_id not in self._interrupted and not task.done()]
            self._interrupted.update(call_id for call_id, _, _ in targets)
        for _, loop, task in targets:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # Closing worker loops are already stopping.

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

    def _ticket_cost(self, ticket: UsageTicket, incoming: int, outgoing: int) -> float | None:
        pricing = ticket.workload.pricing
        if pricing is not None:
            return (incoming * pricing.input_cost_per_million + outgoing * pricing.output_cost_per_million) / 1_000_000
        if ticket.workload.quotas:
            return None
        return self._cost(incoming, outgoing)

    def _reserve(self, ticket: UsageTicket) -> None:
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Counters need only rolling windows. Retain three months of
            # payload-free diagnostics, not an unbounded billing ledger.
            cutoff = (now - timedelta(days=90)).isoformat()
            # Preserve the attribution of already-charged links before deleting
            # detail. A later adoption must not charge these same calls again.
            conn.execute("""INSERT INTO llm_workload_quota_adoptions
                SELECT u.task_id,q.scope_id,SUM(u.input_tokens+u.output_tokens),COUNT(*)
                FROM llm_workload_usage u JOIN llm_workload_quota_usage q ON q.call_id=u.call_id
                WHERE u.created_at<? AND u.status!='reserved' AND u.task_id IS NOT NULL
                GROUP BY u.task_id,q.scope_id
                ON CONFLICT(task_id,scope_id) DO UPDATE
                SET compacted_tokens=compacted_tokens+excluded.compacted_tokens,
                compacted_calls=compacted_calls+excluded.compacted_calls""", (cutoff,))
            conn.execute("""DELETE FROM llm_workload_quota_usage WHERE call_id IN
                (SELECT call_id FROM llm_workload_usage WHERE created_at<? AND status!='reserved')""", (cutoff,))
            conn.execute("DELETE FROM llm_workload_usage WHERE created_at<? AND status!='reserved'", (cutoff,))
            # A killed process cannot retain a reservation forever. Expired
            # dispatched calls stay charged conservatively, never free retries.
            conn.execute("UPDATE llm_workload_usage SET status='estimated' WHERE status='reserved' AND expires_at<=?", (now.isoformat(),))
            tokens = ticket.input_tokens + ticket.output_tokens
            reason = self._circuit_reason(conn, ticket, now, tokens)
            if reason:
                # Persist the stop before raising; rolling budget deferrals
                # ordinarily roll back their admission transaction.
                conn.commit()
                self._interrupt_pool(ticket.workload.pool, exclude=ticket.call_id)
                raise BackgroundCircuitOpen(reason)
            quotas = self._quotas(ticket.workload)
            scoped_usage = {quota.scope_id: self._quota_usage(conn, quota.scope_id, now) for quota in quotas}
            # Exhausted lifetime allowances are permanent even when a rolling
            # limit would also prevent this dispatch.
            for quota in quotas:
                used = scoped_usage[quota.scope_id]
                if ((quota.max_tokens is not None and used["total_tokens"] + tokens > quota.max_tokens)
                        or (quota.max_calls is not None and used["total_calls"] + 1 > quota.max_calls)):
                    raise WorkBudgetExceeded("work budget exhausted")
            if ticket.workload.pool not in {"interactive", "background_memory"}:
                if self.daily_cost_limit and (self._ticket_cost(ticket, 0, 0) is None
                        or (not ticket.workload.pricing and not (self.input_price or self.output_price))):
                    exc = BackgroundBudgetDeferred("background model pricing is unknown")
                    exc.category = exc.error_category = "pricing_unknown"
                    raise exc
                for interval, limit in ((timedelta(hours=1), self.hourly_tokens), (timedelta(days=1), self.daily_tokens)):
                    used = self._pool_usage(conn, ticket.workload.pool, (now - interval).isoformat())["tokens"]
                    if limit and used + ticket.input_tokens + ticket.output_tokens > limit:
                        raise BackgroundBudgetDeferred("background token budget reached")
                cost_row = self._pool_usage(conn, ticket.workload.pool, (now - timedelta(days=1)).isoformat())
                cost_used, unknown_costs = cost_row["cost"], cost_row["unknown_costs"]
                if self.daily_cost_limit and unknown_costs:
                    exc = BackgroundBudgetDeferred("background usage pricing is unknown")
                    exc.category = exc.error_category = "pricing_unknown"
                    raise exc
                if self.daily_cost_limit and cost_used + (self._ticket_cost(ticket, ticket.input_tokens, ticket.output_tokens) or 0) > self.daily_cost_limit:
                    raise BackgroundBudgetDeferred("background cost budget reached")
            # All hierarchy checks and increments share this write transaction.
            for quota in quotas:
                used = scoped_usage[quota.scope_id]
                for interval in ("hourly", "daily"):
                    for unit, amount in (("tokens", tokens), ("calls", 1)):
                        limit = getattr(quota, interval + "_" + unit)
                        if limit is not None and used[interval + "_" + unit] + amount > limit:
                            raise BackgroundBudgetDeferred("scoped rolling budget reached")
            cost = self._ticket_cost(ticket, ticket.input_tokens, ticket.output_tokens)
            pricing = ticket.workload.pricing
            conn.execute("""INSERT INTO llm_workload_usage
                (call_id,pool,task_id,created_at,expires_at,status,input_tokens,output_tokens,cost,duration_seconds,count_method,
                 cost_known,pricing_client,pricing_model,pricing_revision)
                VALUES(?,?,?,?,?,'reserved',?,?,?,NULL,'reserved_estimate',?,?,?,?)""", (
                ticket.call_id, ticket.workload.pool, ticket.workload.task_id,
                now.isoformat(), (now + timedelta(minutes=15)).isoformat(),
                ticket.input_tokens, ticket.output_tokens, cost or 0, int(cost is not None),
                pricing.client_name if pricing else None, pricing.model if pricing else None, pricing.revision if pricing else None,
            ))
            for quota in quotas:
                conn.execute("INSERT INTO llm_workload_quota_usage VALUES(?,?)", (ticket.call_id, quota.scope_id))
                conn.execute("""INSERT INTO llm_workload_quota_totals VALUES(?,?,1)
                    ON CONFLICT(scope_id) DO UPDATE SET total_tokens=total_tokens+excluded.total_tokens,
                    total_calls=total_calls+1""", (quota.scope_id, tokens))

    def _finish(self, ticket: UsageTicket) -> str | None:
        if not ticket.dispatched:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                old = conn.execute("SELECT input_tokens+output_tokens FROM llm_workload_usage WHERE call_id=?", (ticket.call_id,)).fetchone()
                if old is not None:
                    conn.execute("""UPDATE llm_workload_quota_totals SET total_tokens=total_tokens-?,total_calls=total_calls-1
                        WHERE scope_id IN (SELECT scope_id FROM llm_workload_quota_usage WHERE call_id=?)""", (old[0], ticket.call_id))
                conn.execute("DELETE FROM llm_workload_quota_usage WHERE call_id=?", (ticket.call_id,))
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
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT input_tokens+output_tokens FROM llm_workload_usage WHERE call_id=?", (ticket.call_id,)).fetchone()
            if old is not None:
                conn.execute("""UPDATE llm_workload_quota_totals SET total_tokens=total_tokens+?
                    WHERE scope_id IN (SELECT scope_id FROM llm_workload_quota_usage WHERE call_id=?)""", (incoming + outgoing - old[0], ticket.call_id))
            cost = self._ticket_cost(ticket, incoming, outgoing)
            conn.execute("UPDATE llm_workload_usage SET status=?,input_tokens=?,output_tokens=?,cost=?,cost_known=?,duration_seconds=?,count_method=? WHERE call_id=?", (
                "failed" if ticket.failed else "completed", incoming, outgoing,
                cost or 0, int(cost is not None), time.perf_counter() - ticket.started,
                "provider_usage" if known else "conservative_estimate", ticket.call_id,
            ))
            reason = self._circuit_reason(conn, ticket, datetime.now(UTC), 0)
        if reason:
            self._interrupt_pool(ticket.workload.pool, exclude=ticket.call_id)
        return reason

    def provider_limited(self, seconds: float = 30) -> None:
        with self._guard:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    async def _settle(self, ticket: UsageTicket) -> str | None:
        # Cancellation must not release a concurrency slot before persistent
        # accounting has completed, nor let a reset race with its late write.
        settlement = asyncio.create_task(asyncio.to_thread(self._finish, ticket))
        try:
            return await asyncio.shield(settlement)
        except asyncio.CancelledError:
            await settlement
            raise

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
                        task = asyncio.current_task()
                        if task is not None:
                            self._inflight[ticket.call_id] = (workload.pool, asyncio.get_running_loop(), task)
                if not acquired:
                    if not foreground and time.monotonic() - wait_started > 5:
                        raise BackgroundBudgetDeferred("foreground work or provider cooldown takes priority")
                    await asyncio.sleep(0.025)
            reservation = asyncio.create_task(asyncio.to_thread(self._reserve, ticket))
            try:
                await asyncio.shield(reservation)
            except asyncio.CancelledError:
                await reservation
                await self._settle(ticket)
                raise
            try:
                yield ticket
            except BaseException:
                ticket.failed = True
                raise
            finally:
                reason = await self._settle(ticket)
                if reason:
                    raise BackgroundCircuitOpen(reason)
        except asyncio.CancelledError:
            with self._guard:
                interrupted = ticket.call_id in self._interrupted
            if interrupted:
                raise BackgroundCircuitOpen("background token circuit interrupted the request") from None
            raise
        finally:
            with self._guard:
                self._inflight.pop(ticket.call_id, None)
                self._interrupted.discard(ticket.call_id)
                if registered_waiter:
                    self._interactive_waiters -= 1
                if acquired:
                    self._active[workload.pool] -= 1

    def health(self) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with self._connect() as conn:
            rows = conn.execute("SELECT pool,status,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,CASE WHEN SUM(cost_known=0)>0 THEN NULL ELSE SUM(cost) END AS estimated_cost,COUNT(*) AS calls FROM llm_workload_usage WHERE created_at>=? GROUP BY pool,status", (since,)).fetchall()
            states = [dict(row) for row in conn.execute("SELECT * FROM llm_workload_pool_state ORDER BY pool")]
            now = datetime.now(UTC)
            for state in states:
                state["circuit_open"] = state["opened_at"] is not None
                state["hourly"] = dict(self._pool_usage(conn, state["pool"], (now - timedelta(hours=1)).isoformat()))
                state["daily"] = dict(self._pool_usage(conn, state["pool"], since))
            events = [dict(row) for row in conn.execute("SELECT * FROM llm_workload_budget_events ORDER BY event_id DESC LIMIT 30")]
        with self._guard:
            active = dict(self._active)
            waiters = self._interactive_waiters
        return {"active_by_pool": active, "interactive_waiters": waiters,
                "last_24h": [dict(row) for row in rows],
                "budget_pools": states, "budget_events": events,
                "cost_pricing_configured": bool(self.input_price or self.output_price),
                "limits": {"hourly_background_tokens": self.hourly_tokens, "daily_background_tokens": self.daily_tokens,
                           "daily_background_cost": self.daily_cost_limit, "ordinary_limits_apply_to": ["background_message", "background_io"],
                           "memory_emergency_thresholds": self.memory_fuses, "concurrency_by_pool": self.pool_limits}}
