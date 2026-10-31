from __future__ import annotations

import pytest

from app.storage.mysql_dialect import MySQLDialectError, split_script, translate_sql

pytest.importorskip("sqlglot")

def test_sqlglot_translation_keeps_qmarks_outside_literals_and_comments():
    sql = translate_sql("SELECT '?' AS literal FROM t WHERE id=? -- ? stays a comment")
    assert "'?' AS literal" in sql
    assert "id = %s" in sql
    assert "? stays a comment" in sql


def test_qmark_translation_escapes_literal_percent_for_pymysql_formatting():
    sql = translate_sql("SELECT '100%' AS literal, ? AS value WHERE 'ready 50%' LIKE ? -- 20% note")
    assert "'100%%' AS literal" in sql
    assert "'ready 50%%' LIKE %s" in sql
    assert "20%% note" in sql


def test_json_extract_and_json_valid_translate_to_mysql_functions():
    sql = translate_sql("SELECT json_extract(payload,'$.run_id') FROM t WHERE json_valid(payload)")
    assert "JSON_UNQUOTE(JSON_EXTRACT(payload, '$.run_id')) COLLATE utf8mb4_0900_bin" in sql
    assert "JSON_VALID(payload)" in sql


def test_json_text_equality_uses_nopad_binary_collation():
    sql = translate_sql(
        "SELECT 1 FROM agent_safety_reviews WHERE review_payload ->> '$.mode' = ?",
    )
    assert "JSON_UNQUOTE(JSON_EXTRACT(review_payload, '$.mode'))" in sql
    assert sql.endswith("COLLATE utf8mb4_0900_bin = %s")
    assert "WHEN 'NULL' THEN NULL" in sql


def test_json_type_maps_sqlite_type_names_and_boolean_values():
    sql = translate_sql("SELECT json_type(payload,'$.field') FROM t")
    assert "WHEN 'STRING' THEN 'text' COLLATE utf8mb4_0900_bin" in sql
    assert "WHEN 'INTEGER' THEN 'integer' COLLATE utf8mb4_0900_bin" in sql
    assert ("WHEN 'BOOLEAN' THEN IF(JSON_UNQUOTE(JSON_EXTRACT(payload, '$.field')) "
            "COLLATE utf8mb4_0900_bin = 'true', 'true' COLLATE utf8mb4_0900_bin, "
            "'false' COLLATE utf8mb4_0900_bin)") in sql


def test_json_each_supports_dynamic_array_and_object_iteration():
    sql = translate_sql("SELECT d.value FROM t, json_each(t.payload,'$.deferred') d")
    assert "JSON_TABLE(CASE JSON_TYPE(JSON_EXTRACT(t.payload, '$.deferred'))" in sql
    assert "WHEN 'ARRAY' THEN JSON_EXTRACT(t.payload, '$.deferred')" in sql
    assert "COLUMNS(entry LONGTEXT PATH '$', ordinal FOR ORDINALITY)" in sql
    object_sql = translate_sql(
        "SELECT people.key, people.value FROM t, json_each(t.payload,'$.people') people"
    )
    assert "JSON_KEYS(t.payload, '$.people')" in object_sql
    assert "JSON_QUOTE(people.entry)" in object_sql
    assert "people.entry" in object_sql
    assert "people.ordinal - 1" in object_sql
    unaliased_sql = translate_sql("SELECT value FROM json_each('[1,2]')")
    assert "AS _lka_json_each_0" in unaliased_sql
    assert "JSON_EXTRACT('[1,2]', CASE JSON_TYPE(JSON_EXTRACT('[1,2]', '$'))" in unaliased_sql
    assert "CONCAT('$', '[', _lka_json_each_0.ordinal - 1, ']')" in unaliased_sql


def test_upsert_requires_verified_single_unique_target_and_uses_mysql_values():
    statement = "INSERT INTO t(id,value) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value"
    with pytest.raises(MySQLDialectError, match="sole unique target"):
        translate_sql(statement)
    sql = translate_sql(statement, conflict_target=lambda table, columns: table == "t" and columns == ("id",))
    assert "ON DUPLICATE KEY UPDATE value = VALUES(value)" in sql
    assert "%s, %s" in sql


def test_insert_or_ignore_becomes_narrow_noop_upsert_and_replace_is_rejected():
    sql = translate_sql(
        "INSERT OR IGNORE INTO t(id,value) VALUES(?,?)",
        ignore_target=lambda table: "id" if table == "t" else None,
    )
    assert "ON DUPLICATE KEY UPDATE id = id" in sql
    with pytest.raises(MySQLDialectError, match="absence of triggers"):
        translate_sql("INSERT OR IGNORE INTO t(id) VALUES(?)")
    with pytest.raises(MySQLDialectError, match="delete-and-insert"):
        translate_sql("INSERT OR REPLACE INTO t(id) VALUES(?)")


def test_rowid_requires_schema_mapping_and_keeps_sqlite_result_name():
    with pytest.raises(MySQLDialectError, match="schema identity metadata"):
        translate_sql("SELECT rowid FROM t")
    sql = translate_sql("SELECT x.rowid FROM t x", rowid_column=lambda table: "__lka_rowid")
    assert "SELECT x.__lka_rowid AS rowid FROM t AS x" in sql
    sql = translate_sql("SELECT rowid AS cursor FROM t", rowid_column=lambda table: "id")
    assert "SELECT id AS `cursor` FROM t" in sql


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("PRAGMA busy_timeout=5", "PRAGMA"),
        ("SELECT name FROM sqlite_master", "catalog"),
        ("CREATE VIRTUAL TABLE f USING fts5(content)", "PRAGMA"),
        ("CREATE TRIGGER x AFTER INSERT ON t BEGIN SELECT 1; END", "PRAGMA"),
        ("SELECT value FROM json_tree(payload)", "json_tree"),
    ],
)
def test_unported_sqlite_features_fail_closed(statement, message):
    with pytest.raises(MySQLDialectError, match=message):
        translate_sql(statement)


def test_sqlite_begin_immediate_is_explicitly_mapped_without_fifo_claim():
    assert translate_sql("BEGIN IMMEDIATE") == "START TRANSACTION"
    with pytest.raises(MySQLDialectError, match="EXCLUSIVE"):
        translate_sql("BEGIN EXCLUSIVE")


def test_script_parser_preserves_statement_boundaries():
    assert split_script("CREATE TABLE t(id INTEGER PRIMARY KEY); INSERT INTO t VALUES(1);") == [
        "CREATE TABLE t (id INTEGER PRIMARY KEY)",
        "INSERT INTO t VALUES (1)",
    ]


def test_json_type_has_reviewed_sqlite_name_compatibility_mapping():
    sql = translate_sql("SELECT json_type(payload,'$.x') FROM t")
    assert "WHEN 'STRING' THEN 'text' COLLATE utf8mb4_0900_bin" in sql
    assert "WHEN 'INTEGER' THEN 'integer' COLLATE utf8mb4_0900_bin" in sql
    with pytest.raises(MySQLDialectError, match="parameterized json_type"):
        translate_sql("SELECT json_type(payload,?) FROM t")
