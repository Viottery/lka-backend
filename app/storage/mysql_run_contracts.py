"""One-time physical contracts for the two native run-store upserts."""
from __future__ import annotations

from app.storage.mysql_schema import MySQLSchemaError


def verify_native_run_upserts(conn):
    """Validate at store construction; migrations require the coordinator fence.

    Native MySQL duplicate-key handling is safe here only when every possible
    business conflict is the specified primary key and no trigger changes the
    insert/update distinction. The generated rowid is never supplied by us.
    """
    expected = {"agent_runs": ("run_id",), "agent_run_events": ("run_id", "sequence")}
    tables = "('agent_runs','agent_run_events')"
    engines = {row[0]: row[1] for row in conn.execute_native(
        "SELECT TABLE_NAME,ENGINE FROM INFORMATION_SCHEMA.TABLES "
        f"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN {tables}",
    )}
    if set(engines) != set(expected) or any(str(value).lower() != "innodb" for value in engines.values()):
        raise MySQLSchemaError("Native run storage requires both verified InnoDB tables")
    indexes = {}
    for table, name, _seq, column, prefix in conn.execute_native(
        "SELECT TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART FROM INFORMATION_SCHEMA.STATISTICS "
        f"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN {tables} AND NON_UNIQUE=0 "
        "ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX",
    ):
        if prefix is not None:
            raise MySQLSchemaError("Prefix unique keys prevent native run upsert")
        indexes.setdefault((table, name), []).append(column)
    auto = {row[0] for row in conn.execute_native(
        "SELECT TABLE_NAME,EXTRA FROM INFORMATION_SCHEMA.COLUMNS "
        f"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN {tables} AND COLUMN_NAME='__lka_rowid'",
    ) if "auto_increment" in str(row[1]).lower()}
    for table, primary in expected.items():
        if tuple(indexes.get((table, "PRIMARY"), ())) != primary:
            raise MySQLSchemaError("Native run storage primary key differs from its write contract")
    for (table, name), columns in indexes.items():
        if name != "PRIMARY" and not (columns == ["__lka_rowid"] and table in auto):
            raise MySQLSchemaError("Unexpected unique key prevents native run upsert")
    if conn.execute_native(
        "SELECT TRIGGER_NAME FROM INFORMATION_SCHEMA.TRIGGERS "
        f"WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE IN {tables}",
    ).fetchone() is not None:
        raise MySQLSchemaError("Unexpected trigger prevents native run upsert")
