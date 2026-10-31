"""SQL expression ports for the small SQLite JSON1 table-valued API surface."""
from __future__ import annotations

from typing import Any


def _contains_placeholder(node: Any, exp: Any) -> bool:
    return any(isinstance(item, exp.Placeholder) for item in node.walk())


def _json_type(document: Any, path: Any, exp: Any) -> Any:
    return exp.Collate(
        this=exp.Anonymous(
            this="JSON_TYPE",
            expressions=[exp.JSONExtract(this=document.copy(), expression=path.copy())],
        ),
        expression=exp.Var(this="utf8mb4_0900_bin"),
    )


def _concat(exp: Any, *expressions: Any) -> Any:
    return exp.Anonymous(this="CONCAT", expressions=list(expressions))


def _column(alias: str, name: str, exp: Any) -> Any:
    return exp.Column(this=exp.to_identifier(name), table=exp.to_identifier(alias))


def _json_each_table_source(document: Any, path: Any, exp: Any) -> Any:
    container = exp.JSONExtract(this=document.copy(), expression=path.copy())
    kind = exp.Collate(
        this=exp.Anonymous(this="JSON_TYPE", expressions=[container.copy()]),
        expression=exp.Var(this="utf8mb4_0900_bin"),
    )
    keys = exp.Anonymous(
        this="JSON_KEYS", expressions=[document.copy(), path.copy()],
    )
    array_or_scalar = exp.Anonymous(this="JSON_ARRAY", expressions=[container.copy()])
    return exp.Case(
        this=kind,
        ifs=[
            exp.If(this=exp.Literal.string("OBJECT"), true=keys),
            exp.If(this=exp.Literal.string("ARRAY"), true=container.copy()),
            exp.If(this=exp.Literal.string("STRING"), true=array_or_scalar.copy()),
            exp.If(this=exp.Literal.string("INTEGER"), true=array_or_scalar.copy()),
            exp.If(this=exp.Literal.string("DOUBLE"), true=array_or_scalar.copy()),
            exp.If(this=exp.Literal.string("DECIMAL"), true=array_or_scalar.copy()),
            exp.If(this=exp.Literal.string("BOOLEAN"), true=array_or_scalar.copy()),
            exp.If(this=exp.Literal.string("NULL"), true=array_or_scalar.copy()),
        ],
        default=exp.Anonymous(this="JSON_ARRAY"),
    )


def _member_path(document: Any, path: Any, alias: str, exp: Any) -> Any:
    kind = _json_type(document, path, exp)
    entry = _column(alias, "entry", exp)
    ordinal = _column(alias, "ordinal", exp)
    index = exp.Sub(this=ordinal, expression=exp.Literal.number(1))
    object_path = _concat(
        exp, path.copy(), exp.Literal.string("."),
        exp.Anonymous(this="JSON_QUOTE", expressions=[entry.copy()]),
    )
    array_path = _concat(
        exp, path.copy(), exp.Literal.string("["), index,
        exp.Literal.string("]"),
    )
    return exp.Case(
        this=kind,
        ifs=[
            exp.If(this=exp.Literal.string("OBJECT"), true=object_path),
            exp.If(this=exp.Literal.string("ARRAY"), true=array_path),
        ],
        default=path.copy(),
    )


def _sqlite_scalar(document: Any, path: Any, alias: str, exp: Any) -> Any:
    extraction = exp.JSONExtract(
        this=document.copy(), expression=_member_path(document, path, alias, exp),
    )
    kind = exp.Collate(
        this=exp.Anonymous(this="JSON_TYPE", expressions=[extraction.copy()]),
        expression=exp.Var(this="utf8mb4_0900_bin"),
    )
    text = exp.Anonymous(this="JSON_UNQUOTE", expressions=[extraction.copy()])

    def cast(value: Any, target: str) -> Any:
        return exp.Cast(this=value, to=exp.DataType.build(target))

    string_value = exp.Collate(
        this=text.copy(), expression=exp.Var(this="utf8mb4_0900_bin"),
    )
    json_text = exp.Collate(
        this=cast(extraction.copy(), "CHAR"),
        expression=exp.Var(this="utf8mb4_0900_bin"),
    )
    bool_value = exp.If(
        this=exp.EQ(
            this=exp.Collate(
                this=text.copy(), expression=exp.Var(this="utf8mb4_0900_bin"),
            ),
            expression=exp.Literal.string("true"),
        ),
        true=exp.Literal.number(1),
        false=exp.Literal.number(0),
    )
    return exp.Case(
        this=kind,
        ifs=[
            exp.If(this=exp.Literal.string("NULL"), true=exp.Null()),
            exp.If(this=exp.Literal.string("BOOLEAN"), true=bool_value),
            exp.If(this=exp.Literal.string("INTEGER"), true=cast(text.copy(), "BIGINT")),
            exp.If(this=exp.Literal.string("DOUBLE"), true=cast(text.copy(), "DOUBLE")),
            exp.If(this=exp.Literal.string("DECIMAL"), true=cast(text.copy(), "DOUBLE")),
            exp.If(this=exp.Literal.string("STRING"), true=string_value),
        ],
        # Arrays/objects stay valid JSON text. JSON_TABLE LONGTEXT and CAST AS
        # CHAR avoid bounded VARCHAR conversion and preserve nested extraction.
        default=json_text,
    )


def _column_key(document: Any, path: Any, alias: str, exp: Any) -> Any:
    kind = _json_type(document, path, exp)
    entry = _column(alias, "entry", exp)
    ordinal = _column(alias, "ordinal", exp)
    return exp.Case(
        this=kind,
        ifs=[
            exp.If(this=exp.Literal.string("OBJECT"), true=entry),
            exp.If(
                this=exp.Literal.string("ARRAY"),
                true=exp.Sub(this=ordinal, expression=exp.Literal.number(1)),
            ),
        ],
        default=exp.Null(),
    )


def _non_numeric_json_value(document: Any, path: Any, alias: str, exp: Any) -> Any:
    member_path = _member_path(document, path, alias, exp)
    kind = _json_type(document, member_path, exp)
    return exp.Paren(this=exp.Or(
        this=exp.EQ(this=kind.copy(), expression=exp.Literal.string("STRING")),
        expression=exp.Or(
            this=exp.EQ(this=kind.copy(), expression=exp.Literal.string("OBJECT")),
            expression=exp.EQ(this=kind, expression=exp.Literal.string("ARRAY")),
        ),
    ))


def _preserve_numeric_storage_order(comparison: Any, other: Any, document: Any, path: Any, alias: str, exp: Any) -> None:
    outcomes = {
        exp.EQ: False,
        exp.NEQ: True,
        exp.GT: True,
        exp.GTE: True,
        exp.LT: False,
        exp.LTE: False,
    }
    outcome = outcomes.get(type(comparison))
    if outcome is None:
        return
    if not (isinstance(other, exp.Placeholder) or isinstance(other, exp.Literal) and not other.is_string):
        return
    non_null = exp.Not(this=exp.Is(this=other.copy(), expression=exp.Null()))
    condition = exp.And(
        this=non_null,
        expression=_non_numeric_json_value(document, path, alias, exp),
    )
    comparison.replace(exp.Case(
        ifs=[exp.If(this=condition, true=exp.Boolean(this=outcome))],
        default=comparison.copy(),
    ))


def translate_json_each(tree: Any, exp: Any, error_type: type[Exception]) -> None:
    """Rewrite SQLite ``json_each`` into generic object/array ``JSON_TABLE``.

    The generated row source iterates object keys or array members based on the
    runtime JSON type. ``key`` and ``value`` are rewritten to expressions that
    preserve JSON content, NULLs, and SQLite's numeric-vs-text comparison order.
    MySQL presents heterogeneous scalar CASE results as text in result rows.
    ``json_tree`` and extra json_each metadata columns remain unsupported.
    """
    tables = list(tree.find_all(exp.Table))
    unaliased_tables = [
        table for table in tables
        if isinstance(table.this, exp.Anonymous)
        and table.this.name.casefold() == "json_each"
        and not table.alias
    ]
    if len(unaliased_tables) > 1 and any(
        isinstance(column, exp.Column)
        and not column.table
        and column.name.casefold() in {"key", "value"}
        for column in tree.find_all(exp.Column)
    ):
        raise error_type("unqualified json_each columns are ambiguous")
    used_aliases = {
        str(table.alias or table.name).casefold()
        for table in tables if table.alias or table.name
    }
    synthetic_index = 0
    numeric_comparisons: list[tuple[Any, Any, Any, Any, str]] = []

    for table in tables:
        source = table.this
        if not isinstance(source, exp.Anonymous):
            continue
        function = source.name.casefold()
        if function not in {"json_each", "json_tree"}:
            continue
        if function == "json_tree":
            raise error_type("SQLite json_tree requires an explicit recursive JSON_TABLE port")

        args = source.expressions
        if not args or len(args) > 2:
            raise error_type("unsupported json_each argument list")
        document = args[0].copy()
        if _contains_placeholder(document, exp):
            raise error_type("parameterized json_each documents require an explicit native SQL port")
        if len(args) == 1:
            path = exp.Literal.string("$")
        elif isinstance(args[1], exp.Literal) and args[1].is_string:
            path = args[1].copy()
        else:
            raise error_type("json_each requires a literal JSON path")
        if _contains_placeholder(path, exp):
            raise error_type("parameterized json_each paths require an explicit native SQL port")

        alias = table.alias
        if not alias:
            while f"_lka_json_each_{synthetic_index}" in used_aliases:
                synthetic_index += 1
            alias = f"_lka_json_each_{synthetic_index}"
            synthetic_index += 1
            used_aliases.add(alias.casefold())

        for comparison in tree.find_all((exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            for value_side, other in ((comparison.this, comparison.expression), (comparison.expression, comparison.this)):
                if not isinstance(value_side, exp.Column) or value_side.name.casefold() != "value":
                    continue
                qualified = value_side.table.casefold() == str(alias).casefold()
                unqualified = not table.alias and len(unaliased_tables) == 1 and not value_side.table
                if qualified or unqualified:
                    numeric_comparisons.append((comparison, other.copy(), document.copy(), path.copy(), str(alias)))
                    break

        # Capture original column nodes before inserting generated expressions;
        # otherwise those new references could be rewritten recursively.
        columns = list(tree.find_all(exp.Column))
        for column in columns:
            name = column.name.casefold()
            is_qualified = column.table.casefold() == str(alias).casefold()
            is_unqualified = (
                not table.alias and len(unaliased_tables) == 1 and not column.table
            )
            if not (is_qualified or is_unqualified):
                continue
            if name == "key":
                column.replace(_column_key(document, path, str(alias), exp))
            elif name == "value":
                column.replace(_sqlite_scalar(document, path, str(alias), exp))
            elif name in {"type", "atom", "id", "parent", "fullkey", "path"}:
                raise error_type(
                    f"json_each column {name!r} requires an explicit JSON1 metadata port"
                )

        schema = exp.JSONSchema(expressions=[
            exp.JSONColumnDef(
                this=exp.to_identifier("entry"),
                kind=exp.DataType.build("LONGTEXT"),
                path=exp.Literal.string("$"),
            ),
            exp.JSONColumnDef(this=exp.to_identifier("ordinal"), ordinality=True),
        ])
        table.set("this", exp.JSONTable(
            this=_json_each_table_source(document, path, exp),
            path=exp.Literal.string("$[*]"),
            schema=schema,
        ))
        if not table.alias:
            table.set("alias", exp.TableAlias(this=exp.to_identifier(str(alias))))

    for comparison, other, document, path, alias in numeric_comparisons:
        _preserve_numeric_storage_order(comparison, other, document, path, alias, exp)

    # MySQL's semijoin optimizer can drop rows for a correlated JSON_TABLE in
    # EXISTS, while the equivalent scalar count evaluates the lateral source
    # against each outer row. Keep the rewrite narrow to these JSON1 subqueries.
    for exists in list(tree.find_all(exp.Exists)):
        query = exists.this
        if not isinstance(query, exp.Select) or not any(
            isinstance(item, exp.JSONTable) for item in query.find_all(exp.JSONTable)
        ):
            continue
        if query.args.get("group") or query.args.get("having") or query.args.get("distinct"):
            raise error_type("grouped json_each EXISTS requires an explicit MySQL port")
        query.set("expressions", [exp.Count(this=exp.Star())])
        exists.replace(exp.GT(
            this=exp.Subquery(this=query),
            expression=exp.Literal.number(0),
        ))
