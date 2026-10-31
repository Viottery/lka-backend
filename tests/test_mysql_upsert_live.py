from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.storage.mysql import MySQLIntegrityError, connect_mysql
from app.storage.mysql_schema import MySQLSchema, attach_schema_metadata
from tests.test_mysql_owner_live import _config


@pytest.fixture
def upsert_store():
    config = _config()
    table = "lka_test_upsert_" + uuid4().hex[:12]
    audit = table + "_audit"
    trigger = table + "_update"
    conn = connect_mysql(config)
    schema = MySQLSchema(config.database)
    schema.execute_schema_script(conn, f"""CREATE TABLE IF NOT EXISTS {table} (
        id TEXT PRIMARY KEY, alternate TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0);""")
    conn.execute_native(f"CREATE TABLE `{audit}` (event_id BIGINT AUTO_INCREMENT PRIMARY KEY,id VARCHAR(100))")
    migration_file = os.environ.get("LKA_MYSQL_TEST_MIGRATION_CONFIG")
    migration_values = json.loads(Path(migration_file).read_text()) if migration_file else {}
    migration_config = replace(config, **migration_values)
    assert migration_config.database == config.database
    with connect_mysql(migration_config) as migration:
        migration.execute_native(f"CREATE TRIGGER `{trigger}` AFTER UPDATE ON `{table}` FOR EACH ROW "
                                 f"INSERT INTO `{audit}`(id) VALUES(NEW.id)")
    attach_schema_metadata(conn, schema)
    try:
        yield conn, table, audit
    finally:
        conn.rollback()
        conn.execute_native(f"DROP TABLE `{table}`")
        conn.execute_native(f"DROP TABLE `{audit}`")
        for metadata in ("lka_mysql_schema_columns", "lka_mysql_rowid_columns", "lka_mysql_unique_key_mappings"):
            conn.execute_native(f"DELETE FROM `{metadata}` WHERE table_name=%s", (table,))
        conn.execute_native("DELETE FROM lka_mysql_schema_objects WHERE table_name=%s", (table,))
        conn.commit()
        conn.close()


def test_targeted_upsert_does_not_update_a_different_unique_row(upsert_store):
    conn, table, _ = upsert_store
    sql = f"INSERT INTO {table}(id,alternate,payload) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload"
    assert conn.execute(sql, ("one", "alternate-one", "old")).rowcount == 1
    conn.execute(sql, ("one", "unused-alternate", "new"))
    assert tuple(conn.execute(f"SELECT alternate,payload FROM {table} WHERE id='one'").fetchone()) == ("alternate-one", "new")
    with pytest.raises(MySQLIntegrityError):
        conn.execute(sql, ("other", "alternate-one", "must-not-overwrite"))
    assert conn.execute(f"SELECT payload FROM {table} WHERE id='one'").fetchone()[0] == "new"


def test_duplicate_do_nothing_has_no_update_trigger_effect(upsert_store):
    conn, table, audit = upsert_store
    insert = f"INSERT OR IGNORE INTO {table}(id,alternate,payload) VALUES(?,?,?)"
    assert conn.execute(insert, ("one", "alternate", "old")).rowcount == 1
    assert conn.execute(insert, ("one", "alternate", "ignored")).rowcount == 0
    assert conn.execute(f"INSERT INTO {table}(id,alternate,payload) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING",
                        ("one", "different", "ignored")).rowcount == 0
    assert conn.execute_native(f"SELECT COUNT(*) FROM {audit}").fetchone()[0] == 0


def test_conditional_upsert_keeps_cas_predicate_and_percent_literals(upsert_store):
    conn, table, _ = upsert_store
    sql = (f"INSERT INTO {table}(id,alternate,payload,revision) VALUES(?,?,'100% :lka_p0 literal',?) "
           f"ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision "
           f"WHERE {table}.revision < excluded.revision")
    conn.execute(sql, ("one", "alternate", 2))
    assert conn.execute(sql, ("one", "different", 1)).rowcount == 0
    row = conn.execute(f"SELECT payload,revision FROM {table} WHERE id='one'").fetchone()
    assert tuple(row) == ("100% :lka_p0 literal", 2)


def test_upsert_rowcount_matches_sqlite_for_noop_and_failed_cas(upsert_store):
    conn, table, _ = upsert_store
    conn.execute(f"INSERT INTO {table}(id,alternate,payload,revision) VALUES(?,?,?,?)",
                 ("one", "alternate", "same", 1))
    sql = (f"INSERT INTO {table}(id,alternate,payload,revision) VALUES(?,?,?,?) "
           f"ON CONFLICT(id) DO UPDATE SET payload=excluded.payload "
           f"WHERE {table}.revision < excluded.revision")
    assert conn.execute(sql, ("one", "unused-alternate", "same", 2)).rowcount == 1
    assert conn.execute(sql, ("one", "unused-alternate", "same", 0)).rowcount == 0

    unconditional = (f"INSERT INTO {table}(id,alternate,payload,revision) VALUES(?,?,?,?) "
                     f"ON CONFLICT(id) DO UPDATE SET payload=excluded.payload")
    assert conn.execute(unconditional, ("one", "another-alternate", "same", 2)).rowcount == 1


def test_binary_unique_keys_preserve_case_and_trailing_spaces(upsert_store):
    conn, table, _ = upsert_store
    for value in ("a", "a ", "A"):
        conn.execute(f"INSERT OR IGNORE INTO {table}(id,alternate,payload) VALUES(?,?,?)", (value, value, "payload"))
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 3


def test_same_key_concurrent_upserts_serialize_without_duplicate_rows(upsert_store):
    conn, table, _ = upsert_store
    config = replace(_config(), pool_size=12)
    schema = MySQLSchema(config.database)
    statement = (f"INSERT INTO {table}(id,alternate,payload,revision) VALUES(?,?,?,?) "
                 f"ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision "
                 f"WHERE {table}.revision < excluded.revision")

    def race(record_id: str, *, seeded: bool):
        if seeded:
            conn.execute(
                f"INSERT INTO {table}(id,alternate,payload,revision) VALUES(?,?,?,?)",
                (record_id, f"seed-{record_id}", "seed", 0),
            )
            conn.commit()
        barrier = threading.Barrier(8, timeout=10)

        def write(revision: int):
            with connect_mysql(config) as worker:
                attach_schema_metadata(worker, schema, ensure_metadata=False)
                barrier.wait()
                worker.execute(statement, (record_id, f"alternate-{revision}", f"payload-{revision}", revision))

        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(write, range(1, 9)))

        count, payload, revision = conn.execute(
            f"SELECT COUNT(*),MAX(payload),MAX(revision) FROM {table} WHERE id=?", (record_id,),
        ).fetchone()
        assert count == 1
        assert payload == "payload-8"
        assert revision == 8

    race("seeded-concurrent", seeded=True)
    race("fresh-concurrent", seeded=False)


def test_general_qmark_query_handles_percent_literals_and_like(upsert_store):
    conn, _, _ = upsert_store
    row = conn.execute("SELECT '100%' AS literal WHERE ? LIKE 'ready 50%'", ("ready 50%",)).fetchone()
    assert row[0] == "100%"
