"""Real-engine JSON1 table iteration checks; requires a named lab/test database."""
from __future__ import annotations

import json

from app.storage.mysql import connect_mysql
from tests.test_mysql_owner_live import _config


def _sql_literal(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return "'" + encoded.replace("'", "''") + "'"


def test_object_iteration_preserves_quoted_keys_and_nested_filtering():
    special = '空 格."slash\\key'
    document = {
        "people": {
            "ordinary": {"hidden": False, "suppressed": False, "buckets": ["active"]},
            special: {"hidden": False, "suppressed": False, "buckets": ["active"]},
            "hidden": {"hidden": True, "suppressed": False, "buckets": ["active"]},
            "suppressed": {"hidden": False, "suppressed": True, "buckets": ["active"]},
            "no_buckets": {"hidden": False, "suppressed": False, "buckets": []},
        },
        "pool": ["ordinary", special, "hidden", "suppressed", "no_buckets"],
    }
    source = _sql_literal(document)
    statement = (
        "SELECT people.key FROM (SELECT " + source + " AS state_json) i, "
        "json_each(i.state_json,'$.people') people "
        "WHERE COALESCE(json_extract(people.value,'$.hidden'),0)=0 "
        "AND COALESCE(json_extract(people.value,'$.suppressed'),0)=0 "
        "AND EXISTS (SELECT 1 FROM json_each(i.state_json,'$.pool') pool "
        "WHERE pool.value=people.key) "
        "AND EXISTS (SELECT 1 FROM json_each(people.value,'$.buckets') bucket "
        "WHERE bucket.value>'') ORDER BY people.key"
    )
    with connect_mysql(_config()) as conn:
        rows = conn.execute(statement).fetchall()
    assert [row[0] for row in rows] == sorted(["ordinary", special])


def test_array_and_object_values_preserve_nested_json_and_predicate_semantics():
    document = {
        "values": ["plain", True, False, None, 7, 2.5, {"nested": "x"}, [1, "y"]],
        "object": {
            "nullable": None,
            "truth": True,
            "falsehood": False,
            "number": 9,
            "text": "null",
            "nested": {"child": "value"},
        },
    }
    source = _sql_literal(document)
    array_sql = (
        "SELECT a.key, a.value FROM (SELECT " + source + " AS doc) x, "
        "json_each(x.doc,'$.values') a ORDER BY a.key"
    )
    object_sql = (
        "SELECT o.key, o.value FROM (SELECT " + source + " AS doc) x, "
        "json_each(x.doc,'$.object') o ORDER BY o.key"
    )
    with connect_mysql(_config()) as conn:
        arrays = conn.execute(array_sql).fetchall()
        objects = conn.execute(object_sql).fetchall()
        predicates = conn.execute(
            "SELECT SUM(value=1), SUM(value=0), SUM(value IS NULL), SUM(value=7), "
            "SUM(value='null') FROM json_each(" + source + ",'$.values')"
        ).fetchone()

    assert [(row[0], row[1]) for row in arrays[:6]] == [
        (str(index), value) for index, value in enumerate(("plain", "1", "0", None, "7", "2.5"))
    ]
    assert json.loads(arrays[6][1]) == {"nested": "x"}
    assert json.loads(arrays[7][1]) == [1, "y"]
    by_key = dict(objects)
    assert by_key["nullable"] is None
    assert by_key["truth"] == "1"
    assert by_key["falsehood"] == "0"
    assert by_key["number"] == "9"
    assert by_key["text"] == "null"
    assert json.loads(by_key["nested"]) == {"child": "value"}

    assert tuple(predicates) == (1, 1, 1, 1, 0)
