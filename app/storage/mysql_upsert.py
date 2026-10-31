"""Targeted SQLite UPSERT semantics on InnoDB, including multiple unique keys.

MySQL's ON DUPLICATE KEY cannot name the conflicting key and runs UPDATE
triggers even for a no-op. Single-row VALUES statements use a savepoint and a
real INSERT instead. Only an actual duplicate-key error enters the conflict
path; failed type/check/FK validations are never swallowed.
"""
from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass

from app.storage.mysql_dialect import MySQLDialectError
from app.storage.mysql_parameters import format_named_sql, name_parameters


@dataclass(frozen=True)
class UpsertOutcome:
    rowcount: int
    lastrowid: int | None


def _source_unique_keys(connection, table, exp, parse):
    rows = connection._schema.master_objects(connection, object_type="table", names=[table])
    groups = set()
    for _, source_sql in rows:
        definition = parse(source_sql, read="sqlite")
        for column in definition.this.expressions:
            if isinstance(column, exp.ColumnDef):
                for constraint in column.args.get("constraints") or []:
                    if isinstance(constraint.args.get("kind"), (exp.PrimaryKeyColumnConstraint, exp.UniqueColumnConstraint)):
                        groups.add((column.name.casefold(),))
            elif isinstance(column, exp.PrimaryKey):
                groups.add(tuple(part.name.casefold() for part in column.expressions))
            elif isinstance(column, exp.UniqueColumnConstraint):
                schema = column.args.get("this")
                groups.add(tuple(part.name.casefold() for part in schema.expressions))
    # Explicit unique indexes are part of the source contract too.
    for _, source_sql in connection._schema.master_objects(connection, object_type="index", table_name=table):
        definition = parse(source_sql, read="sqlite")
        if definition.args.get("unique"):
            params = definition.this.args["params"]
            if params.args.get("where") is not None:
                continue
            columns = [part.this if isinstance(part, exp.Ordered) else part for part in params.args["columns"]]
            if all(isinstance(part, exp.Column) for part in columns):
                groups.add(tuple(part.name.casefold() for part in columns))
    return groups


def execute_targeted_upsert(connection, statement, parameters=()):
    """Return an outcome if handled, otherwise leave the reviewed dialect path.

    SELECT-based bulk inserts stay with the dialect's stricter sole-key rules.
    Unsupported targeted expressions fail closed rather than broadening the
    conflict target to any unique index.
    """
    if connection._schema is None or not re.match(r"\s*INSERT\b", statement, re.IGNORECASE):
        return None
    if not re.search(r"\bON\s+CONFLICT\b|\bINSERT\s+OR\s+IGNORE\b", statement, re.IGNORECASE):
        return None
    import sqlglot
    from sqlglot import exp

    named, bindings = name_parameters(statement, parameters)
    tree = sqlglot.parse_one(named, read="sqlite")
    if not isinstance(tree, exp.Insert):
        return None
    values = tree.args.get("expression")
    if not isinstance(values, exp.Values) or len(values.expressions) != 1:
        return None
    target = tree.this
    table = target.this if isinstance(target, exp.Schema) else target
    if not isinstance(table, exp.Table) or table.db or table.catalog:
        raise MySQLDialectError("UPSERT requires one unqualified authority table")
    columns = ([part.name for part in target.expressions] if isinstance(target, exp.Schema)
               else [row[1] for row in connection._schema.table_info(connection, table.name)])
    incoming = dict(zip((name.casefold() for name in columns), values.expressions[0].expressions, strict=True))
    conflict = tree.args.get("conflict")
    ignore = str(tree.args.get("alternative", "")).upper() == "IGNORE"
    keys = []
    if conflict is not None:
        if conflict.args.get("index_predicate") is not None:
            raise MySQLDialectError("partial conflict targets require a native migration")
        for part in conflict.args.get("conflict_keys") or []:
            part = part.this if isinstance(part, exp.Ordered) else part
            if not isinstance(part, exp.Column) or part.table:
                raise MySQLDialectError("conflict target must be simple columns")
            keys.append(part.name.casefold())
        if keys and tuple(keys) not in _source_unique_keys(connection, table.name, exp, sqlglot.parse_one):
            raise MySQLDialectError("conflict target is not a declared unique key")
        if not keys and str(conflict.args["action"]).upper() != "DO NOTHING":
            raise MySQLDialectError("targeted update requires a unique conflict key")
    if not ignore:
        for key in keys:
            value = incoming.get(key)
            if value is None or not isinstance(value, (exp.Literal, exp.Placeholder, exp.Null, exp.Boolean, exp.Neg)):
                raise MySQLDialectError("conflict keys require explicit deterministic VALUES")
    plain = tree.copy()
    plain.set("conflict", None)
    plain.set("alternative", None)
    plain.set("ignore", False)
    connection._before_statement(statement)
    savepoint = "lka_upsert_statement"
    raw = connection._raw.cursor()
    raw.execute("SAVEPOINT " + savepoint)
    def apply_existing():
        predicates = [exp.EQ(this=exp.column(key, quoted=True), expression=incoming[key].copy()) for key in keys]
        predicate = exp.and_(*predicates)
        lookup = exp.select(*[exp.column(key, quoted=True) for key in keys]).from_(table.copy()).where(predicate.copy())
        raw.execute(format_named_sql(lookup.sql(dialect="mysql") + " FOR UPDATE", bindings), bindings)
        if raw.fetchone() is None:
            return None
        action = str(conflict.args["action"]).upper()
        if action == "DO NOTHING":
            return UpsertOutcome(0, None)
        if action != "DO UPDATE":
            raise MySQLDialectError("unsupported targeted upsert action")
        assignments = []
        def replace_excluded(node):
            if isinstance(node, exp.Column) and node.table.casefold() == "excluded":
                value = incoming.get(node.name.casefold())
                if value is None:
                    raise MySQLDialectError("excluded default values need an explicit native port")
                return value.copy()
            return node
        for assignment in conflict.args.get("expressions") or []:
            assignments.append(assignment.transform(replace_excluded))
        where = conflict.args.get("where")
        if where is not None:
            predicate = exp.and_(predicate, where.this.transform(replace_excluded))
        update = exp.Update(this=table.copy(), expressions=assignments, where=exp.Where(this=predicate))
        raw.execute(format_named_sql(update.sql(dialect="mysql"), bindings), bindings)
        affected = raw.rowcount
        if affected == 0:
            # SQLite reports a matched ON CONFLICT DO UPDATE row even when
            # every assigned value is unchanged. MySQL's default affected-row
            # count is zero in that case. The target row is already locked, so
            # a read-only predicate check distinguishes a no-op update from a
            # failed conditional CAS without changing global CLIENT_FOUND_ROWS.
            matched = exp.select(exp.Literal.number(1)).from_(table.copy()).where(predicate.copy())
            raw.execute(
                format_named_sql(matched.sql(dialect="mysql") + " FOR UPDATE", bindings), bindings,
            )
            affected = 1 if raw.fetchone() is not None else 0
        return UpsertOutcome(affected, None)

    try:
        # Lock an existing target before INSERT can acquire a shared duplicate
        # key lock. Otherwise concurrent updates can deadlock upgrading those
        # shared locks, even when each touches only the same one logical key.
        outcome = apply_existing() if keys and not ignore else None
        if outcome is None:
            try:
                raw.execute(format_named_sql(plain.sql(dialect="mysql"), bindings), bindings)
                outcome = UpsertOutcome(raw.rowcount, raw.lastrowid)
            except Exception as duplicate:
                if not getattr(duplicate, "args", ()) or duplicate.args[0] != 1062:
                    raise
                raw.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                if ignore or not keys:
                    outcome = UpsertOutcome(0, None)
                else:
                    outcome = apply_existing()
                    if outcome is None:
                        raise  # collision with a different unique key
        raw.execute("RELEASE SAVEPOINT " + savepoint)
        return outcome
    except Exception:
        # A deadlock already rolls back the complete transaction in InnoDB.
        # Do not mask its native error with a missing-savepoint error.
        with suppress(Exception):
            raw.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            raw.execute("RELEASE SAVEPOINT " + savepoint)
        raise
    finally:
        raw.close()
