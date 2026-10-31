from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from scripts import import_sqlite_mysql as importer


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _add_fts_tables(conn: sqlite3.Connection) -> None:
    for table, fields in importer._FTS_TABLES.items():
        conn.execute(
            f"CREATE VIRTUAL TABLE `{table}` USING fts5(" + ",".join(
                f"`{field}`" for field in fields
            ) + ")"
        )


def _create_vec_snapshot(path: Path, *, index_key: str, dimensions: int = 4,
                         metadata: bool = True, extra_shadow: bool = False) -> bytes:
    sqlite_vec = pytest.importorskip("sqlite_vec")
    conn = sqlite3.connect(path)
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        if metadata:
            conn.execute(
                "CREATE TABLE knowledge_embedding_records(vector_id INTEGER PRIMARY KEY, "
                "index_key TEXT NOT NULL, dimensions INTEGER NOT NULL)",
            )
        table = "knowledge_vec_" + hashlib.sha256(index_key.encode()).hexdigest()[:16]
        conn.execute(
            f"CREATE VIRTUAL TABLE {table} USING vec0(vector_id INTEGER PRIMARY KEY, "
            f"embedding float[{dimensions}] distance_metric=cosine)",
        )
        raw = sqlite_vec.serialize_float32([0.25, -0.5, 1.0, 2.0])
        if metadata:
            conn.execute(
                "INSERT INTO knowledge_embedding_records VALUES(17,?,?)",
                (index_key, dimensions),
            )
        conn.execute(f"INSERT INTO {table}(vector_id,embedding) VALUES(17,?)", (raw,))
        if extra_shadow:
            conn.execute(f"CREATE TABLE {table}_unreviewed(value TEXT)")
        conn.commit()
        return raw
    finally:
        conn.close()


def test_identifier_rejects_sql_fragments() -> None:
    assert importer._ident("agent_sessions") == "`agent_sessions`"
    with pytest.raises(importer.ImportErrorSafe):
        importer._ident("x`; DROP TABLE agent_sessions; --")


def test_inventory_preserves_implicit_rowid_and_source_types() -> None:
    source = _db()
    source.execute("CREATE TABLE records(payload BLOB, metadata TEXT, created_at TEXT)")
    source.execute(
        "INSERT INTO records(rowid,payload,metadata,created_at) VALUES(7,?,?,?)",
        (bytes.fromhex("00ff"), '{"n":9007199254740993,"s":"\u00e9"}', "2032-01-02T03:04:05+08:00"),
    )
    _add_fts_tables(source)
    source.commit()
    rowids = {"records": ("__lka_rowid", "invisible_auto_increment")}

    inventory, excluded = importer._inventory_source(source, {"records"}, rowids)

    assert [item.name for item in inventory] == [
        "records", "conversation_history_fts", "knowledge_chunks_fts",
        "mail_messages_fts", "matters_fts", "memory_entries_fts",
    ]
    item = inventory[0]
    assert item.rows == 1
    assert item.rowid_column == "__lka_rowid"
    assert item.source_columns == ("payload", "metadata", "created_at")
    assert len(item.digest) == 64
    assert "sqlite_sequence" not in excluded


def test_inventory_rejects_unmapped_legacy_application_table() -> None:
    source = _db()
    source.execute("CREATE TABLE old_trace_history(trace_id TEXT)")
    _add_fts_tables(source)
    source.commit()
    with pytest.raises(importer.ImportErrorSafe, match="unknown application tables"):
        importer._inventory_source(source, set(), {})


def test_inventory_requires_every_logical_fts_projection() -> None:
    source = _db()
    source.execute("CREATE TABLE records(value TEXT)")
    source.commit()
    with pytest.raises(importer.ImportErrorSafe, match="logical FTS projection"):
        importer._inventory_source(source, {"records"}, {"records": ("__lka_rowid", "invisible_auto_increment")})


def test_source_catalog_excludes_fts_shadows_and_sqlite_metadata() -> None:
    source = _db()
    source.execute("CREATE TABLE ordinary(value TEXT)")
    source.execute("CREATE VIRTUAL TABLE memory_entries_fts USING fts5(content)")
    source.execute("CREATE TABLE sequenced(id INTEGER PRIMARY KEY AUTOINCREMENT)")
    source.execute("INSERT INTO sequenced DEFAULT VALUES")
    source.commit()

    included, excluded = importer._source_tables(source)

    assert included == {"ordinary", "sequenced", "memory_entries_fts"}
    assert "memory_entries_fts_data" in excluded
    assert "sqlite_sequence" in excluded


def test_copy_preserves_implicit_rowids_blobs_json_and_iso_text() -> None:
    source = _db()
    source.execute("CREATE TABLE records(payload BLOB, metadata TEXT, created_at TEXT)")
    source.executemany(
        "INSERT INTO records(rowid,payload,metadata,created_at) VALUES(?,?,?,?)",
        [
            (3, bytes.fromhex("00ff"), '{"n":9007199254740993}', "2032-01-02T03:04:05+08:00"),
            (17, b"raw", '{ "keep": [true, null] }', "2032-01-02T03:04:05Z"),
        ],
    )
    source.commit()
    columns = importer._column_names(source, "records")
    item = importer.TableInventory(
        name="records", rows=2, source_columns=tuple(columns), rowid_column="__lka_rowid",
        rowid_kind="invisible_auto_increment", sequence=None,
        digest=importer._source_digest(source, "records", columns, "rowid"),
    )
    target = _db()
    target.execute("CREATE TABLE records(payload BLOB, metadata TEXT, created_at TEXT, __lka_rowid INTEGER UNIQUE)")
    target.commit()

    class TargetAdapter:
        def executemany(self, sql, values):
            self.conn.executemany(sql.replace("%s", "?"), values)

        def executemany_native(self, sql, values):
            self.executemany(sql, values)

        def commit(self):
            self.conn.commit()

        def __init__(self, conn):
            self.conn = conn

        def execute_native(self, sql, params=()):
            return self.conn.execute(sql.replace("%s", "?"), params)

    adapter = TargetAdapter(target)
    importer._copy_ordinary_table(source, adapter, item, batch_size=1)
    importer._verify_table_with_rowid(source, adapter, item, columns, "__lka_rowid", "invisible_auto_increment")

    copied = target.execute(
        "SELECT __lka_rowid,payload,metadata,created_at FROM records ORDER BY __lka_rowid"
    ).fetchall()
    assert [row[0] for row in copied] == [3, 17]
    assert copied[0][1] == bytes.fromhex("00ff")
    assert copied[0][2] == '{"n":9007199254740993}'
    assert copied[0][3] == "2032-01-02T03:04:05+08:00"


def test_copy_preserves_integer_primary_key_zero() -> None:
    source = _db()
    source.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)")
    source.executemany("INSERT INTO records(id,value) VALUES(?,?)", [(0, "zero"), (8, "eight")])
    source.commit()
    columns = importer._column_names(source, "records")
    item = importer.TableInventory(
        "records", 2, tuple(columns), "id", "integer_primary_key", None,
        importer._source_digest(source, "records", columns, "id"),
    )
    target = _db()
    target.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)")
    target.commit()

    class TargetAdapter:
        def __init__(self, conn):
            self.conn = conn

        def executemany(self, sql, values):
            self.conn.executemany(sql.replace("%s", "?"), values)

        def executemany_native(self, sql, values):
            self.executemany(sql, values)

        def commit(self):
            self.conn.commit()

        def execute_native(self, sql, params=()):
            return self.conn.execute(sql.replace("%s", "?"), params)

    adapter = TargetAdapter(target)
    importer._copy_ordinary_table(source, adapter, item, batch_size=1)
    importer._verify_table_with_rowid(source, adapter, item, columns, "id", "integer_primary_key")
    assert [row[0] for row in target.execute("SELECT id FROM records ORDER BY id")] == [0, 8]


def test_copy_batches_bound_rows_and_payload_bytes_with_single_large_row() -> None:
    source = _db()
    source.execute("CREATE TABLE records(payload BLOB)")
    source.executemany(
        "INSERT INTO records(rowid,payload) VALUES(?,?)",
        [(1, b"x" * 128), (2, b"a" * 5), (3, b"b" * 5), (4, b"c" * 5)],
    )
    source.commit()
    columns = importer._column_names(source, "records")
    item = importer.TableInventory(
        "records", 4, tuple(columns), "__lka_rowid", "invisible_auto_increment", None,
        importer._source_digest(source, "records", columns, "rowid"),
    )
    target = _db()
    target.execute("CREATE TABLE records(payload BLOB, __lka_rowid INTEGER UNIQUE)")
    target.commit()

    class TargetAdapter:
        def __init__(self, conn):
            self.conn = conn
            self.batch_sizes = []

        def executemany_native(self, sql, rows):
            rows = list(rows)
            self.batch_sizes.append(len(rows))
            self.conn.executemany(sql.replace("%s", "?"), rows)

        def commit(self):
            self.conn.commit()

    adapter = TargetAdapter(target)
    importer._copy_ordinary_table(
        source, adapter, item, batch_size=100, payload_budget=48,
    )

    assert adapter.batch_sizes == [1, 2, 1]
    assert target.execute("SELECT count(*) FROM records").fetchone()[0] == 4
    assert target.execute("SELECT payload FROM records WHERE __lka_rowid=1").fetchone()[0] == b"x" * 128


def test_fts_copy_batches_by_payload_budget() -> None:
    source = _db()
    source.execute("CREATE VIRTUAL TABLE conversation_history_fts USING fts5(content)")
    source.executemany(
        "INSERT INTO conversation_history_fts(rowid,content) VALUES(?,?)",
        [(1, "a" * 8), (2, "b" * 8), (3, "c" * 80)],
    )
    source.commit()
    fields = importer._FTS_TABLES["conversation_history_fts"]
    item = importer.TableInventory(
        "conversation_history_fts", 3, tuple(fields), "rowid", "fts_rowid", None,
        importer._source_digest(source, "conversation_history_fts", list(fields), "rowid"),
    )
    target = _db()
    target.execute("CREATE VIRTUAL TABLE conversation_history_fts USING fts5(content)")
    target.commit()

    class TargetAdapter:
        def __init__(self, conn):
            self.conn = conn
            self.batch_sizes = []

        def executemany_native(self, sql, rows):
            rows = list(rows)
            self.batch_sizes.append(len(rows))
            self.conn.executemany(sql.replace("%s", "?"), rows)

        def commit(self):
            self.conn.commit()

    adapter = TargetAdapter(target)
    importer._copy_fts(source, adapter, item, batch_size=100, payload_budget=50)
    assert adapter.batch_sizes == [2, 1]
    assert target.execute("SELECT count(*) FROM conversation_history_fts").fetchone()[0] == 3


def test_target_digest_uses_bounded_keyset_pages() -> None:
    target = _db()
    target.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)")
    target.executemany(
        "INSERT INTO records VALUES(?,?)", [(2, "a"), (5, "b"), (9, "c"), (12, "d"), (20, "e")],
    )
    target.commit()

    class TargetAdapter:
        def __init__(self, conn):
            self.conn = conn
            self.queries = []

        def execute_native(self, sql, params=()):
            self.queries.append((sql, params))
            return self.conn.execute(sql.replace("%s", "?"), params)

    adapter = TargetAdapter(target)
    digest = importer._target_digest(adapter, "records", ["id", "value"], "id", batch_size=2)

    assert digest == importer._digest_rows([(2, "a"), (5, "b"), (9, "c"), (12, "d"), (20, "e")])
    assert len(adapter.queries) == 3
    assert "WHERE `id`>%s" not in adapter.queries[0][0]
    assert all("LIMIT %s" in statement and "OFFSET" not in statement for statement, _ in adapter.queries)
    assert adapter.queries[1][1] == (5, 2)
    assert adapter.queries[2][1] == (12, 2)


def test_real_sqlite_vec_shadows_and_raw_float32_export(tmp_path: Path) -> None:
    index_key = "synthetic-provider:synthetic-model:4"
    snapshot = tmp_path / "vec0.sqlite3"
    raw = _create_vec_snapshot(snapshot, index_key=index_key)

    source = importer._open_snapshot(snapshot)
    try:
        included, excluded = importer._source_tables(source)
        vector_name = "knowledge_vec_" + hashlib.sha256(index_key.encode()).hexdigest()[:16]
        assert included == {"knowledge_embedding_records"}
        assert vector_name in excluded
        assert {f"{vector_name}_{name}" for name in importer._KNOWLEDGE_VEC_SHADOWS}.issubset(excluded)
        inventory = importer._knowledge_vector_inventory(source)
        assert inventory.rows == 1
        assert inventory.source_tables == (vector_name,)
        assert inventory.digest == importer._digest_rows([(17, raw)])

        target = _db()
        target.execute(
            "CREATE TABLE knowledge_vector_blobs(vector_id INTEGER PRIMARY KEY, embedding BLOB NOT NULL)",
        )
        target.commit()

        class TargetAdapter:
            def __init__(self, conn):
                self.conn = conn

            def execute_native(self, sql, params=()):
                return self.conn.execute(sql.replace("%s", "?"), params)

            def executemany_native(self, sql, params):
                return self.conn.executemany(sql.replace("%s", "?"), params)

            def commit(self):
                self.conn.commit()

        adapter = TargetAdapter(target)
        importer._copy_knowledge_vectors(source, adapter, inventory, batch_size=1)
        importer._verify_knowledge_vectors(adapter, inventory)
        assert tuple(target.execute(
            "SELECT vector_id,embedding FROM knowledge_vector_blobs",
        ).fetchone()) == (17, raw)
    finally:
        source.close()


def test_sqlite_vec_requires_metadata_and_rejects_unknown_shadow(tmp_path: Path) -> None:
    index_key = "synthetic-provider:missing-metadata:4"
    missing_metadata = tmp_path / "vec0-missing-metadata.sqlite3"
    _create_vec_snapshot(missing_metadata, index_key=index_key, metadata=False)
    with (
        importer._open_snapshot(missing_metadata) as source,
        pytest.raises(importer.ImportErrorSafe, match="unique matching knowledge metadata"),
    ):
        importer._knowledge_vector_inventory(source)

    extra_shadow = tmp_path / "vec0-extra-shadow.sqlite3"
    _create_vec_snapshot(extra_shadow, index_key=index_key, extra_shadow=True)
    with (
        importer._open_snapshot(extra_shadow) as source,
        pytest.raises(importer.ImportErrorSafe, match="unrecognized shadow"),
    ):
        importer._source_tables(source)


def test_empty_strict_vec_cache_without_metadata_key_is_reported_and_fingerprinted(tmp_path: Path) -> None:
    sqlite_vec = pytest.importorskip("sqlite_vec")
    index_key = "synthetic-provider:lazy-empty-cache:512"
    path = tmp_path / "empty-vec-cache.sqlite3"
    source = sqlite3.connect(path)
    try:
        source.enable_load_extension(True)
        try:
            sqlite_vec.load(source)
        finally:
            source.enable_load_extension(False)
        source.execute(
            "CREATE TABLE knowledge_embedding_records(vector_id INTEGER PRIMARY KEY, "
            "index_key TEXT NOT NULL, dimensions INTEGER NOT NULL)",
        )
        table = "knowledge_vec_" + hashlib.sha256(index_key.encode()).hexdigest()[:16]
        source.execute(
            f"CREATE VIRTUAL TABLE {table} USING vec0(vector_id INTEGER PRIMARY KEY, "
            "embedding float[512] distance_metric=cosine)",
        )
        source.commit()
    finally:
        source.close()

    with importer._open_snapshot(path) as snapshot:
        inventory = importer._knowledge_vector_inventory(snapshot)
        assert inventory.rows == 0
        assert inventory.source_tables == (table,)
        assert inventory.empty_source_tables == (table,)
        assert inventory.tables == ()

    no_cache_identity = importer.KnowledgeVectorInventory((), 0, inventory.digest)
    assert importer._source_fingerprint([], inventory) != importer._source_fingerprint([], no_cache_identity)
    report = importer._report([], [], None, "check_only", knowledge_vectors=inventory)
    assert report["knowledge_vectors"]["empty_source_tables"] == [table]


def test_nonempty_vec_cache_without_metadata_still_fails_closed(tmp_path: Path) -> None:
    index_key = "synthetic-provider:nonempty-missing-metadata:4"
    path = tmp_path / "vec0-nonempty-missing-metadata.sqlite3"
    _create_vec_snapshot(path, index_key=index_key, metadata=False)
    with importer._open_snapshot(path) as source, pytest.raises(
        importer.ImportErrorSafe, match="unique matching knowledge metadata",
    ):
        importer._knowledge_vector_inventory(source)


def test_auto_increment_restores_only_supported_target_rowid_columns() -> None:
    class TargetAdapter:
        def __init__(self, extra, maximum=0):
            self.extra = extra
            self.maximum = maximum
            self.next_value = None

        def execute_native(self, sql, params=()):
            if "SELECT EXTRA FROM INFORMATION_SCHEMA.COLUMNS" in sql:
                return _OneRow((self.extra,))
            if sql.startswith("SELECT MAX("):
                return _OneRow((self.maximum,))
            if sql.startswith("ALTER TABLE"):
                self.next_value = int(sql.rsplit("=", 1)[1])
                return _OneRow(None)
            if "SELECT AUTO_INCREMENT" in sql:
                return _OneRow((self.next_value,))
            raise AssertionError(sql)

    class _OneRow:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    no_auto = TargetAdapter("")
    importer._set_auto_increment(
        no_auto,
        importer.TableInventory("singletons", 1, ("id",), "id", "integer_primary_key", None, "a" * 64),
        "id",
    )
    assert no_auto.next_value is None
    with pytest.raises(importer.ImportErrorSafe, match="cannot be restored"):
        importer._set_auto_increment(
            no_auto,
            importer.TableInventory("journal", 1, ("id",), "id", "integer_primary_key", 8, "b" * 64),
            "id",
        )

    auto = TargetAdapter("auto_increment", maximum=7)
    importer._set_auto_increment(
        auto,
        importer.TableInventory("journal", 1, ("id",), "id", "integer_primary_key", 9, "c" * 64),
        "id",
    )
    assert auto.next_value == 10


def test_source_fingerprint_covers_sqlite_sequence() -> None:
    first = importer.TableInventory("records", 1, ("id",), "id", "integer_primary_key", 6, "a" * 64)
    second = importer.TableInventory("records", 1, ("id",), "id", "integer_primary_key", 7, "a" * 64)

    assert importer._source_fingerprint([first]) != importer._source_fingerprint([second])


def test_schema_compatibility_ignores_only_column_order_and_known_manager_check_migration() -> None:
    from app.storage.mysql_schema import MySQLSchema

    schema = MySQLSchema("fixture")
    source = "CREATE TABLE agent_sessions (id TEXT PRIMARY KEY, metadata TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'ordinary')"
    current = "CREATE TABLE agent_sessions (id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'ordinary', metadata TEXT NOT NULL)"
    assert importer._schema_ast_key(schema, source) != importer._schema_ast_key(schema, current)
    assert importer._schema_ast_key_with_column_order_normalized(schema, source) == (
        importer._schema_ast_key_with_column_order_normalized(schema, current)
    )

    extra_column = current[:-1] + ", extra TEXT)"
    assert importer._schema_ast_key_with_column_order_normalized(schema, source) != (
        importer._schema_ast_key_with_column_order_normalized(schema, extra_column)
    )
    changed_constraint = current.replace("DEFAULT 'ordinary'", "DEFAULT 'ordinary' CHECK(kind IN ('ordinary','manager'))")
    assert importer._schema_ast_key_with_column_order_normalized(schema, source) != (
        importer._schema_ast_key_with_column_order_normalized(schema, changed_constraint)
    )

    manager_target = (
        "CREATE TABLE manager_controllers (session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "reasoning_level TEXT CHECK(reasoning_level IN ('off','low','high','max')))"
    )
    manager_historical = (
        "CREATE TABLE manager_controllers (session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "reasoning_level TEXT)"
    )
    historical_key = importer._schema_ast_key_with_column_order_normalized(
        schema, manager_target, drop_manager_reasoning_check=True,
    )
    assert historical_key == importer._schema_ast_key_with_column_order_normalized(schema, manager_historical)
    altered_definition = manager_historical.replace("reasoning_level TEXT", "reasoning_level INTEGER")
    assert historical_key != importer._schema_ast_key_with_column_order_normalized(schema, altered_definition)


def test_historical_manager_reasoning_constraint_is_checked_against_rows() -> None:
    source = _db()
    source.execute("CREATE TABLE manager_controllers(reasoning_level TEXT)")
    source.execute("INSERT INTO manager_controllers VALUES('high')")
    assert importer._manager_reasoning_values_are_supported(source)
    source.execute("INSERT INTO manager_controllers VALUES('uncatalogued')")
    assert not importer._manager_reasoning_values_are_supported(source)


def test_fk_order_tolerates_cycles_for_session_scoped_check_disable() -> None:
    source = _db()
    source.execute("CREATE TABLE first(id INTEGER PRIMARY KEY, second_id INTEGER REFERENCES second(id))")
    source.execute("CREATE TABLE second(id INTEGER PRIMARY KEY, first_id INTEGER REFERENCES first(id))")
    source.commit()

    assert importer._fk_order(source, {"first", "second"}) == ["first", "second"]


def test_report_contains_only_table_inventory_and_digests() -> None:
    item = importer.TableInventory(
        "private_rows", 2, ("secret_body",), "id", "integer_primary_key", 2, "a" * 64,
    )
    report = importer._report([item], ["sqlite_sequence"], None, "check_only")

    assert report["tables"] == [{"name": "private_rows", "rows": 2, "sha256": "a" * 64}]
    assert report["source_absent_target_tables"] == []
    assert report["external_file_snapshot_required"] is True
    assert "secret_body" not in str(report)
    assert "source_path" not in str(report)


def test_inventory_allows_known_optional_target_tables_to_be_absent() -> None:
    source = _db()
    source.execute("CREATE TABLE known_rows(value TEXT)")
    _add_fts_tables(source)
    source.commit()

    inventory, _excluded = importer._inventory_source(
        source, {"known_rows", "optional_table"},
        {
            "known_rows": ("__lka_rowid", "invisible_auto_increment"),
            "optional_table": ("__lka_rowid", "invisible_auto_increment"),
        },
    )

    assert "known_rows" in {item.name for item in inventory}
    assert "optional_table" not in {item.name for item in inventory}


def test_snapshot_open_is_read_only_and_checks_fk(tmp_path: Path) -> None:
    path = tmp_path / "source.sqlite3"
    source = sqlite3.connect(path)
    source.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
    source.execute("CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id))")
    source.execute("INSERT INTO parent VALUES(1)")
    source.execute("INSERT INTO child VALUES(2,1)")
    source.commit()
    source.close()

    snapshot = importer._open_snapshot(path)
    with pytest.raises(sqlite3.OperationalError):
        snapshot.execute("INSERT INTO parent VALUES(3)")
    snapshot.close()


def test_snapshot_refuses_nonempty_wal_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "source.sqlite3"
    sqlite3.connect(path).close()
    Path(str(path) + "-wal").write_bytes(b"pending")

    with pytest.raises(importer.ImportErrorSafe, match="WAL sidecars"):
        importer._open_snapshot(path)


def test_source_validation_receipt_is_exact_snapshot_bound(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.sqlite3"
    sqlite3.connect(snapshot).close()
    receipt = tmp_path / "validation.json"

    assert not importer._read_validation_receipt(receipt, snapshot)
    importer._write_validation_receipt(receipt, snapshot)
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert importer._read_validation_receipt(receipt, snapshot)
    snapshot.write_bytes(snapshot.read_bytes() + b"changed")
    with pytest.raises(importer.ImportErrorSafe, match="does not match"):
        importer._read_validation_receipt(receipt, snapshot)


def test_source_validation_receipt_rejects_unknown_or_incomplete_format(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.sqlite3"
    sqlite3.connect(snapshot).close()
    receipt = tmp_path / "validation.json"
    receipt.write_text(json.dumps({"format": 1}), encoding="utf-8")

    with pytest.raises(importer.ImportErrorSafe, match="unsupported format"):
        importer._read_validation_receipt(receipt, snapshot)


def test_import_wait_timeout_is_restored_even_when_body_fails() -> None:
    class FakeConnection:
        def __init__(self):
            self.timeout = 30

        def execute_native(self, statement, parameters=()):
            if statement == "SELECT @@SESSION.wait_timeout":
                return type("Result", (), {"fetchone": lambda _self: (self.timeout,)})()
            assert statement == "SET SESSION wait_timeout=%s"
            self.timeout = parameters[0]

    connection = FakeConnection()
    with pytest.raises(RuntimeError), importer._temporary_import_wait_timeout(connection):
        assert connection.timeout == 7200
        raise RuntimeError("fixture")
    assert connection.timeout == 30


def test_import_wait_timeout_reset_failure_preserves_original_and_discards() -> None:
    class FakeConnection:
        def __init__(self):
            self.timeout = 30
            self._broken = False
            self.closed = False

        def execute_native(self, statement, parameters=()):
            if statement == "SELECT @@SESSION.wait_timeout":
                return type("Result", (), {"fetchone": lambda _self: (self.timeout,)})()
            value = parameters[0]
            if value == 30 and self.timeout == 7200:
                raise OSError("connection disappeared")
            self.timeout = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

    connection = FakeConnection()
    original = ValueError("original import failure")
    with pytest.raises(ValueError, match="original import failure") as caught, connection, importer._temporary_import_wait_timeout(connection):
        raise original
    assert caught.value is original
    assert connection._broken
    assert connection.closed


def test_do_check_releases_mysql_lease_during_source_inventory(monkeypatch) -> None:
    from contextlib import contextmanager

    active = 0
    opens = []

    class FakeConfig:
        host, port, database = "lab", 3306, "lab"

    class FakeConnection:
        _schema = object()

    @contextmanager
    def fake_connect(_config, *, read_only=False):
        nonlocal active
        active += 1
        opens.append(read_only)
        try:
            yield FakeConnection()
        finally:
            active -= 1

    from app.storage import mysql, mysql_bootstrap

    monkeypatch.setattr(mysql, "connect_mysql", fake_connect)
    monkeypatch.setattr(mysql_bootstrap, "bootstrap_mysql", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "_mysql_business_tables", lambda _conn: {"rows"})
    monkeypatch.setattr(importer, "_mysql_rowid_map", lambda _conn, _tables: {"rows": ("rid", "invisible_auto_increment")})
    item = importer.TableInventory("rows", 0, ("value",), "rid", "invisible_auto_increment", None, "0" * 64)

    def inventory(_source, _tables, _rowids):
        assert active == 0
        return [item], []

    monkeypatch.setattr(importer, "_inventory_source", inventory)
    monkeypatch.setattr(importer, "_knowledge_vector_inventory", lambda _source: importer.KnowledgeVectorInventory((), 0, "0" * 64))
    monkeypatch.setattr(importer, "_validate_source_target_columns", lambda *_args: None)
    monkeypatch.setattr(importer, "_validate_mysql_vector_table", lambda *_args: None)
    monkeypatch.setattr(importer, "_target_is_empty", lambda *_args: None)
    monkeypatch.setattr(importer, "_assert_fresh_import_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "_load_trigger_definitions", lambda *_args: [])

    importer._do_check(_db(), FakeConfig(), FakeConfig(), batch_size=100)
    assert opens == [True, True, True]
    assert active == 0


@pytest.mark.skipif(not os.getenv("LKA_MYSQL_TEST_CONFIG"), reason="opt-in isolated MySQL integration")
def test_sqlite_snapshot_import_real_mysql_roundtrip(tmp_path: Path, monkeypatch, capsys) -> None:
    """Exercise real SQLite schema/service writes against an isolated empty lab."""
    import sqlite_vec

    from app.core.config import Settings
    from app.core.local_config import (
        AgentConfig,
        EmbeddingConfig,
        LLMProviderConfig,
        LocalAppConfig,
        MemoryConfig,
        MessageHistoryConfig,
        RerankerConfig,
    )
    from app.core.runtime import LocalKnowledgeAgentRuntime
    from app.domains.conversation_semantic import ConversationSemanticIndex
    from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput
    from app.domains.knowledge_retrieval import EmbeddingModelInfo, SemanticVectorRecord
    from app.domains.memory import MemoryInput, MemorySourceInput
    from app.integrations.local_semantic import SQLiteVecSemanticIndex
    from app.integrations.web_search import BraveSearchQuota
    from app.storage.context_records import ContextRecordStore

    migration_file = os.getenv("LKA_MYSQL_TEST_MIGRATION_CONFIG")
    if not migration_file:
        pytest.skip("separate migration connection is required")
    local = LocalAppConfig(
        llm=LLMProviderConfig(provider="mock"),
        agent=AgentConfig(unified_entry_enabled=False),
        auth={"enabled": True}, devices={"enabled": True},
        connections={"enabled": True},
        sync={"enabled": True, "role": "hub", "hub_id": "synthetic-import-hub",
              "memory_aggregation_enabled": True, "memory_write_enabled": True},
        remote_execution={"enabled": True},
        memory=MemoryConfig(enabled=True, background_enabled=False),
        message_history=MessageHistoryConfig(enabled=False, background_enabled=False),
        embedding=EmbeddingConfig(enabled=False), reranker=RerankerConfig(enabled=False),
    )
    monkeypatch.setattr(Settings, "load_local_config", lambda _settings: local)
    source_data = tmp_path / "source-data"
    settings = Settings(
        _env_file=None, storage_backend="sqlite", data_dir=source_data,
        workspace_roots=str(tmp_path), db_timing_enabled=False,
    )
    runtime = LocalKnowledgeAgentRuntime(settings)
    try:
        session = runtime.create_session(title="Synthetic import fixture")
        source_id = runtime.memory_service.register_source(MemorySourceInput(
            source_type="synthetic_fixture", source_ref="fixture:memory", trusted_source=True,
        ))
        memory = runtime.memory_service.create(MemoryInput(
            content="Synthetic memory survives the migration.", memory_type="fact",
            source_id=source_id, user_confirmed=True,
            metadata={"large_integer": 9007199254740993, "verbatim": "  keep spacing  "},
        ))
        document = runtime.knowledge_service.import_text_document(KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                source_type="synthetic_fixture", display_name="Synthetic vector fixture",
            ),
            title="Synthetic vector document", text="Synthetic vector source text " * 20,
            chunk_chars=200,
        ))
        with runtime._conn() as conn:
            chunk_id = str(conn.execute(
                "SELECT chunk_id FROM knowledge_chunks WHERE document_id=? ORDER BY chunk_index LIMIT 1",
                (document.document_id,),
            ).fetchone()[0])
        vector_model = EmbeddingModelInfo(
            provider_name="synthetic", model_name="fixed-float32", dimensions=4, normalized=False,
        )
        vector_index = SQLiteVecSemanticIndex(conn_factory=runtime._conn, model_info=vector_model)
        vector_index.upsert([SemanticVectorRecord(
            chunk_id=chunk_id, checksum="synthetic-checksum", vector=[0.25, -0.5, 1.0, 2.0],
        )])
        ContextRecordStore(runtime._conn).ensure_schema()

        class SyntheticProvider:
            class Info:
                index_key = "synthetic-import"
                dimensions = 4
            model_info = Info()

        semantic = ConversationSemanticIndex(runtime._conn, SyntheticProvider())
        semantic.ensure_schema()
        with runtime._conn() as conn:
            conn.execute(
                "INSERT INTO conversation_semantic_state(session_id,fingerprint,watermark,"
                "source_revision,index_revision,dimensions) VALUES(?,?,?,?,?,?)",
                (session.session.session_id, "synthetic-fingerprint", 9, 3, 2, 4),
            )
            conn.execute(
                "INSERT INTO conversation_semantic_vectors(session_id,seq,fingerprint,source_hash,vector,dimensions) "
                "VALUES(?,?,?,?,?,?)",
                (session.session.session_id, 11, "synthetic-fingerprint", "f" * 64, b"\x00LKA\xff", 4),
            )
        runtime.memory_files.generate(scope="global")
        BraveSearchQuota(runtime._conn, monthly_limit=100).reserve()
        with runtime._conn() as conn:
            sequence = conn.execute(
                "INSERT INTO shared_sync_journal(kind,entity_key) VALUES(?,?)",
                ("synthetic", "deleted-watermark"),
            ).lastrowid
            conn.execute("DELETE FROM shared_sync_journal WHERE sequence=?", (sequence,))
            source_journal = int(conn.execute(
                "SELECT coalesce(max(sequence),0) FROM shared_sync_journal",
            ).fetchone()[0])
            source_sequence = int(conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='shared_sync_journal'",
            ).fetchone()[0])
            assert source_sequence > source_journal
            assert source_journal > 0
            source_head_count = int(conn.execute("SELECT count(*) FROM shared_sync_heads").fetchone()[0])
            assert source_head_count > 0
            source_memory_fts = conn.execute(
                "SELECT memory_id,content,memory_type FROM memory_entries_fts WHERE memory_id=?",
                (memory.memory_id,),
            ).fetchone()
            assert tuple(source_memory_fts) == (
                memory.memory_id, "Synthetic memory survives the migration.", "fact",
            )
        with runtime._conn() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        runtime.stop()
    except BaseException:
        runtime.stop()
        raise

    source_path = source_data / "lka.sqlite3"
    snapshot = tmp_path / "synthetic-consistent-snapshot.sqlite3"
    import shutil

    shutil.copy2(source_path, snapshot)
    runtime_file = Path(os.environ["LKA_MYSQL_TEST_CONFIG"])
    from app.storage.mysql import connect_mysql

    runtime_config = importer._load_config(runtime_file)
    with connect_mysql(runtime_config, read_only=True) as target:
        target_database = str(target.execute_native("SELECT DATABASE()").fetchone()[0])
        target_tables = importer._mysql_business_tables(target)
        rowids = importer._mysql_rowid_map(target, target_tables)
    with importer._open_snapshot(snapshot) as source:
        inventory, _excluded = importer._inventory_source(source, target_tables, rowids)
        source_vectors = importer._knowledge_vector_inventory(source)
    source_fingerprint = importer._source_fingerprint(inventory, source_vectors)
    private_log = tmp_path / "mysql-import-test.log"
    private_log.write_text(
        f"phase=prepared\ntarget_database={target_database}\n"
        f"source_snapshot={snapshot}\nsource_fingerprint={source_fingerprint}\n"
        f"source_table_count={len(inventory)}\nvector_rows={source_vectors.rows}\n",
        encoding="utf-8",
    )
    private_log.chmod(0o600)

    def log_cli(phase: str, result: int, captured) -> None:
        with private_log.open("a", encoding="utf-8") as stream:
            stream.write(f"phase={phase}\nexit_code={result}\n")
            if captured.out:
                stream.write("stdout_begin\n" + captured.out + "stdout_end\n")
            if captured.err:
                stream.write("stderr_begin\n" + captured.err + "stderr_end\n")

    args_base = [
        "--sqlite-snapshot", str(snapshot), "--snapshot-confirmed",
        "--runtime-connection-file", str(runtime_file),
        "--migration-connection-file", migration_file,
    ]
    check_result = importer.main([*args_base, "--check-only"])
    check_capture = capsys.readouterr()
    log_cli("check_only", check_result, check_capture)
    assert check_result == 0, f"See private integration log: {private_log}"
    check_report = json.loads(check_capture.out)
    assert check_report["mode"] == "check_only"
    assert check_report["knowledge_vectors"]["rows"] == 1
    assert check_report["knowledge_vectors"]["source_tables"] == [
        f"knowledge_vec_{hashlib.sha256(vector_model.index_key.encode()).hexdigest()[:16]}",
    ]
    assert "remote_execution_node_tasks" in check_report["source_absent_target_tables"]
    assert "shared_memory_outbox" in check_report["source_absent_target_tables"]

    report_file = tmp_path / "import-report.json"
    execute_result = importer.main([*args_base, "--execute", "--report-file", str(report_file)])
    execute_capture = capsys.readouterr()
    log_cli("execute", execute_result, execute_capture)
    assert execute_result == 0, f"See private integration log: {private_log}"
    output = execute_capture.out
    execute_report = json.loads(output)
    assert execute_report["mode"] == "execute"
    assert execute_report["source_absent_target_tables"] == check_report["source_absent_target_tables"]
    assert "Synthetic memory" not in output and "fixture:memory" not in output
    assert report_file.is_file()

    from app.storage.mysql_bootstrap import bootstrap_mysql

    with connect_mysql(runtime_config, read_only=True) as conn:
        bootstrap_mysql(conn, install=False)
        row = conn.execute_native(
            "SELECT vector FROM conversation_semantic_vectors WHERE fingerprint=%s",
            ("synthetic-fingerprint",),
        ).fetchone()
        assert bytes(row[0]) == b"\x00LKA\xff"
        vector_blob = conn.execute_native(
            "SELECT vector_id,embedding FROM knowledge_vector_blobs",
        ).fetchone()
        assert int(vector_blob[0]) == 1
        assert bytes(vector_blob[1]) == sqlite_vec.serialize_float32([0.25, -0.5, 1.0, 2.0])
        assert int(conn.execute_native(
            "SELECT max(sequence) FROM shared_sync_journal",
        ).fetchone()[0]) == source_journal
        next_sequence = conn.execute_native(
            "SELECT AUTO_INCREMENT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='shared_sync_journal'",
        ).fetchone()[0]
        assert int(next_sequence) == source_sequence + 1
        assert int(conn.execute_native("SELECT count(*) FROM shared_sync_heads").fetchone()[0]) == source_head_count
        fts = conn.execute_native(
            "SELECT memory_id,content,memory_type FROM memory_entries_fts WHERE memory_id=%s",
            (memory.memory_id,),
        ).fetchone()
        assert tuple(fts) == (
            memory.memory_id, "Synthetic memory survives the migration.", "fact",
        )
        assert int(conn.execute_native("SELECT requests FROM web_search_quota LIMIT 1").fetchone()[0]) == 1

    # The durable verified marker permits normal app bootstrap; a second import
    # is rejected before touching rows, triggers, sequence values, or journals.
    with connect_mysql(runtime_config, read_only=True) as conn:
        bootstrap_mysql(conn, install=False)
        before_second_attempt = (
            int(conn.execute_native("SELECT count(*) FROM agent_sessions").fetchone()[0]),
            int(conn.execute_native("SELECT max(sequence) FROM shared_sync_journal").fetchone()[0]),
            int(conn.execute_native(
                "SELECT count(*) FROM lka_data_imports WHERE status='verified'",
            ).fetchone()[0]),
        )
    refusal_result = importer.main([*args_base, "--execute"])
    refusal_capture = capsys.readouterr()
    log_cli("second_import_refusal", refusal_result, refusal_capture)
    assert refusal_result == 2
    refusal = refusal_capture.err
    assert "SQLite/MySQL import refused" in refusal
    assert "already has an import attempt" in refusal or "target is not empty" in refusal
    with connect_mysql(runtime_config, read_only=True) as conn:
        after_second_attempt = (
            int(conn.execute_native("SELECT count(*) FROM agent_sessions").fetchone()[0]),
            int(conn.execute_native("SELECT max(sequence) FROM shared_sync_journal").fetchone()[0]),
            int(conn.execute_native(
                "SELECT count(*) FROM lka_data_imports WHERE status='verified'",
            ).fetchone()[0]),
        )
    assert after_second_attempt == before_second_attempt
