"""Versioned, schema-only bootstrap for a fresh MySQL application database."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.storage.mysql import MySQLConnection
from app.storage.mysql_schema import (
    MySQLSchema,
    MySQLSchemaError,
    _column_meta,
    _table_key_groups,
    attach_schema_metadata,
)

_ASSET_PATH = Path(__file__).with_name("mysql_schema_v1.json")
_VARIANTS_V2_PATH = Path(__file__).with_name("mysql_schema_variants_v2.json")
_VARIANTS_V3_PATH = Path(__file__).with_name("mysql_schema_variants_v3.json")


@lru_cache(maxsize=1)
def _load_schema_asset() -> tuple[dict[str, Any], bytes]:
    raw = _ASSET_PATH.read_bytes()
    try:
        asset = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MySQLSchemaError("native MySQL schema asset is invalid") from exc
    if asset.get("format") != 1 or asset.get("version") != 1:
        raise MySQLSchemaError("unsupported native MySQL schema asset version")
    objects = asset.get("objects")
    expected_count = asset.get("expected_runtime_object_count")
    if not isinstance(objects, list) or expected_count != len(objects):
        raise MySQLSchemaError("native MySQL schema asset has an unexpected catalog")
    names = [(item.get("object_type"), item.get("name")) for item in objects]
    if len(names) != len(set(names)):
        raise MySQLSchemaError("native MySQL schema asset contains duplicate object names")
    return asset, raw


@lru_cache(maxsize=1)
def _load_variants_v2() -> tuple[dict[str, Any], bytes]:
    raw = _VARIANTS_V2_PATH.read_bytes()
    try:
        asset = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MySQLSchemaError("native MySQL schema variant asset is invalid") from exc
    if asset.get("format") != 1 or asset.get("version") != 2:
        raise MySQLSchemaError("unsupported native MySQL schema variant asset version")
    variants = asset.get("variants")
    identities = [
        (item.get("object_type"), item.get("object_name"), item.get("source_sql"))
        for item in variants if isinstance(item, dict)
    ] if isinstance(variants, list) else []
    if not isinstance(variants, list) or len(identities) != len(variants) or len(identities) != len(set(identities)):
        raise MySQLSchemaError("native MySQL schema variant asset has duplicate or invalid entries")
    return asset, raw


@lru_cache(maxsize=1)
def _load_variants_v3() -> tuple[dict[str, Any], bytes]:
    raw = _VARIANTS_V3_PATH.read_bytes()
    try:
        asset = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MySQLSchemaError("native MySQL schema variant asset is invalid") from exc
    if asset.get("format") != 1 or asset.get("version") != 3:
        raise MySQLSchemaError("unsupported native MySQL schema variant asset version")
    variants = asset.get("variants")
    identities = [
        (item.get("object_type"), item.get("object_name"), item.get("source_sql"))
        for item in variants if isinstance(item, dict)
    ] if isinstance(variants, list) else []
    if not isinstance(variants, list) or len(identities) != len(variants) or len(identities) != len(set(identities)):
        raise MySQLSchemaError("native MySQL schema variant asset has duplicate or invalid entries")
    return asset, raw


def _mysql_database(conn: MySQLConnection) -> str:
    database = getattr(getattr(getattr(conn, "_pool", None), "config", None), "database", None)
    if not database:
        raise MySQLSchemaError("MySQL bootstrap requires a configured database identity")
    return str(database)


def _sqlglot():
    try:
        from sqlglot import exp, parse
    except ImportError as exc:  # pragma: no cover - project optional dependency boundary
        raise MySQLSchemaError("MySQL bootstrap requires sqlglot") from exc
    return exp, parse


def _simple_index_parts(sql: str, *, allow_partial: bool = False):
    exp, parse = _sqlglot()
    try:
        create = parse(sql, read="sqlite")[0]
    except Exception as exc:
        raise MySQLSchemaError("native MySQL index definition is invalid") from exc
    if not isinstance(create, exp.Create) or str(create.args.get("kind", "")).upper() != "INDEX":
        raise MySQLSchemaError("expected a SQLite CREATE INDEX definition")
    index = create.this
    params = index.args.get("params")
    if params is None:
        raise MySQLSchemaError("index definition has no columns")
    where = params.args.get("where")
    if where is not None and (create.args.get("unique") or not allow_partial):
        raise MySQLSchemaError("partial unique or unsupported index needs an explicit port")
    table_name = index.args.get("table").name
    names: list[str] = []
    for ordered in params.args.get("columns") or []:
        value = ordered.this if isinstance(ordered, exp.Ordered) else ordered
        if not isinstance(value, exp.Column) or value.table:
            return create, table_name, None
        names.append(value.name)
    if not names:
        raise MySQLSchemaError("index definition has no key columns")
    return create, table_name, tuple(names)


def _table_source_columns(sql: str) -> tuple[dict[str, str], list[tuple[str, ...]]]:
    exp, parse = _sqlglot()
    create = parse(sql, read="sqlite")[0]
    if not isinstance(create, exp.Create) or not isinstance(create.this, exp.Schema):
        raise MySQLSchemaError("expected a regular SQLite CREATE TABLE definition")
    types = {
        column.name.casefold(): column.args["kind"].this.name.upper()
        for column in create.this.expressions
        if isinstance(column, exp.ColumnDef) and column.args.get("kind") is not None
    }
    groups: list[tuple[str, ...]] = []
    for item in create.this.expressions:
        if isinstance(item, exp.PrimaryKey):
            groups.append(tuple(part.name for part in item.expressions))
        elif isinstance(item, exp.UniqueColumnConstraint):
            schema = item.args.get("this")
            if schema is not None:
                groups.append(tuple(part.name for part in schema.expressions))
        elif isinstance(item, exp.ColumnDef):
            for constraint in item.args.get("constraints") or []:
                if isinstance(constraint.args.get("kind"), (
                    exp.PrimaryKeyColumnConstraint, exp.UniqueColumnConstraint,
                )):
                    groups.append((item.name,))
    return types, groups


def _prepare_table(schema: MySQLSchema, item: dict[str, Any], unique_index_groups,
                   hash_ports: list[dict[str, Any]]):
    exp, parse = _sqlglot()
    sql = item["source_sql"]
    create = parse(sql, read="sqlite")[0]
    if not isinstance(create, exp.Create) or not isinstance(create.this, exp.Schema):
        raise MySQLSchemaError("bootstrap only accepts explicit CREATE TABLE statements")

    table_name = item["name"]
    source_types, source_groups = _table_source_columns(sql)
    for column in create.this.expressions:
        if (isinstance(column, exp.ColumnDef) and column.args.get("kind") is not None
                and column.args["kind"].sql(dialect="sqlite").upper() == "BLOB"):
            # SQLGlot represents SQLite BLOB as VARBINARY internally, but its
            # MySQL renderer omits a required length. Preserve the unbounded
            # SQLite value with native LONGBLOB storage.
            column.set("kind", exp.DataType.build("LONGBLOB"))
    table_hash_ports = [port for port in hash_ports if port["table"] == table_name]
    hashes = {port["column"].casefold(): port["generated_column"] for port in table_hash_ports}

    # Replace only reviewed unbounded unique-key parts with full-value hash
    # surrogates. The source text remains intact; collision guards fail closed.
    mapped_source_groups = []
    for group in source_groups:
        mapped_source_groups.append(tuple(
            hashes.get(column.casefold(), column) for column in group
        ))
        if any(column.casefold() in hashes for column in group):
            mapped = tuple(hashes.get(column.casefold(), column) for column in group)
            _remove_source_key(create.this, group)
            constraint_sql = (
                f"CREATE TABLE t (UNIQUE KEY `{_unique_port_name(table_name, group)}` "
                f"({', '.join(f'`{column}`' for column in mapped)}))"
            )
            constraint = parse(constraint_sql, read="mysql")[0].this.expressions[0]
            create.this.append("expressions", constraint.copy())
    unique_index_groups = [tuple(
        hashes.get(column.casefold(), column) for column in group
    ) for group in unique_index_groups]
    for port in table_hash_ports:
        generated_sql = (
            f"CREATE TABLE t (`{port['generated_column']}` BINARY(32) "
            f"GENERATED ALWAYS AS (UNHEX(SHA2(`{port['column']}`,256))) STORED INVISIBLE)"
        )
        generated = parse(generated_sql, read="mysql")[0].this.expressions[0]
        create.this.append("expressions", generated.copy())
    if table_name == "agent_runs":
        generated_sql = (
            "CREATE TABLE t (`parent_run_id_key` VARCHAR(191) GENERATED ALWAYS AS "
            "(LEFT(JSON_UNQUOTE(JSON_EXTRACT(`record_payload`,'$.parent_run_id')),191)) "
            "VIRTUAL INVISIBLE)"
        )
        generated = parse(generated_sql, read="mysql")[0].this.expressions[0]
        create.this.append("expressions", generated.copy())

    # The native primary-key port still reports the original SQLite PK shape.
    original_tree = parse(sql, read="sqlite")[0]
    original_columns = [
        column for column in original_tree.this.expressions
        if isinstance(column, exp.ColumnDef)
    ]
    original_column_metadata = _column_meta(
        original_columns, _table_primary_groups(original_tree.this),
    )

    create.set("exists", True)
    prepared = schema._prepare_create_table(
        create, sql, [*unique_index_groups, *mapped_source_groups],
    )
    mysql_create = parse(prepared.sql, read="mysql")[0]
    if not isinstance(mysql_create, exp.Create) or not isinstance(mysql_create.this, exp.Schema):
        raise MySQLSchemaError("MySQL table conversion did not produce a CREATE TABLE")
    for node in list(mysql_create.walk()):
        if not isinstance(node, exp.Length) or not isinstance(node.this, exp.Cast):
            continue
        if node.this.args.get("to") is None or node.this.args["to"].this != exp.DType.VARBINARY:
            continue
        node.replace(exp.Anonymous(
            this="OCTET_LENGTH", expressions=[node.this.this.copy()],
        ))

    # MySQL requires column COLLATE clauses before DEFAULT/CHECK constraints.
    for column in mysql_create.this.expressions:
        if not isinstance(column, exp.ColumnDef):
            continue
        constraints = list(column.args.get("constraints") or [])
        has_check_on_integer_pk = (
            column.name.casefold() == (prepared.rowid_column or "").casefold()
            and prepared.rowid_kind == "integer_primary_key"
            and any(isinstance(item.args.get("kind"), exp.CheckColumnConstraint)
                    for item in constraints)
        )
        collations = [
            item for item in constraints
            if isinstance(item.args.get("kind"), exp.CollateColumnConstraint)
        ]
        remaining = [
            item for item in constraints
            if not isinstance(item.args.get("kind"), exp.CollateColumnConstraint)
            and not (has_check_on_integer_pk
                     and isinstance(item.args.get("kind"), exp.AutoIncrementColumnConstraint))
        ]
        if collations or len(remaining) != len(constraints):
            column.set("constraints", collations + remaining)

    _bound_unique_key_columns(
        mysql_create, [*unique_index_groups, *mapped_source_groups],
    )

    source_types, _ = _table_source_columns(sql)
    checks = []
    for column in mysql_create.this.expressions:
        if not isinstance(column, exp.ColumnDef):
            continue
        if source_types.get(column.name.casefold()) != "TEXT":
            continue
        kind = column.args.get("kind")
        if kind is None or kind.this != exp.DType.VARCHAR:
            continue
        length = int(kind.expressions[0].this.this)
        constraint_name = "chk_lka_len_" + hashlib.sha256(
            f"{table_name}.{column.name}".encode(),
        ).hexdigest()[:20]
        check = parse(
            f"CREATE TABLE t (CONSTRAINT `{constraint_name}` "
            f"CHECK (CHAR_LENGTH(`{column.name}`) <= {length}))",
            read="mysql",
        )[0].this.expressions[0]
        checks.append(check)
    for check in checks:
        mysql_create.this.append("expressions", check)
    return replace(
        prepared,
        sql=mysql_create.sql(dialect="mysql"),
        columns=original_column_metadata,
    )


def _align_foreign_key_text_columns(prepared_tables):
    """Give text FK endpoints identical bounded InnoDB types and collations.

    SQLite permits TEXT foreign keys, while InnoDB does not permit LONGTEXT
    key parts. The referenced key already has a reviewed bounded VARCHAR type;
    copy that bound to each referencing column and add a CHECK so values are
    rejected rather than silently truncated. If a child key requires a tighter
    bound, propagate the smallest bound across the whole FK component.
    """
    exp, parse = _sqlglot()
    trees = {
        name.casefold(): parse(prepared.sql, read="mysql")[0]
        for name, prepared in prepared_tables.items()
    }
    definitions = {
        table: {
            column.name.casefold(): column
            for column in tree.this.expressions if isinstance(column, exp.ColumnDef)
        }
        for table, tree in trees.items()
    }
    parents: dict[tuple[str, str], tuple[str, str]] = {}
    edges: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for table_name, tree in trees.items():
        for constraint in tree.this.expressions:
            if not isinstance(constraint, exp.ForeignKey):
                continue
            reference = constraint.args.get("reference")
            target = reference.this if reference is not None else None
            if not isinstance(target, exp.Schema):
                raise MySQLSchemaError(f"unparsed foreign key in {table_name}")
            target_table = target.this.name.casefold()
            for child_col, parent_col in zip(constraint.expressions, target.expressions):
                child = (table_name, child_col.name.casefold())
                parent = (target_table, parent_col.name.casefold())
                if parent[0] not in definitions or parent[1] not in definitions[parent[0]]:
                    raise MySQLSchemaError(f"foreign key target is missing for {table_name}.{child[1]}")
                edges.append((child, parent))

    def root(node):
        parents.setdefault(node, node)
        if parents[node] != node:
            parents[node] = root(parents[node])
        return parents[node]

    for left, right in edges:
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            parents[right_root] = left_root

    components: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for node in list(parents):
        components.setdefault(root(node), []).append(node)

    for nodes in components.values():
        columns = [definitions[table][column] for table, column in nodes]
        source_text = []
        bounds = []
        for (table, column), definition in zip(nodes, columns):
            original = prepared_tables[prepared_tables_key(prepared_tables, table)].original_sql
            source_types, _ = _table_source_columns(original)
            if source_types.get(column) == "TEXT":
                source_text.append((table, column))
            kind = definition.args.get("kind")
            if kind is not None and kind.this in {exp.DType.VARCHAR, exp.DType.CHAR}:
                bounds.append(int(kind.expressions[0].this.this))
        # Numeric and other non-text foreign keys must already have compatible
        # types. Text endpoints are handled together below.
        if not source_text:
            continue
        if len(source_text) != len(nodes):
            raise MySQLSchemaError("mixed text/non-text foreign-key columns need an explicit port")
        if not bounds:
            raise MySQLSchemaError("referenced text foreign-key key is not bounded")
        bound = min(bounds)
        for table, column_name in nodes:
            column = definitions[table][column_name]
            column.set("kind", exp.DataType.build(f"VARCHAR({bound})"))
            constraints = [
                item for item in column.args.get("constraints") or []
                if not isinstance(item.args.get("kind"), exp.CollateColumnConstraint)
            ]
            constraints.append(exp.ColumnConstraint(kind=exp.CollateColumnConstraint(
                this=exp.Var(this="utf8mb4_0900_bin"),
            )))
            column.set("constraints", constraints)
            source_types, _ = _table_source_columns(
                prepared_tables[prepared_tables_key(prepared_tables, table)].original_sql,
            )
            if source_types.get(column_name) == "TEXT":
                check_name = "chk_lka_len_" + hashlib.sha256(
                    f"{table}.{column_name}".encode(),
                ).hexdigest()[:20]
                existing = {
                    item.args.get("this").name
                    for item in trees[table].this.expressions
                    if isinstance(item, exp.Constraint)
                    and isinstance(item.args.get("this"), exp.Identifier)
                }
                if check_name not in existing:
                    check = parse(
                        f"CREATE TABLE t (CONSTRAINT `{check_name}` "
                        f"CHECK (CHAR_LENGTH(`{column_name}`) <= {bound}))",
                        read="mysql",
                    )[0].this.expressions[0]
                    trees[table].this.append("expressions", check)

    return {
        prepared_tables_key(prepared_tables, table): replace(
            prepared, sql=trees[table.casefold()].sql(dialect="mysql"),
        )
        for table, prepared in prepared_tables.items()
    }


def prepared_tables_key(prepared_tables, casefolded_name: str) -> str:
    """Return the manifest spelling for a table name found in FK SQL."""
    for name in prepared_tables:
        if name.casefold() == casefolded_name:
            return name
    raise MySQLSchemaError(f"foreign key table {casefolded_name!r} is not bootstrapped")


def _table_primary_groups(table_schema) -> list[tuple[str, list[str]]]:
    exp, _parse = _sqlglot()
    groups = []
    for item in table_schema.expressions:
        if isinstance(item, exp.PrimaryKey):
            groups.append(("primary", [column.name for column in item.expressions]))
        elif isinstance(item, exp.ColumnDef):
            for constraint in item.args.get("constraints") or []:
                if isinstance(constraint.args.get("kind"), exp.PrimaryKeyColumnConstraint):
                    groups.append(("primary", [item.name]))
    return groups


def _remove_source_key(table_schema, group: tuple[str, ...]) -> None:
    exp, _parse = _sqlglot()
    target = tuple(column.casefold() for column in group)
    expressions = []
    for expression in table_schema.expressions:
        if isinstance(expression, (exp.PrimaryKey, exp.UniqueColumnConstraint)):
            schema = expression if isinstance(expression, exp.PrimaryKey) else expression.args.get("this")
            names = tuple(part.name.casefold() for part in (schema.expressions if schema else []))
            if names == target:
                continue
        if isinstance(expression, exp.ColumnDef):
            constraints = []
            for constraint in expression.args.get("constraints") or []:
                kind = constraint.args.get("kind")
                if (expression.name.casefold() == target[0] and len(target) == 1
                        and isinstance(kind, (exp.PrimaryKeyColumnConstraint, exp.UniqueColumnConstraint))):
                    continue
                constraints.append(constraint)
            expression.set("constraints", constraints)
        expressions.append(expression)
    table_schema.set("expressions", expressions)


def _unique_port_name(table: str, group: tuple[str, ...]) -> str:
    return "uq_lka_" + hashlib.sha256(
        (table + ":" + ",".join(group)).encode(),
    ).hexdigest()[:24]


def _column_max_bytes(column) -> int:
    exp, _parse = _sqlglot()
    kind = column.args.get("kind")
    if kind is None:
        return 0
    name = kind.this
    length = int(kind.expressions[0].this.this) if kind.expressions else 0
    if name in {exp.DType.VARCHAR, exp.DType.CHAR}:
        return length * 4
    if name in {exp.DType.BINARY, exp.DType.VARBINARY}:
        return length
    return {
        exp.DType.TINYINT: 1,
        exp.DType.SMALLINT: 2,
        exp.DType.MEDIUMINT: 3,
        exp.DType.INT: 4,
        exp.DType.BIGINT: 8,
        exp.DType.FLOAT: 4,
        exp.DType.DOUBLE: 8,
        exp.DType.DATE: 3,
        exp.DType.TIME: 3,
        exp.DType.DATETIME: 8,
        exp.DType.TIMESTAMP: 4,
        exp.DType.BOOLEAN: 1,
    }.get(name, 4)


def _bound_unique_key_columns(create, external_unique_groups) -> None:
    exp, _parse = _sqlglot()
    definitions = {
        column.name.casefold(): column
        for column in create.this.expressions if isinstance(column, exp.ColumnDef)
    }
    groups = [names for _kind, names in _table_key_groups(create.this.expressions)]
    groups.extend(tuple(group) for group in external_unique_groups)
    bounds: dict[str, int] = {}
    for group in groups:
        text_columns = [
            name for name in group
            if name.casefold() in definitions
            and definitions[name.casefold()].args.get("kind") is not None
            and definitions[name.casefold()].args["kind"].this in {exp.DType.VARCHAR, exp.DType.CHAR}
        ]
        if not text_columns:
            continue
        non_text_bytes = sum(
            _column_max_bytes(definitions[name.casefold()])
            for name in group if name not in text_columns and name.casefold() in definitions
        )
        per_text_chars = max(1, (3072 - non_text_bytes) // (4 * len(text_columns)))
        for name in text_columns:
            bounds[name.casefold()] = min(bounds.get(name.casefold(), 768), per_text_chars)
    for lowered, max_length in bounds.items():
        column = definitions[lowered]
        kind = column.args["kind"]
        current = int(kind.expressions[0].this.this)
        if current > max_length:
            kind.set("expressions", [exp.DataTypeParam(this=exp.Literal.number(max_length))])


def _index_sql(item: dict[str, Any], text_columns: dict[str, str], hash_columns: dict[str, str],
               text_prefix_limits: dict[str, int] | None = None
               ) -> tuple[str, tuple[str, ...], tuple[int | None, ...], bool]:
    exp, _parse = _sqlglot()
    create, _table_name, names = _simple_index_parts(
        item["source_sql"], allow_partial=True,
    )
    if names is None:
        if item["name"] != "idx_agent_runs_parent_run_id":
            raise MySQLSchemaError("unported SQLite expression index")
        # The generated field is deliberately invisible and only accelerates
        # the same JSON scalar lookup; truncating a nonunique key is safe.
        return (
            "CREATE INDEX `idx_agent_runs_parent_run_id` ON `agent_runs` (`parent_run_id_key`)",
            ("parent_run_id_key",), (None,), False,
        )

    params = create.this.args["params"]
    params.set("where", None)  # A nonunique full index is a safe superset.
    indexed_columns = []
    prefix_lengths: list[int | None] = []
    rewritten = []
    text_names = [
        (ordered.this if isinstance(ordered, exp.Ordered) else ordered).name.casefold()
        for ordered in params.args["columns"]
        if isinstance((ordered.this if isinstance(ordered, exp.Ordered) else ordered), exp.Column)
        and text_columns.get(
            (ordered.this if isinstance(ordered, exp.Ordered) else ordered).name.casefold(),
        ) == "TEXT"
    ]
    non_text_bytes = sum(
        8 if text_columns.get(name, "") in {"INTEGER", "BIGINT", "REAL", "FLOAT", "DOUBLE"}
        else 4
        for name in text_names if text_columns.get(name) != "TEXT"
    )
    indexed_text_count = sum(name in text_columns for name in text_names)
    prefix = min(
        191,
        max(1, (3072 - non_text_bytes) // max(4, 4 * indexed_text_count)),
    )
    limits = text_prefix_limits or {}
    prefix = min(
        prefix,
        *(limits.get(name, 191) for name in text_names if name not in hash_columns),
    ) if any(name not in hash_columns for name in text_names) else prefix
    for ordered in params.args["columns"]:
        column = ordered.this if isinstance(ordered, exp.Ordered) else ordered
        if isinstance(column, exp.Column) and column.name.casefold() in hash_columns:
            column = exp.Column(this=exp.to_identifier(hash_columns[column.name.casefold()]))
        elif (isinstance(column, exp.Column) and text_columns.get(column.name.casefold()) == "TEXT"
              and not create.args.get("unique")):
            column = exp.Anonymous(
                this=column.copy(), expressions=[exp.Literal.number(prefix)],
            )
        if isinstance(ordered, exp.Ordered):
            ordered = ordered.copy()
            ordered.set("this", column)
        else:
            ordered = column
        rewritten.append(ordered)
        indexed_columns.append(
            column.this.name if isinstance(column, exp.Anonymous)
            and isinstance(column.this, exp.Column) else
            column.name if isinstance(column, exp.Column) else str(column)
        )
        prefix_lengths.append(
            int(column.expressions[0].this) if isinstance(column, exp.Anonymous) else None
        )
    params.set("columns", rewritten)
    create.set("exists", False)
    create.set("catalog", None)
    return (
        create.sql(dialect="mysql"), tuple(indexed_columns),
        tuple(prefix_lengths), bool(create.args.get("unique")),
    )


def _trigger_sql(source_sql: str) -> str:
    match = re.fullmatch(
        r"\s*CREATE\s+TRIGGER\s+([A-Za-z_][A-Za-z0-9_$]*)\s+"
        r"(BEFORE|AFTER)\s+(INSERT|UPDATE|DELETE)(?:\s+OF\s+[^\n]+?)?\s+ON\s+"
        r"([A-Za-z_][A-Za-z0-9_$]*)\s+BEGIN\s*(.*?)\s*END\s*;?\s*",
        source_sql,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise MySQLSchemaError("unsupported SQLite trigger definition")
    name, timing, event, table, body = match.groups()
    if name.startswith("conversation_semantic_"):
        body = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT IGNORE INTO", body,
                      flags=re.IGNORECASE)
        body = re.sub(r"\bON\s+CONFLICT\s*\([^)]*\)\s+DO\s+UPDATE\s+SET\b",
                      "ON DUPLICATE KEY UPDATE", body, flags=re.IGNORECASE)
        body = re.sub(r"\bexcluded\s*\.\s*([A-Za-z_][A-Za-z0-9_$]*)",
                      r"VALUES(`\1`)", body, flags=re.IGNORECASE)
    return (
        f"CREATE TRIGGER `{name}` {timing.upper()} {event.upper()} ON `{table}` "
        f"FOR EACH ROW BEGIN {body} END"
    )


def _object_is_present(conn: MySQLConnection, schema: MySQLSchema, item: dict[str, Any]) -> bool:
    object_type, name = item["object_type"], item["name"]
    if object_type == "table":
        return schema.table_exists(conn, name)
    if object_type == "index":
        return schema.index_exists(conn, name, item["table_name"])
    if object_type == "trigger":
        return conn.execute_native(
            "SELECT 1 FROM INFORMATION_SCHEMA.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s LIMIT 1", (name,),
        ).fetchone() is not None
    return False


def _source_object_metadata(schema: MySQLSchema, conn: MySQLConnection,
                            item: dict[str, Any]) -> tuple[str, str | None] | None:
    rows = schema.master_objects(
        conn, object_type=item["object_type"], names=[item["name"]],
    )
    return tuple(rows[0]) if rows else None


def _check_existing_objects(conn: MySQLConnection, schema: MySQLSchema,
                            objects: list[dict[str, Any]]) -> None:
    for item in objects:
        if item["handling"] == "external":
            continue
        existing_sql = _source_object_metadata(schema, conn, item)
        present = _object_is_present(conn, schema, item)
        if present and (existing_sql is None or existing_sql[1] != item["source_sql"]):
            raise MySQLSchemaError(
                f"refusing to adopt unverified existing {item['object_type']} {item['name']}"
            )
        if existing_sql is not None and existing_sql[1] != item["source_sql"]:
            raise MySQLSchemaError(
                f"existing schema metadata differs for {item['object_type']} {item['name']}"
            )


def _seed_metadata(conn: MySQLConnection, schema: MySQLSchema,
                   objects: list[dict[str, Any]], prepared_tables,
                   hash_ports: list[dict[str, Any]]) -> None:
    for item in objects:
        schema.remember_object(
            conn,
            object_type=item["object_type"],
            object_name=item["name"],
            table_name=item["table_name"],
            source_sql=item["source_sql"],
        )
    for item in objects:
        if item["object_type"] != "table":
            continue
        prepared = prepared_tables[item["name"]]
        schema.remember_rowid(
            conn, item["name"], prepared.rowid_column or "",
            proxy_kind=prepared.rowid_kind or "invisible_auto_increment",
        )
        schema.remember_columns(conn, item["name"], prepared.columns)
    unique_mappings: dict[tuple[str, tuple[str, ...], tuple[str, ...]], set[str]] = {}
    for port in hash_ports:
        key = (
            port["table"], tuple(port["source_columns"]),
            tuple(port["physical_columns"]),
        )
        unique_mappings.setdefault(key, set()).update(port["guards"])
    for (table, source_columns, physical_columns), guard_names in unique_mappings.items():
        schema.remember_unique_key_mapping(
            conn,
            table,
            source_columns,
            physical_columns,
            sorted(guard_names),
        )
    conn.commit()


def _apply_tables(conn: MySQLConnection, schema: MySQLSchema,
                  objects: list[dict[str, Any]], prepared_tables,
                  hash_ports: list[dict[str, Any]], *, install: bool) -> None:
    exp, parse = _sqlglot()
    table_items = [item for item in objects if item["object_type"] == "table"]
    dependencies: dict[str, set[str]] = {item["name"].casefold(): set() for item in table_items}
    item_by_name = {item["name"].casefold(): item for item in table_items}
    for table_name, prepared in prepared_tables.items():
        tree = parse(prepared.sql, read="mysql")[0]
        for constraint in tree.this.expressions:
            if not isinstance(constraint, exp.ForeignKey):
                continue
            reference = constraint.args.get("reference")
            target = reference.this if reference is not None else None
            if isinstance(target, exp.Schema):
                dependency = target.this.name.casefold()
                if dependency in dependencies and dependency != table_name.casefold():
                    dependencies[table_name.casefold()].add(dependency)
    ordered: list[dict[str, Any]] = []
    remaining = set(dependencies)
    while remaining:
        ready = [
            name for name in dependencies
            if name in remaining and not (dependencies[name] & remaining)
        ]
        if not ready:
            raise MySQLSchemaError("cyclic inter-table foreign keys need an explicit schema port")
        ordered.extend(item_by_name[name] for name in ready)
        remaining.difference_update(ready)
    for item in ordered:
        prepared = prepared_tables[item["name"]]
        if not schema.table_exists(conn, item["name"]):
            if not install:
                raise MySQLSchemaError(f"native table {item['name']} is missing; run the schema migration")
            conn.execute_native(prepared.sql)
            conn.commit()
        expected = {column.name.casefold() for column in prepared.columns}
        expected.add((prepared.rowid_column or "").casefold())
        expected.update(
            port["generated_column"] for port in hash_ports
            if port["table"].casefold() == item["name"].casefold()
        )
        if item["name"] == "agent_runs":
            expected.add("parent_run_id_key")
        actual = {name.casefold() for name in schema.column_names(conn, item["name"])}
        if not expected.issubset(actual):
            raise MySQLSchemaError(f"incomplete native table schema for {item['name']}")


def _apply_indexes(conn: MySQLConnection, schema: MySQLSchema,
                    objects: list[dict[str, Any]], table_columns: dict[str, dict[str, str]],
                    hash_ports: list[dict[str, Any]], prepared_tables,
                    *, install: bool) -> None:
    hashes_by_table: dict[str, dict[str, str]] = {}
    for port in hash_ports:
        hashes_by_table.setdefault(port["table"].casefold(), {})[
            port["column"].casefold()
        ] = port["generated_column"]
    for item in objects:
        if item["object_type"] != "index":
            continue
        exp, parse = _sqlglot()
        prepared_tree = parse(
            prepared_tables[item["table_name"]].sql, read="mysql",
        )[0]
        prefix_limits = {
            column.name.casefold(): int(column.args["kind"].expressions[0].this.this)
            for column in prepared_tree.this.expressions
            if isinstance(column, exp.ColumnDef)
            and column.args.get("kind") is not None
            and column.args["kind"].this in {exp.DType.VARCHAR, exp.DType.CHAR}
        }
        ddl, expected_columns, expected_prefixes, expected_unique = _index_sql(
            item,
            table_columns[item["table_name"].casefold()],
            hashes_by_table.get(item["table_name"].casefold(), {}),
            prefix_limits,
        )
        if not schema.index_exists(conn, item["name"], item["table_name"]):
            if not install:
                raise MySQLSchemaError(f"native index {item['name']} is missing; run the schema migration")
            conn.execute_native(ddl)
            conn.commit()
        rows = conn.execute_native(
            "SELECT COLUMN_NAME,NON_UNIQUE,SUB_PART FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s "
            "ORDER BY SEQ_IN_INDEX", (item["table_name"], item["name"]),
        ).fetchall()
        actual_columns = tuple(str(row[0]) for row in rows)
        actual_prefixes = tuple(int(row[2]) if row[2] is not None else None for row in rows)
        actual_unique = bool(rows) and int(rows[0][1]) == 0
        if (
            tuple(name.casefold() for name in actual_columns)
            != tuple(name.casefold() for name in expected_columns)
            or actual_prefixes != expected_prefixes
            or actual_unique != expected_unique
        ):
            raise MySQLSchemaError(f"native index definition mismatch for {item['name']}")


def _hash_collision_triggers(hash_ports: list[dict[str, Any]]) -> dict[str, str]:
    statements: dict[str, str] = {}
    for port in hash_ports:
        table, column, generated = port["table"], port["column"], port["generated_column"]
        other_columns = [
            name for name in port["source_columns"] if name.casefold() != column.casefold()
        ]
        matches = " AND ".join(
            f"`{name}` <=> NEW.`{name}`" for name in other_columns
        )
        collision_test = (
            f"NEW.`{column}` IS NOT NULL AND EXISTS (SELECT 1 FROM `{table}` "
            f"WHERE {matches + ' AND ' if matches else ''}"
            f"`{generated}`=UNHEX(SHA2(NEW.`{column}`,256)) "
            f"AND `{column}`<>NEW.`{column}`)"
        )
        for action in ("INSERT", "UPDATE"):
            trigger_name = port["guards"][0 if action == "INSERT" else 1]
            statements[trigger_name] = (
                f"CREATE TRIGGER `{trigger_name}` BEFORE {action} ON `{table}` "
                "FOR EACH ROW BEGIN IF " + collision_test + " THEN "
                f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='unique key hash collision on {table}.{column}'; "
                "END IF; END"
            )
    return statements


def _apply_hash_guard_triggers(conn: MySQLConnection, hash_ports: list[dict[str, Any]], *, install: bool) -> None:
    for name, statement in _hash_collision_triggers(hash_ports).items():
        exists = conn.execute_native(
            "SELECT 1 FROM INFORMATION_SCHEMA.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s LIMIT 1", (name,),
        ).fetchone()
        if exists is None:
            if not install:
                raise MySQLSchemaError(f"native collision guard {name} is missing; run the schema migration")
            conn.execute_native(statement)
            conn.commit()


def _apply_triggers(conn: MySQLConnection, objects: list[dict[str, Any]], *, install: bool) -> None:
    for item in objects:
        if item["object_type"] != "trigger" or item["handling"] == "external":
            continue
        exists = conn.execute_native(
            "SELECT 1 FROM INFORMATION_SCHEMA.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s LIMIT 1",
            (item["name"],),
        ).fetchone()
        if exists is None:
            if not install:
                raise MySQLSchemaError(f"native trigger {item['name']} is missing; run the schema migration")
            conn.execute_native(_trigger_sql(item["source_sql"]))
            conn.commit()


def _apply_native_fulltext(conn: MySQLConnection, objects: list[dict[str, Any]], *, install: bool) -> None:
    try:
        from app.storage.mysql_fulltext import bootstrap_fulltext
    except ImportError as exc:  # pragma: no cover - integration installed with fulltext module
        raise MySQLSchemaError("native MySQL fulltext bootstrap is unavailable") from exc
    bootstrap_fulltext(conn, objects, install=install)


def _apply_coordinator_owner_schema(conn: MySQLConnection, *, install: bool) -> None:
    exists = conn.execute_native(
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='lka_coordinator_owner' AND TABLE_TYPE='BASE TABLE' LIMIT 1",
    ).fetchone() is not None
    if exists:
        return
    if not install:
        raise MySQLSchemaError("coordinator owner table is missing; run the schema migration")
    try:
        from app.storage.mysql_owner import install_coordinator_schema
    except ImportError as exc:  # pragma: no cover
        raise MySQLSchemaError("MySQL coordinator schema installer is unavailable") from exc
    install_coordinator_schema(conn)


def _apply_source_variant_migration(
    conn: MySQLConnection, schema: MySQLSchema, asset, raw: bytes, *, install: bool,
) -> None:
    version, name = int(asset["version"]), str(asset["migration_name"])
    checksum = hashlib.sha256(raw).hexdigest()
    current = conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s",
        (version,),
    ).fetchone()
    if current is None and not install:
        raise MySQLSchemaError("schema source-variant migration v2 is missing; run the schema migration")
    if current is not None and (current[0] != name or current[1] != checksum):
        raise MySQLSchemaError("schema source-variant migration v2 checksum mismatch")

    expected: dict[tuple[str, str], set[str]] = {}
    for variant in asset["variants"]:
        key = (variant["object_type"], variant["object_name"])
        normalized = schema._normalize_probe_sql(variant["source_sql"])
        expected.setdefault(key, set()).add(normalized)

    if current is None:
        for variant in asset["variants"]:
            schema.remember_object_variant(
                conn,
                object_type=variant["object_type"],
                object_name=variant["object_name"],
                source_sql=variant["source_sql"],
            )
        conn.commit()
        conn.execute_native(
            "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
            (version, name, checksum),
        )
        conn.commit()

    for (object_type, object_name), expected_variants in expected.items():
        actual = schema.object_source_variants(conn, object_type, object_name)
        if actual != expected_variants:
            raise MySQLSchemaError(
                f"registered source variants differ for {object_type} {object_name}"
            )


def _apply_source_variants_v2(conn: MySQLConnection, schema: MySQLSchema, *, install: bool) -> None:
    asset, raw = _load_variants_v2()
    _apply_source_variant_migration(conn, schema, asset, raw, install=install)


def _apply_source_variants_v3(conn: MySQLConnection, schema: MySQLSchema, *, install: bool) -> None:
    asset, raw = _load_variants_v3()
    _apply_source_variant_migration(conn, schema, asset, raw, install=install)


def _prepare_manifest(schema: MySQLSchema, objects, hash_ports):
    table_columns: dict[str, dict[str, str]] = {}
    for item in objects:
        if item["object_type"] == "table":
            table_columns[item["name"].casefold()] = _table_source_columns(item["source_sql"])[0]
    unique_groups: dict[str, list[tuple[str, ...]]] = {}
    for item in objects:
        if item["object_type"] != "index":
            continue
        create, table_name, names = _simple_index_parts(item["source_sql"], allow_partial=True)
        if names is not None and create.args.get("unique"):
            if create.this.args["params"].args.get("where") is not None:
                raise MySQLSchemaError("partial unique index needs an explicit schema port")
            unique_groups.setdefault(table_name.casefold(), []).append(names)
    prepared_tables = {}
    for item in objects:
        if item["object_type"] == "table":
            prepared_tables[item["name"]] = _prepare_table(
                schema, item, unique_groups.get(item["name"].casefold(), []), hash_ports,
            )
    return table_columns, _align_foreign_key_text_columns(prepared_tables)


def _run_migration(conn: MySQLConnection, schema: MySQLSchema, asset, raw: bytes,
                   *, install: bool) -> None:
    objects = asset["objects"]
    version, name = int(asset["version"]), str(asset["migration_name"])
    checksum = hashlib.sha256(raw).hexdigest()
    if install:
        schema.ensure_metadata(conn)
        schema.ensure_object_variants_metadata(conn)
        attach_schema_metadata(conn, schema, ensure_metadata=False)
    else:
        migration_table = conn.execute_native(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME='lka_schema_migrations' AND TABLE_TYPE='BASE TABLE' LIMIT 1",
        ).fetchone()
        if migration_table is None:
            raise MySQLSchemaError("native schema is not installed; run scripts/migrate_mysql.py")
        attach_schema_metadata(conn, schema, ensure_metadata=False)
        current = conn.execute_native(
            "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s",
            (version,),
        ).fetchone()
        if current is None:
            raise MySQLSchemaError("native schema is not installed; run scripts/migrate_mysql.py")
        if current[0] != name or current[1] != checksum:
            raise MySQLSchemaError("native schema bootstrap checksum mismatch")
        _check_existing_objects(conn, schema, objects)
        hash_ports = asset["ports"]["unbounded_unique_text_hashes"]
        table_columns, prepared_tables = _prepare_manifest(schema, objects, hash_ports)
        _apply_tables(conn, schema, objects, prepared_tables, hash_ports, install=False)
        _apply_indexes(
            conn, schema, objects, table_columns, hash_ports, prepared_tables, install=False,
        )
        _apply_hash_guard_triggers(conn, hash_ports, install=False)
        _apply_triggers(conn, objects, install=False)
        _apply_native_fulltext(conn, objects, install=False)
        _apply_coordinator_owner_schema(conn, install=False)
        _apply_source_variants_v2(conn, schema, install=False)
        _apply_source_variants_v3(conn, schema, install=False)
        return
    lock_name = "lka-schema-" + hashlib.sha256(schema.database.encode()).hexdigest()[:40]
    lock = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
    if not lock or int(lock[0] or 0) != 1:
        raise MySQLSchemaError("timed out acquiring native schema bootstrap lock")
    try:
        current = conn.execute_native(
            "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s",
            (version,),
        ).fetchone()
        if current is not None:
            if current[0] != name or current[1] != checksum:
                raise MySQLSchemaError("native schema bootstrap checksum mismatch")
            _check_existing_objects(conn, schema, objects)
            hash_ports = asset["ports"]["unbounded_unique_text_hashes"]
            table_columns, prepared_tables = _prepare_manifest(schema, objects, hash_ports)
            _apply_tables(conn, schema, objects, prepared_tables, hash_ports, install=False)
            _apply_indexes(
                conn, schema, objects, table_columns, hash_ports, prepared_tables, install=False,
            )
            _apply_hash_guard_triggers(conn, hash_ports, install=False)
            _apply_triggers(conn, objects, install=False)
            _apply_native_fulltext(conn, objects, install=False)
            _apply_coordinator_owner_schema(conn, install=False)
            _apply_source_variants_v2(conn, schema, install=True)
            _apply_source_variants_v3(conn, schema, install=True)
            return

        _check_existing_objects(conn, schema, objects)
        hash_ports = asset["ports"]["unbounded_unique_text_hashes"]
        table_columns, prepared_tables = _prepare_manifest(schema, objects, hash_ports)
        _seed_metadata(conn, schema, objects, prepared_tables, hash_ports)
        _apply_tables(conn, schema, objects, prepared_tables, hash_ports, install=True)
        _apply_indexes(
            conn, schema, objects, table_columns, hash_ports, prepared_tables, install=True,
        )
        _apply_hash_guard_triggers(conn, hash_ports, install=True)
        _apply_triggers(conn, objects, install=True)
        _apply_native_fulltext(conn, objects, install=True)
        _apply_coordinator_owner_schema(conn, install=True)
        conn.execute_native(
            "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
            (version, name, checksum),
        )
        conn.commit()
        _apply_source_variants_v2(conn, schema, install=True)
        _apply_source_variants_v3(conn, schema, install=True)
    finally:
        conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()


def _verify_completed_imports(conn: MySQLConnection) -> None:
    database = _mysql_database(conn)
    rows = conn.execute_native(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=%s "
        "AND table_name IN ('lka_data_imports','lka_checkpoint_imports')",
        (database,),
    ).fetchall()
    for row in rows:
        table = row[0]
        if table not in {"lka_data_imports", "lka_checkpoint_imports"}:
            raise MySQLSchemaError("unexpected data import marker identity")
        incomplete = conn.execute_native(
            f"SELECT 1 FROM `{table}` WHERE status IS NULL OR status<>'verified' LIMIT 1"
        ).fetchone()
        if incomplete:
            raise MySQLSchemaError("MySQL data import is incomplete; do not start this database")


def _resume_fulltext_collation_install(conn: MySQLConnection) -> None:
    """Repair a marked V12 DDL interruption before V1 can rebuild its indexes."""
    if not conn.execute_native(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() "
        "AND table_name='lka_schema_migrations'"
    ).fetchone():
        return
    if not conn.execute_native(
        "SELECT 1 FROM lka_schema_migrations WHERE version=%s", (12,),
    ).fetchone():
        return
    from app.storage.mysql_fulltext_collation import install_fulltext_collation_v12

    install_fulltext_collation_v12(conn, install=True)


def bootstrap_mysql(conn: MySQLConnection, *, install: bool = False,
                    allow_incomplete_import: bool = False) -> MySQLSchema:
    """Bootstrap all native business tables and attach SQLite compatibility metadata.

    Runtime calls default to read-only verification. The one-time deployment CLI
    passes ``install=True`` with its separately scoped migration account. That
    path only creates schema objects; it never copies rows from SQLite or applies
    data migrations. DDL runs under a database-scoped advisory lock and a
    version/checksum record, outside application transactions.

    ``allow_incomplete_import`` is reserved for an offline importer holding
    the coordinator lock while it validates its unfinished target. Runtime
    startup must use the default and refuse partial business/checkpoint data.
    """
    if conn.in_transaction:
        raise MySQLSchemaError("MySQL schema bootstrap cannot run inside a transaction")
    asset, raw = _load_schema_asset()
    if not allow_incomplete_import:
        _verify_completed_imports(conn)
    if install:
        _resume_fulltext_collation_install(conn)
    schema = MySQLSchema(_mysql_database(conn))
    _run_migration(conn, schema, asset, raw, install=install)
    # V4 orders the shared-sync journal's AUTO_INCREMENT sequence by commit
    # visibility. It uses the existing hub identity row as its scoped lock.
    from app.domains.shared_sync_mysql import (
        install_shared_sync_mysql,
        install_shared_sync_mysql_v5,
        install_shared_sync_mysql_v6,
        install_shared_sync_mysql_v7,
        verify_shared_sync_mysql,
    )

    if install:
        install_shared_sync_mysql(conn)
        install_shared_sync_mysql_v5(conn)
        install_shared_sync_mysql_v6(conn)
        install_shared_sync_mysql_v7(conn)
    else:
        verify_shared_sync_mysql(conn)
    from app.storage.mysql_fulltext import install_fulltext_v8, install_fulltext_v9

    install_fulltext_v8(conn, install=install)
    install_fulltext_v9(conn, install=install)
    from app.storage.mysql_watermarks import install_watermark_order_v10

    install_watermark_order_v10(conn, install=install)
    from app.storage.mysql_knowledge_vectors import install_knowledge_vector_blobs_v11

    install_knowledge_vector_blobs_v11(conn, install=install)
    from app.storage.mysql_fulltext_collation import install_fulltext_collation_v12

    install_fulltext_collation_v12(conn, install=install)
    attach_schema_metadata(conn, schema, ensure_metadata=False)
    return schema
