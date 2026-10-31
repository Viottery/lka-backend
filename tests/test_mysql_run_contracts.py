from __future__ import annotations

import pytest

from app.storage.mysql_run_contracts import verify_native_run_upserts
from app.storage.mysql_schema import MySQLSchemaError


class Rows(list):
    def fetchone(self):
        return self[0] if self else None


class Schema:
    def __init__(self):
        self.engines = [("agent_runs", "InnoDB"), ("agent_run_events", "InnoDB")]
        self.indexes = [("agent_runs", "PRIMARY", 1, "run_id", None),
                        ("agent_run_events", "PRIMARY", 1, "run_id", None),
                        ("agent_run_events", "PRIMARY", 2, "sequence", None),
                        ("agent_runs", "rowid", 1, "__lka_rowid", None)]
        self.auto = [("agent_runs", "auto_increment")]
        self.triggers = []

    def execute_native(self, sql):
        for table, rows in [("TABLES", self.engines), ("STATISTICS", self.indexes),
                            ("COLUMNS", self.auto), ("TRIGGERS", self.triggers)]:
            if f"INFORMATION_SCHEMA.{table} " in sql:
                return Rows(rows)
        raise AssertionError("unexpected contract query")


def test_known_primary_keys_and_generated_rowid_are_accepted():
    verify_native_run_upserts(Schema())


@pytest.mark.parametrize("drift", ["unique", "primary", "prefix", "trigger", "rowid", "engine", "missing"])
def test_physical_schema_drift_is_rejected_before_native_writes(drift):
    schema = Schema()
    if drift == "unique":
        schema.indexes.append(("agent_runs", "unexpected", 1, "session_id", None))
    elif drift == "primary":
        schema.indexes[0] = ("agent_runs", "PRIMARY", 1, "session_id", None)
    elif drift == "prefix":
        schema.indexes[0] = ("agent_runs", "PRIMARY", 1, "run_id", 8)
    elif drift == "trigger":
        schema.triggers = [("unexpected_insert_trigger",)]
    elif drift == "rowid":
        schema.auto = []
    elif drift == "engine":
        schema.engines[0] = ("agent_runs", "MyISAM")
    elif drift == "missing":
        schema.engines.pop()
    with pytest.raises(MySQLSchemaError):
        verify_native_run_upserts(schema)
