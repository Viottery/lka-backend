from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from app.storage import mysql

pytest.importorskip("sqlglot")

class FakeDriverError(Exception):
    def __init__(self, errno, message="synthetic failure", sqlstate=None):
        super().__init__(errno, message)
        self.errno = errno
        self.sqlstate = sqlstate


class FakeCursor:
    def __init__(self, raw):
        self.raw = raw
        self.description = None
        self.rowcount = -1
        self.lastrowid = None
        self.arraysize = 1
        self.rows = []
        self.closed = False

    def execute(self, statement, parameters=()):
        self.raw.statements.append((statement, tuple(parameters or ())))
        if self.raw.fail_execute is not None:
            error, self.raw.fail_execute = self.raw.fail_execute, None
            raise error
        normalized = statement.upper()
        if "@@SESSION.SQL_MODE" in normalized:
            self.description = (("@@SESSION.sql_mode",),)
            self.rows = [("",)]
        elif normalized.startswith("SELECT ID, NAME"):
            self.description = (("id",), ("name",))
            self.rows = [(7, "sample")]
        else:
            self.description = None
            self.rows = []
            self.rowcount = 1 if normalized.startswith(("INSERT", "UPDATE", "DELETE")) else -1
            self.lastrowid = 7 if normalized.startswith("INSERT") else None
        return self

    def executemany(self, statement, parameters):
        values = list(parameters)
        self.raw.statements.append((statement, values))
        self.rowcount = len(values)
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchmany(self, size=None):
        size = self.arraysize if size is None else size
        result, self.rows = self.rows[:size], self.rows[size:]
        return result

    def fetchall(self):
        result, self.rows = self.rows, []
        return result

    def close(self):
        self.closed = True

    def __iter__(self):
        return iter(self.fetchall())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeRawConnection:
    def __init__(self):
        self.statements = []
        self.in_transaction = False
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.ping_count = 0
        self.closed = False
        self.fail_execute = None
        self.fail_commit = None

    def cursor(self):
        return FakeCursor(self)

    def begin(self):
        self.begin_count += 1
        self.in_transaction = True

    def commit(self):
        self.commit_count += 1
        if self.fail_commit is not None:
            error, self.fail_commit = self.fail_commit, None
            raise error
        self.in_transaction = False

    def rollback(self):
        self.rollback_count += 1
        self.in_transaction = False

    def ping(self, reconnect=False):
        self.ping_count += 1
        if self.closed:
            raise FakeDriverError(2006)

    def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self):
        self.connections = []
        self.options = []

    def connect(self, **kwargs):
        self.options.append(kwargs)
        connection = FakeRawConnection()
        self.connections.append(connection)
        return connection


@pytest.fixture
def fake_driver(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(mysql, "_load_driver", lambda: driver)
    yield driver
    mysql.close_mysql_pools()


def config(**overrides):
    values = {
        "host": "127.0.0.1", "port": 3306, "user": "unit",
        "password": "secret-value", "database": "lka_test",
        "pool_size": 2, "pool_wait_seconds": 0.1,
    }
    values.update(overrides)
    return mysql.MySQLConfig(**values)


def test_config_hides_password_and_validates_bounds():
    value = config()
    assert "secret-value" not in repr(value)
    with pytest.raises(ValueError, match="pool_size"):
        config(pool_size=0)
    with pytest.raises(ValueError, match="port"):
        config(port=70000)


def test_row_supports_sqlite_row_access_shapes():
    row = mysql.MySQLRow(("id", "name"), (4, "Ada"))
    assert row[0] == row["id"] == 4
    assert row.keys() == ["id", "name"]
    assert dict(row) == {"id": 4, "name": "Ada"}
    id_value, name_value = row
    assert (id_value, name_value) == (4, "Ada")


def test_connection_sets_required_session_modes_and_maps_rows(fake_driver):
    conn = mysql.connect_mysql(config(), read_only=True)
    raw = fake_driver.connections[0]
    assert fake_driver.options[0]["autocommit"] is True
    setup = "\n".join(sql for sql, _ in raw.statements)
    assert "SET NAMES utf8mb4 COLLATE utf8mb4_0900_bin" in setup
    assert "READ COMMITTED" in setup
    assert "innodb_lock_wait_timeout" in setup
    assert ("SET SESSION collation_connection = 'utf8mb4_0900_bin'", ()) in raw.statements
    assert ("SET SESSION time_zone = '+00:00'", ()) in raw.statements
    assert ("SET SESSION wait_timeout = %s", (30,)) in raw.statements
    assert "TRANSACTION READ ONLY" in setup
    assert "STRICT_TRANS_TABLES" in raw.statements[-2][1][0]
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id,name FROM sample").fetchone()
    assert row[0] == row["id"] == 7
    assert dict(row) == {"id": 7, "name": "sample"}
    conn.close()


def test_sqlite_row_factory_maps_synthetic_pragma_and_catalog_rows(fake_driver):
    conn = mysql.connect_mysql(config())
    conn.row_factory = sqlite3.Row

    class Schema:
        @staticmethod
        def pragma_table_info_query(_conn, _statement, _parameters=()):
            return ("cid", "name", "type"), [(0, "id", "TEXT")]

        @staticmethod
        def catalog_query(_conn, _statement, _parameters=()):
            return ("name", "sql"), [("items", "CREATE TABLE items(id TEXT)")]

    conn._schema = Schema()
    pragma = conn.execute("PRAGMA table_info('items')").fetchone()
    assert pragma["name"] == "id"
    assert pragma.keys() == ["cid", "name", "type"]
    catalog = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchone()
    assert catalog["name"] == "items"
    assert dict(catalog) == {"name": "items", "sql": "CREATE TABLE items(id TEXT)"}
    conn.close()


def test_dml_starts_local_transaction_and_close_resets_before_pool_return(fake_driver):
    conn = mysql.connect_mysql(config())
    raw = fake_driver.connections[0]
    conn.execute("UPDATE sample SET name=? WHERE id=?", ("x", 1))
    assert conn.in_transaction and raw.begin_count == 1
    assert raw.statements[-1][0].replace(" ", "").upper() == "UPDATEsampleSETname=%sWHEREid=%s".upper()
    conn.close()
    assert not raw.in_transaction
    again = mysql.connect_mysql(config())
    assert len(fake_driver.connections) == 1
    again.close()


def test_transaction_guard_runs_once_after_start_and_not_for_reads(fake_driver):
    conn = mysql.connect_mysql(config())
    raw = fake_driver.connections[0]
    guarded = []

    def guard(active):
        guarded.append("checked")
        active.execute_native("SELECT 1")

    conn.set_transaction_guard(guard)
    conn.execute("SELECT id FROM sample")
    assert guarded == []
    conn.execute("UPDATE sample SET name='first'")
    conn.execute("UPDATE sample SET name='second'")
    assert guarded == ["checked"]
    assert raw.statements.count(("SELECT 1", ())) == 1
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    assert guarded == ["checked", "checked"]
    conn.rollback()
    conn.close()


def test_transaction_guard_failure_rolls_back_before_first_write(fake_driver):
    conn = mysql.connect_mysql(config())
    raw = fake_driver.connections[0]
    conn.set_transaction_guard(lambda _active: (_ for _ in ()).throw(RuntimeError("stale owner")))
    with pytest.raises(mysql.MySQLTransactionGuardError) as caught:
        conn.execute("UPDATE sample SET name='blocked'")
    assert caught.value.category == "transaction_guard_rejected"
    assert not conn.in_transaction
    assert raw.rollback_count >= 1
    assert not any(sql.startswith("UPDATE sample") for sql, _ in raw.statements)
    conn.close()


def test_targeted_upsert_hook_exposes_exact_rowcount_and_resets_cursor_overrides(fake_driver, monkeypatch):
    from app.storage import mysql_upsert

    conn = mysql.connect_mysql(config())
    conn._schema = object()
    monkeypatch.setattr(
        mysql_upsert, "execute_targeted_upsert",
        lambda _conn, _statement, _parameters: mysql_upsert.UpsertOutcome(1, 17),
    )
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO t(id) VALUES(?)", (1,))
    assert cursor.rowcount == 1 and cursor.lastrowid == 17

    monkeypatch.setattr(mysql_upsert, "execute_targeted_upsert", lambda *_args: None)
    cursor.execute("UPDATE sample SET name=?", ("next",))
    assert cursor.lastrowid is None
    cursor.close()
    conn.close()


def test_executemany_targeted_upserts_iterate_each_row_for_sqlite_ignore_semantics(fake_driver, monkeypatch):
    from app.storage import mysql_upsert

    conn = mysql.connect_mysql(config())
    conn._schema = object()
    seen = []

    def handle(_conn, statement, parameters):
        seen.append(tuple(parameters))
        return mysql_upsert.UpsertOutcome(1 if parameters[0] == 1 else 0, None)

    monkeypatch.setattr(mysql_upsert, "execute_targeted_upsert", handle)
    cursor = conn.cursor()
    cursor.executemany("INSERT OR IGNORE INTO t(id) VALUES(?)", [(1,), (2,)])
    assert seen == [(1,), (2,)]
    assert cursor.rowcount == 1 and cursor.lastrowid is None
    cursor.close()
    conn.close()


def test_context_manager_commits_and_releases_pool_lease(fake_driver):
    raw = None
    with mysql.connect_mysql(config()) as conn:
        raw = fake_driver.connections[0]
        conn.execute("INSERT INTO sample(name) VALUES(?)", ("x",))
        assert conn.in_transaction
    assert conn.closed
    assert raw.commit_count == 2  # session setup and the user transaction
    again = mysql.connect_mysql(config())
    assert len(fake_driver.connections) == 1
    again.close()


def test_pool_wait_is_bounded_without_global_write_serialization(fake_driver):
    cfg = config(pool_size=1, pool_wait_seconds=0.03)
    first = mysql.connect_mysql(cfg)
    outcomes = []

    def second_checkout():
        with pytest.raises(mysql.MySQLPoolTimeout):
            mysql.connect_mysql(cfg)
        outcomes.append("timed-out")

    thread = threading.Thread(target=second_checkout)
    thread.start()
    thread.join(timeout=0.5)
    assert not thread.is_alive() and outcomes == ["timed-out"]
    first.close()
    second = mysql.connect_mysql(cfg)
    second.close()
    assert len(fake_driver.connections) == 1


def test_transaction_duration_rolls_back_and_abandons_lease(fake_driver, caplog):
    conn = mysql.connect_mysql(config(transaction_seconds=0.01))
    raw = fake_driver.connections[0]
    conn.execute("UPDATE sample SET name='x'")
    time.sleep(0.02)
    with pytest.raises(mysql.MySQLTransactionTimeout):
        conn.execute("SELECT id FROM sample")
    conn.close()
    assert raw.rollback_count >= 1 and raw.closed
    diagnostic = next(record for record in caplog.records if "deadline exceeded" in record.message)
    assert "before READ" in diagnostic.message and diagnostic.stack_info
    assert "UPDATE sample" not in diagnostic.message


@pytest.mark.parametrize("errno,expected", [
    (1205, mysql.MySQLLockError),
    (1213, mysql.MySQLDeadlockError),
])
def test_caught_lock_error_rolls_back_and_invalidates_lease(fake_driver, errno, expected):
    conn = mysql.connect_mysql(config())
    raw = fake_driver.connections[0]
    checks = []
    conn.set_transaction_guard(lambda _active: checks.append("checked"))

    # Establish a write transaction with one successful statement, then emulate
    # the server reporting either lock timeout or full transaction deadlock.
    conn.execute("UPDATE sample SET name='before error'")
    raw.fail_execute = FakeDriverError(errno, sqlstate="40001" if errno == 1213 else "HY000")
    with pytest.raises(expected):
        conn.execute("UPDATE sample SET name='contended'")

    assert checks == ["checked"]
    assert raw.rollback_count >= 1
    assert not raw.in_transaction
    assert not conn.in_transaction

    # Catching the SQL error cannot turn the next write into an unguarded
    # autocommit statement on the same lease.
    statement_count = len(raw.statements)
    with pytest.raises(mysql.MySQLLeaseInvalidatedError):
        conn.execute("UPDATE sample SET name='must not run'")
    with pytest.raises(mysql.MySQLLeaseInvalidatedError):
        conn.commit()
    assert len(raw.statements) == statement_count

    conn.close()
    assert raw.closed
    replacement = mysql.connect_mysql(config())
    replacement.close()
    assert len(fake_driver.connections) == 2


def test_server_idle_timeout_is_rounded_up_and_at_least_one_second(fake_driver):
    conn = mysql.connect_mysql(config(transaction_seconds=0.01))
    raw = fake_driver.connections[0]
    assert ("SET SESSION wait_timeout = %s", (1,)) in raw.statements
    conn.close()


def test_ddl_cannot_implicitly_commit_an_active_business_transaction(fake_driver):
    conn = mysql.connect_mysql(config())
    raw = fake_driver.connections[0]
    conn.execute("UPDATE sample SET name='x'")
    with pytest.raises(mysql.MySQLOperationalError, match="implicitly commits"):
        conn.execute("CREATE TABLE sample_copy(id INTEGER PRIMARY KEY)")
    assert not any("CREATE TABLE" in sql for sql, _ in raw.statements)
    conn.close()


def test_commit_error_is_unknown_and_never_retried(fake_driver):
    conn = mysql.connect_mysql(config())
    raw = fake_driver.connections[0]
    conn.execute("INSERT INTO sample(name) VALUES('x')")
    raw.fail_commit = FakeDriverError(2013, sqlstate="HY000")
    with pytest.raises(mysql.MySQLCommitOutcomeUnknown) as caught:
        conn.commit()
    assert caught.value.mysql_errno == 2013
    assert caught.value.category == "commit_outcome_unknown"
    assert raw.closed and len(fake_driver.connections) == 1


@pytest.mark.parametrize(
    ("errno", "sqlstate", "expected"),
    [
        (1062, "23000", mysql.MySQLIntegrityError),
        (1205, "HY000", mysql.MySQLLockError),
        (1213, "40001", mysql.MySQLDeadlockError),
        (2013, "HY000", mysql.MySQLOperationalError),
    ],
)
def test_vendor_errors_keep_mysql_classification(errno, sqlstate, expected):
    error = mysql._vendor_error(FakeDriverError(errno, sqlstate=sqlstate))
    assert isinstance(error, expected)
    assert error.mysql_errno == errno and error.mysql_sqlstate == sqlstate
    assert not hasattr(error, "sqlite_errorcode")


def test_vendor_error_message_exposes_errno_without_driver_details():
    error = mysql._vendor_error(FakeDriverError(2013, message="private sql and bind values"))
    assert str(error) == "MySQL operation failed (errno=2013)"
    assert "private sql" not in str(error)
    assert "bind values" not in str(error)


def _await_pool_waiters(pool, count):
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with pool._condition:
            if len(pool._waiters) == count:
                return
        time.sleep(.002)
    pytest.fail('pool waiters did not reach expected bounded count')


def test_pool_returns_slots_in_fifo_order_without_holding_waiter_connections(fake_driver):
    cfg = config(pool_size=1, pool_wait_seconds=2)
    first = mysql.connect_mysql(cfg)
    pool = first._pool
    acquired = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    order, errors = [], []

    def checkout(index):
        try:
            with mysql.connect_mysql(cfg):
                order.append(index)
                acquired[index].set()
                assert release[index].wait(2)
        except BaseException as exc:  # noqa: BLE001 - surface every thread failure in the parent assertion.
            errors.append(exc)

    threads = [threading.Thread(target=checkout, args=(index,)) for index in range(2)]
    try:
        threads[0].start()
        _await_pool_waiters(pool, 1)
        threads[1].start()
        _await_pool_waiters(pool, 2)
        assert len(fake_driver.connections) == 1
        first.close()
        assert acquired[0].wait(1)
        assert not acquired[1].is_set()
        release[0].set()
        assert acquired[1].wait(1)
    finally:
        for event in release:
            event.set()
        first.close()
        for thread in threads:
            if thread.ident is not None:
                thread.join(3)
    assert order == [0, 1] and not errors
    assert all(not thread.is_alive() for thread in threads)


def test_pool_bounded_queue_refuses_excess_waiters_and_recovers(fake_driver):
    cfg = config(pool_size=1, pool_wait_seconds=2, pool_queue_capacity=1)
    first = mysql.connect_mysql(cfg)
    acquired = threading.Event()
    errors = []

    def checkout():
        try:
            with mysql.connect_mysql(cfg):
                acquired.set()
        except BaseException as exc:  # noqa: BLE001 - surface every thread failure in the parent assertion.
            errors.append(exc)

    thread = threading.Thread(target=checkout)
    thread.start()
    try:
        _await_pool_waiters(first._pool, 1)
        with pytest.raises(mysql.MySQLPoolTimeout, match='queue is full'):
            mysql.connect_mysql(cfg)
        assert len(fake_driver.connections) == 1
    finally:
        first.close()
        thread.join(3)
    assert acquired.is_set() and not errors and not thread.is_alive()
    with mysql.connect_mysql(cfg):
        pass


def test_pool_close_wakes_all_waiters_before_their_deadline(fake_driver):
    cfg = config(pool_size=1, pool_wait_seconds=30)
    first = mysql.connect_mysql(cfg)
    errors = []

    def checkout():
        try:
            mysql.connect_mysql(cfg)
        except mysql.MySQLOperationalError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=checkout) for _ in range(3)]
    for index, thread in enumerate(threads):
        thread.start()
        _await_pool_waiters(first._pool, index + 1)
    mysql.close_mysql_pools(cfg)
    for thread in threads:
        thread.join(1)
    first.close()
    assert all(not thread.is_alive() for thread in threads)
    assert len(errors) == 3 and all('closed' in error for error in errors)
    assert not first._pool._waiters


@pytest.mark.parametrize('field', ['pool_wait_seconds', 'connect_timeout', 'read_timeout', 'transaction_seconds'])
@pytest.mark.parametrize('value', [float('inf'), float('nan')])
def test_pool_deadlines_reject_non_finite_configuration(field, value):
    with pytest.raises(ValueError, match='finite'):
        config(**{field: value})


def test_native_batch_bypasses_sqlite_translation_and_preserves_transaction(fake_driver, monkeypatch):
    conn = mysql.connect_mysql(config())
    statement = 'INSERT INTO sample(name) VALUES(%s)'
    rows = [('first',), ('second',)]
    def refused(*args, **kwargs):
        raise AssertionError('native batch entered the SQLite dialect adapter')
    monkeypatch.setattr(mysql, 'translate_sql', refused)
    cursor = conn.executemany_native(statement, rows)
    assert conn.in_transaction and cursor.rowcount == 2
    assert fake_driver.connections[0].statements[-1] == (statement, rows)
    cursor.close()
    conn.rollback()
    conn.close()


@pytest.mark.parametrize('started_transaction', [False, True])
def test_rollback_cleanup_preserves_original_failure_and_discards_socket(fake_driver, monkeypatch, started_transaction):
    cfg = config(pool_size=1)
    conn = mysql.connect_mysql(cfg)
    raw = fake_driver.connections[0]
    if started_transaction:
        conn.execute('INSERT INTO sample(name) VALUES(?)', ('synthetic',))
    def broken_rollback():
        raise FakeDriverError(2013)
    monkeypatch.setattr(raw, 'rollback', broken_rollback)
    original = ValueError('synthetic validation failure')
    with pytest.raises(ValueError) as caught, conn:
        raise original
    assert caught.value is original
    assert conn.closed and raw.closed
    assert 'discarded' in str(original.__notes__)
    with mysql.connect_mysql(cfg):
        assert len(fake_driver.connections) == 2
