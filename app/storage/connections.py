"""Select the configured authority for a runtime database; SQLite stays the default.

Only explicitly registered paths use MySQL. Local replicas, imports, and derived
caches retain their own SQLite files; a MySQL failure never falls back to SQLite.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

_guard = threading.RLock()
_targets: dict[str, object] = {}
_transaction_guards: dict[str, object] = {}
_verified_schemas: dict[str, tuple[object, tuple]] = {}
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _key(database) -> str | None:
    raw = os.fspath(database)
    if raw == ":memory:":
        return None
    if raw.startswith("file:"):
        parsed = urlsplit(raw)
        if parsed.path in {":memory:", "/:memory:"} or parse_qs(parsed.query).get("mode") == ["memory"]:
            return None
        raw = unquote(parsed.path)
    return os.path.normcase(str(Path(raw).expanduser().resolve()))


def configure_mysql(database, config, *, transaction_guard=None) -> None:
    key = _key(database)
    if key is None:
        raise ValueError("MySQL authority requires a persistent runtime identity")
    with _guard:
        current = _targets.get(key)
        if current is not None and current != config:
            raise RuntimeError("Database identity is already configured")
        _targets[key] = config
        if transaction_guard is not None:
            if _transaction_guards.get(key) != transaction_guard:
                _verified_schemas.pop(key, None)
            _transaction_guards[key] = transaction_guard


def mysql_config_for(database):
    key = _key(database)
    with _guard:
        return _targets.get(key)


def unregister_mysql(database, *, expected=None) -> None:
    key = _key(database)
    with _guard:
        if expected is None or _targets.get(key) == expected:
            _targets.pop(key, None)
            _transaction_guards.pop(key, None)
            _verified_schemas.pop(key, None)


def mark_mysql_schema_verified(database, config, signature: tuple) -> None:
    """Cache only a complete, version/checksum-verified startup identity.

    Connections never create or migrate schema. Reconfiguration/teardown
    invalidates this cache; startup still runs the full migration verifier.
    """
    key = _key(database)
    if not signature:
        raise ValueError("Schema verification requires a version/checksum signature")
    with _guard:
        if _targets.get(key) != config:
            raise RuntimeError("Schema verification belongs to another database identity")
        _verified_schemas[key] = (config, signature)


def mysql_schema_is_verified(database) -> bool:
    key = _key(database)
    with _guard:
        verified = _verified_schemas.get(key)
        return bool(verified and verified[0] == _targets.get(key))


def is_mysql(connection) -> bool:
    return getattr(connection, "dialect", None) == "mysql"


def lock_row(connection, table: str, key_column: str, key):
    """Lock a known row inside the caller's transaction, without committing it."""
    if not _IDENTIFIER.fullmatch(table) or not _IDENTIFIER.fullmatch(key_column):
        raise ValueError("Invalid database identifier")
    if not connection.in_transaction:
        raise ValueError("Row locking requires the caller's transaction")
    suffix = " FOR UPDATE" if is_mysql(connection) else ""
    return connection.execute(
        f"SELECT * FROM `{table}` WHERE `{key_column}`=?" + suffix, (key,),
    ).fetchone()


def connect_database(database, *args, **kwargs):
    config = mysql_config_for(database)
    if config is None:
        from app.storage.sqlite_timing import connect_sqlite

        return connect_sqlite(database, *args, **kwargs)
    if args or "factory" in kwargs:
        raise TypeError("MySQL connections do not accept SQLite connection factories")
    allowed = {"timeout", "check_same_thread", "cached_statements", "uri", "isolation_level"}
    if set(kwargs) - allowed:
        raise TypeError("Unsupported MySQL connection option")
    from app.storage.mysql import connect_mysql

    query = parse_qs(urlsplit(os.fspath(database)).query)
    conn = connect_mysql(config, read_only=query.get("mode") == ["ro"])
    from app.storage.mysql_schema import MySQLSchema, attach_schema_metadata

    with _guard:
        key = _key(database)
        transaction_guard = _transaction_guards.get(key)
        verified = _verified_schemas.get(key)
    try:
        attach_schema_metadata(conn, MySQLSchema(config.database), ensure_metadata=False,
                               verify_metadata=not (verified and verified[0] == config))
        if transaction_guard is not None and query.get("mode") != ["ro"]:
            conn.set_transaction_guard(transaction_guard)
        if "isolation_level" in kwargs:
            conn.isolation_level = kwargs["isolation_level"]
    except BaseException:
        conn.close()
        raise
    return conn
