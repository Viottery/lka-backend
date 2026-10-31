"""Real-engine checks for SQLite-compatible JSON text collation."""
from __future__ import annotations

from app.storage.mysql import connect_mysql
from tests.test_mysql_owner_live import _config


def test_json_scalar_text_equality_uses_binary_no_pad_semantics():
    with connect_mysql(_config()) as conn:
        equal = conn.execute(
            "SELECT json_extract('{\"mode\":\"manual\"}', '$.mode') = ?", ("manual",),
        ).fetchone()[0]
        trailing_space = conn.execute(
            "SELECT json_extract('{\"mode\":\"manual \"}', '$.mode') = ?", ("manual",),
        ).fetchone()[0]
    assert equal == 1
    assert trailing_space == 0


def test_json_type_maps_array_object_null_missing_boolean_and_text():
    with connect_mysql(_config()) as conn:
        row = conn.execute(
            "SELECT json_type('{\"array\":[1],\"object\":{},\"nil\":null,"
            "\"truth\":true,\"word\":\"x\"}', '$.array'), "
            "json_type('{\"array\":[1],\"object\":{},\"nil\":null,"
            "\"truth\":true,\"word\":\"x\"}', '$.object'), "
            "json_type('{\"array\":[1],\"object\":{},\"nil\":null,"
            "\"truth\":true,\"word\":\"x\"}', '$.nil'), "
            "json_type('{\"array\":[1],\"object\":{},\"nil\":null,"
            "\"truth\":true,\"word\":\"x\"}', '$.missing'), "
            "json_type('{\"array\":[1],\"object\":{},\"nil\":null,"
            "\"truth\":true,\"word\":\"x\"}', '$.truth'), "
            "json_type('{\"array\":[1],\"object\":{},\"nil\":null,"
            "\"truth\":true,\"word\":\"x\"}', '$.word')",
        ).fetchone()
    assert tuple(row) == ("array", "object", "null", None, "true", "text")


def test_json_null_boolean_and_string_null_preserve_control_predicates():
    with connect_mysql(_config()) as conn:
        row = conn.execute(
            "SELECT json_extract('{\"parent_run_id\":null}', '$.parent_run_id') IS NULL, "
            "json_extract('{\"parent_run_id\":\"null\"}', '$.parent_run_id') IS NULL, "
            "json_extract('{\"cancel_requested\":true}', '$.cancel_requested')=1, "
            "json_extract('{\"cancel_requested\":false}', '$.cancel_requested')=0, "
            "json_extract('{\"x\":\"null\"}', '$.x')='null', "
            "json_extract('{}', '$.missing') IS NULL"
        ).fetchone()
    assert tuple(row) == (1, 0, 1, 1, 1, 1)


def test_sqlite_is_null_safe_value_comparisons_and_unaliased_derived_tables():
    with connect_mysql(_config()) as conn:
        row = conn.execute(
            "SELECT a IS b,a IS NOT b,a IS ?,b IS ? FROM (SELECT NULL AS a,'run-one' AS b)",
            (None, "run-one"),
        ).fetchone()
    assert tuple(row) == (0, 1, 1, 1)


def test_iso_julian_dates_match_sqlite_across_offsets_and_invalid_values():
    import sqlite3

    values = ["2026-10-29T00:00:00+00:00", "2026-10-29T08:00:00+08:00",
              "2026-10-29T00:00:00.123456+00:00", "2026-10-29", "invalid", None]
    with sqlite3.connect(":memory:") as reference, connect_mysql(_config()) as conn:
        for value in values:
            expected = reference.execute("SELECT julianday(?)", (value,)).fetchone()[0]
            actual = conn.execute("SELECT julianday(?)", (value,)).fetchone()[0]
            if expected is None:
                assert actual is None
            else:
                assert abs(float(actual) - expected) < 0.00000002
