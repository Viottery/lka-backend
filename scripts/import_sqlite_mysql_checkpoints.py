"""Import a consistent SQLite LangGraph checkpoint snapshot into MySQL.

The source must be an offline SQLite snapshot. The target must already have the
checkpoint schema installed and be empty. This command never calls models or
tools and deliberately leaves partial target rows in place after a failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4


class CheckpointImportError(RuntimeError):
    """Safe migration refusal without source row data."""


_SOURCE_COLUMNS = {
    "checkpoints": ("thread_id", "checkpoint_ns", "checkpoint_id", "parent_checkpoint_id", "type", "checkpoint", "metadata"),
    "writes": ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx", "channel", "type", "value"),
}
_TARGET_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


class _MultisetDigest:
    """Order-independent SHA-256 digest for streamed rows under DB collations."""

    def __init__(self) -> None:
        self.count = 0
        self._xor = 0
        self._sum = 0

    def add(self, row: list[Any]) -> None:
        packed = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        value = int.from_bytes(hashlib.sha256(packed).digest(), "big")
        self.count += 1
        self._xor ^= value
        self._sum = (self._sum + value) % (1 << 256)

    def hexdigest(self) -> str:
        packed = self.count.to_bytes(8, "big") + self._xor.to_bytes(32, "big") + self._sum.to_bytes(32, "big")
        return hashlib.sha256(packed).hexdigest()


def _quote_ident(name: str) -> str:
    allowed = set(_SOURCE_COLUMNS) | {column for columns in _SOURCE_COLUMNS.values() for column in columns}
    if name not in allowed:
        raise CheckpointImportError("unsupported SQLite checkpoint table")
    return f'"{name}"'


def validate_source_schema(conn: sqlite3.Connection) -> None:
    objects = conn.execute(
        "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    tables = {str(name) for kind, name in objects if kind == "table"}
    views = {str(name) for kind, name in objects if kind == "view"}
    if views or tables != set(_SOURCE_COLUMNS) or any(kind not in {"table"} for kind, _name in objects):
        raise CheckpointImportError("SQLite checkpoint schema must contain only checkpoints and writes")
    for table, expected in _SOURCE_COLUMNS.items():
        info = [row for row in conn.execute(f"PRAGMA table_xinfo({_quote_ident(table)})") if int(row[6]) == 0]
        columns = tuple(str(row[1]) for row in info)
        if columns != expected:
            raise CheckpointImportError(f"SQLite {table} columns do not match the supported saver schema")
        expected_types = {
            "thread_id": "TEXT", "checkpoint_ns": "TEXT", "checkpoint_id": "TEXT",
            "parent_checkpoint_id": "TEXT", "type": "TEXT", "checkpoint": "BLOB",
            "metadata": "BLOB", "task_id": "TEXT", "idx": "INTEGER", "channel": "TEXT", "value": "BLOB",
        }
        required = {"thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx", "channel"}
        for row in info:
            name = str(row[1])
            if str(row[2]).upper() != expected_types[name] or bool(row[3]) != (name in required):
                raise CheckpointImportError(f"SQLite {table} column definitions are unsupported")
    pk_checkpoints = tuple(str(row[1]) for row in sorted(
        (row for row in conn.execute('PRAGMA table_info("checkpoints")') if int(row[5]) > 0),
        key=lambda r: int(r[5]),
    ))
    pk_writes = tuple(str(row[1]) for row in sorted(
        (row for row in conn.execute('PRAGMA table_info("writes")') if int(row[5]) > 0),
        key=lambda r: int(r[5]),
    ))
    if pk_checkpoints != ("thread_id", "checkpoint_ns", "checkpoint_id"):
        raise CheckpointImportError("SQLite checkpoints primary key is unsupported")
    if pk_writes != ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"):
        raise CheckpointImportError("SQLite writes primary key is unsupported")


def _source_write_digest(conn: sqlite3.Connection, limits: dict[str, int] | None = None) -> tuple[int, str]:
    digest = _MultisetDigest()
    cursor = conn.execute(
        "SELECT thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value "
        "FROM writes ORDER BY thread_id,checkpoint_ns,checkpoint_id,task_id,idx"
    )
    for row in cursor:
        if limits is not None:
            for column, value in (("thread_id", row[0]), ("checkpoint_ns", row[1]),
                                  ("checkpoint_id", row[2]), ("task_id", row[3]),
                                  ("channel", row[5])):
                if len(str(value)) > limits[column]:
                    raise CheckpointImportError(f"source {column} exceeds target column length")
        digest.add([
            row[0], row[1], row[2], row[3], int(row[4]), row[5], row[6],
            None if row[7] is None else bytes(row[7]).hex(),
        ])
    return digest.count, digest.hexdigest()


def _target_write_digest(connection) -> tuple[int, str]:
    digest = _MultisetDigest()
    import pymysql

    try:
        cursor_context = connection.cursor(pymysql.cursors.SSCursor)
    except TypeError:  # Small fake DB adapters used by offline unit tests.
        cursor_context = connection.cursor()
    with cursor_context as cursor:
        cursor.execute(
            "SELECT thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,`blob` "
            "FROM checkpoint_writes"
        )
        for row in cursor:
            digest.add([
                row[0], row[1], row[2], row[3], int(row[4]), row[5], row[6],
                None if row[7] is None else bytes(row[7]).hex(),
            ])
    return digest.count, digest.hexdigest()


def _target_empty(connection) -> None:
    with connection.cursor() as cursor:
        for table in _TARGET_TABLES:
            cursor.execute(f"SELECT 1 FROM `{table}` LIMIT 1")
            if cursor.fetchone() is not None:
                raise CheckpointImportError("target checkpoint tables are not empty")


def _target_limits(connection) -> dict[str, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name,column_name,character_maximum_length "
            "FROM information_schema.columns WHERE table_schema=DATABASE() "
            "AND table_name IN ('checkpoints','checkpoint_blobs','checkpoint_writes')"
        )
        rows = cursor.fetchall()
    metadata = {(str(r[0]), str(r[1])): r[2] for r in rows}
    required_columns = {
        "checkpoints": {"thread_id", "checkpoint_ns", "checkpoint_id", "parent_checkpoint_id", "type", "checkpoint", "metadata", "checkpoint_ns_hash"},
        "checkpoint_blobs": {"thread_id", "checkpoint_ns", "channel", "version", "type", "blob", "checkpoint_ns_hash"},
        "checkpoint_writes": {"thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "task_path", "idx", "channel", "type", "blob", "checkpoint_ns_hash"},
    }
    actual_columns = {
        table: {column for table_name, column in metadata if table_name == table}
        for table in required_columns
    }
    if actual_columns != required_columns:
        raise CheckpointImportError("target checkpoint columns do not match the supported saver schema")
    shared_lengths = (
        ("thread_id", ("checkpoints", "thread_id"), ("checkpoint_blobs", "thread_id"), ("checkpoint_writes", "thread_id")),
        ("checkpoint_ns", ("checkpoints", "checkpoint_ns"), ("checkpoint_blobs", "checkpoint_ns"), ("checkpoint_writes", "checkpoint_ns")),
        ("checkpoint_id", ("checkpoints", "checkpoint_id"), ("checkpoint_writes", "checkpoint_id")),
        ("channel", ("checkpoint_blobs", "channel"), ("checkpoint_writes", "channel")),
    )
    for _name, *keys in shared_lengths:
        values = [metadata.get(key) for key in keys]
        if any(value is None for value in values) or len({int(value) for value in values}) != 1:
            raise CheckpointImportError("target checkpoint column lengths are inconsistent")
    expected = {
        "thread_id": ("checkpoints", "thread_id"),
        "checkpoint_ns": ("checkpoints", "checkpoint_ns"),
        "checkpoint_id": ("checkpoints", "checkpoint_id"),
        "parent_checkpoint_id": ("checkpoints", "parent_checkpoint_id"),
        "task_id": ("checkpoint_writes", "task_id"),
        "channel": ("checkpoint_writes", "channel"),
    }
    limits: dict[str, int] = {}
    for name, key in expected.items():
        value = metadata.get(key)
        if value is None:
            raise CheckpointImportError("target checkpoint schema is incomplete")
        limits[name] = int(value)
    # Versions are stored as strings in checkpoint_blobs.
    version_len = metadata.get(("checkpoint_blobs", "version"))
    if version_len is None:
        raise CheckpointImportError("target checkpoint blob schema is incomplete")
    limits["version"] = int(version_len)
    return limits


def _validate_checkpoint_lengths(source: sqlite3.Connection, limits: dict[str, int], saver) -> int:
    if any(value < 1 for value in limits.values()):
        raise CheckpointImportError("target checkpoint schema has unsupported length metadata")
    count = 0
    cursor = source.execute(
        "SELECT thread_id,checkpoint_ns,checkpoint_id,type,checkpoint,metadata FROM checkpoints "
        "ORDER BY thread_id,checkpoint_ns,checkpoint_id"
    )
    try:
        for thread_id, ns, checkpoint_id, type_name, raw, metadata in cursor:
            try:
                checkpoint = saver.serde.loads_typed((type_name, bytes(raw)))
                json.loads(metadata) if metadata is not None else {}
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                raise CheckpointImportError("source checkpoint is not valid typed JSON") from exc
            versions = checkpoint.get("channel_versions", {}) if isinstance(checkpoint, dict) else {}
            if not isinstance(versions, dict):
                raise CheckpointImportError("source checkpoint channel versions are invalid")
            for channel, version in versions.items():
                if len(str(channel)) > limits["channel"] or len(str(version)) > limits["version"]:
                    raise CheckpointImportError("source checkpoint channel/version exceeds target length")
            for column, value in (("thread_id", thread_id), ("checkpoint_ns", ns),
                                  ("checkpoint_id", checkpoint_id)):
                if len(str(value)) > limits[column]:
                    raise CheckpointImportError(f"source {column} exceeds target column length")
            count += 1
    finally:
        cursor.close()
    return count


def _iter_checkpoints_by_source_key(source: sqlite3.Connection, saver):
    """Decode checkpoint history in SQLite primary-key order without reading writes."""
    from langgraph.checkpoint.base import CheckpointTuple

    cursor = source.execute(
        "SELECT thread_id,checkpoint_ns,checkpoint_id,parent_checkpoint_id,type,checkpoint,metadata "
        "FROM checkpoints ORDER BY thread_id,checkpoint_ns,checkpoint_id"
    )
    try:
        for thread_id, namespace, checkpoint_id, parent_id, type_name, raw, metadata_raw in cursor:
            config = {"configurable": {
                "thread_id": thread_id, "checkpoint_ns": namespace, "checkpoint_id": checkpoint_id,
            }}
            try:
                checkpoint = saver.serde.loads_typed((type_name, raw))
                metadata = json.loads(metadata_raw) if metadata_raw is not None else {}
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                raise CheckpointImportError("source checkpoint is not valid typed JSON") from exc
            parent_config = (
                {"configurable": {
                    "thread_id": thread_id, "checkpoint_ns": namespace,
                    "checkpoint_id": parent_id,
                }}
                if parent_id else None
            )
            # Pending writes are copied and verified by the raw-row path below;
            # this pass must not deserialize and discard them per checkpoint.
            yield CheckpointTuple(config, checkpoint, metadata, parent_config, None)
    finally:
        cursor.close()


def _marker(connection, run_id: str, status: str, checkpoint_count: int = 0, write_count: int = 0) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO lka_checkpoint_imports(import_id,status,checkpoint_count,write_count,updated_at) "
            "VALUES(%s,%s,%s,%s,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE "
            "status=VALUES(status),checkpoint_count=VALUES(checkpoint_count),write_count=VALUES(write_count),updated_at=VALUES(updated_at)",
            (run_id, status, checkpoint_count, write_count),
        )
    connection.commit()


def _pending_write_signature(saver, pending_writes) -> list[tuple[str, str, str, bytes]]:
    signature = []
    for task_id, channel, value in pending_writes:
        type_name, raw = saver.serde.dumps_typed(value)
        signature.append((str(task_id), str(channel), str(type_name), bytes(raw)))
    return sorted(signature)


def _assert_tuple_equivalent(source_saver, target_saver, old, new) -> None:
    if old is None or new is None:
        raise CheckpointImportError("checkpoint history verification failed")
    if (old.config != new.config or old.checkpoint != new.checkpoint
            or old.metadata != new.metadata or old.parent_config != new.parent_config
            or _pending_write_signature(source_saver, old.pending_writes)
            != _pending_write_signature(target_saver, new.pending_writes)):
        raise CheckpointImportError("checkpoint history verification failed")


def migrate(source: sqlite3.Connection, connection, *, run_id: str | None = None,
            pre_verify=None) -> dict[str, Any]:
    """Perform logical checkpoint import under an already-held coordinator lock."""
    from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
    from langgraph.checkpoint.sqlite import SqliteSaver

    run_id = run_id or uuid4().hex
    if len(run_id) != 32 or any(char not in "0123456789abcdef" for char in run_id):
        raise CheckpointImportError("checkpoint import ID must be 32 lowercase hexadecimal characters")
    validate_source_schema(source)
    _target_empty(connection)
    limits = _target_limits(connection)
    source_saver = SqliteSaver(source)
    source_saver.is_setup = True  # Never permit source DDL.
    source_count = _validate_checkpoint_lengths(source, limits, source_saver)
    row = source.execute("SELECT 1 FROM writes WHERE idx < -2147483648 OR idx > 2147483647 LIMIT 1").fetchone()
    if row:
        raise CheckpointImportError("source write index exceeds the target integer range")
    source_writes, expected_digest = _source_write_digest(source, limits)
    _marker(connection, run_id, "in_progress")
    imported = 0
    copied_writes = 0
    try:
        target_saver = PyMySQLSaver(connection)
        for item in _iter_checkpoints_by_source_key(source, source_saver):
            cfg = item.parent_config or {
                "configurable": {
                    "thread_id": item.config["configurable"]["thread_id"],
                    "checkpoint_ns": item.config["configurable"].get("checkpoint_ns", ""),
                }
            }
            checkpoint = item.checkpoint
            result = target_saver.put(cfg, checkpoint, item.metadata, checkpoint.get("channel_versions", {}))
            if result["configurable"].get("checkpoint_id") != checkpoint.get("id"):
                raise CheckpointImportError("target saver returned an unexpected checkpoint identity")
            imported += 1
        # Copy raw SQLite write records so idx (including negative/sparse values)
        # and typed serializer bytes remain unchanged.
        cursor = source.execute(
            "SELECT thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value FROM writes "
            "ORDER BY thread_id,checkpoint_ns,checkpoint_id,task_id,idx"
        )
        with connection.cursor() as target_cursor:
            for row in cursor:
                target_cursor.execute(
                    "INSERT INTO checkpoint_writes "
                    "(thread_id,checkpoint_ns,checkpoint_ns_hash,checkpoint_id,task_id,task_path,idx,channel,type,`blob`) "
                    "VALUES(%s,%s,UNHEX(MD5(%s)),%s,%s,'',%s,%s,%s,%s)",
                    (row[0], row[1], row[1], row[2], row[3], int(row[4]), row[5], row[6], row[7]),
                )
                copied_writes += 1
        connection.commit()
        if imported != source_count:
            raise CheckpointImportError("checkpoint history count differs after import")
        for thread_id, namespace, checkpoint_id in source.execute(
            "SELECT thread_id,checkpoint_ns,checkpoint_id FROM checkpoints ORDER BY thread_id,checkpoint_ns,checkpoint_id"
        ):
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": namespace, "checkpoint_id": checkpoint_id}}
            old = source_saver.get_tuple(config)
            new = target_saver.get_tuple(config)
            _assert_tuple_equivalent(source_saver, target_saver, old, new)
        actual_writes, actual_digest = _target_write_digest(connection)
        if (actual_writes, actual_digest) != (source_writes, expected_digest):
            raise CheckpointImportError("raw pending writes verification failed")
        # Ensure latest tuple retrieval resolves the same history endpoint per thread/namespace.
        for thread_id, namespace in source.execute(
            "SELECT DISTINCT thread_id,checkpoint_ns FROM checkpoints"
        ):
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": namespace}}
            old = source_saver.get_tuple(config)
            new = target_saver.get_tuple(config)
            _assert_tuple_equivalent(source_saver, target_saver, old, new)
        if pre_verify is not None:
            pre_verify()
        _marker(connection, run_id, "verified", imported, actual_writes)
        return {"import_id": run_id, "status": "verified", "checkpoints": imported, "writes": actual_writes}
    except BaseException:
        try:
            _marker(connection, run_id, "failed", imported, copied_writes)
        except Exception:  # noqa: BLE001, S110 - retain original error if marker update also fails.
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="offline SQLite checkpoint snapshot")
    parser.add_argument("--snapshot-confirmed", action="store_true", required=True,
                        help="confirm --source is a consistent offline SQLite backup")
    parser.add_argument("--connection-file", type=Path, required=True,
                        help="runtime MySQL DML connection JSON")
    parser.add_argument("--migration-connection-file", type=Path, required=True,
                        help="separate MySQL coordinator/schema connection JSON")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.storage.mysql import MySQLConfig
    from app.storage.mysql_owner import MySQLCoordinatorOwner
    try:
        config = MySQLConfig(**json.loads(args.connection_file.expanduser().resolve(strict=True).read_text()))
        migration_config = MySQLConfig(**json.loads(
            args.migration_connection_file.expanduser().resolve(strict=True).read_text()
        ))
        if (config.host, config.port, config.database) != (
            migration_config.host, migration_config.port, migration_config.database,
        ):
            raise CheckpointImportError("runtime and migration configurations must target the same MySQL database")
        source_path = args.source.expanduser().resolve(strict=True)
        if source_path.stat().st_size == 0:
            raise CheckpointImportError("SQLite checkpoint source is empty")
        wal_path = source_path.with_name(source_path.name + "-wal")
        if wal_path.exists() and wal_path.stat().st_size:
            raise CheckpointImportError("SQLite snapshot has a non-empty WAL sidecar")
        owner = MySQLCoordinatorOwner(migration_config)
        if not owner.acquire():
            raise CheckpointImportError("a MySQL coordinator is active; stop it before checkpoint import")
        try:
            import pymysql

            from app.storage.mysql import connect_mysql
            from app.storage.mysql_bootstrap import bootstrap_mysql

            # The ordinary startup path must approve the complete current schema
            # and reject prior failed/incomplete import attempts before proceeding.
            with connect_mysql(config) as runtime_connection:
                bootstrap_mysql(runtime_connection, install=False)

            # Only the separate migration identity creates the operation marker.
            ddl_connection = pymysql.connect(
                host=migration_config.host, port=migration_config.port, user=migration_config.user,
                password=migration_config.password, database=migration_config.database,
                charset="utf8mb4", autocommit=True,
                connect_timeout=int(migration_config.connect_timeout),
                read_timeout=int(migration_config.read_timeout), cursorclass=pymysql.cursors.Cursor,
            )
            try:
                with ddl_connection.cursor() as cursor:
                    cursor.execute("CREATE TABLE IF NOT EXISTS lka_checkpoint_imports ("
                                   "import_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,"
                                   "status ENUM('in_progress','failed','verified') NOT NULL,"
                                   "checkpoint_count BIGINT UNSIGNED NOT NULL DEFAULT 0,"
                                   "write_count BIGINT UNSIGNED NOT NULL DEFAULT 0,"
                                   "updated_at DATETIME(6) NOT NULL) ENGINE=InnoDB")
            finally:
                ddl_connection.close()

            raw = pymysql.connect(host=config.host, port=config.port, user=config.user, password=config.password,
                                  database=config.database, charset="utf8mb4", autocommit=True,
                                  connect_timeout=int(config.connect_timeout), read_timeout=int(config.read_timeout),
                                  cursorclass=pymysql.cursors.Cursor)
            try:
                with raw.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name IN (%s,%s,%s)", _TARGET_TABLES)
                    if int(cursor.fetchone()[0]) != 3:
                        raise CheckpointImportError("target checkpoint schema is not installed")
                    cursor.execute("SELECT 1 FROM lka_checkpoint_imports LIMIT 1")
                    if cursor.fetchone():
                        raise CheckpointImportError("a prior checkpoint import attempt already exists")
                with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
                    source.row_factory = None
                    source.execute("PRAGMA query_only=ON")
                    def verify_incomplete_schema() -> None:
                        with connect_mysql(config) as validation_connection:
                            bootstrap_mysql(validation_connection, install=False, allow_incomplete_import=True)

                    result = migrate(source, raw, pre_verify=verify_incomplete_schema)
                try:
                    with connect_mysql(config) as validation_connection:
                        bootstrap_mysql(validation_connection, install=False)
                except Exception:
                    _marker(raw, result["import_id"], "failed", result["checkpoints"], result["writes"])
                    raise
            finally:
                raw.close()
        finally:
            owner.release()
    except CheckpointImportError as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - suppress driver messages that may disclose connection metadata.
        # Do not echo DB driver messages which can contain connection metadata.
        raise SystemExit(f"checkpoint import failed ({type(exc).__name__})") from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
