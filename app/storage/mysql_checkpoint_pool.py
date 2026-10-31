"""Short-lived MySQL checkpoint connection leases.

Logical LangGraph saver instances keep this pool/provider, never a connection.
The sync pool is process-wide per runtime; async pools are owned by their event
loop and share a cancellation-safe four-checkout process budget.
"""
from __future__ import annotations

import asyncio
import threading
import time
import weakref
from collections import deque
from contextlib import asynccontextmanager, contextmanager


class _AsyncCheckoutBudget:
    """Cancellation-safe capacity shared by async pools on different loops."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._active = 0
        self._lock = threading.Lock()
        self._waiters: deque[dict] = deque()

    async def acquire(self, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        waiter = {"loop": loop, "future": future, "granted": False, "cancelled": False}
        with self._lock:
            if self._active < self._capacity and not self._waiters:
                self._active += 1
                return
            self._waiters.append(waiter)
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except BaseException:
            if not future.done():
                future.cancel()
            return_permit = False
            with self._lock:
                if waiter["granted"]:
                    return_permit = True
                else:
                    waiter["cancelled"] = True
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
            if return_permit:
                self.release()
            raise

    def release(self) -> None:
        recipient = None
        with self._lock:
            while self._waiters:
                candidate = self._waiters.popleft()
                if candidate["cancelled"]:
                    continue
                if candidate["loop"].is_closed():
                    candidate["cancelled"] = True
                    continue
                candidate["granted"] = True
                recipient = candidate
                break
            if recipient is None:
                if self._active <= 0:
                    raise RuntimeError("async checkpoint connection budget released twice")
                self._active -= 1
        if recipient is not None:
            loop = recipient["loop"]
            future = recipient["future"]

            def wake() -> None:
                if not future.done():
                    future.set_result(True)

            try:
                loop.call_soon_threadsafe(wake)
            except RuntimeError:
                with self._lock:
                    recipient["cancelled"] = True
                    recipient["granted"] = False
                self.release()


class MySQLCheckpointPool:
    def __init__(self, connect_args: dict[str, object], *, sync_size: int = 4,
                 async_size: int = 4, pool_wait_seconds: float = 5.0,
                 idle_timeout_seconds: int = 300) -> None:
        self._connect_args = dict(connect_args)
        self._sync_size = sync_size
        self._async_size = async_size
        self._pool_wait_seconds = pool_wait_seconds
        self._idle_timeout_seconds = idle_timeout_seconds

        self._sync_condition = threading.Condition()
        self._sync_idle = deque()
        self._sync_created = 0
        self._closed = False

        self._async_checkout_budget = _AsyncCheckoutBudget(async_size)
        self._async_pools: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._async_pool_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._async_pool_map_lock = threading.RLock()

    def _connect_sync(self):
        import pymysql
        from pymysql.cursors import DictCursor

        return pymysql.connect(**self._connect_args, cursorclass=DictCursor)

    def _create_sync_connection(self):
        with self._sync_condition:
            if self._closed:
                raise RuntimeError("MySQL checkpoint pool is closed")
            self._sync_created += 1
        try:
            return self._connect_sync()
        except BaseException:
            with self._sync_condition:
                self._sync_created -= 1
                self._sync_condition.notify()
            raise

    def _acquire_sync(self):
        deadline = time.monotonic() + self._pool_wait_seconds
        while True:
            with self._sync_condition:
                if self._closed:
                    raise RuntimeError("MySQL checkpoint pool is closed")
                if self._sync_idle:
                    connection = self._sync_idle.pop()
                    create = False
                elif self._sync_created < self._sync_size:
                    self._sync_created += 1
                    connection = None
                    create = True
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Timed out waiting for a MySQL checkpoint connection.")
                    self._sync_condition.wait(remaining)
                    continue
            if create:
                try:
                    return self._connect_sync()
                except BaseException:
                    with self._sync_condition:
                        self._sync_created -= 1
                        self._sync_condition.notify()
                    raise
            try:
                connection.ping(reconnect=False)
                return connection
            except Exception:  # noqa: BLE001 - an unverified socket is discarded.
                connection.close()
                with self._sync_condition:
                    self._sync_created -= 1
                    self._sync_condition.notify()

    @staticmethod
    def _sync_connection_is_known_clean(connection) -> bool:
        """Skip cleanup SQL only when PyMySQL proves autocommit and no txn."""
        try:
            return (
                connection.get_autocommit() is True
                and isinstance(connection.server_status, int)
                and not (connection.server_status & 0x0001)  # SERVER_STATUS_IN_TRANS
            )
        except Exception:  # noqa: BLE001 - uncertain state requires rollback.
            return False

    def _release_sync(self, connection, *, reusable: bool = True) -> None:
        if reusable and not self._sync_connection_is_known_clean(connection):
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001 - a failed reset must not be pooled.
                reusable = False
        with self._sync_condition:
            if self._closed:
                reusable = False
            if reusable:
                self._sync_idle.append(connection)
            else:
                self._sync_created -= 1
            self._sync_condition.notify()
        if not reusable:
            connection.close()

    @contextmanager
    def sync_connection(self):
        connection = self._acquire_sync()
        try:
            yield connection
        except BaseException:
            self._release_sync(connection, reusable=False)
            raise
        else:
            self._release_sync(connection)

    async def _acquire_async_budget(self) -> None:
        await self._async_checkout_budget.acquire(self._pool_wait_seconds)

    async def _async_pool(self):
        loop = asyncio.get_running_loop()
        with self._async_pool_map_lock:
            pool = self._async_pools.get(loop)
            lock = self._async_pool_locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._async_pool_locks[loop] = lock
        if pool is not None:
            return pool
        async with lock:
            with self._async_pool_map_lock:
                pool = self._async_pools.get(loop)
            if pool is not None:
                return pool
            if self._closed:
                raise RuntimeError("MySQL checkpoint pool is closed")
            # AioMySQL pools are event-loop-affine. If this runtime gains a
            # second loop, prune idle sockets in existing pools before the
            # new pool can create sockets; active checkouts are bounded by the
            # shared four-operation budget below.
            await self._clear_other_loop_idle_pools_safely(loop)
            import aiomysql
            from aiomysql.cursors import DictCursor

            args = dict(self._connect_args)
            args["db"] = args.pop("database")
            # aiomysql.connect has no read_timeout parameter. The server-side
            # lock/statement/idle bounds and cursor deadline remain active.
            args.pop("read_timeout", None)
            args["cursorclass"] = DictCursor
            pool = await aiomysql.create_pool(
                minsize=0,
                maxsize=self._async_size,
                pool_recycle=self._idle_timeout_seconds,
                **args,
            )
            with self._async_pool_map_lock:
                self._async_pools[loop] = pool
            await self._clear_other_loop_idle_pools_safely(loop)
            return pool

    async def _clear_other_loop_idle_pools_safely(self, current_loop) -> None:
        task = current_loop.create_task(self._clear_other_loop_idle_pools(current_loop))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Finish capacity cleanup before returning the shared async permit.
            await task
            raise

    async def _clear_other_loop_idle_pools(self, current_loop) -> None:
        with self._async_pool_map_lock:
            pools = list(self._async_pools.items())
        if not pools:
            return
        for owner_loop, pool in pools:
            if owner_loop is current_loop:
                continue
            if owner_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(pool.clear(), owner_loop)
                await asyncio.wrap_future(future)
            else:
                # A stopped loop cannot reuse its pool. terminate() closes its
                # leased and idle sockets without touching another loop.
                pool.terminate()
                with self._async_pool_map_lock:
                    self._async_pools.pop(owner_loop, None)

    async def _clear_all_idle_pools(self, current_loop) -> None:
        with self._async_pool_map_lock:
            pools = list(self._async_pools.items())
        for owner_loop, pool in pools:
            if owner_loop is current_loop:
                await pool.clear()
            elif owner_loop.is_running():
                try:
                    future = asyncio.run_coroutine_threadsafe(pool.clear(), owner_loop)
                    await asyncio.wrap_future(future)
                except RuntimeError:
                    # The loop may stop between is_running() and submission.
                    pool.terminate()
                    with self._async_pool_map_lock:
                        self._async_pools.pop(owner_loop, None)
            else:
                pool.terminate()
                with self._async_pool_map_lock:
                    self._async_pools.pop(owner_loop, None)

    async def _clear_all_idle_pools_safely(self, current_loop) -> None:
        task = current_loop.create_task(self._clear_all_idle_pools(current_loop))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Keep the shared permit until cleanup has completed, even when the
            # caller is cancelled while returning its connection lease.
            await task
            raise

    @asynccontextmanager
    async def async_connection(self):
        await self._acquire_async_budget()
        pool = None
        try:
            pool = await self._async_pool()
            await self._clear_other_loop_idle_pools_safely(asyncio.get_running_loop())
            async with pool.acquire() as connection:
                yield connection
        finally:
            try:
                with self._async_pool_map_lock:
                    multiple_loop_pools = len(self._async_pools) > 1
                if pool is not None and multiple_loop_pools:
                    # Runs on success, operation failure, and cancellation;
                    # the lease has already returned to its pool at this point.
                    await self._clear_all_idle_pools_safely(asyncio.get_running_loop())
            finally:
                self._async_checkout_budget.release()

    def close(self) -> None:
        with self._sync_condition:
            if self._closed:
                return
            self._closed = True
            idle = list(self._sync_idle)
            self._sync_idle.clear()
            self._sync_created -= len(idle)
            self._sync_condition.notify_all()
        for connection in idle:
            connection.close()

        with self._async_pool_map_lock:
            pools = list(self._async_pools.items())
            self._async_pools.clear()
        for loop, pool in pools:
            if loop.is_running():
                def close_on_owner_loop(target=pool, owner_loop=loop):
                    target.close()
                    owner_loop.create_task(target.wait_closed())

                loop.call_soon_threadsafe(close_on_owner_loop)
            else:
                pool.terminate()

    async def aclose(self) -> None:
        """Close pools and await socket shutdown when called from async lifespan."""
        with self._sync_condition:
            if self._closed:
                return
            self._closed = True
            idle = list(self._sync_idle)
            self._sync_idle.clear()
            self._sync_created -= len(idle)
            self._sync_condition.notify_all()
        for connection in idle:
            connection.close()

        with self._async_pool_map_lock:
            pools = list(self._async_pools.items())
            self._async_pools.clear()
        current_loop = asyncio.get_running_loop()
        for owner_loop, pool in pools:
            if owner_loop is current_loop:
                pool.close()
                await pool.wait_closed()
            elif owner_loop.is_running():
                async def close_on_owner_loop(target=pool):
                    target.close()
                    await target.wait_closed()

                future = asyncio.run_coroutine_threadsafe(
                    close_on_owner_loop(), owner_loop
                )
                await asyncio.wrap_future(future)
            else:
                pool.terminate()
