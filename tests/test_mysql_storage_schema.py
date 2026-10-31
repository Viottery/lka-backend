from __future__ import annotations

import json

import pytest

from app.storage.mysql_schema import MySQLSchema, MySQLSchemaError, verify_object_variants


def prepared(script):
    schema = MySQLSchema("lka_unit")
    return schema, schema._prepared_statements(script, schema)


def test_create_table_innodb_schema_adds_hidden_rowid_and_bounds_exact_text_keys():
    _schema, statements = prepared("""
        CREATE TABLE items (
            key TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            body TEXT NOT NULL DEFAULT '{}',
            UNIQUE(key,state), CHECK(json_valid(body))
        );
        CREATE INDEX IF NOT EXISTS items_state ON items(state);
    """)
    create, index = statements
    assert "ENGINE=InnoDB" in create.sql
    assert "COLLATE=utf8mb4_0900_bin" in create.sql
    assert "VARCHAR(384)" in create.sql
    assert "COLLATE utf8mb4_0900_bin" in create.sql
    assert "LONGTEXT NOT NULL DEFAULT ('{}')" in create.sql
    assert "JSON_VALID(body)" in create.sql
    assert "__lka_rowid BIGINT UNSIGNED NOT NULL AUTO_INCREMENT INVISIBLE UNIQUE" in create.sql
    assert create.rowid_column == "__lka_rowid"
    assert create.rowid_kind == "invisible_auto_increment"
    assert [(column.name, column.declared_type) for column in create.columns] == [
        ("key", "TEXT"), ("state", "TEXT"), ("body", "TEXT"),
    ]
    assert index.sql == "CREATE INDEX items_state ON items(state)"
    assert index.original_sql.startswith("CREATE INDEX IF NOT EXISTS")


def test_integer_primary_key_reuses_sqlite_rowid_and_is_auto_increment():
    _, statements = prepared("CREATE TABLE things(id INTEGER PRIMARY KEY, body TEXT)")
    create = statements[0]
    assert "id INT PRIMARY KEY AUTO_INCREMENT" in create.sql
    assert "__lka_rowid" not in create.sql
    assert create.rowid_column == "id"
    assert create.rowid_kind == "integer_primary_key"


def test_text_collation_precedes_default_and_check_constraints_in_mysql_ddl():
    _, statements = prepared(
        "CREATE TABLE states(status TEXT NOT NULL DEFAULT 'paused' "
        "CHECK(status IN ('paused','open')), UNIQUE(status))"
    )
    sql = statements[0].sql
    assert "status VARCHAR(768) COLLATE utf8mb4_0900_bin NOT NULL DEFAULT 'paused' " \
        "CHECK (status IN ('paused', 'open'))" in sql


@pytest.mark.parametrize(
    "script",
    [
        "CREATE VIRTUAL TABLE f USING fts5(content)",
        "CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT 1; END",
        "CREATE INDEX partial_idx ON x(status) WHERE status='active'",
        "CREATE INDEX expr_idx ON x(json_extract(payload,'$.id'))",
        "CREATE TABLE x(id INTEGER PRIMARY KEY) WITHOUT ROWID",
    ],
)
def test_unsupported_ddl_fails_closed(script):
    with pytest.raises(MySQLSchemaError):
        prepared(script)


pytest.importorskip("sqlglot")

class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _CatalogConn:
    def __init__(self):
        self.info = [(0, "session_id", "TEXT", 1, None, 1),
                     (1, "metadata", "TEXT", 1, "{}", 0)]

    def execute_native(self, sql, parameters=()):
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            assert parameters == ("agent_sessions",)
            return _Rows(self.info)
        raise AssertionError(sql)


def test_pragma_table_info_emulation_uses_recorded_sqlite_shape(monkeypatch):
    schema = MySQLSchema("lka_unit")
    monkeypatch.setattr(schema, "table_info", lambda conn, table: _CatalogConn().info)
    columns, rows = schema.pragma_table_info_query(_CatalogConn(), "PRAGMA table_info('agent_sessions')")
    assert columns == ("cid", "name", "type", "notnull", "dflt_value", "pk")
    assert rows[0] == (0, "session_id", "TEXT", 1, None, 1)


def test_catalog_query_emulates_current_table_and_index_queries(monkeypatch):
    schema = MySQLSchema("lka_unit")
    monkeypatch.setattr(
        schema, "master_objects",
        lambda conn, **kwargs: [("agent_sessions", "CREATE TABLE ...")] if kwargs.get("names") else [
            ("agent_sessions", "CREATE TABLE ..."), ("idx_sessions", "CREATE INDEX ..."),
        ],
    )
    conn = object()
    assert schema.catalog_query(
        conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", ("agent_sessions",),
    ) == (("1",), [(1,)])
    columns, rows = schema.catalog_query(
        conn, "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='agent_sessions' "
        "AND sql IS NOT NULL",
    )
    assert columns == ("name", "sql")
    assert rows == [("agent_sessions", "CREATE TABLE ..."), ("idx_sessions", "CREATE INDEX ...")]


def test_identifier_validation_prevents_catalog_sql_injection():
    schema = MySQLSchema("lka_unit")
    with pytest.raises(ValueError, match="identifier"):
        schema.table_exists(object(), "sessions; DROP DATABASE x")


def test_if_not_exists_table_probe_only_skips_matching_bootstrapped_definition(monkeypatch):
    schema = MySQLSchema("lka_unit")
    conn = object()
    monkeypatch.setattr(schema, "table_exists", lambda _conn, name: name == "items")
    monkeypatch.setattr(
        schema, "master_objects",
        lambda _conn, **kwargs: [("items", "CREATE TABLE items (id INTEGER PRIMARY KEY, body TEXT)")],
    )
    monkeypatch.setattr(schema, "object_source_variants", lambda *_args: set())
    prepared = schema._prepared_statements(
        "CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, body TEXT)", schema, conn,
    )
    assert prepared == [None]
    monkeypatch.setattr(
        schema, "master_objects",
        lambda _conn, **kwargs: [("items", "CREATE TABLE items (id INTEGER PRIMARY KEY, other TEXT)")],
    )
    with pytest.raises(MySQLSchemaError, match="does not match"):
        schema._prepared_statements(
            "CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, body TEXT)", schema, conn,
        )


def test_existing_ddl_probe_accepts_only_exact_reviewed_source_variant(monkeypatch):
    schema = MySQLSchema("lka_unit")
    conn = object()
    base = "CREATE TABLE llm_workload_usage(call_id TEXT PRIMARY KEY, cost_known INTEGER)"
    legacy = "CREATE TABLE llm_workload_usage(call_id TEXT PRIMARY KEY)"
    monkeypatch.setattr(schema, "master_objects", lambda *_args, **_kwargs: [("llm_workload_usage", base)])
    monkeypatch.setattr(
        schema, "object_source_variants",
        lambda *_args: {schema._normalize_probe_sql(legacy)},
    )
    schema._verify_existing_probe(conn, "table", "llm_workload_usage", legacy)
    with pytest.raises(MySQLSchemaError, match="does not match"):
        schema._verify_existing_probe(
            conn, "table", "llm_workload_usage",
            "CREATE TABLE llm_workload_usage(call_id TEXT PRIMARY KEY, cost_known INTEGER, unexpected TEXT)",
        )


def test_existing_probe_accepts_only_lexical_ast_equivalence(monkeypatch):
    schema = MySQLSchema("lka_unit")
    conn = object()
    base = "CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT NOT NULL DEFAULT 'Mixed Case')"
    monkeypatch.setattr(schema, "master_objects", lambda *_args, **_kwargs: [("items", base)])
    monkeypatch.setattr(schema, "object_source_variants", lambda *_args: set())
    schema._verify_existing_probe(
        conn, "table", "items",
        'CREATE TABLE IF NOT EXISTS `ITEMS` (`ID` INTEGER PRIMARY KEY, `BODY` TEXT NOT NULL DEFAULT \'Mixed Case\')',
    )
    with pytest.raises(MySQLSchemaError, match="does not match"):
        schema._verify_existing_probe(
            conn, "table", "items",
            "CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT NOT NULL DEFAULT 'mixed case')",
        )


def test_v2_variant_manifest_verification_is_exact(monkeypatch):
    schema = MySQLSchema("lka_unit")
    reviewed = "CREATE TABLE llm_workload_usage(call_id TEXT PRIMARY KEY)"
    monkeypatch.setattr(
        schema, "object_source_variants",
        lambda _conn, _kind, _name: {schema._normalize_probe_sql(reviewed)},
    )
    records = [{"object_type": "table", "object_name": "llm_workload_usage", "source_sql": reviewed,
                "columns": ["call_id"]}]
    verify_object_variants(object(), schema, records)
    with pytest.raises(MySQLSchemaError, match="do not match v2 manifest"):
        verify_object_variants(object(), schema, [
            {**records[0], "source_sql": "CREATE TABLE llm_workload_usage(call_id TEXT)"},
        ])


def test_trigger_and_external_fts_probes_require_bootstrapped_source_match(monkeypatch):
    schema = MySQLSchema("lka_unit")

    class _TriggerConn:
        def execute_native(self, _sql, _parameters=()):
            return _Rows([(1,)])

    trigger = "CREATE TRIGGER IF NOT EXISTS tr AFTER INSERT ON items BEGIN UPDATE items SET id=id; END"
    monkeypatch.setattr(
        schema, "master_objects",
        lambda _conn, **kwargs: [("tr", "CREATE TRIGGER tr AFTER INSERT ON items BEGIN UPDATE items SET id=id; END")],
    )
    assert schema._prepared_statements(trigger, schema, _TriggerConn()) == [None]

    fts = "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(content)"
    monkeypatch.setattr(
        schema, "master_objects",
        lambda _conn, **kwargs: [("docs_fts", "CREATE VIRTUAL TABLE docs_fts USING fts5(content)")],
    )
    assert schema._prepared_statements(fts, schema, _TriggerConn()) == [None]


def test_external_trigger_probe_is_noop_only_when_manifest_source_matches(monkeypatch):
    schema = MySQLSchema("lka_unit")

    class _TriggerConn:
        def execute_native(self, _sql, _parameters=()):
            return _Rows([])

    trigger = """CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memory_entries BEGIN
        SELECT ';';
    END;
    CREATE TRIGGER IF NOT EXISTS conversation_history_source_delete AFTER DELETE ON messages BEGIN
        DELETE FROM conversation_history_dirty WHERE seq=OLD.seq;
    END;"""
    definitions = {
        "memory_fts_insert": "CREATE TRIGGER memory_fts_insert AFTER INSERT ON memory_entries BEGIN SELECT ';'; END",
        "conversation_history_source_delete": "CREATE TRIGGER conversation_history_source_delete AFTER DELETE ON messages BEGIN DELETE FROM conversation_history_dirty WHERE seq=OLD.seq; END",
    }
    monkeypatch.setattr(
        schema, "master_objects",
        lambda _conn, **kwargs: [(name, definitions[name]) for name in kwargs.get("names", [])],
    )
    assert schema._prepared_statements(trigger, schema, _TriggerConn()) == [None, None]


def test_schema_script_splitter_keeps_nested_trigger_statements_whole():
    script = """CREATE TABLE t(id INTEGER PRIMARY KEY);
    CREATE TRIGGER tr1 AFTER INSERT ON t BEGIN
      INSERT INTO t VALUES(2);
      UPDATE t SET id=CASE WHEN id=1 THEN 3 ELSE id END WHERE id=2;
    END;
    CREATE INDEX t_id ON t(id);"""
    statements = MySQLSchema._split_sql_statements(script)
    assert len(statements) == 3
    assert statements[0].startswith("CREATE TABLE")
    assert statements[1].startswith("CREATE TRIGGER") and statements[1].endswith("END")
    assert statements[2] == "CREATE INDEX t_id ON t(id)"


def test_unique_key_mapping_translates_only_with_present_collision_guard():
    schema = MySQLSchema("lka_unit")

    class _MappingConn:
        def __init__(self, guard_exists=True, guard_names=("path_key_collision_guard",)):
            self.guard_exists = guard_exists
            self.guard_names = guard_names

        def execute_native(self, statement, parameters=()):
            if "FROM `lka_mysql_rowid_columns`" in statement:
                return _Rows([("__lka_rowid",)])
            if "INFORMATION_SCHEMA.STATISTICS" in statement:
                return _Rows([
                    ("__lka_rowid", 0, 1, "__lka_rowid"),
                    ("path_key_hash_unique", 0, 1, "path_key_hash"),
                ])
            if "FROM `lka_mysql_unique_key_mappings`" in statement:
                return _Rows([(
                    json.dumps(["path_key"]), json.dumps(["path_key_hash"]),
                    json.dumps(["path_key_collision_guard"]),
                )])
            if "INFORMATION_SCHEMA.TRIGGERS" in statement:
                return _Rows([(name,) for name in self.guard_names] if self.guard_exists else [])
            raise AssertionError(statement)

    assert schema.validate_conflict_target(
        _MappingConn(), "memory_project_paths", ("path_key",),
    )
    assert not schema.validate_conflict_target(
        _MappingConn(guard_exists=False), "memory_project_paths", ("path_key",),
    )


def test_metadata_schema_upgrades_legacy_proxy_kind_width():
    schema = MySQLSchema("lka_unit")

    class _MetadataConn:
        in_transaction = False

        def __init__(self, width):
            self.width = width
            self.statements = []

        def execute_native(self, statement, _parameters=()):
            self.statements.append(statement)
            if "CHARACTER_MAXIMUM_LENGTH" in statement:
                return _Rows([(self.width,)])
            return _Rows([])

    conn = _MetadataConn(16)
    schema.ensure_metadata(conn)
    assert any("ALTER TABLE `lka_mysql_rowid_columns` MODIFY proxy_kind VARCHAR(32)" in sql
               for sql in conn.statements)
    assert any("proxy_kind VARCHAR(32)" in sql for sql in conn.statements if "CREATE TABLE IF NOT EXISTS `lka_mysql_rowid_columns`" in sql)
