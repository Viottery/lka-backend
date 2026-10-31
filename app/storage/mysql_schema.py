"""MySQL schema metadata, catalog compatibility and versioned DDL utilities."""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.storage.mysql import MySQLConnection, MySQLStorageError

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,190}$")
_MIGRATIONS = "lka_schema_migrations"
_OBJECTS = "lka_mysql_schema_objects"
_ROWIDS = "lka_mysql_rowid_columns"
_COLUMNS = "lka_mysql_schema_columns"
_UNIQUE_KEYS = "lka_mysql_unique_key_mappings"
_VARIANTS = "lka_mysql_schema_object_variants"


class MySQLSchemaError(MySQLStorageError):
    category = "schema"


def _identifier(name: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise ValueError("invalid MySQL schema identifier")
    return f"`{name}`"


@dataclass(frozen=True, slots=True)
class SQLiteColumnMetadata:
    name: str
    declared_type: str
    not_null: bool
    default_value: Any = None
    primary_key_position: int = 0


@dataclass(frozen=True, slots=True)
class _PreparedDDL:
    sql: str
    original_sql: str
    object_type: str
    object_name: str
    table_name: str | None = None
    rowid_column: str | None = None
    rowid_kind: str | None = None
    columns: tuple[SQLiteColumnMetadata, ...] = ()


def _sqlglot():
    try:
        import sqlglot
    except ImportError as exc:  # pragma: no cover - optional dependency boundary
        raise MySQLSchemaError("MySQL schema adaptation requires sqlglot") from exc
    return sqlglot


def _text_key_lengths(index_groups: Sequence[tuple[Sequence[str], int]]) -> dict[str, int]:
    lengths: dict[str, int] = {}
    for group, non_text_bytes in index_groups:
        text_count = len(group)
        if not text_count:
            continue
        per_column = max(1, min(768, (3072 - non_text_bytes) // (4 * text_count)))
        for column in group:
            lengths[column.casefold()] = min(lengths.get(column.casefold(), 768), per_column)
    return lengths


def _table_key_groups(expressions):
    from sqlglot import exp

    groups: list[tuple[str, list[str]]] = []
    for expression in expressions:
        if isinstance(expression, exp.ColumnDef):
            for item in expression.args.get("constraints") or []:
                kind = item.args.get("kind")
                if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                    groups.append(("primary", [expression.name]))
                elif isinstance(kind, exp.UniqueColumnConstraint):
                    groups.append(("unique", [expression.name]))
        elif isinstance(expression, exp.PrimaryKey):
            groups.append(("primary", [item.name for item in expression.expressions]))
        elif isinstance(expression, exp.UniqueColumnConstraint):
            schema = expression.args.get("this")
            names = [item.name for item in (schema.expressions if schema else [])
                     if isinstance(item, exp.Identifier)]
            if names:
                groups.append(("unique", names))
    return groups


def _index_column_group(create):
    from sqlglot import exp

    index = create.this
    table = index.args.get("table")
    params = index.args.get("params")
    if table is None or params is None:
        raise MySQLSchemaError("CREATE INDEX is missing a table or index column list")
    if params.args.get("where") is not None:
        raise MySQLSchemaError("partial indexes require an explicit MySQL index port")
    group = []
    for item in params.args.get("columns") or []:
        value = item.this if isinstance(item, exp.Ordered) else item
        if not isinstance(value, exp.Column) or value.table:
            raise MySQLSchemaError("expression indexes require an explicit MySQL generated-column port")
        group.append(value.name)
    if not group:
        raise MySQLSchemaError("CREATE INDEX has no simple column key")
    return table.name, group, bool(create.args.get("unique"))


def _column_meta(expressions, table_key_groups) -> tuple[SQLiteColumnMetadata, ...]:
    from sqlglot import exp

    pk_position: dict[str, int] = {}
    for kind, names in table_key_groups:
        if kind == "primary":
            for pos, name in enumerate(names, 1):
                pk_position[name.casefold()] = pos
    result = []
    for expression in expressions:
        if not isinstance(expression, exp.ColumnDef):
            continue
        name = expression.name
        kind = expression.args.get("kind")
        declared_type = kind.sql(dialect="sqlite") if kind is not None else ""
        constraints = expression.args.get("constraints") or []
        not_null = any(
            isinstance(item.args.get("kind"), exp.NotNullColumnConstraint)
            for item in constraints
        )
        default_value = None
        for item in constraints:
            constraint = item.args.get("kind")
            if isinstance(constraint, exp.DefaultColumnConstraint) and constraint.this is not None:
                value = constraint.this
                if isinstance(value, exp.Literal) and value.is_string:
                    default_value = value.this
                elif isinstance(value, exp.Literal):
                    default_value = value.to_py()
                else:
                    default_value = value.sql(dialect="sqlite")
        result.append(SQLiteColumnMetadata(
            name=name, declared_type=declared_type, not_null=not_null,
            default_value=default_value, primary_key_position=pk_position.get(name.casefold(), 0),
        ))
    return tuple(result)


class MySQLSchema:
    """Provider metadata for schema versions, object SQL and rowid proxies.

    This stores schema metadata only, never application row data or a SQLite
    shadow database.
    """

    def __init__(self, database: str):
        if not database:
            raise ValueError("database is required")
        self.database = database

    @staticmethod
    def _prepared_statements(script: str, schema: MySQLSchema, conn=None):
        from sqlglot import exp

        # SQLGlot currently falls back to a generic Command node for several
        # SQLite-only constructs. Never let that fallback through as if it were
        # an ordinary SQL statement; MySQL must reject it before rendering.
        has_special_ddl = re.search(
            r"\bCREATE\s+(?:TRIGGER|VIRTUAL\s+TABLE)\b", script, re.IGNORECASE,
        )
        if has_special_ddl:
            statements = schema._split_sql_statements(script)
            if len(statements) > 1:
                prepared = []
                for statement in statements:
                    if re.match(r"\s*CREATE\s+TRIGGER\b", statement, re.IGNORECASE):
                        prepared.append(schema._verified_trigger_probe(statement, conn))
                    elif re.match(r"\s*CREATE\s+VIRTUAL\s+TABLE\b", statement, re.IGNORECASE):
                        prepared.append(schema._verified_fts_probe(statement, conn))
                    else:
                        prepared.extend(schema._prepared_statements(statement, schema, conn))
                return prepared
            if re.search(r"\bCREATE\s+TRIGGER\b", script, re.IGNORECASE):
                return [schema._verified_trigger_probe(script, conn)]
            return [schema._verified_fts_probe(script, conn)]
        if re.search(r"\bWITHOUT\s+ROWID\b", script, re.IGNORECASE):
            raise MySQLSchemaError("SQLite WITHOUT ROWID tables require an explicit MySQL migration")
        sqlglot = _sqlglot()
        try:
            parsed = sqlglot.parse(script, read="sqlite")
        except Exception as exc:
            raise MySQLSchemaError("schema script is outside the supported SQLite grammar") from exc
        if not parsed or any(item is None for item in parsed):
            raise MySQLSchemaError("schema script contains an empty or invalid statement")

        indexes_by_table: dict[str, list[list[str]]] = {}
        for create in parsed:
            if isinstance(create, exp.Create) and str(create.args.get("kind", "")).upper() == "INDEX":
                index = create.this
                if (conn is not None and create.args.get("exists")
                        and schema.index_exists(conn, index.this.name, index.args["table"].name)):
                    continue
                table_name, columns, _ = _index_column_group(create)
                indexes_by_table.setdefault(table_name.casefold(), []).append(columns)

        prepared = []
        for tree in parsed:
            original = tree.sql(dialect="sqlite")
            if isinstance(tree, exp.Create):
                kind = str(tree.args.get("kind", "")).upper()
                if kind == "TABLE":
                    table_name = tree.this.this.name
                    if tree.args.get("exists") and conn is not None and schema.table_exists(conn, table_name):
                        schema._verify_existing_probe(conn, "table", table_name, original)
                        prepared.append(None)
                        continue
                    prepared.append(schema._prepare_create_table(
                        tree, original, indexes_by_table.get(tree.this.this.name.casefold(), []),
                    ))
                    continue
                if kind == "INDEX":
                    index = tree.this
                    name, table_name = index.this.name, index.args["table"].name
                    if conn is not None and tree.args.get("exists") and schema.index_exists(conn, name, table_name):
                        schema._verify_existing_probe(conn, "index", name, original)
                        prepared.append(None)
                        continue
                    tree.set("exists", False)
                    prepared.append(_PreparedDDL(
                        sql=tree.sql(dialect="mysql"), original_sql=original,
                        object_type="index", object_name=name, table_name=table_name,
                    ))
                    continue
                raise MySQLSchemaError(f"CREATE {kind or 'UNKNOWN'} needs an explicit MySQL schema port")
            if isinstance(tree, (exp.Alter, exp.Drop)):
                # Keep alterations explicit: SQLGlot can translate the subset,
                # but callers must still handle old/new schema metadata.
                prepared.append(_PreparedDDL(
                    sql=tree.sql(dialect="mysql"), original_sql=original,
                    object_type="ddl", object_name="",
                ))
                continue
            prepared.append(_PreparedDDL(
                sql=tree.sql(dialect="mysql"), original_sql=original,
                object_type="statement", object_name="",
            ))
        return prepared

    @staticmethod
    def _split_sql_statements(script: str) -> list[str]:
        """Split SQL while preserving semicolons inside SQLite trigger bodies."""
        statements: list[str] = []
        start = 0
        i = 0
        quote: str | None = None
        line_comment = False
        block_comment = False
        first_words: list[str] = []
        trigger = False
        trigger_depth = 0
        while i < len(script):
            char = script[i]
            next_char = script[i + 1] if i + 1 < len(script) else ""
            if line_comment:
                if char == "\n":
                    line_comment = False
                i += 1
                continue
            if block_comment:
                if char == "*" and next_char == "/":
                    block_comment = False
                    i += 2
                else:
                    i += 1
                continue
            if quote is not None:
                if char == "\\" and quote == "'":
                    i += 2
                    continue
                if char == quote:
                    if next_char == quote:
                        i += 2
                        continue
                    quote = None
                i += 1
                continue
            if char in "'\"`":
                quote = char
                i += 1
                continue
            if char == "-" and next_char == "-":
                line_comment = True
                i += 2
                continue
            if char == "/" and next_char == "*":
                block_comment = True
                i += 2
                continue
            if char == "#":
                line_comment = True
                i += 1
                continue
            if char.isalpha() or char == "_":
                end = i + 1
                while end < len(script) and (script[end].isalnum() or script[end] in "_$"):
                    end += 1
                word = script[i:end].upper()
                if not trigger:
                    first_words.append(word)
                    if len(first_words) > 2:
                        first_words.pop(0)
                    if first_words == ["CREATE", "TRIGGER"]:
                        trigger = True
                if trigger:
                    if word == "BEGIN" or (word == "CASE" and trigger_depth):
                        trigger_depth += 1
                    elif word == "END" and trigger_depth:
                        trigger_depth -= 1
                i = end
                continue
            if char == ";" and (not trigger or trigger_depth == 0):
                statement = script[start:i].strip()
                if statement:
                    statements.append(statement)
                start = i + 1
                first_words = []
                trigger = False
                trigger_depth = 0
            i += 1
        if quote is not None or block_comment:
            raise MySQLSchemaError("unterminated quote/comment in SQL script")
        tail = script[start:].strip()
        if tail:
            statements.append(tail)
        return statements

    @staticmethod
    def _normalize_probe_sql(statement: str) -> str:
        """Normalize insignificant whitespace/case while preserving literals."""
        value = statement.strip().rstrip(";").strip()
        value = re.sub(
            r"^(CREATE\s+(?:TABLE|INDEX|TRIGGER|VIRTUAL\s+TABLE)\s+)IF\s+NOT\s+EXISTS\s+",
            r"\1", value, count=1, flags=re.IGNORECASE,
        )
        out: list[str] = []
        quote: str | None = None
        i = 0
        while i < len(value):
            char = value[i]
            if quote is not None:
                out.append(char)
                if char == quote:
                    if i + 1 < len(value) and value[i + 1] == quote:
                        i += 1
                        out.append(value[i])
                    else:
                        quote = None
                elif char == "\\" and quote == "'" and i + 1 < len(value):
                    i += 1
                    out.append(value[i])
                i += 1
                continue
            if char in "'\"`":
                quote = char
                out.append(char)
            elif not char.isspace():
                out.append(char.casefold())
            i += 1
        if quote is not None:
            raise MySQLSchemaError("unterminated quote in schema probe")
        return "".join(out)

    @staticmethod
    def _probe_ast_key(statement: str) -> str:
        """Canonicalize only lexical CREATE differences, retaining structure/literals."""
        from sqlglot import exp

        parsed = _sqlglot().parse_one(statement, read="sqlite")
        for identifier in parsed.find_all(exp.Identifier):
            identifier.set("this", str(identifier.this).casefold())
            identifier.set("quoted", False)
        if isinstance(parsed, exp.Create):
            parsed.set("exists", False)
        return parsed.sql(dialect="sqlite", normalize=True, comments=False)

    def _verify_existing_probe(
        self, conn: MySQLConnection, object_type: str, object_name: str, probe_sql: str,
    ) -> None:
        stored = self.master_objects(conn, object_type=object_type, names=[object_name])
        if len(stored) != 1 or stored[0][1] is None:
            raise MySQLSchemaError(
                f"existing {object_type} {object_name!r} lacks reviewed source schema metadata"
            )
        normalized_probe = self._normalize_probe_sql(probe_sql)
        if self._normalize_probe_sql(stored[0][1]) == normalized_probe:
            return
        if self._probe_ast_key(stored[0][1]) == self._probe_ast_key(probe_sql):
            return
        if normalized_probe in self.object_source_variants(conn, object_type, object_name):
            return
        else:
            raise MySQLSchemaError(
                f"existing {object_type} {object_name!r} does not match its reviewed source definition"
            )

    def _verified_trigger_probe(self, script: str, conn) -> _PreparedDDL | None:
        match = re.match(
            r"\s*CREATE\s+TRIGGER\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_$]*)\b",
            script, re.IGNORECASE,
        )
        if conn is None or not match:
            raise MySQLSchemaError("SQLite triggers require an existing verified MySQL trigger")
        name = match.group(1)
        exists = conn.execute_native(
            "SELECT 1 FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
            "AND TRIGGER_NAME=%s LIMIT 1", (name,),
        ).fetchone()
        if exists is None:
            external = self.master_objects(conn, object_type="external_trigger", names=[name])
            if len(external) != 1 or external[0][1] is None:
                raise MySQLSchemaError(f"trigger {name!r} has not been bootstrapped")
            if self._normalize_probe_sql(external[0][1]) != self._normalize_probe_sql(script):
                raise MySQLSchemaError(
                    f"external trigger {name!r} does not match its reviewed source definition"
                )
            return None
        self._verify_existing_probe(conn, "trigger", name, script)
        return None

    def _verified_fts_probe(self, script: str, conn) -> _PreparedDDL | None:
        match = re.match(
            r"\s*CREATE\s+VIRTUAL\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
            r"([A-Za-z_][A-Za-z0-9_$]*)\s+USING\s+FTS5\b",
            script, re.IGNORECASE,
        )
        if conn is None or not match:
            raise MySQLSchemaError("SQLite FTS requires an explicitly bootstrapped external index")
        name = match.group(1)
        self._verify_existing_probe(conn, "fts", name, script)
        return None

    @staticmethod
    def _prepare_create_table(tree, original_sql: str, index_groups):
        from sqlglot import exp

        schema_node = tree.this
        if not isinstance(schema_node, exp.Schema):
            raise MySQLSchemaError("CREATE TABLE AS SELECT requires an explicit MySQL migration")
        if re.search(r"\bWITHOUT\s+ROWID\b", original_sql, re.IGNORECASE):
            raise MySQLSchemaError("SQLite WITHOUT ROWID tables require an explicit MySQL migration")
        table_name = schema_node.this.name
        _identifier(table_name)
        expressions = list(schema_node.expressions)
        columns = [item for item in expressions if isinstance(item, exp.ColumnDef)]
        by_name = {item.name.casefold(): item for item in columns}
        keys = _table_key_groups(expressions)
        for index_columns in index_groups:
            if any(name.casefold() not in by_name for name in index_columns):
                raise MySQLSchemaError("index references a missing table column")
            keys.append(("index", index_columns))

        # Bound indexed TEXT columns within InnoDB's 3072-byte key limit.
        text_names = {
            column.name.casefold() for column in columns
            if column.args.get("kind") is not None
            and column.args["kind"].this == exp.DType.TEXT
        }
        index_bounds = []
        for _, group in keys:
            indexed_text = [name for name in group if name.casefold() in text_names]
            if not indexed_text:
                continue
            non_text_bytes = sum(
                8 if by_name[name.casefold()].args["kind"].this in {
                    exp.DType.BIGINT, exp.DType.DOUBLE, exp.DType.FLOAT, exp.DType.DECFLOAT,
                } else 4
                for name in group if name.casefold() not in text_names
            )
            index_bounds.append((indexed_text, non_text_bytes))
        lengths = _text_key_lengths(index_bounds)
        column_metadata = _column_meta(columns, keys)

        primary_groups = [names for kind, names in keys if kind == "primary"]
        integer_primary = None
        if len(primary_groups) == 1 and len(primary_groups[0]) == 1:
            candidate = by_name.get(primary_groups[0][0].casefold())
            if (candidate is not None and candidate.args.get("kind") is not None
                    and candidate.args["kind"].this in {exp.DType.INT, exp.DType.BIGINT}):
                integer_primary = candidate
        if any(column.name.casefold() == "__lka_rowid" for column in columns):
            raise MySQLSchemaError("table already uses reserved __lka_rowid column name")

        for column in columns:
            kind = column.args.get("kind")
            if kind is None:
                continue
            lowered = column.name.casefold()
            if lowered in lengths:
                column.set("kind", exp.DataType.build(f"VARCHAR({lengths[lowered]})"))
                constraints = list(column.args.get("constraints") or [])
                constraints.insert(0, exp.ColumnConstraint(kind=exp.CollateColumnConstraint(
                    this=exp.Var(this="utf8mb4_0900_bin"),
                )))
                column.set("constraints", constraints)
            elif kind.this == exp.DType.TEXT:
                column.set("kind", exp.DataType.build("LONGTEXT"))
            elif kind.this == exp.DType.BLOB:
                column.set("kind", exp.DataType.build("LONGBLOB"))
            for item in column.args.get("constraints") or []:
                constraint = item.args.get("kind")
                if (isinstance(constraint, exp.DefaultColumnConstraint)
                        and isinstance(constraint.this, exp.Literal)
                        and constraint.this.is_string
                        and column.args["kind"].this in {exp.DType.TEXT, exp.DType.LONGTEXT}):
                    constraint.set("this", exp.Paren(this=constraint.this.copy()))

        proxy_kind = "integer_primary_key" if integer_primary is not None else "invisible_auto_increment"
        if integer_primary is not None:
            constraints = list(integer_primary.args.get("constraints") or [])
            if not any(isinstance(item.args.get("kind"), exp.AutoIncrementColumnConstraint)
                       for item in constraints):
                constraints.append(exp.ColumnConstraint(kind=exp.AutoIncrementColumnConstraint()))
                integer_primary.set("constraints", constraints)
            rowid_column = integer_primary.name
        else:
            rowid_column = "__lka_rowid"
            expressions.append(exp.ColumnDef(
                this=exp.to_identifier(rowid_column),
                kind=exp.DataType.build("BIGINT UNSIGNED"),
                constraints=[
                    exp.ColumnConstraint(kind=exp.NotNullColumnConstraint()),
                    exp.ColumnConstraint(kind=exp.AutoIncrementColumnConstraint()),
                    exp.ColumnConstraint(kind=exp.InvisibleColumnConstraint()),
                    exp.ColumnConstraint(kind=exp.UniqueColumnConstraint()),
                ],
            ))
            schema_node.set("expressions", expressions)

        properties = tree.args.get("properties")
        props = list(properties.expressions) if properties is not None else []
        props.extend([
            exp.EngineProperty(this=exp.Var(this="InnoDB")),
            exp.CharacterSetProperty(this=exp.Var(this="utf8mb4"), default=True),
            exp.CollateProperty(this=exp.Var(this="utf8mb4_0900_bin")),
        ])
        tree.set("properties", exp.Properties(expressions=props))
        return _PreparedDDL(
            sql=tree.sql(dialect="mysql"), original_sql=original_sql,
            object_type="table", object_name=table_name, table_name=table_name,
            rowid_column=rowid_column, rowid_kind=proxy_kind, columns=column_metadata,
        )

    def execute_schema_statement(self, conn: MySQLConnection, statement: str):
        prepared = self._prepared_statements(statement, self, conn)
        if len(prepared) != 1:
            raise MySQLSchemaError("expected one schema statement")
        item = prepared[0]
        if item is None:
            return conn.execute_native("SELECT 1 WHERE 0")
        cursor = conn.execute_native(item.sql)
        conn.commit()
        self._remember_prepared(conn, item)
        return cursor

    def execute_schema_script(self, conn: MySQLConnection, script: str):
        if conn.in_transaction:
            raise MySQLSchemaError("schema script cannot run inside a business transaction")
        prepared = self._prepared_statements(script, self, conn)
        result = None
        for item in prepared:
            if item is None:
                continue
            if item.object_type in {"table", "index", "ddl"}:
                result = conn.execute_native(item.sql)
                conn.commit()
                self._remember_prepared(conn, item)
            else:
                result = conn.execute(item.sql)
        return result if result is not None else conn.execute_native("SELECT 1 WHERE 0")

    def _remember_prepared(self, conn: MySQLConnection, item: _PreparedDDL):
        if item.object_type in {"table", "index"}:
            self.remember_object(
                conn, object_type=item.object_type, object_name=item.object_name,
                table_name=item.table_name, source_sql=item.original_sql,
            )
            if item.object_type == "table":
                self.remember_rowid(
                    conn, item.object_name, item.rowid_column or "",
                    proxy_kind=item.rowid_kind or "invisible_auto_increment",
                )
                self.remember_columns(conn, item.object_name, item.columns)
            conn.commit()

    def ensure_metadata(self, conn: MySQLConnection) -> None:
        if conn.in_transaction:
            raise MySQLSchemaError("schema bootstrap cannot run inside a business transaction")
        statements = (
            f"""CREATE TABLE IF NOT EXISTS `{_MIGRATIONS}` (
                version BIGINT NOT NULL PRIMARY KEY,
                name VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
                checksum CHAR(64) COLLATE ascii_bin NOT NULL,
                applied_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
            f"""CREATE TABLE IF NOT EXISTS `{_OBJECTS}` (
                object_type VARCHAR(16) COLLATE utf8mb4_bin NOT NULL,
                object_name VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
                table_name VARCHAR(191) COLLATE utf8mb4_bin,
                source_sql LONGTEXT,
                PRIMARY KEY(object_type,object_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
            f"""CREATE TABLE IF NOT EXISTS `{_ROWIDS}` (
                table_name VARCHAR(191) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
                column_name VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
                proxy_kind VARCHAR(32) COLLATE utf8mb4_bin NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
            f"""CREATE TABLE IF NOT EXISTS `{_COLUMNS}` (
                table_name VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
                column_name VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
                declared_type VARCHAR(191) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
                not_null TINYINT(1) NOT NULL DEFAULT 0,
                default_value LONGTEXT,
                primary_key_position INT NOT NULL DEFAULT 0,
                PRIMARY KEY(table_name,column_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
            f"""CREATE TABLE IF NOT EXISTS `{_UNIQUE_KEYS}` (
                table_name VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
                mapping_hash CHAR(64) COLLATE ascii_bin NOT NULL,
                sqlite_columns LONGTEXT NOT NULL,
                mysql_columns LONGTEXT NOT NULL,
                guard_trigger_names LONGTEXT NOT NULL,
                PRIMARY KEY(table_name,mapping_hash)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
        )
        for statement in statements:
            conn.execute_native(statement)
        # Early lab builds used VARCHAR(16), which truncated/rejected the
        # invisible proxy enum. Upgrade this metadata-only column safely.
        length = conn.execute_native(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME='proxy_kind'",
            (_ROWIDS,),
        ).fetchone()
        if length is not None and int(length[0]) < 32:
            conn.execute_native(
                f"ALTER TABLE `{_ROWIDS}` MODIFY proxy_kind VARCHAR(32) "
                "COLLATE utf8mb4_bin NOT NULL"
            )

    def ensure_object_variants_metadata(self, conn: MySQLConnection) -> None:
        """Create the v2 allowlist store for reviewed legacy CREATE probes.

        This is called only by the privileged, checksum-versioned schema
        migration. Runtime attach validates that this table already exists.
        """
        if conn.in_transaction:
            raise MySQLSchemaError("schema migration cannot run inside a business transaction")
        conn.execute_native(
            f"""CREATE TABLE IF NOT EXISTS `{_VARIANTS}` (
                object_type VARCHAR(16) COLLATE utf8mb4_bin NOT NULL,
                object_name VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
                source_hash CHAR(64) COLLATE ascii_bin NOT NULL,
                source_sql LONGTEXT NOT NULL,
                PRIMARY KEY(object_type,object_name,source_hash)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
        )
        conn.commit()

    def remember_object_variant(
        self, conn: MySQLConnection, *, object_type: str, object_name: str, source_sql: str,
    ) -> None:
        """Persist one exact source-SQL variant from a reviewed schema asset."""
        if object_type not in {"table", "index", "trigger", "fts", "external_trigger"}:
            raise ValueError("unsupported schema object type")
        _identifier(object_name)
        if not isinstance(source_sql, str) or not source_sql.strip():
            raise ValueError("source_sql must be non-empty text")
        normalized = self._normalize_probe_sql(source_sql)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        base = self.master_objects(conn, object_type=object_type, names=[object_name])
        if len(base) != 1 or base[0][1] is None:
            raise MySQLSchemaError("a source variant requires a reviewed base schema object")
        if self._normalize_probe_sql(base[0][1]) == normalized:
            return
        current = conn.execute_native(
            f"SELECT source_sql FROM `{_VARIANTS}` "
            "WHERE object_type=%s AND object_name=%s AND source_hash=%s",
            (object_type, object_name, digest),
        ).fetchone()
        if current is not None and self._normalize_probe_sql(current[0]) != normalized:
            raise MySQLSchemaError("schema source variant hash collision")
        conn.execute_native(
            f"INSERT INTO `{_VARIANTS}`(object_type,object_name,source_hash,source_sql) "
            "VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE source_sql=VALUES(source_sql)",
            (object_type, object_name, digest, source_sql),
        )

    def object_source_variants(
        self, conn: MySQLConnection, object_type: str, object_name: str,
    ) -> set[str]:
        """Return verified normalized variants recorded by the v2 migration."""
        _identifier(object_name)
        rows = conn.execute_native(
            f"SELECT source_hash,source_sql FROM `{_VARIANTS}` "
            "WHERE object_type=%s AND object_name=%s",
            (object_type, object_name),
        ).fetchall()
        result = set()
        for digest, source_sql in rows:
            normalized = self._normalize_probe_sql(source_sql)
            actual = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if actual != str(digest):
                raise MySQLSchemaError("reviewed schema source variant checksum mismatch")
            result.add(normalized)
        return result

    def table_exists(self, conn: MySQLConnection, table_name: str) -> bool:
        _identifier(table_name)
        return conn.execute_native(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s LIMIT 1",
            (table_name,),
        ).fetchone() is not None

    def column_names(self, conn: MySQLConnection, table_name: str) -> set[str]:
        _identifier(table_name)
        rows = conn.execute_native(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            (table_name,),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def index_exists(self, conn: MySQLConnection, index_name: str, table_name: str) -> bool:
        _identifier(index_name)
        _identifier(table_name)
        return conn.execute_native(
            "SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s LIMIT 1",
            (table_name, index_name),
        ).fetchone() is not None

    def remember_object(
        self, conn: MySQLConnection, *, object_type: str, object_name: str,
        source_sql: str, table_name: str | None = None,
    ) -> None:
        if object_type not in {
            "table", "index", "view", "trigger", "fts", "external_trigger",
        }:
            raise ValueError("unsupported schema object type")
        _identifier(object_name)
        if table_name is not None:
            _identifier(table_name)
        conn.execute_native(
            f"INSERT INTO `{_OBJECTS}`(object_type,object_name,table_name,source_sql) "
            "VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "table_name=VALUES(table_name),source_sql=VALUES(source_sql)",
            (object_type, object_name, table_name, source_sql),
        )

    def remember_rowid(
        self, conn: MySQLConnection, table_name: str, column_name: str,
        *, proxy_kind: str,
    ) -> None:
        _identifier(table_name)
        _identifier(column_name)
        if proxy_kind not in {"integer_primary_key", "invisible_auto_increment"}:
            raise ValueError("unsupported rowid proxy kind")
        conn.execute_native(
            f"INSERT INTO `{_ROWIDS}`(table_name,column_name,proxy_kind) VALUES(%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE column_name=VALUES(column_name),proxy_kind=VALUES(proxy_kind)",
            (table_name, column_name, proxy_kind),
        )

    def rowid_column(self, conn: MySQLConnection, table_name: str) -> str | None:
        _identifier(table_name)
        row = conn.execute_native(
            f"SELECT column_name FROM `{_ROWIDS}` WHERE table_name=%s", (table_name,),
        ).fetchone()
        return str(row[0]) if row else None

    def validate_conflict_target(
        self, conn: MySQLConnection, table_name: str, target: tuple[str, ...],
    ) -> bool:
        """Confirm MySQL's any-unique-key upsert matches SQLite's one target."""
        _identifier(table_name)
        identity = self.rowid_column(conn, table_name)
        rows = conn.execute_native(
            "SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME "
            "FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME=%s ORDER BY INDEX_NAME,SEQ_IN_INDEX",
            (table_name,),
        ).fetchall()
        unique_groups: dict[str, list[str]] = {}
        for row in rows:
            index_name, non_unique, _, column_name = row
            if int(non_unique) != 0:
                continue
            # The hidden generated SQLite-rowid proxy can never collide with a
            # normal implicit insert; it is not part of the source schema's
            # ON CONFLICT target set.
            if str(column_name).casefold() == "__lka_rowid":
                continue
            unique_groups.setdefault(str(index_name), []).append(str(column_name))
        mappings = conn.execute_native(
            f"SELECT sqlite_columns,mysql_columns,guard_trigger_names FROM `{_UNIQUE_KEYS}` "
            "WHERE table_name=%s", (table_name,),
        ).fetchall()
        physical_to_source: dict[tuple[str, ...], tuple[str, ...]] = {}
        guard_triggers: set[str] = set()
        for sqlite_json, mysql_json, triggers_json in mappings:
            sqlite_columns = tuple(str(name).casefold() for name in json.loads(sqlite_json))
            mysql_columns = tuple(str(name).casefold() for name in json.loads(mysql_json))
            trigger_names = tuple(str(name) for name in json.loads(triggers_json))
            if not sqlite_columns or not mysql_columns:
                return False
            if sqlite_columns != mysql_columns and not trigger_names:
                return False
            physical_to_source[mysql_columns] = sqlite_columns
            guard_triggers.update(trigger_names)
        if guard_triggers:
            placeholders = ",".join("%s" for _ in guard_triggers)
            existing = conn.execute_native(
                "SELECT TRIGGER_NAME FROM INFORMATION_SCHEMA.TRIGGERS "
                f"WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME IN ({placeholders})",
                tuple(sorted(guard_triggers)),
            ).fetchall()
            if {str(row[0]) for row in existing} != guard_triggers:
                return False
        source_groups = {
            index_name: physical_to_source.get(
                tuple(column.casefold() for column in columns),
                tuple(column.casefold() for column in columns),
            )
            for index_name, columns in unique_groups.items()
        }
        normalized_target = tuple(item.casefold() for item in target)
        return bool(unique_groups) and all(
            columns == normalized_target for columns in source_groups.values()
        ) and identity is not None

    def remember_unique_key_mapping(
        self, conn: MySQLConnection, table_name: str,
        sqlite_columns: Sequence[str], mysql_columns: Sequence[str],
        guard_trigger_names: Sequence[str] = (),
    ) -> None:
        """Record a reviewed source-key to physical-key mapping.

        Used for SHA-256 generated physical keys that preserve the original
        unbounded source columns and whose collision guards are installed by
        the versioned schema manifest.
        """
        _identifier(table_name)
        source = tuple(sqlite_columns)
        physical = tuple(mysql_columns)
        guards = tuple(guard_trigger_names)
        if not source or not physical:
            raise ValueError("source and physical unique key columns are required")
        for name in (*source, *physical, *guards):
            _identifier(name)
        source_json = json.dumps(source, separators=(",", ":"))
        physical_json = json.dumps(physical, separators=(",", ":"))
        guards_json = json.dumps(guards, separators=(",", ":"))
        mapping_hash = hashlib.sha256(
            (table_name + "\0" + source_json).encode("utf-8"),
        ).hexdigest()
        conn.execute_native(
            f"INSERT INTO `{_UNIQUE_KEYS}`(table_name,mapping_hash,sqlite_columns,mysql_columns,"
            "guard_trigger_names) VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "mysql_columns=VALUES(mysql_columns),guard_trigger_names=VALUES(guard_trigger_names)",
            (table_name, mapping_hash, source_json, physical_json, guards_json),
        )

    def validate_ignore_target(self, conn: MySQLConnection, table_name: str) -> str | None:
        """Return a no-op update column only when IGNORE has no trigger effects."""
        _identifier(table_name)
        trigger = conn.execute_native(
            "SELECT 1 FROM INFORMATION_SCHEMA.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=%s LIMIT 1",
            (table_name,),
        ).fetchone()
        if trigger is not None:
            return None
        column = conn.execute_native(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME=%s AND EXTRA NOT LIKE '%%INVISIBLE%%' ORDER BY ORDINAL_POSITION LIMIT 1",
            (table_name,),
        ).fetchone()
        return str(column[0]) if column else None

    def remember_columns(
        self, conn: MySQLConnection, table_name: str,
        columns: Iterable[SQLiteColumnMetadata],
    ) -> None:
        _identifier(table_name)
        for column in columns:
            _identifier(column.name)
            conn.execute_native(
                f"INSERT INTO `{_COLUMNS}`(table_name,column_name,declared_type,not_null,"
                "default_value,primary_key_position) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE declared_type=VALUES(declared_type),"
                "not_null=VALUES(not_null),default_value=VALUES(default_value),"
                "primary_key_position=VALUES(primary_key_position)",
                (table_name, column.name, column.declared_type, int(column.not_null),
                 column.default_value, column.primary_key_position),
            )

    def table_info(self, conn: MySQLConnection, table_name: str) -> list[tuple[Any, ...]]:
        """Return SQLite `PRAGMA table_info`-shaped rows from recorded metadata."""
        _identifier(table_name)
        rows = conn.execute_native(
            "SELECT c.COLUMN_NAME,c.COLUMN_TYPE,c.IS_NULLABLE,c.COLUMN_DEFAULT,c.COLUMN_KEY,"
            "c.ORDINAL_POSITION,m.declared_type,m.not_null,m.default_value,m.primary_key_position "
            "FROM INFORMATION_SCHEMA.COLUMNS c LEFT JOIN `" + _COLUMNS + "` m "
            "ON m.table_name=c.TABLE_NAME AND m.column_name=c.COLUMN_NAME "
            "WHERE c.TABLE_SCHEMA=DATABASE() AND c.TABLE_NAME=%s "
            "AND c.EXTRA NOT LIKE '%%INVISIBLE%%' ORDER BY c.ORDINAL_POSITION",
            (table_name,),
        ).fetchall()
        return [
            (int(row[5]) - 1, row[0], row[6] or row[1], int(row[7] if row[7] is not None else row[2] == "NO"),
             row[8] if row[8] is not None else row[3], int(row[9] or 0))
            for row in rows
        ]

    def master_objects(
        self, conn: MySQLConnection, *, object_type: str | None = None,
        table_name: str | None = None, names: Sequence[str] | None = None,
    ) -> list[tuple[str, str | None]]:
        """Return saved SQLite catalog `(name, sql)` projections for migration adapters."""
        clauses, args = [], []
        if object_type is not None:
            if object_type not in {
                "table", "index", "view", "trigger", "fts", "external_trigger",
            }:
                raise ValueError("unsupported schema object type")
            if object_type == "table":
                clauses.append("object_type IN (%s,%s)")
                args.extend(("table", "fts"))
            elif object_type == "trigger":
                clauses.append("object_type IN (%s,%s)")
                args.extend(("trigger", "external_trigger"))
            else:
                clauses.append("object_type=%s")
                args.append(object_type)
        if table_name is not None:
            _identifier(table_name)
            clauses.append("table_name=%s")
            args.append(table_name)
        if names is not None:
            if not names:
                return []
            for name in names:
                _identifier(name)
            clauses.append("object_name IN (" + ",".join("%s" for _ in names) + ")")
            args.extend(names)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return conn.execute_native(
            f"SELECT object_name,source_sql FROM `{_OBJECTS}`{where} ORDER BY object_name",
            tuple(args),
        ).fetchall()

    @staticmethod
    def _literal_or_parameter(value: str, parameters: Sequence[Any], offset: int) -> tuple[Any, int]:
        value = value.strip()
        if value == "?":
            if offset >= len(parameters):
                raise MySQLSchemaError("catalog query has too few parameters")
            return parameters[offset], offset + 1
        if len(value) >= 2 and value[0] == value[-1] == "'":
            return value[1:-1].replace("''", "'"), offset
        raise MySQLSchemaError("unsupported SQLite catalog predicate")

    def catalog_query(
        self, conn: MySQLConnection, statement: str, parameters: Sequence[Any] = (),
    ) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]:
        """Emulate only catalog SELECT patterns present in current application code."""
        match = re.fullmatch(
            r"\s*SELECT\s+(1|name(?:\s*,\s*sql)?)\s+FROM\s+sqlite_(?:temp_)?master"
            r"(?:\s+WHERE\s+(.+?))?\s*;?\s*", statement, re.IGNORECASE | re.DOTALL,
        )
        if not match:
            raise MySQLSchemaError("unsupported sqlite_master SELECT; use MySQLSchema APIs")
        projection = re.sub(r"\s+", "", match.group(1)).casefold()
        where = match.group(2) or ""
        object_type = None
        type_match = re.search(r"\btype\s*=\s*'([^']+)'", where, re.IGNORECASE)
        if type_match:
            object_type = type_match.group(1).casefold()
        table_name = None
        table_match = re.search(r"\btbl_name\s*=\s*(\?|\s*'[^']*')", where, re.IGNORECASE)
        param_offset = 0
        if table_match:
            table_name, param_offset = self._literal_or_parameter(
                table_match.group(1), parameters, param_offset,
            )
        names = None
        name_in = re.search(r"\bname\s+IN\s*\(([^)]*)\)", where, re.IGNORECASE)
        name_eq = re.search(r"\bname\s*=\s*(\?|\s*'[^']*')", where, re.IGNORECASE)
        if name_in:
            values = [item.strip() for item in name_in.group(1).split(",") if item.strip()]
            names = []
            for value in values:
                item, param_offset = self._literal_or_parameter(value, parameters, param_offset)
                names.append(str(item))
        elif name_eq:
            item, param_offset = self._literal_or_parameter(
                name_eq.group(1), parameters, param_offset,
            )
            names = [str(item)]
        if param_offset != len(parameters):
            raise MySQLSchemaError("catalog query has unused parameters")
        if "sqlite_temp_master" in statement.casefold():
            objects = []
        else:
            objects = self.master_objects(
                conn, object_type=object_type, table_name=table_name, names=names,
            )
        if projection == "1":
            return ("1",), [(1,)] if objects else []
        if projection == "name,sql":
            return ("name", "sql"), [tuple(row) for row in objects]
        return ("name",), [(row[0],) for row in objects]

    def pragma_table_info_query(
        self, conn: MySQLConnection, statement: str, parameters: Sequence[Any] = (),
    ) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]:
        match = re.fullmatch(
            r"\s*PRAGMA\s+table_info\s*\(\s*['`\"]?([A-Za-z_][A-Za-z0-9_$]*)['`\"]?\s*\)\s*;?\s*",
            statement, re.IGNORECASE,
        )
        if not match or parameters:
            raise MySQLSchemaError("unsupported PRAGMA; use MySQLSchema INFORMATION_SCHEMA APIs")
        rows = self.table_info(conn, match.group(1))
        return ("cid", "name", "type", "notnull", "dflt_value", "pk"), rows

    def apply_migration(
        self, conn: MySQLConnection, *, version: int, name: str,
        statements: Sequence[str],
    ) -> None:
        """Apply idempotent DDL outside business transactions and record checksum.

        MySQL DDL implicitly commits, so a failed migration may be retried only
        when its DDL statements are individually idempotent.
        """
        if conn.in_transaction:
            raise MySQLSchemaError("schema migration cannot interrupt an active business transaction")
        if version < 1 or not name:
            raise ValueError("migration version and name are required")
        self.ensure_metadata(conn)
        checksum = hashlib.sha256("\n".join(statements).encode()).hexdigest()
        lock_name = "lka-schema-" + hashlib.sha256(self.database.encode()).hexdigest()[:40]
        acquired = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
        if not acquired or int(acquired[0] or 0) != 1:
            raise MySQLSchemaError("timed out acquiring MySQL schema migration lock")
        try:
            existing = conn.execute_native(
                f"SELECT name,checksum FROM `{_MIGRATIONS}` WHERE version=%s", (version,),
            ).fetchone()
            if existing is not None:
                if existing[0] != name or existing[1] != checksum:
                    raise MySQLSchemaError("applied schema migration checksum/name mismatch")
                return
            for statement in statements:
                conn.executescript(statement)
            conn.execute_native(
                f"INSERT INTO `{_MIGRATIONS}`(version,name,checksum) VALUES(%s,%s,%s)",
                (version, name, checksum),
            )
            conn.commit()
        finally:
            conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()


def attach_schema_metadata(
    conn: MySQLConnection, schema: MySQLSchema, *, ensure_metadata: bool = True,
    verify_metadata: bool = True,
) -> None:
    """Install SQLite compatibility resolvers, optionally requiring read-only attach.

    Schema migration paths create/upgrade metadata tables. Runtime connections
    should pass ``ensure_metadata=False`` so attaching never executes DDL.
    """
    if ensure_metadata:
        schema.ensure_metadata(conn)
    elif verify_metadata:
        required = {_MIGRATIONS, _OBJECTS, _ROWIDS, _COLUMNS, _UNIQUE_KEYS, _VARIANTS}
        placeholders = ",".join("%s" for _ in required)
        present = conn.execute_native(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE() "
            f"AND TABLE_NAME IN ({placeholders})", tuple(sorted(required)),
        ).fetchall()
        missing = sorted(required - {row[0] for row in present})
        if missing:
            raise MySQLSchemaError(
                f"MySQL schema metadata is incomplete; run the privileged migration: {missing}"
            )
    rowid_cache: dict[str, str | None] = {}
    connection = weakref.proxy(conn)

    def resolve(table_name: str) -> str | None:
        key = table_name.casefold()
        if key not in rowid_cache:
            rowid_cache[key] = schema.rowid_column(connection, table_name)
        return rowid_cache[key]

    conn._schema = schema
    conn.set_rowid_resolver(resolve)
    conn.set_conflict_target_resolver(
        lambda table_name, columns: schema.validate_conflict_target(connection, table_name, columns),
    )
    conn.set_ignore_target_resolver(
        lambda table_name: schema.validate_ignore_target(connection, table_name),
    )


def verify_object_variants(
    conn: MySQLConnection, schema: MySQLSchema, records: Sequence[Mapping[str, Any]],
) -> None:
    """Check the installed v2 source-probe allowlist against its asset records."""
    expected: dict[tuple[str, str], set[str]] = {}
    for record in records:
        object_type = record.get("object_type", "")
        object_name = record.get("object_name", record.get("name", ""))
        source_sql = record.get("source_sql", "")
        if not object_type or not object_name or not source_sql:
            raise MySQLSchemaError("source variant manifest record is incomplete")
        key = (object_type, object_name)
        normalized = schema._normalize_probe_sql(source_sql)
        bucket = expected.setdefault(key, set())
        if normalized in bucket:
            raise MySQLSchemaError("source variant manifest contains duplicate definitions")
        bucket.add(normalized)
    for (object_type, object_name), reviewed in expected.items():
        installed = schema.object_source_variants(conn, object_type, object_name)
        if installed != reviewed:
            raise MySQLSchemaError(
                f"installed source variants for {object_type} {object_name!r} do not match v2 manifest"
            )
