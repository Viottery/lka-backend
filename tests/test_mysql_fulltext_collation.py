from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

import pytest

from app.storage import mysql_fulltext_collation as migration
from app.storage.mysql_schema import MySQLSchemaError


class _Cursor:
    def __init__(self, row=None, rows=()):
        self.row = row
        self.rows = list(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)


class _Connection:
    dialect = "mysql"
    in_transaction = False

    def __init__(self):
        self._pool = type("Pool", (), {"config": type("Config", (), {"database": "collation-test"})()})()
        self.statements = []
        self.ledger = None
        self.columns = {}
        self.indexes = defaultdict(list)
        self.parsers = {}
        from app.storage.mysql_fulltext import _FULLTEXT_INDEXES

        for table, index, fields in _FULLTEXT_INDEXES:
            all_columns = {
                "mail_messages_fts": ("rowid", "message_id", "subject", "sender", "folder", "body_text"),
                "matters_fts": ("rowid", "matter_id", "title", "summary", "tags"),
                "knowledge_chunks_fts": ("rowid", "chunk_id", "document_id", "title", "text", "source_type"),
                "memory_entries_fts": ("rowid", "memory_id", "content", "memory_type"),
                "conversation_history_fts": ("rowid", "content"),
            }[table]
            definitions = migration._definitions()[table]["columns"]
            fts = {name: (column_type, nullable) for name, column_type, nullable in definitions}
            first_fts_column = fields[0]
            rows = []
            for ordinal, name in enumerate(all_columns, 1):
                if name in fts:
                    column_type, nullable = fts[name]
                    if nullable is None:
                        nullable = True
                    rows.append((name, column_type, "YES" if nullable else "NO", None, "", ordinal,
                                 "utf8mb4", "utf8mb4_0900_bin", "", "",
                                 "MUL" if name == first_fts_column else ""))
                else:
                    unique_key = {"message_id", "matter_id", "chunk_id", "memory_id"}
                    rows.append((name, "bigint unsigned" if name == "rowid" else "varchar(768)", "NO",
                                 None, "auto_increment" if name == "rowid" else "", ordinal,
                                 None if name == "rowid" else "utf8mb4",
                                 None if name == "rowid" else "utf8mb4_0900_bin", "", "",
                                 "PRI" if name == "rowid" else "UNI" if name in unique_key else ""))
            self.columns[table] = rows
            self.indexes[table] = [
                (index, position, field, "FULLTEXT")
                for position, field in enumerate(fields, 1)
            ]
            self.parsers[table] = "ngram"

    def execute_native(self, sql, parameters=()):
        self.statements.append((sql, parameters))
        if "SELECT name,checksum FROM `lka_schema_migrations`" in sql:
            return _Cursor(self.ledger if parameters == (12,) else None)
        if "SELECT GET_LOCK" in sql:
            return _Cursor((1,))
        if "SELECT RELEASE_LOCK" in sql:
            return _Cursor((1,))
        if "FROM INFORMATION_SCHEMA.COLUMNS" in sql:
            return _Cursor(rows=self.columns[parameters[0]])
        if "FROM INFORMATION_SCHEMA.STATISTICS" in sql:
            return _Cursor(rows=self.indexes[parameters[0]])
        if sql.startswith("SHOW CREATE TABLE"):
            table = sql.split("`")[1]
            entries = self.indexes[table]
            if not entries:
                fulltext = ""
            else:
                name = entries[0][0]
                fields = ",".join(f"`{row[2]}`" for row in entries)
                fulltext = f"FULLTEXT KEY `{name}` ({fields}) /*!50100 WITH PARSER `{self.parsers[table]}` */"
            return _Cursor((table, f"CREATE TABLE `{table}` ({fulltext}) ENGINE=InnoDB"))
        if sql.startswith("DROP INDEX"):
            index, table = sql.split("`", 2)[1], sql.split("ON `", 1)[1].split("`", 1)[0]
            self.indexes[table] = []
            fields = {column for column, _type, _nullable in migration._definitions()[table]["columns"]}
            for i, row in enumerate(self.columns[table]):
                if row[0] in fields:
                    self.columns[table][i] = (*row[:10], "")
            return _Cursor()
        if sql.startswith("ALTER TABLE"):
            parts = sql.split("`")
            table, column = parts[1], parts[3]
            rows = self.columns[table]
            for i, row in enumerate(rows):
                if row[0] == column:
                    self.columns[table][i] = (*row[:7], "utf8mb4_0900_as_ci", *row[8:])
                    break
            return _Cursor()
        if sql.startswith("CREATE FULLTEXT INDEX"):
            import re

            parts = sql.split("`")
            index, table = parts[1], parts[3]
            fields = re.findall(r"`([^`]+)`", sql.split("(", 1)[1].split(")", 1)[0])
            self.indexes[table] = [(index, i, field, "FULLTEXT")
                                   for i, field in enumerate(fields, 1)]
            self.parsers[table] = "ngram"
            rows = self.columns[table]
            first = next(i for i, row in enumerate(rows) if row[0] == fields[0])
            rows[first] = (*rows[first][:10], "MUL")
            return _Cursor()
        if sql.startswith("INSERT INTO `lka_schema_migrations`"):
            self.ledger = parameters[1:]
            return _Cursor()
        return _Cursor()

    def commit(self):
        self.statements.append(("COMMIT", ()))


def test_v12_installs_only_fts_text_collations_and_records_ledger(monkeypatch):
    verified_v11 = []
    monkeypatch.setattr(migration, "_verify_v11", lambda conn: verified_v11.append(conn))
    conn = _Connection()

    migration.install_fulltext_collation_v12(conn, install=True)

    assert verified_v11 == [conn]
    assert conn.ledger == (migration._NAME, migration._checksum())
    assert sum(sql.startswith("DROP INDEX") for sql, _ in conn.statements) == 5
    assert sum(sql.startswith("ALTER TABLE") for sql, _ in conn.statements) == 13
    assert sum(sql.startswith("CREATE FULLTEXT INDEX") for sql, _ in conn.statements) == 5
    for table, rows in conn.columns.items():
        for row in rows:
            if row[0] in {column for column, _, _ in migration._definitions()[table]["columns"]}:
                assert row[7] == "utf8mb4_0900_as_ci"
            elif row[0] == "rowid":
                assert row[7] is None
            else:
                assert row[7] == "utf8mb4_0900_bin"
    assert set(conn.parsers.values()) == {"ngram"}


def test_v12_verify_only_requires_ledger_and_never_executes_ddl(monkeypatch):
    monkeypatch.setattr(migration, "_verify_v11", lambda _conn: None)
    conn = _Connection()
    with pytest.raises(MySQLSchemaError, match="V12 is missing"):
        migration.install_fulltext_collation_v12(conn)
    assert not any(sql.startswith(("DROP", "ALTER", "CREATE", "INSERT")) for sql, _ in conn.statements)

    conn = _Connection()
    for table in migration._definitions():
        conn.columns[table] = [(*row[:7], "utf8mb4_0900_as_ci", *row[8:])
                               if row[0] in {col for col, _, _ in migration._definitions()[table]["columns"]}
                               else row for row in conn.columns[table]]
    conn.ledger = (migration._NAME, migration._checksum())
    migration.install_fulltext_collation_v12(conn)
    assert not any(sql.startswith(("DROP", "ALTER", "CREATE", "INSERT")) for sql, _ in conn.statements)


@pytest.mark.parametrize("corruption", ["column_type", "extra", "default", "index_order", "parser"])
def test_v12_install_fails_closed_on_unrecognized_layout(monkeypatch, corruption):
    monkeypatch.setattr(migration, "_verify_v11", lambda _conn: None)
    conn = _Connection()
    table = "mail_messages_fts"
    rows = conn.columns[table]
    idx = next(i for i, row in enumerate(rows) if row[0] == "subject")
    if corruption == "column_type":
        conn.columns[table][idx] = (*rows[idx][:1], "mediumtext", *rows[idx][2:])
    elif corruption == "extra":
        conn.columns[table][idx] = (*rows[idx][:4], "invisible", *rows[idx][5:])
    elif corruption == "default":
        conn.columns[table][idx] = (*rows[idx][:3], "unexpected", *rows[idx][4:])
    elif corruption == "index_order":
        conn.indexes[table][0], conn.indexes[table][1] = conn.indexes[table][1], conn.indexes[table][0]
    else:
        conn.parsers[table] = "builtin"

    with pytest.raises(MySQLSchemaError):
        migration.install_fulltext_collation_v12(conn, install=True)
    assert not any(sql.startswith("DROP INDEX") for sql, _ in conn.statements)


def test_v12_install_resumes_after_interrupted_drop(monkeypatch):
    monkeypatch.setattr(migration, "_verify_v11", lambda _conn: None)
    conn = _Connection()
    conn.indexes["mail_messages_fts"] = []
    rows = conn.columns["mail_messages_fts"]
    idx = next(i for i, row in enumerate(rows) if row[0] == "subject")
    conn.columns["mail_messages_fts"][idx] = (*rows[idx][:10], "")
    conn.ledger = (migration._NAME, migration._checksum())

    migration.install_fulltext_collation_v12(conn, install=True)

    assert conn.ledger == (migration._NAME, migration._checksum())
    assert any(sql.startswith("CREATE FULLTEXT INDEX `ft_mail_messages_fts`") for sql, _ in conn.statements)


def test_v12_rejects_missing_legacy_index_without_recovery_marker(monkeypatch):
    monkeypatch.setattr(migration, "_verify_v11", lambda _conn: None)
    conn = _Connection()
    conn.indexes["mail_messages_fts"] = []

    with pytest.raises(MySQLSchemaError, match="index is missing"):
        migration.install_fulltext_collation_v12(conn, install=True)
    assert not any(sql.startswith("DROP INDEX") for sql, _ in conn.statements)


@pytest.mark.skipif(not os.environ.get("LKA_MYSQL_TEST_MIGRATION_CONFIG"),
                    reason="Dedicated MySQL migration config required")
def test_live_v12_install_and_full_bootstrap_verification(tmp_path):
    from app.storage.connections import configure_mysql, connect_database, unregister_mysql
    from app.storage.mysql import MySQLConfig, close_mysql_pools
    from app.storage.mysql_bootstrap import bootstrap_mysql

    config = MySQLConfig(**json.loads(
        Path(os.environ["LKA_MYSQL_TEST_MIGRATION_CONFIG"]).read_text(encoding="utf-8"),
    ))
    if not any(part in config.database.lower() for part in ("test", "lab")):
        pytest.fail("live MySQL migration requires an explicitly named test/lab database")
    identity = tmp_path / f"fulltext-collation-{uuid4().hex}.sqlite3"
    configure_mysql(identity, config)
    try:
        with connect_database(identity) as conn:
            migration.install_fulltext_collation_v12(conn, install=True)
            migration.install_fulltext_collation_v12(conn)
            bootstrap_mysql(conn, install=False)
            for table, definition in migration._definitions().items():
                rows = conn.execute_native(
                    "SELECT COLUMN_NAME,COLLATION_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                    (table,),
                ).fetchall()
                collations = {str(row[0]): row[1] for row in rows}
                assert all(collations[column] == migration._TARGET_COLLATION
                           for column, _type, _nullable in definition["columns"])
    finally:
        unregister_mysql(identity, expected=config)
        close_mysql_pools(config)
