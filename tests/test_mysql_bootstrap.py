from __future__ import annotations

import pytest

sqlglot = pytest.importorskip("sqlglot")
exp, parse = sqlglot.exp, sqlglot.parse

from app.storage.mysql_bootstrap import (
    _align_foreign_key_text_columns,
    _apply_coordinator_owner_schema,
    _index_sql,
    _load_schema_asset,
    _load_variants_v2,
    _load_variants_v3,
    _prepare_table,
    _simple_index_parts,
    _table_source_columns,
    _trigger_sql,
)
from app.storage.mysql_schema import MySQLSchema, MySQLSchemaError


def _prepared_tables(asset):
    schema = MySQLSchema("lka_bootstrap_unit")
    objects = asset["objects"]
    prepared = {}
    for item in objects:
        if item["object_type"] != "table":
            continue
        unique_groups = []
        for index in objects:
            if index["object_type"] != "index" or index["table_name"].casefold() != item["name"].casefold():
                continue
            create, _table, names = _simple_index_parts(index["source_sql"], allow_partial=True)
            if names is not None and create.args.get("unique"):
                unique_groups.append(names)
        prepared[item["name"]] = _prepare_table(
            schema, item, unique_groups, asset["ports"]["unbounded_unique_text_hashes"],
        )
    return _align_foreign_key_text_columns(prepared)


def test_v1_asset_is_complete_and_excludes_fts_shadow_tables():
    asset, _raw = _load_schema_asset()
    assert asset["expected_runtime_object_count"] == len(asset["objects"])
    assert asset["source_object_count"] == 231
    assert asset["excluded_fts_shadow_count"] == 25
    names = {(item["object_type"], item["name"]) for item in asset["objects"]}
    assert ("fts", "memory_entries_fts") in names
    assert ("fts", "conversation_history_fts") in names
    assert ("external_trigger", "memory_fts_insert") in names
    assert not any("_fts_" in name and name.endswith("_config") for _, name in names)
    assert len([item for item in asset["objects"] if item.get("runtime_source")]) == 55


def test_v2_source_variants_are_exact_reviewed_runtime_create_statements():
    v1, _ = _load_schema_asset()
    v2, _ = _load_variants_v2()
    expected_objects = {
        "agent_session_context_state", "llm_workload_usage", "manager_controllers",
        "manager_delegates", "message_history_policies", "message_reading_families",
        "message_reading_schedules",
    }
    assert v2["version"] == 2
    assert {item["object_name"] for item in v2["variants"]} == expected_objects
    base_sql = {
        item["name"]: item["source_sql"]
        for item in v1["objects"] if item["object_type"] == "table"
    }
    for variant in v2["variants"]:
        create = parse(variant["source_sql"], read="sqlite")[0]
        assert isinstance(create, exp.Create)
        assert create.this.this.name == variant["object_name"]
        assert variant["source_sql"] != base_sql[variant["object_name"]]


def test_v3_variants_cover_remaining_structural_legacy_creates():
    v1, _ = _load_schema_asset()
    v3, _ = _load_variants_v3()
    expected_objects = {
        "manager_goals", "message_history_conversations", "message_history_messages",
    }
    assert v3["version"] == 3
    assert {item["object_name"] for item in v3["variants"]} == expected_objects
    base_sql = {
        item["name"]: item["source_sql"]
        for item in v1["objects"] if item["object_type"] == "table"
    }
    for variant in v3["variants"]:
        create = parse(variant["source_sql"], read="sqlite")[0]
        assert isinstance(create, exp.Create)
        assert create.this.this.name == variant["object_name"]
        assert variant["source_sql"] != base_sql[variant["object_name"]]


def test_every_manifest_table_index_and_native_trigger_has_a_reviewed_port():
    asset, _raw = _load_schema_asset()
    prepared = _prepared_tables(asset)
    assert len(prepared) == 183
    for item in asset["objects"]:
        if item["object_type"] == "index":
            _index_sql(item, {}, {})
        elif item["object_type"] == "trigger" and item["handling"] == "mysql":
            assert _trigger_sql(item["source_sql"]).startswith("CREATE TRIGGER")

    # No indexed key may retain an unbounded text component. All source text
    # converted to a bounded key gets a CHECK instead of silent truncation.
    for name, ddl in prepared.items():
        tree = parse(ddl.sql, read="mysql")[0]
        columns = {
            column.name.casefold(): column
            for column in tree.this.expressions if isinstance(column, exp.ColumnDef)
        }
        for index in tree.this.expressions:
            if isinstance(index, exp.UniqueColumnConstraint):
                key = index.args.get("this")
                if isinstance(key, exp.Schema):
                    for part in key.expressions:
                        assert columns[part.name.casefold()].args["kind"].this != exp.DType.LONGTEXT
        if name in {"knowledge_documents", "memory_sources", "mail_sync_state"}:
            assert "COLLATE utf8mb4_0900_bin" in ddl.sql


def test_fk_text_endpoints_share_no_pad_type_and_fail_closed_length_checks():
    asset, _raw = _load_schema_asset()
    prepared = _prepared_tables(asset)
    trees = {name.casefold(): parse(item.sql, read="mysql")[0] for name, item in prepared.items()}
    definitions = {
        name: {
            column.name.casefold(): column
            for column in tree.this.expressions if isinstance(column, exp.ColumnDef)
        }
        for name, tree in trees.items()
    }
    edge_count = 0
    for table, tree in trees.items():
        for fk in tree.this.expressions:
            if not isinstance(fk, exp.ForeignKey):
                continue
            reference = fk.args["reference"].this
            parent = reference.this.name.casefold()
            for left, right in zip(fk.expressions, reference.expressions):
                child_col, parent_col = left.name.casefold(), right.name.casefold()
                child_type = definitions[table][child_col].args["kind"].sql(dialect="mysql")
                parent_type = definitions[parent][parent_col].args["kind"].sql(dialect="mysql")
                assert child_type == parent_type
                edge_count += 1
    assert edge_count >= 25
    assert "CHAR_LENGTH(`run_id`) <= 767" in prepared["agent_run_events"].sql
    assert "COLLATE utf8mb4_0900_bin" in prepared["agent_run_events"].sql


def test_unbounded_composite_keys_hash_every_text_component_with_collision_guards():
    asset, _raw = _load_schema_asset()
    ports = asset["ports"]["unbounded_unique_text_hashes"]
    prepared = _prepared_tables(asset)
    expected = {
        ("knowledge_documents", "uri"), ("knowledge_documents", "checksum"),
        ("memory_sources", "source_type"), ("memory_sources", "source_ref"),
        ("memory_sources", "checksum"),
        ("mail_sync_state", "provider"), ("mail_sync_state", "account_id"),
        ("mail_sync_state", "folder"), ("memory_project_paths", "path_key"),
    }
    assert {(port["table"], port["column"]) for port in ports} == expected
    for port in ports:
        assert len(port["guards"]) == 2
        assert port["generated_column"] in prepared[port["table"]].sql
    for table in ("knowledge_documents", "memory_sources", "mail_sync_state"):
        sql = prepared[table].sql
        assert "UNIQUE" in sql
        assert "LONGTEXT" in sql  # full source values remain stored, not truncated
        assert all(port["generated_column"] in sql for port in ports if port["table"] == table)


def test_nonunique_partial_index_widens_and_expression_index_is_explicit():
    asset, _raw = _load_schema_asset()
    partial = next(
        item for item in asset["objects"]
        if item["object_type"] == "index" and "WHERE" in item["source_sql"].upper()
    )
    ddl, _columns, _prefixes, unique = _index_sql(partial, {}, {})
    assert not unique
    assert " WHERE " not in ddl.upper()
    expression = next(
        item for item in asset["objects"]
        if item["object_type"] == "index" and item["name"] == "idx_agent_runs_parent_run_id"
    )
    ddl, columns, prefixes, unique = _index_sql(expression, {}, {})
    assert columns == ("parent_run_id_key",)
    assert prefixes == (None,)
    assert not unique
    assert "parent_run_id_key" in ddl


def test_sqlite_blob_is_preserved_as_mysql_longblob():
    asset, _raw = _load_schema_asset()
    item = next(
        value for value in asset["objects"]
        if value["object_type"] == "table" and value["name"] == "conversation_semantic_vectors"
    )
    prepared = _prepare_table(
        MySQLSchema("lka_bootstrap_unit"), item, [],
        asset["ports"]["unbounded_unique_text_hashes"],
    )
    assert "vector LONGBLOB NOT NULL" in prepared.sql
    assert "VARBINARY NOT NULL" not in prepared.sql


def test_nontext_index_parts_never_receive_string_prefixes():
    asset, _raw = _load_schema_asset()
    item = next(
        value for value in asset["objects"]
        if value["object_type"] == "index"
        and value["name"] == "idx_context_messages_role_seq"
    )
    table = next(
        value for value in asset["objects"]
        if value["object_type"] == "table"
        and value["name"] == item["table_name"]
    )
    source_types, _groups = _table_source_columns(table["source_sql"])
    ddl, columns, prefixes, unique = _index_sql(item, source_types, {})
    assert not unique
    assert columns == ("session_id", "role", "seq")
    assert prefixes == (191, 191, None)
    assert "SEQ(191)" not in ddl.upper()


def test_no_prefix_unique_index_port_exists_in_manifest():
    asset, _raw = _load_schema_asset()
    prepared = _prepared_tables(asset)
    for item in asset["objects"]:
        if item["object_type"] != "index":
            continue
        source, _table, _names = _simple_index_parts(item["source_sql"], allow_partial=True)
        table_source = next(
            table for table in asset["objects"]
            if table["object_type"] == "table"
            and table["name"].casefold() == item["table_name"].casefold()
        )
        source_types, _groups = _table_source_columns(table_source["source_sql"])
        ddl, _columns, prefixes, unique = _index_sql(
            item,
            source_types,
            {
                port["column"].casefold(): port["generated_column"]
                for port in asset["ports"]["unbounded_unique_text_hashes"]
                if port["table"].casefold() == item["table_name"].casefold()
            },
        )
        if source.args.get("unique"):
            assert unique
            assert not any(prefix is not None for prefix in prefixes)
            assert "UNIQUE" in ddl.upper()
        assert item["table_name"] in prepared


class _OneRow:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _OwnerSchemaConnection:
    def __init__(self, exists):
        self.exists = exists
        self.statements = []

    def execute_native(self, sql, parameters=()):
        self.statements.append((sql, parameters))
        return _OneRow((1,) if self.exists else None)


def test_runtime_owner_schema_check_is_read_only_and_migration_installs_explicitly():
    existing = _OwnerSchemaConnection(exists=True)
    _apply_coordinator_owner_schema(existing, install=False)
    assert len(existing.statements) == 1
    assert "INFORMATION_SCHEMA.TABLES" in existing.statements[0][0]

    missing = _OwnerSchemaConnection(exists=False)
    try:
        _apply_coordinator_owner_schema(missing, install=False)
    except MySQLSchemaError as exc:
        assert "run the schema migration" in str(exc)
    else:
        raise AssertionError("runtime verification must reject a missing owner table")
    assert len(missing.statements) == 1

    migration = _OwnerSchemaConnection(exists=False)
    _apply_coordinator_owner_schema(migration, install=True)
    assert len(migration.statements) == 2
    assert "CREATE TABLE IF NOT EXISTS lka_coordinator_owner" in migration.statements[1][0]


@pytest.mark.parametrize('table', ['lka_data_imports', 'lka_checkpoint_imports'])
@pytest.mark.parametrize('incomplete', [True, False])
def test_bootstrap_checks_import_completion_before_accepting_data(table, incomplete):
    from types import SimpleNamespace

    from app.storage.mysql_bootstrap import _verify_completed_imports

    class Cursor:
        def __init__(self, rows):
            self.rows = rows
        def fetchall(self):
            return self.rows
        def fetchone(self):
            return self.rows[0] if self.rows else None

    statements = []
    def execute(statement, parameters=()):
        statements.append(statement)
        return Cursor([(table,)]) if 'information_schema' in statement else Cursor([(1,)] if incomplete else [])

    conn = SimpleNamespace(_pool=SimpleNamespace(config=SimpleNamespace(database='lka_import_test')),
                           execute_native=execute)
    if incomplete:
        with pytest.raises(MySQLSchemaError, match='data import is incomplete'):
            _verify_completed_imports(conn)
    else:
        _verify_completed_imports(conn)
    assert len(statements) == 2


@pytest.mark.parametrize("table_exists,marked", [(False, False), (True, False), (True, True)])
def test_fulltext_recovery_requires_an_existing_owned_migration_marker(monkeypatch, table_exists, marked):
    from types import SimpleNamespace

    from app.storage import mysql_fulltext_collation
    from app.storage.mysql_bootstrap import _resume_fulltext_collation_install

    calls = []
    def execute(sql, params=()):
        return _OneRow((1,) if (table_exists if 'information_schema' in sql else marked) else None)
    monkeypatch.setattr(mysql_fulltext_collation, 'install_fulltext_collation_v12',
                        lambda conn, *, install: calls.append(install))
    _resume_fulltext_collation_install(SimpleNamespace(execute_native=execute))
    assert calls == ([True] if table_exists and marked else [])


@pytest.mark.parametrize('install', [True, False])
def test_bootstrap_repairs_marked_fulltext_before_the_base_installer(monkeypatch, install):
    from types import SimpleNamespace

    from app.storage import mysql_bootstrap

    order = []
    monkeypatch.setattr(mysql_bootstrap, '_load_schema_asset', lambda: ({}, b''))
    monkeypatch.setattr(mysql_bootstrap, '_verify_completed_imports', lambda _: None)
    monkeypatch.setattr(mysql_bootstrap, '_mysql_database', lambda _: 'synthetic_test')
    monkeypatch.setattr(mysql_bootstrap, '_resume_fulltext_collation_install',
                        lambda _: order.append('repair'))
    def base(*args, **kwargs):
        order.append('base')
        raise RuntimeError('stop before unrelated schema installation')
    monkeypatch.setattr(mysql_bootstrap, '_run_migration', base)
    with pytest.raises(RuntimeError, match='stop before unrelated'):
        mysql_bootstrap.bootstrap_mysql(SimpleNamespace(in_transaction=False), install=install)
    assert order == (['repair', 'base'] if install else ['base'])
