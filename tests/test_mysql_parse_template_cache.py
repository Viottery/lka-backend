from __future__ import annotations

import pytest

sqlglot = pytest.importorskip("sqlglot")

import app.storage.mysql_dialect as dialect
from app.storage.mysql_dialect import (
    _PARSE_TEMPLATE_CACHE,
    _PARSE_TEMPLATE_CACHE_MAX_ENTRIES,
    MySQLDialectError,
    _parse_template_cache_clear,
    translate_sql,
)


@pytest.fixture(autouse=True)
def clear_parse_template_cache():
    _parse_template_cache_clear()
    yield
    _parse_template_cache_clear()


def test_repeated_short_statement_parses_once(monkeypatch):
    original_parse = sqlglot.parse
    calls = 0

    def counted_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(sqlglot, "parse", counted_parse)
    statement = "SELECT value FROM things WHERE id=?"
    assert translate_sql(statement) == translate_sql(statement)
    assert calls == 1


def test_cached_upsert_ast_is_copied_and_conflict_callback_runs_each_time():
    statement = "INSERT INTO t(id,value) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value"
    seen = []

    def accepted(table, columns):
        seen.append((table, columns))
        return True

    first = translate_sql(statement, conflict_target=accepted)
    assert "ON DUPLICATE KEY UPDATE value = VALUES(value)" in first
    with pytest.raises(MySQLDialectError, match="sole unique target"):
        translate_sql(statement, conflict_target=lambda _table, _columns: False)
    second = translate_sql(statement, conflict_target=accepted)

    assert second == first
    assert seen == [("t", ("id",)), ("t", ("id",))]


def test_cached_rowid_ast_uses_fresh_schema_callback_result_and_rechecks_rejection():
    statement = "SELECT rowid FROM t"
    first = translate_sql(statement, rowid_column=lambda table: "first_id" if table == "t" else None)
    assert "SELECT first_id AS rowid FROM t" in first

    with pytest.raises(MySQLDialectError, match="missing rowid identity mapping"):
        translate_sql(statement, rowid_column=lambda _table: None)

    second = translate_sql(statement, rowid_column=lambda table: "second_id" if table == "t" else None)
    assert "SELECT second_id AS rowid FROM t" in second
    assert "first_id" not in second


def test_oversized_statement_bypasses_cache(monkeypatch):
    original_parse = sqlglot.parse
    calls = 0

    def counted_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(sqlglot, "parse", counted_parse)
    statement = "SELECT 1 /*" + ("x" * 5000) + "*/"
    assert translate_sql(statement) == translate_sql(statement)
    assert calls == 2
    assert not _PARSE_TEMPLATE_CACHE


def test_unusually_large_ast_bypasses_cache(monkeypatch):
    original_parse = sqlglot.parse
    calls = 0

    def counted_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(sqlglot, "parse", counted_parse)
    monkeypatch.setattr(dialect, "_PARSE_TEMPLATE_CACHE_MAX_AST_NODES", 2)
    statement = "SELECT value FROM things WHERE id=?"
    assert translate_sql(statement) == translate_sql(statement)
    assert calls == 2
    assert not _PARSE_TEMPLATE_CACHE


def test_parse_failure_does_not_poison_cache(monkeypatch):
    original_parse = sqlglot.parse
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("temporary parser failure")
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(sqlglot, "parse", fail_once)
    statement = "SELECT 1"
    with pytest.raises(MySQLDialectError, match="outside the supported SQLite SQL grammar"):
        translate_sql(statement)
    assert translate_sql(statement) == "SELECT 1"
    assert calls == 2


def test_template_cache_entry_count_is_bounded():
    for value in range(_PARSE_TEMPLATE_CACHE_MAX_ENTRIES + 1):
        translate_sql(f"SELECT {value}")
    assert len(_PARSE_TEMPLATE_CACHE) == _PARSE_TEMPLATE_CACHE_MAX_ENTRIES
