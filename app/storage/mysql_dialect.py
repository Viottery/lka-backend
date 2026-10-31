"""Fail-closed SQL adaptation for the MySQL storage backend.

This module intentionally supports a small SQLGlot-backed subset. SQLite
schema/introspection and virtual-table features belong in mysql_schema or a
domain adapter; they are never silently emitted as MySQL SQL.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any


class MySQLDialectError(ValueError):
    """A SQLite statement has no reviewed MySQL equivalent."""


_UNSUPPORTED_PREFIXES = (
    re.compile(r"^\s*PRAGMA\b", re.IGNORECASE),
    re.compile(r"^\s*CREATE\s+VIRTUAL\s+TABLE\b", re.IGNORECASE),
    re.compile(r"^\s*CREATE\s+TRIGGER\b", re.IGNORECASE),
)
_UNSUPPORTED_SCHEMA_NAMES = re.compile(r"\b(?:sqlite_master|sqlite_temp_master)\b", re.IGNORECASE)
_UNSUPPORTED_INSERT_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.IGNORECASE)
_PARSE_TEMPLATE_CACHE_MAX_ENTRIES = 64
_PARSE_TEMPLATE_CACHE_MAX_STATEMENT_CHARS = 4096
_PARSE_TEMPLATE_CACHE_MAX_AST_NODES = 2048
_PARSE_TEMPLATE_CACHE: OrderedDict[str, Any] = OrderedDict()
_PARSE_TEMPLATE_CACHE_LOCK = threading.Lock()


def _parse_template_cache_clear() -> None:
    """Clear process-local SQL parse templates (primarily for isolated tests)."""
    with _PARSE_TEMPLATE_CACHE_LOCK:
        _PARSE_TEMPLATE_CACHE.clear()


def _parse_statement_template(statement: str, sqlglot: Any) -> Any:
    """Return a fresh AST for a validated short SQL source template."""
    if len(statement) <= _PARSE_TEMPLATE_CACHE_MAX_STATEMENT_CHARS:
        with _PARSE_TEMPLATE_CACHE_LOCK:
            template = _PARSE_TEMPLATE_CACHE.get(statement)
            if template is not None:
                _PARSE_TEMPLATE_CACHE.move_to_end(statement)
        if template is not None:
            # Templates are immutable; copying need not serialize other readers.
            return template.copy()

    expressions = sqlglot.parse(statement, read="sqlite")
    if len(expressions) != 1 or expressions[0] is None:
        raise MySQLDialectError("expected exactly one SQL statement")
    tree = expressions[0]
    if len(statement) > _PARSE_TEMPLATE_CACHE_MAX_STATEMENT_CHARS:
        return tree

    # Keep pathological ASTs out of the shared cache. Stop counting at the
    # cap so this check itself remains bounded for unusually deep/wide input.
    for node_count, _ in enumerate(tree.walk(), start=1):
        if node_count > _PARSE_TEMPLATE_CACHE_MAX_AST_NODES:
            return tree
    template = tree.copy()
    with _PARSE_TEMPLATE_CACHE_LOCK:
        if statement in _PARSE_TEMPLATE_CACHE:
            _PARSE_TEMPLATE_CACHE.move_to_end(statement)
        else:
            _PARSE_TEMPLATE_CACHE[statement] = template
            if len(_PARSE_TEMPLATE_CACHE) > _PARSE_TEMPLATE_CACHE_MAX_ENTRIES:
                _PARSE_TEMPLATE_CACHE.popitem(last=False)
    return tree


def _sqlglot():
    try:
        import sqlglot
    except ImportError as exc:  # pragma: no cover - environment/dependency boundary
        raise MySQLDialectError(
            "MySQL SQL adaptation requires the optional sqlglot dependency"
        ) from exc
    return sqlglot


def leading_keyword(statement: str) -> str:
    """Return the first SQL keyword after whitespace and ordinary comments."""
    value = statement.lstrip()
    while value:
        if value.startswith(("--", "#")):
            end = value.find("\n")
            value = "" if end < 0 else value[end + 1:].lstrip()
            continue
        if value.startswith("/*"):
            end = value.find("*/", 2)
            if end < 0:
                raise MySQLDialectError("unterminated SQL comment")
            value = value[end + 2:].lstrip()
            continue
        break
    match = re.match(r"[A-Za-z_]+", value)
    return match.group(0).upper() if match else ""


def _qmarks_to_format(sql: str) -> str:
    """Translate DB-API positional markers without touching quoted SQL text."""
    out: list[str] = []
    i = 0
    quote: str | None = None
    while i < len(sql):
        char = sql[i]
        # PyMySQL performs old-style %-formatting even with positional binds.
        # Escape literal percent signs in SQL text; placeholders emitted below
        # are appended as a unit and must remain a single %s.
        if char == "%":
            out.append("%%")
            i += 1
            continue
        if quote is not None:
            out.append(char)
            if char == "\\" and quote != "`" and i + 1 < len(sql):
                i += 1
                out.append(sql[i])
            elif char == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    i += 1
                    out.append(sql[i])
                else:
                    quote = None
            i += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
            out.append(char)
            i += 1
            continue
        if char == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            end = sql.find("\n", i)
            if end < 0:
                out.append(sql[i:].replace("%", "%%"))
                break
            out.append(sql[i:end + 1].replace("%", "%%"))
            i = end + 1
            continue
        if char == "/" and i + 1 < len(sql) and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise MySQLDialectError("unterminated SQL comment")
            out.append(sql[i:end + 2].replace("%", "%%"))
            i = end + 2
            continue
        if char == "#":
            end = sql.find("\n", i)
            if end < 0:
                out.append(sql[i:].replace("%", "%%"))
                break
            out.append(sql[i:end + 1].replace("%", "%%"))
            i = end + 1
            continue
        out.append("%s" if char == "?" else char)
        i += 1
    if quote is not None:
        raise MySQLDialectError("unterminated quoted SQL value")
    return "".join(out)


def _rowid_to_primary_proxy(
    tree: Any,
    *,
    rowid_column: Callable[[str], str | None] | None,
) -> None:
    """Rewrite rowid references only when schema metadata resolves the owner."""
    sqlglot = _sqlglot()
    exp = sqlglot.exp
    for select in tree.find_all(exp.Select):
        local_tables = [
            table for table in select.find_all(exp.Table)
            if table.find_ancestor(exp.Select) is select
        ]
        aliases: dict[str, str] = {}
        for table in local_tables:
            name = table.name
            alias = table.alias or name
            if name and alias:
                aliases[str(alias).casefold()] = str(name)
        for column in list(select.find_all(exp.Column)):
            if column.find_ancestor(exp.Select) is not select:
                continue
            if column.name.casefold() not in {"rowid", "_rowid_", "oid"}:
                continue
            original_name = column.name
            qualifier = column.table
            if qualifier:
                table_name = aliases.get(str(qualifier).casefold())
                if table_name is None:
                    raise MySQLDialectError("cannot resolve table alias for SQLite rowid")
            elif len(local_tables) == 1:
                table_name = str(local_tables[0].name)
            else:
                raise MySQLDialectError("ambiguous SQLite rowid; qualify it with a table alias")
            if rowid_column is None:
                raise MySQLDialectError("SQLite rowid requires MySQL schema identity metadata")
            target = rowid_column(table_name)
            if not target:
                raise MySQLDialectError(f"missing rowid identity mapping for table {table_name!r}")
            column.set("this", exp.to_identifier(target))
            if column.parent is select and not column.alias:
                column.replace(exp.alias_(column.copy(), original_name, copy=False))


def _translate_upsert(tree: Any, conflict_target: Callable[[str, tuple[str, ...]], bool] | None) -> None:
    sqlglot = _sqlglot()
    exp = sqlglot.exp
    conflict = tree.args.get("conflict")
    if conflict is None or conflict.args.get("duplicate"):
        return
    target = []
    for item in conflict.args.get("conflict_keys") or []:
        value = item.this if isinstance(item, exp.Ordered) else item
        if not isinstance(value, exp.Column) or value.table:
            raise MySQLDialectError("expression or collated UPSERT targets need an explicit MySQL port")
        target.append(value.name)
    table = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
    table_name = getattr(table, "name", "")
    if not target or conflict_target is None or not conflict_target(table_name, tuple(target)):
        raise MySQLDialectError(
            "MySQL duplicate-key UPSERT is safe only when schema metadata confirms the sole unique target"
        )
    if conflict.args.get("where") is not None or conflict.args.get("index_predicate") is not None:
        raise MySQLDialectError("partial UPSERT conflict targets need an explicit MySQL port")
    action = str(getattr(conflict.args.get("action"), "this", "")).upper()
    if action == "DO NOTHING":
        name = target[0]
        left = exp.Column(this=exp.to_identifier(name))
        right = exp.Column(this=exp.to_identifier(name))
        updates = [exp.EQ(this=left, expression=right)]
    elif action == "DO UPDATE":
        updates = list(conflict.args.get("expressions") or [])
        if not updates:
            raise MySQLDialectError("DO UPDATE has no update expressions")
        for update in updates:
            for column in list(update.find_all(exp.Column)):
                if column.table.casefold() == "excluded":
                    column.replace(exp.Anonymous(
                        this="VALUES", expressions=[exp.to_identifier(column.name)],
                    ))
    else:
        raise MySQLDialectError("unsupported SQLite UPSERT action")
    conflict.set("duplicate", True)
    conflict.set("conflict_keys", None)
    conflict.set("where", None)
    conflict.set("index_predicate", None)
    conflict.set("action", exp.Var(this="UPDATE"))
    conflict.set("expressions", updates)


def _translate_insert_ignore(tree: Any, ignore_target: Callable[[str], str | None] | None) -> None:
    sqlglot = _sqlglot()
    exp = sqlglot.exp
    if str(tree.args.get("alternative", "")).upper() != "IGNORE":
        return
    table_expr = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
    table_name = getattr(table_expr, "name", "")
    columns = tree.this.expressions if isinstance(tree.this, exp.Schema) else []
    fallback_column = ignore_target(table_name) if ignore_target is not None else None
    if not fallback_column:
        raise MySQLDialectError(
            "INSERT OR IGNORE requires schema confirmation of unique keys and absence of triggers"
        )
    first = columns[0].name if columns else fallback_column
    tree.set("alternative", None)
    tree.set("ignore", False)
    tree.set("conflict", exp.OnConflict(
        duplicate=True,
        action=exp.Var(this="UPDATE"),
        expressions=[exp.EQ(
            this=exp.Column(this=exp.to_identifier(first)),
            expression=exp.Column(this=exp.to_identifier(first)),
        )],
    ))


def _translate_json_type(node: Any) -> Any:
    sqlglot = _sqlglot()
    exp = sqlglot.exp
    document, path = node.this, node.args.get("expression")
    if path is None or any(
        isinstance(item, exp.Placeholder) for root in (document, path) for item in root.walk()
    ):
        raise MySQLDialectError("parameterized json_type requires an explicit typed SQL port")
    json_type_value = exp.Anonymous(
        this="JSON_TYPE",
        expressions=[exp.JSONExtract(this=document.copy(), expression=path.copy())],
    )
    json_type = exp.Collate(
        this=json_type_value, expression=exp.Var(this="utf8mb4_0900_bin"),
    )

    def text(value: str) -> Any:
        return exp.Collate(
            this=exp.Literal.string(value), expression=exp.Var(this="utf8mb4_0900_bin"),
        )

    return exp.Case(
        this=json_type,
        ifs=[
            exp.If(this=exp.Literal.string("STRING"), true=text("text")),
            exp.If(this=exp.Literal.string("INTEGER"), true=text("integer")),
            exp.If(this=exp.Literal.string("DOUBLE"), true=text("real")),
            exp.If(this=exp.Literal.string("DECIMAL"), true=text("real")),
            exp.If(this=exp.Literal.string("ARRAY"), true=text("array")),
            exp.If(this=exp.Literal.string("OBJECT"), true=text("object")),
            exp.If(this=exp.Literal.string("NULL"), true=text("null")),
            exp.If(
                this=exp.Literal.string("BOOLEAN"),
                true=exp.If(
                    this=exp.EQ(
                        this=exp.Collate(
                            this=exp.Anonymous(this="JSON_UNQUOTE", expressions=[
                                exp.JSONExtract(this=document.copy(), expression=path.copy()),
                            ]), expression=exp.Var(this="utf8mb4_0900_bin"),
                        ),
                        expression=exp.Literal.string("true"),
                    ),
                    true=text("true"),
                    false=text("false"),
                ),
            ),
        ],
    )


def _translate_json_each(tree: Any) -> None:
    from app.storage.mysql_json import translate_json_each

    translate_json_each(tree, _sqlglot().exp, MySQLDialectError)


def translate_sql(
    statement: str,
    *,
    rowid_column: Callable[[str], str | None] | None = None,
    conflict_target: Callable[[str, tuple[str, ...]], bool] | None = None,
    ignore_target: Callable[[str], str | None] | None = None,
) -> str:
    """Translate one reviewed SQLite statement to MySQL SQL.

    Unsupported statements raise `MySQLDialectError`; callers must not retry
    them against SQLite or strip the incompatible part.
    """
    if not isinstance(statement, str) or not statement.strip():
        raise MySQLDialectError("SQL statement must be non-empty text")
    for pattern in _UNSUPPORTED_PREFIXES:
        if pattern.search(statement):
            raise MySQLDialectError("SQLite PRAGMA, virtual tables and triggers need explicit ports")
    if _UNSUPPORTED_SCHEMA_NAMES.search(statement):
        raise MySQLDialectError("SQLite catalog references need INFORMATION_SCHEMA adaptation")
    keyword = leading_keyword(statement)
    if keyword in {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
        if keyword == "BEGIN":
            if re.match(r"^\s*BEGIN\s+EXCLUSIVE\b", statement, re.IGNORECASE):
                raise MySQLDialectError("SQLite BEGIN EXCLUSIVE has no MySQL equivalent")
            return re.sub(r"^\s*BEGIN(?:\s+(?:IMMEDIATE|DEFERRED))?\b", "START TRANSACTION", statement, count=1,
                          flags=re.IGNORECASE).strip().rstrip(";")
        if keyword == "RELEASE":
            if re.match(r"^\s*RELEASE\s+SAVEPOINT\b", statement, re.IGNORECASE):
                return statement.strip().rstrip(";")
            return re.sub(r"^\s*RELEASE\s+", "RELEASE SAVEPOINT ", statement, count=1,
                          flags=re.IGNORECASE).strip().rstrip(";")
        return statement.strip().rstrip(";")
    if _UNSUPPORTED_INSERT_REPLACE.search(statement):
        raise MySQLDialectError("INSERT OR REPLACE has delete-and-insert effects and needs an explicit MySQL port")

    sqlglot = _sqlglot()
    try:
        tree = _parse_statement_template(statement, sqlglot)
    except MySQLDialectError:
        raise
    except Exception as exc:  # sqlglot exposes several parser-specific errors
        raise MySQLDialectError("statement is outside the supported SQLite SQL grammar") from exc
    # Product dates are ISO-8601 strings (including offsets), not SQLite's
    # numeric Julian-day inputs or relative date modifiers. Parse once with
    # microsecond precision in a UTC session, retaining NULL for invalid dates.
    for node in list(tree.find_all(sqlglot.exp.Anonymous)):
        if node.name.casefold() != "julianday":
            continue
        if len(node.expressions) != 1:
            raise MySQLDialectError("julianday date modifiers need an explicit MySQL port")
        argument = node.expressions[0]
        if isinstance(argument, sqlglot.exp.Literal) and not argument.is_string:
            raise MySQLDialectError("numeric julianday input needs an explicit MySQL port")
        exp = sqlglot.exp
        parsed = exp.Cast(this=argument.copy(), to=exp.DataType.build("DATETIME(6)", dialect="mysql"))
        difference = exp.Anonymous(this="TIMESTAMPDIFF", expressions=[
            exp.Var(this="MICROSECOND"), exp.Literal.string("1970-01-01"), parsed,
        ])
        node.replace(exp.Paren(this=exp.Add(
            # Scientific notation selects floating division; MySQL's default
            # DECIMAL division scale (4) would discard sub-second precision.
            this=exp.Div(this=difference, expression=exp.Literal.number("86400000000e0")),
            expression=exp.Literal.number("2440587.5"),
        )))
    for node in list(tree.walk()):
        if node.key.casefold() in {"jsontype", "jsonbtype"}:
            node.replace(_translate_json_type(node))
    # SQLite JSON text extraction compares with BINARY semantics. MySQL's
    # JSON_UNQUOTE/->> result uses utf8mb4_bin (PAD SPACE), which conflicts
    # with indexed utf8mb4_0900_bin text and treats trailing spaces differently.
    # Pin scalar JSON text to the same NO PAD binary collation as business keys.
    for node in list(tree.find_all(sqlglot.exp.JSONExtractScalar)):
        exp = sqlglot.exp
        if any(isinstance(part, exp.Placeholder) for part in node.walk()):
            raise MySQLDialectError("parameterized JSON extraction needs a named native SQL port")
        extraction = exp.JSONExtract(this=node.this.copy(), expression=node.expression.copy())
        text_value = exp.Collate(
            this=exp.Anonymous(this="JSON_UNQUOTE", expressions=[extraction.copy()]),
            expression=exp.Var(this="utf8mb4_0900_bin"),
        )
        kind = exp.Collate(
            this=exp.Anonymous(this="JSON_TYPE", expressions=[extraction.copy()]),
            expression=exp.Var(this="utf8mb4_0900_bin"),
        )
        # SQLite JSON null is SQL NULL, and JSON booleans participate in
        # integer predicates as 0/1. Treating them as the strings "null" and
        # "true" breaks root-run selection and cancellation/safety predicates.
        value = exp.Case(this=kind, ifs=[
            exp.If(this=exp.Literal.string("NULL"), true=exp.Null()),
            exp.If(this=exp.Literal.string("BOOLEAN"), true=exp.If(
                this=exp.EQ(this=text_value.copy(), expression=exp.Literal.string("true")),
                true=exp.Literal.number(1), false=exp.Literal.number(0),
            )),
        ], default=text_value)
        node.replace(exp.Collate(this=value, expression=exp.Var(this="utf8mb4_0900_bin")))
    # SQLite IS compares arbitrary values with NULL-safe equality. MySQL
    # reserves IS for NULL/TRUE/FALSE, and uses <=> for the general case.
    for node in list(tree.find_all(sqlglot.exp.Is)):
        if not isinstance(node.expression, (sqlglot.exp.Null, sqlglot.exp.Boolean)):
            node.replace(sqlglot.exp.Paren(this=sqlglot.exp.NullSafeEQ(
                this=node.this.copy(), expression=node.expression.copy(),
            )))
    # MySQL requires aliases for every derived table; SQLite permits unnamed
    # FROM/JOIN subqueries. Preserve explicit aliases and avoid name collisions.
    aliases = {table.alias_or_name.casefold() for table in tree.find_all(sqlglot.exp.Table)}
    derived_index = 0
    for node in tree.find_all(sqlglot.exp.Subquery):
        if node.alias or not isinstance(node.parent, (sqlglot.exp.From, sqlglot.exp.Join)):
            continue
        while f"__lka_derived_{derived_index}" in aliases:
            derived_index += 1
        alias = f"__lka_derived_{derived_index}"
        aliases.add(alias)
        node.set("alias", sqlglot.exp.TableAlias(this=sqlglot.exp.to_identifier(alias)))
        derived_index += 1
    _translate_json_each(tree)
    _translate_insert_ignore(tree, ignore_target)
    _translate_upsert(tree, conflict_target)
    _rowid_to_primary_proxy(tree, rowid_column=rowid_column)
    try:
        rendered = tree.sql(dialect="mysql")
    except Exception as exc:
        raise MySQLDialectError("SQLGlot cannot render this statement for MySQL") from exc
    return _qmarks_to_format(rendered)


def split_script(script: str) -> list[str]:
    """Parse a SQL script without naïvely splitting trigger/procedure bodies."""
    if not isinstance(script, str) or not script.strip():
        return []
    if (re.search(r"\bPRAGMA\b", script, re.IGNORECASE)
            or re.search(r"\bCREATE\s+VIRTUAL\s+TABLE\b", script, re.IGNORECASE)
            or re.search(r"\bCREATE\s+TRIGGER\b", script, re.IGNORECASE)):
        raise MySQLDialectError("scripts containing PRAGMA, virtual tables or triggers need explicit ports")
    if _UNSUPPORTED_SCHEMA_NAMES.search(script):
        raise MySQLDialectError("SQLite catalog references need INFORMATION_SCHEMA adaptation")
    sqlglot = _sqlglot()
    try:
        expressions = sqlglot.parse(script, read="sqlite")
    except Exception as exc:
        raise MySQLDialectError("SQL script is outside the supported SQLite grammar") from exc
    if any(expression is None for expression in expressions):
        raise MySQLDialectError("SQL script contains an empty or unsupported statement")
    return [expression.sql(dialect="sqlite") for expression in expressions if expression is not None]
