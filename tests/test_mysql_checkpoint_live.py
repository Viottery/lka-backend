"""Real sync/async LangGraph durability with a mock-free deterministic graph."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.core.agent_checkpoints import create_mysql_checkpoint_runtime
from app.storage.mysql import MySQLConfig, connect_mysql
from tests.test_mysql_owner_live import _config


class State(TypedDict):
    value: int


def _builder():
    builder = StateGraph(State)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    def gate(state):
        accepted = interrupt("synthetic confirmation")
        return {"value": state["value"] + int(accepted)}
    builder.add_node("gate", gate)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", "gate")
    builder.add_edge("gate", END)
    return builder


def _assert_sync_session_limits(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT @@SESSION.innodb_lock_wait_timeout AS lock_wait, "
            "@@SESSION.wait_timeout AS idle, "
            "@@SESSION.max_execution_time AS select_ms, "
            "@@SESSION.time_zone AS time_zone"
        )
        row = cursor.fetchone()
        lock_wait, idle, select_ms, time_zone = (
            row["lock_wait"], row["idle"], row["select_ms"], row["time_zone"]
        )
    assert lock_wait == 5
    assert idle == 300
    assert select_ms == 30000
    assert time_zone == "+00:00"


async def _assert_async_session_limits(connection):
    async with connection.cursor() as cursor:
        await cursor.execute(
            "SELECT @@SESSION.innodb_lock_wait_timeout AS lock_wait, "
            "@@SESSION.wait_timeout AS idle, "
            "@@SESSION.max_execution_time AS select_ms, "
            "@@SESSION.time_zone AS time_zone"
        )
        row = await cursor.fetchone()
        lock_wait, idle, select_ms, time_zone = (
            row["lock_wait"], row["idle"], row["select_ms"], row["time_zone"]
        )
    assert lock_wait == 5
    assert idle == 300
    assert select_ms == 30000
    assert time_zone == "+00:00"


def test_sync_checkpoint_reopens_and_resumes_without_replaying_first_node():
    config = _config()
    thread = {"configurable": {"thread_id": "test-checkpoint-" + uuid4().hex}}
    first = create_mysql_checkpoint_runtime(config)
    first.verify_schema()
    try:
        with first.sync_saver() as saver:
            with first._checkpoint_pool.sync_connection() as connection:
                _assert_sync_session_limits(connection)
            graph = _builder().compile(checkpointer=saver)
            paused = graph.invoke({"value": 0}, thread)
            assert paused["value"] == 1
            assert paused["__interrupt__"]
    finally:
        first.close()
    second = create_mysql_checkpoint_runtime(config)
    second.verify_schema()
    try:
        with second.sync_saver() as saver:
            graph = _builder().compile(checkpointer=saver)
            assert graph.invoke(Command(resume=2), thread)["value"] == 3
            assert graph.get_state(thread).next == ()
            saver.delete_thread(thread["configurable"]["thread_id"])
    finally:
        second.close()


def test_async_checkpoint_reopens_and_resumes_without_replaying_first_node():
    config = _config()
    thread = {"configurable": {"thread_id": "test-checkpoint-" + uuid4().hex}}
    async def scenario():
        first = create_mysql_checkpoint_runtime(config)
        first.verify_schema()
        try:
            async with first.async_saver() as saver:
                async with first._checkpoint_pool.async_connection() as connection:
                    await _assert_async_session_limits(connection)
                graph = _builder().compile(checkpointer=saver)
                paused = await graph.ainvoke({"value": 0}, thread)
                assert paused["value"] == 1 and paused["__interrupt__"]
        finally:
            await first.aclose()
        second = create_mysql_checkpoint_runtime(config)
        second.verify_schema()
        try:
            async with second.async_saver() as saver:
                graph = _builder().compile(checkpointer=saver)
                assert (await graph.ainvoke(Command(resume=2), thread))["value"] == 3
                assert (await graph.aget_state(thread)).next == ()
                await saver.adelete_thread(thread["configurable"]["thread_id"])
        finally:
            await second.aclose()
    asyncio.run(scenario())


def test_checkpoint_logical_savers_hold_no_connection_between_operations():
    config = _config()
    runtime = create_mysql_checkpoint_runtime(config)
    runtime.verify_schema()

    async def scenario():
        try:
            async with AsyncExitStack() as stack:
                savers = [await stack.enter_async_context(runtime.async_saver()) for _ in range(16)]
                pool = await runtime._checkpoint_pool._async_pool()
                assert pool.size == 0  # saver construction reserves no database connection

                async def save(saver):
                    async with saver._cursor(pipeline=True):
                        pass

                await asyncio.gather(*(save(saver) for saver in savers))
                assert pool.maxsize == 4
                assert pool.size <= 4
                assert len(pool._used) == 0
                checked_out = pool.size - pool.freesize
                assert checked_out == 0
                # A long model/tool wait occurs outside `_cursor`; only idle pool
                # connections remain, never checked out by the logical saver.
                size_before_wait = pool.size
                await asyncio.sleep(0.1)
                assert pool.size == size_before_wait
                assert len(pool._used) == 0
        finally:
            await runtime.aclose()

    asyncio.run(scenario())

    sync_runtime = create_mysql_checkpoint_runtime(config)
    sync_runtime.verify_schema()
    try:
        with ExitStack() as stack:
            savers = [stack.enter_context(sync_runtime.sync_saver()) for _ in range(16)]
            assert sync_runtime._checkpoint_pool._sync_created == 0
            for saver in savers:
                with saver._cursor(pipeline=True):
                    pass
                with sync_runtime._checkpoint_pool._sync_condition:
                    assert sync_runtime._checkpoint_pool._sync_created <= 4
    finally:
        sync_runtime.close()


def test_checkpoint_writes_are_fenced_after_coordinator_connection_loss():
    import pytest

    from app.storage.mysql import MySQLOperationalError
    from app.storage.mysql_owner import MySQLCoordinatorOwner

    config = _config()
    owner = MySQLCoordinatorOwner(config)
    replacement = MySQLCoordinatorOwner(config)
    assert owner.acquire()
    first = create_mysql_checkpoint_runtime(config)
    second = create_mysql_checkpoint_runtime(config)
    thread = {"configurable": {"thread_id": "test-checkpoint-fence-" + uuid4().hex}}
    try:
        first.verify_schema()
        first.set_coordinator_owner(owner)
        with first.sync_saver() as saver:
            graph = _builder().compile(checkpointer=saver)
            assert graph.invoke({"value": 0}, thread)["__interrupt__"]
            owner._connection.close()
            with pytest.raises(MySQLOperationalError, match="generation changed"):
                graph.invoke(Command(resume=2), thread)
        assert replacement.acquire()
        second.verify_schema()
        second.set_coordinator_owner(replacement)
        with second.sync_saver() as saver:
            graph = _builder().compile(checkpointer=saver)
            assert graph.invoke(Command(resume=2), thread)["value"] == 3
            saver.delete_thread(thread["configurable"]["thread_id"])
    finally:
        first.close()
        second.close()
        owner.release()
        replacement.release()


def test_async_checkpoint_cursor_deadline_closes_blocked_connection():
    migration_file = os.environ.get("LKA_MYSQL_TEST_MIGRATION_CONFIG")
    if not migration_file:
        pytest.skip("dedicated migration config not supplied for isolated InnoDB fixture")

    app_config = _config()
    migration_config = MySQLConfig(**json.loads(Path(migration_file).read_text(encoding="utf-8")))
    table = "lka_checkpoint_timeout_" + uuid4().hex
    quoted_table = f"`{table}`"
    with connect_mysql(migration_config) as conn:
        conn.execute_native(
            f"CREATE TABLE {quoted_table} (id INT NOT NULL PRIMARY KEY) ENGINE=InnoDB"
        )
        conn.execute_native(f"INSERT INTO {quoted_table} (id) VALUES (1)")

    blocker = connect_mysql(app_config)
    blocker.execute("BEGIN")
    blocker.execute_native(f"SELECT id FROM {quoted_table} WHERE id = 1 FOR UPDATE").fetchone()

    bounded_config = dataclasses.replace(
        app_config, transaction_seconds=0.15, lock_wait_seconds=1
    )
    runtime = create_mysql_checkpoint_runtime(bounded_config)
    runtime.verify_schema()

    async def blocked_operation():
        try:
            async with runtime.async_saver() as saver:
                pool = await runtime._checkpoint_pool._async_pool()
                started = time.monotonic()
                with pytest.raises(TimeoutError, match="exceeded its deadline"):
                    async with saver._cursor(pipeline=True) as cursor:
                        await cursor.execute(
                            f"SELECT id FROM {quoted_table} WHERE id = 1 FOR UPDATE"
                        )
                elapsed = time.monotonic() - started
                assert elapsed < 1.0, f"async checkpoint cursor timeout took {elapsed:.3f}s"
                assert pool.size == 0  # the timed-out socket was discarded, not returned idle
                async with (
                    runtime._checkpoint_pool.async_connection() as connection,
                    connection.cursor() as cursor,
                ):
                    await cursor.execute("SELECT 1 AS probe")
                    assert (await cursor.fetchone())["probe"] == 1
        finally:
            await runtime.aclose()

    try:
        asyncio.run(blocked_operation())
    finally:
        blocker.rollback()
        blocker.close()
        with connect_mysql(migration_config) as conn:
            conn.execute_native(f"DROP TABLE {quoted_table}")

    with connect_mysql(app_config) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
