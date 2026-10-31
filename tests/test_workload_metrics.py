from __future__ import annotations

from scripts.workload_metrics import Probe, _Aggregate, _operation, _role_from_path, install


def test_statement_classifier_keeps_only_fixed_low_cardinality_labels():
    private_sql = "UPDATE users SET token='do-not-retain' WHERE id='secret-id'"
    assert _operation(private_sql) == "UPDATE"
    assert _operation(b"COMMIT") == "COMMIT"
    assert _operation("SELECT\n1") == "SELECT"
    assert _operation("EXPLAIN QUERY PLAN SELECT 1") == "OTHER"


def test_aggregate_has_bounded_histogram_and_upper_percentile_buckets():
    aggregate = _Aggregate()
    aggregate.add(0.08)
    aggregate.add(1.5)
    aggregate.add(9000, error=True)
    result = aggregate.as_dict()
    assert result["count"] == 3
    assert result["errors"] == 1
    assert result["p50_upper_ms"] == 2
    assert result["p95_upper_ms"] is None
    assert len(result["bucket_counts"]) == len(result["bucket_bounds_ms"])


def test_probe_separates_views_and_never_retains_sql_or_parameters(tmp_path):
    probe = Probe(tmp_path)
    probe.start()
    probe._record("mysql_dbapi", "main_db", "execute", _operation("INSERT INTO x VALUES('secret')"), 3.0)
    probe._record("sqlite_trace", "main_db", "driver_sql", "INSERT", 4.0)
    probe._record("mysql_dbapi", "untrusted-db-name", "execute", "SELECT", 1.0)
    snapshot = probe.snapshot()
    assert {(item["view"], item["role"], item["operation"]) for item in snapshot["events"]} == {
        ("mysql_dbapi", "main_db", "INSERT"), ("sqlite_trace", "main_db", "INSERT"),
        ("mysql_dbapi", "aux_db", "SELECT"),
    }
    assert "secret" not in repr(snapshot)
    assert "INSERT INTO" not in repr(snapshot)
    stopped = probe.stop()
    probe._record("mysql_dbapi", "main_db", "execute", "DELETE", 1.0)
    assert stopped == probe.snapshot()
    assert all(item["operation"] != "DELETE" for item in probe.snapshot()["events"])


def test_start_clears_previous_window_and_path_labels_are_fixed(tmp_path):
    probe = Probe(tmp_path)
    probe.start()
    probe._record("sqlite_timing", "checkpoint_db", "connect", "OTHER", 2.0)
    assert probe.snapshot()["events"]
    probe.start()
    assert probe.snapshot()["events"] == []
    assert _role_from_path(tmp_path / "lka.sqlite3") == "main_db"
    assert _role_from_path(tmp_path / "agent_checkpoints.sqlite3") == "checkpoint_db"
    assert _role_from_path(tmp_path / "private-account.sqlite3") == "aux_db"
    probe.stop()


def test_sqlite_execute_fetch_and_mysql_app_calls_have_distinct_views(tmp_path):
    probe = Probe(tmp_path).start()
    probe._record("sqlite_timing", "main_db", "execute", "SELECT", 3)
    probe._record("sqlite_timing", "main_db", "fetch", "SELECT", 2)
    probe._record("mysql_dbapi", "main_db", "execute", "SELECT", 4)
    events = probe.snapshot()["events"]
    assert {(event["view"], event["phase"], event["count"]) for event in events} == {
        ("sqlite_timing", "execute", 1),
        ("sqlite_timing", "fetch", 1),
        ("mysql_dbapi", "execute", 1),
    }
    probe.stop()


def test_checkpoint_sqlite_connection_created_after_install_gets_driver_trace(tmp_path):
    from contextlib import closing

    from app.storage.sqlite_timing import connect_sqlite

    probe = install(tmp_path)
    probe.start()
    checkpoint_path = tmp_path / "agent_checkpoints.sqlite3"
    with closing(connect_sqlite(str(checkpoint_path))) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO sample(id) VALUES(?)", (1,))
        connection.commit()
    events = probe.snapshot()["events"]
    operations = {
        event["operation"] for event in events
        if event["view"] == "sqlite_trace" and event["role"] == "checkpoint_db"
        and event["phase"] == "driver_sql"
    }
    assert {"CREATE", "INSERT", "COMMIT"} <= operations
    assert "sample" not in repr(probe.snapshot())
    probe.stop()


def test_fake_method_hook_preserves_return_and_exception_without_logging_payload(tmp_path):
    class FakeCursor:
        def execute(self, statement, parameters=()):
            if statement.startswith("FAIL"):
                raise RuntimeError("private error text")
            return statement, parameters

    probe = Probe(tmp_path)

    def make_wrapper(original):
        def execute(cursor, statement, parameters=()):
            try:
                return original(cursor, statement, parameters)
            except BaseException:
                probe._record("mysql_dbapi", "main_db", "execute", _operation(statement), 1, error=True)
                raise
            finally:
                if not statement.startswith("FAIL"):
                    probe._record("mysql_dbapi", "main_db", "execute", _operation(statement), 1)
        return execute

    original = FakeCursor.execute
    probe._patch_method(FakeCursor, "execute", make_wrapper)
    probe.start()
    assert FakeCursor().execute("SELECT secret", ("private-param",)) == (
        "SELECT secret", ("private-param",),
    )
    try:
        FakeCursor().execute("FAIL private statement")
    except RuntimeError:
        pass
    else:
        raise AssertionError("fake failure was swallowed")
    snapshot = probe.stop()
    assert FakeCursor.execute is original
    assert {item["operation"] for item in snapshot["events"]} == {"SELECT", "OTHER"}
    assert "private-param" not in repr(snapshot)
    assert "private error text" not in repr(snapshot)
    assert "private statement" not in repr(snapshot)


def test_install_and_stop_need_no_runtime_or_database(tmp_path):
    from app.storage import mysql, sqlite_timing

    original_sqlite_connect = sqlite_timing.sqlite3.connect
    original_mysql_execute = mysql.MySQLCursor.execute
    probe = install(tmp_path)
    assert sqlite_timing.sqlite3.connect is not original_sqlite_connect
    assert mysql.MySQLCursor.execute is not original_mysql_execute
    probe.start()
    assert probe.snapshot()["events"] == []
    snapshot = probe.stop()
    assert snapshot["events"] == []
    assert "SQL text" in snapshot["metric_contract"]["privacy"]
    assert sqlite_timing.sqlite3.connect is original_sqlite_connect
    assert mysql.MySQLCursor.execute is original_mysql_execute


def test_sqlite_probe_does_not_keep_implicitly_closed_connections_alive(tmp_path):
    import gc
    import weakref

    from app.storage.sqlite_timing import connect_sqlite

    probe = install(tmp_path).start()
    connection = connect_sqlite(tmp_path / 'lka.sqlite3')
    connection.execute('SELECT 1').fetchall()
    reference = weakref.ref(connection)
    del connection
    gc.collect()
    assert reference() is None
    assert not probe._sqlite_connections
    assert probe.snapshot()['leases']['main_db']['active'] == 0
    probe.stop()


def test_old_sqlite_connection_collection_does_not_decrement_new_window(tmp_path):
    import gc

    from app.storage.sqlite_timing import connect_sqlite

    probe = install(tmp_path).start()
    old = connect_sqlite(tmp_path / 'lka.sqlite3')
    probe.start()
    current = connect_sqlite(tmp_path / 'lka.sqlite3')
    del old
    gc.collect()
    assert probe.snapshot()['leases']['main_db']['active'] == 1
    current.close()
    assert probe.snapshot()['leases']['main_db']['active'] == 0
    probe.stop()
