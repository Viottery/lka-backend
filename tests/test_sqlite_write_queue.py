"""Adversarial contracts for the managed SQLite writer queue."""

from __future__ import annotations

import gc
import sqlite3
import threading
import time
import weakref

import pytest

from app.storage.sqlite_timing import connect_sqlite
from app.storage.sqlite_write_queue import (
    SqliteTransactionTimeoutError,
    SqliteWriteQueueError,
    WriteQueuePolicy,
    _watch_connection,
)


def policy(**overrides):
    values = {"capacity": 8, "wait_seconds": 0.4, "transaction_seconds": 1.0,
              "watchdog_seconds": 0.02}
    values.update(overrides)
    return WriteQueuePolicy(**values)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_deadline_limits_cannot_disable_the_watchdog(value):
    with pytest.raises(ValueError):
        policy(transaction_seconds=value)


def test_watchdog_does_not_retain_closed_connections(tmp_path):
    import gc

    conn = db(tmp_path / "collect.db")
    reference = weakref.ref(conn)
    conn.close()
    del conn
    deadline = time.monotonic() + 1
    while reference() is not None and time.monotonic() < deadline:
        gc.collect()
        threading.Event().wait(0.005)
    assert reference() is None


def db(path, **overrides):
    connect_options = {}
    if "isolation_level" in overrides:
        connect_options["isolation_level"] = overrides.pop("isolation_level")
    return connect_sqlite(path, write_policy=policy(**overrides), **connect_options)


def test_fifo_writer_admission_and_implicit_cte_and_cursor_writes(tmp_path):
    path = tmp_path / "fifo.db"
    setup = db(path)
    setup.execute("CREATE TABLE t(value INTEGER)").close()
    setup.close()
    first = db(path)
    second = db(path, isolation_level=None)
    third = db(path, isolation_level=None)
    first.execute("BEGIN IMMEDIATE")
    entered = []
    ready = [threading.Event(), threading.Event()]
    go = [threading.Event(), threading.Event()]

    def writer(index, conn, sql):
        ready[index].set()
        go[index].wait(2)
        conn.execute(sql).close()
        entered.append(index)
        conn.commit()

    threads = [
        threading.Thread(target=writer, args=(0, second, "INSERT INTO t VALUES (2)")),
        threading.Thread(target=writer, args=(1, third, "WITH x(v) AS (VALUES (3)) INSERT INTO t SELECT v FROM x")),
    ]
    for thread in threads:
        thread.start()
    assert all(event.wait(1) for event in ready)
    go[0].set()
    # Wait until writer 0 has queued before admitting writer 1.
    deadline = time.monotonic() + 1
    while len(second._write_queue.waiters) < 1 and time.monotonic() < deadline:
        threading.Event().wait(0.002)
    go[1].set()
    deadline = time.monotonic() + 1
    while len(second._write_queue.waiters) < 2 and time.monotonic() < deadline:
        threading.Event().wait(0.002)
    first.rollback()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert entered == [0, 1]

    # Autocommit RETURNING holds admission through result consumption.
    cur = second.execute("INSERT INTO t VALUES (4) RETURNING value")
    assert cur.fetchone() == (4,)
    assert cur.fetchone() is None
    assert third.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    first.close()
    second.close()
    third.close()


def test_releasing_writer_notifies_only_fifo_head(tmp_path):
    path = tmp_path / "head-wakeup.db"
    owner = db(path)
    owner.execute("CREATE TABLE t(value INTEGER)").close()
    owner.execute("BEGIN IMMEDIATE")
    first, second = db(path), db(path)
    first_acquired, finish_first, second_acquired = (
        threading.Event(), threading.Event(), threading.Event(),
    )
    errors = []

    def write_and_hold(conn, value, acquired, finish=None):
        try:
            conn.execute("INSERT INTO t VALUES (?)", (value,)).close()
            acquired.set()
            if finish is not None:
                finish.wait(2)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - report thread failure to the test.
            errors.append(exc)

    first_thread = threading.Thread(
        target=write_and_hold, args=(first, 1, first_acquired, finish_first),
    )
    first_thread.start()
    deadline = time.monotonic() + 1
    while len(owner._write_queue.waiters) < 1 and time.monotonic() < deadline:
        time.sleep(.002)
    assert len(owner._write_queue.waiters) == 1

    second_thread = threading.Thread(target=write_and_hold, args=(second, 2, second_acquired))
    second_thread.start()
    deadline = time.monotonic() + 1
    while len(owner._write_queue.waiters) < 2 and time.monotonic() < deadline:
        time.sleep(.002)
    assert len(owner._write_queue.waiters) == 2
    second_ticket = owner._write_queue.waiters[1]
    notified = []
    original_notify = second_ticket.condition.notify

    def record_notify(n=1):
        notified.append(n)
        return original_notify(n)

    second_ticket.condition.notify = record_notify
    owner.rollback()
    assert first_acquired.wait(1)
    assert not second_acquired.is_set()
    assert notified == []

    finish_first.set()
    assert second_acquired.wait(1)
    first_thread.join(1)
    second_thread.join(1)
    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert errors == []
    assert notified == [1]
    for conn in (owner, first, second):
        conn.close()


def test_timed_out_fifo_head_removal_wakes_next_writer(tmp_path):
    path = tmp_path / "timeout-head-wakeup.db"
    owner = db(path)
    owner.execute("CREATE TABLE t(value INTEGER)").close()
    owner.execute("BEGIN IMMEDIATE")
    head = db(path, wait_seconds=.1)
    next_writer = db(path, wait_seconds=1)
    head_result, next_acquired = [], threading.Event()

    def time_out_head():
        try:
            head.execute("INSERT INTO t VALUES (1)").close()
        except sqlite3.Error as exc:
            head_result.append(exc)

    def write_next():
        next_writer.execute("INSERT INTO t VALUES (2)").close()
        next_acquired.set()
        next_writer.commit()

    head_thread = threading.Thread(target=time_out_head)
    head_thread.start()
    deadline = time.monotonic() + 1
    while len(owner._write_queue.waiters) < 1 and time.monotonic() < deadline:
        time.sleep(.002)
    assert len(owner._write_queue.waiters) == 1
    next_thread = threading.Thread(target=write_next)
    next_thread.start()
    deadline = time.monotonic() + 1
    while len(owner._write_queue.waiters) < 2 and time.monotonic() < deadline:
        time.sleep(.002)
    assert len(owner._write_queue.waiters) == 2

    head_thread.join(1)
    assert not head_thread.is_alive()
    assert len(head_result) == 1
    assert isinstance(head_result[0], SqliteWriteQueueError)
    assert not next_acquired.is_set()
    owner.rollback()
    assert next_acquired.wait(1)
    next_thread.join(1)
    assert not next_thread.is_alive()
    assert owner.execute("SELECT value FROM t").fetchall() == [(2,)]
    for conn in (owner, head, next_writer):
        conn.close()


@pytest.mark.parametrize("sql", [
    "-- leading line comment\nBEGIN IMMEDIATE",
    "/* leading block comment */ BEGIN IMMEDIATE",
])
def test_commented_begin_immediate_joins_fifo_before_sqlite_lock(tmp_path, sql):
    path = tmp_path / "commented_begin.db"
    owner, waiter = db(path), db(path)
    owner.execute("CREATE TABLE t(value)").close()
    owner.execute("BEGIN IMMEDIATE")
    acquired = threading.Event()
    finish = threading.Event()
    outcome = []

    def begin_then_hold():
        try:
            waiter.execute(sql).close()
            acquired.set()
            finish.wait(2)
            waiter.rollback()
        except sqlite3.Error as exc:
            outcome.append(exc)

    thread = threading.Thread(target=begin_then_hold)
    thread.start()
    deadline = time.monotonic() + 1
    while len(owner._write_queue.waiters) < 1 and time.monotonic() < deadline:
        threading.Event().wait(0.002)
    assert len(owner._write_queue.waiters) == 1
    assert not acquired.is_set()

    owner.rollback()
    assert acquired.wait(1)
    external = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            external.execute("BEGIN IMMEDIATE")
    finally:
        external.close()
        finish.set()
        thread.join(2)
        owner.close()
        waiter.close()
    assert not thread.is_alive()
    assert outcome == []


def test_watchdog_cleanup_on_other_database_does_not_wait_for_blocked_udf(tmp_path):
    blocked = db(tmp_path / "blocked_udf.db", transaction_seconds=0.08,
                 watchdog_seconds=0.01)
    blocked.execute("CREATE TABLE t(value)").close()
    blocked.execute("BEGIN IMMEDIATE")
    udf_entered = threading.Event()
    release_udf = threading.Event()

    def wait_in_udf():
        udf_entered.set()
        release_udf.wait(2)
        return 1

    blocked.create_function("wait_in_udf", 0, wait_in_udf)
    watchdog_waiting_for_blocked_connection = threading.Event()
    inner_lock = blocked._connection_lock

    class WatchdogObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "sqlite-write-watchdog":
                watchdog_waiting_for_blocked_connection.set()
            inner_lock.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            inner_lock.release()

        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == "sqlite-write-watchdog":
                watchdog_waiting_for_blocked_connection.set()
            return inner_lock.acquire(*args, **kwargs)

        def release(self):
            return inner_lock.release()

    blocked._connection_lock = WatchdogObservedLock()
    query_errors = []

    def run_blocked_udf():
        try:
            blocked.execute("SELECT wait_in_udf()").fetchone()
        except sqlite3.Error as exc:
            query_errors.append(exc)

    query_thread = threading.Thread(target=run_blocked_udf)
    query_thread.start()
    try:
        assert udf_entered.wait(1)
        assert watchdog_waiting_for_blocked_connection.wait(1)

        other = db(tmp_path / "other_idle_tx.db", transaction_seconds=0.08,
                   watchdog_seconds=0.01)
        other.execute("CREATE TABLE t(value)").close()
        other.execute("BEGIN IMMEDIATE")
        # Make it eligible immediately; the watchdog is known to be blocked on
        # the first connection's serialization lock at this point.
        other._write_started = time.monotonic() - other._write_policy.transaction_seconds
        deadline = time.monotonic() + 0.5
        while other._write_queue.owner is other and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        assert other._write_queue.owner is None
        other.close()
    finally:
        release_udf.set()
        query_thread.join(2)
        blocked.close()
    assert not query_thread.is_alive()
    assert query_errors


def test_stale_watchdog_cleanup_cannot_expire_new_lease_on_same_connection(tmp_path):
    conn = db(tmp_path / "lease_generation.db", transaction_seconds=10.0,
              watchdog_seconds=0.02, isolation_level=None)
    conn.execute("CREATE TABLE t(value)").close()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO t VALUES (1)").close()
    old_generation = conn._lease_generation
    delegate = conn._connection_lock
    raced = False

    class CommitRaceLock:
        def acquire(self, *args, **kwargs):
            nonlocal raced
            if not raced and kwargs.get("blocking") is False:
                raced = True
                # Model a commit that wins after the watchdog marks this lease
                # expired but before it can acquire the connection lock.
                sqlite3.Connection.commit(conn)
                conn._finish_write()
                assert not conn._write_expired
                assert conn._write_started is None
                conn._check_live()
                conn.execute("BEGIN IMMEDIATE")
            return delegate.acquire(*args, **kwargs)

        def release(self):
            return delegate.release()

        def __enter__(self):
            delegate.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            delegate.release()

    conn._connection_lock = CommitRaceLock()
    try:
        # Keep the background watchdog from racing this deliberately injected
        # commit window; the lifecycle lock is reentrant for the direct call.
        with conn._lifecycle_lock:
            _watch_connection(
                conn,
                conn._write_started + conn._write_policy.transaction_seconds + 1,
            )
        assert raced
        assert conn._lease_generation > old_generation
        assert conn._write_queue.owner is conn
        assert conn.in_transaction
        assert not conn._write_expired
        conn.execute("INSERT INTO t VALUES (2)").close()
        conn.commit()
        assert conn.execute("SELECT value FROM t ORDER BY value").fetchall() == [(1,), (2,)]
    finally:
        conn.close()


def test_queue_full_and_wait_timeout_do_not_run_expired_write(tmp_path):
    path = tmp_path / "full.db"
    owner = db(path, capacity=1, wait_seconds=0.08)
    owner.execute("CREATE TABLE t(value)").close()
    owner.execute("BEGIN IMMEDIATE")
    queued = db(path, capacity=1, wait_seconds=0.08)
    extra = db(path, capacity=1, wait_seconds=0.08)
    outcome = []

    def blocked():
        try:
            queued.execute("INSERT INTO t VALUES (1)")
        except sqlite3.Error as exc:
            outcome.append(exc)

    thread = threading.Thread(target=blocked)
    thread.start()
    deadline = time.monotonic() + 1
    while len(owner._write_queue.waiters) < 1 and time.monotonic() < deadline:
        threading.Event().wait(0.002)
    extra_result = []

    def overflow():
        try:
            extra.execute("INSERT INTO t VALUES (2)")
        except sqlite3.Error as exc:
            extra_result.append(exc)

    overflow_thread = threading.Thread(target=overflow)
    overflow_thread.start()
    overflow_thread.join(1)
    assert len(extra_result) == 1
    assert isinstance(extra_result[0], SqliteWriteQueueError)
    assert "full" in str(extra_result[0])
    thread.join(1)
    assert len(outcome) == 1 and isinstance(outcome[0], SqliteWriteQueueError)
    owner.rollback()
    assert owner.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    owner.close()
    queued.close()
    extra.close()


def test_watchdog_rolls_back_idle_transaction_and_fences_expired_connection(tmp_path):
    path = tmp_path / "lease.db"
    first = db(path, transaction_seconds=0.12, watchdog_seconds=0.01)
    first.execute("CREATE TABLE t(value)").close()
    first.execute("BEGIN IMMEDIATE")
    first.execute("INSERT INTO t VALUES (1)").close()
    # No caller operation is needed for the watchdog to clean up the lease.
    second = db(path, transaction_seconds=0.12, watchdog_seconds=0.01)
    deadline = time.monotonic() + 1
    while second._write_queue.owner is first and time.monotonic() < deadline:
        threading.Event().wait(0.005)
    assert second._write_queue.owner is None
    with pytest.raises(SqliteTransactionTimeoutError):
        first.commit()
    with pytest.raises(SqliteTransactionTimeoutError):
        first.execute("INSERT INTO t VALUES (9)")
    second.execute("INSERT INTO t VALUES (2)").close()
    second.close()


def test_expensive_statement_is_interrupted_after_transaction_lease(tmp_path):
    conn = db(tmp_path / "interrupt.db", transaction_seconds=0.1, watchdog_seconds=0.01)
    conn.execute("CREATE TABLE t(value)").close()
    conn.execute("BEGIN IMMEDIATE")
    result = []

    def expensive():
        try:
            conn.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n").fetchone()
        except sqlite3.Error as exc:
            result.append(exc)

    thread = threading.Thread(target=expensive)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert result and isinstance(result[0], sqlite3.Error)
    with pytest.raises(SqliteTransactionTimeoutError):
        conn.commit()
    conn.close()


def test_readers_and_other_databases_are_not_serialized(tmp_path):
    first = db(tmp_path / "one.db")
    first.execute("PRAGMA journal_mode=WAL").fetchone()
    first.execute("CREATE TABLE t(value)").close()
    first.execute("BEGIN IMMEDIATE")
    first.execute("INSERT INTO t VALUES (1)").close()
    other = db(tmp_path / "two.db")
    other.execute("CREATE TABLE t(value)").close()
    other.execute("INSERT INTO t VALUES (2)").close()
    assert other.execute("SELECT value FROM t").fetchone() == (2,)
    reader = db(tmp_path / "one.db")
    assert reader.execute("SELECT count(*) FROM t").fetchone() == (0,)
    first.rollback()
    for conn in (first, other, reader):
        conn.close()


def test_schema_implicit_and_cursor_write_admission_releases_on_fetch_close(tmp_path):
    path = tmp_path / "implicit.db"
    a, b = db(path, isolation_level=None), db(path, isolation_level=None)
    cur = a.execute("CREATE TABLE t(value)")
    # A RETURNING cursor owns admission until it is exhausted or closed.
    cur.close()
    cur = a.execute("INSERT INTO t VALUES (1),(2) RETURNING value")
    assert cur.fetchone() == (1,)
    assert a._write_queue.owner is a
    cur.close()
    assert a._write_queue.owner is None
    b.execute("DROP TABLE t").close()
    a.close()
    b.close()


def test_scripts_rollback_context_failure_and_close_release_admission(tmp_path):
    conn = db(tmp_path / "cleanup.db")
    conn.executescript("CREATE TABLE t(value UNIQUE); INSERT INTO t VALUES (1);")
    assert conn._write_queue.owner is None
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("INSERT INTO t VALUES (2)").close()
        conn.execute("INSERT INTO t VALUES (1)").close()
    assert conn._write_queue.owner is None
    conn.execute("BEGIN IMMEDIATE")
    conn.rollback()
    assert conn._write_queue.owner is None
    conn.execute("BEGIN IMMEDIATE")
    conn.close()
    assert conn._write_queue.owner is None


def test_expired_context_exit_raises_timeout_after_watchdog_cleanup(tmp_path):
    conn = db(tmp_path / "expired_exit.db", transaction_seconds=0.1, watchdog_seconds=0.01)
    conn.execute("CREATE TABLE t(value)").close()
    conn.execute("BEGIN IMMEDIATE")
    deadline = time.monotonic() + 1
    while conn._write_queue.owner is conn and time.monotonic() < deadline:
        threading.Event().wait(0.005)
    with pytest.raises(SqliteTransactionTimeoutError):
        conn.__exit__(None, None, None)
    conn.close()


def test_autocommit_returning_commit_keeps_lease_until_cursor_finishes(tmp_path):
    conn = db(tmp_path / "returning.db", isolation_level=None)
    conn.execute("CREATE TABLE t(value)").close()
    cur = conn.execute("INSERT INTO t VALUES (1),(2) RETURNING value")
    assert cur.fetchone() == (1,)
    assert conn._write_queue.owner is conn
    conn.commit()
    assert conn._write_queue.owner is conn
    assert cur.fetchall() == [(2,)]
    assert conn._write_queue.owner is None
    conn.close()


def test_pragma_row_cursor_collection_does_not_leak_write_lease(tmp_path):
    conn = db(tmp_path / "pragma.db")
    cur = conn.execute("PRAGMA journal_mode=WAL")
    cur.fetchone()
    del cur
    gc.collect()
    assert conn._write_queue.owner is None
    conn.close()


def test_path_aliases_share_queue_and_same_thread_nested_write_fails_fast(tmp_path):
    path = tmp_path / "identity.db"
    first = db(path)
    alias = db(tmp_path / "." / "identity.db")
    first.execute("BEGIN IMMEDIATE")
    with pytest.raises(SqliteWriteQueueError, match="nested"):
        alias.execute("CREATE TABLE t(value)")
    first.rollback()
    first.close()
    alias.close()

    uri_path = (tmp_path / "uri.db").resolve().as_uri() + "?mode=rwc"
    uri_a = connect_sqlite(uri_path, uri=True, write_policy=policy())
    uri_b = connect_sqlite(uri_path, uri=True, write_policy=policy())
    uri_a.execute("CREATE TABLE t(value)").close()
    uri_a.execute("BEGIN IMMEDIATE")
    with pytest.raises(SqliteWriteQueueError, match="nested"):
        uri_b.execute("INSERT INTO t VALUES (1)")
    uri_a.rollback()
    uri_a.close()
    uri_b.close()
