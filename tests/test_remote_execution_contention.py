"""Idle remote polling must not compete for locks or replay an admitted task."""
import sqlite3
from contextlib import closing

import pytest
from fastapi import HTTPException

from app.api.routes.remote_execution import _call
from tests.test_remote_execution import hub as hub_fixture
from tests.test_remote_execution import setup_remote as remote_fixture


@pytest.fixture
def hub(tmp_path, monkeypatch):
    yield from hub_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def remote(hub, tmp_path, monkeypatch):
    yield from remote_fixture.__wrapped__(hub, tmp_path, monkeypatch)


def test_idle_reconciliation_reads_while_another_writer_is_active(remote):
    service = remote["service"]
    with closing(sqlite3.connect(remote["hub"].agent_run_store.db_path)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        assert service.reconcile() == 0
        blocker.rollback()


def test_claim_retries_only_lock_admission_without_repeating_lease(remote, monkeypatch):
    service = remote["service"]
    path = remote["hub"].agent_run_store.db_path
    with closing(sqlite3.connect(path)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        attempts = []

        class Connection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == "BEGIN IMMEDIATE":
                    attempts.append(sql)
                try:
                    return super().execute(sql, parameters)
                except sqlite3.OperationalError:
                    blocker.rollback()
                    raise

        monkeypatch.setattr(service, "_conn", lambda: sqlite3.connect(path, timeout=0.01, factory=Connection))
        assert service.claim(remote["principal"], environment_id="linux") == {"task": None}
        assert len(attempts) == 2
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT count(*) FROM remote_execution_tasks").fetchone()[0] == 0


def test_busy_response_is_temporary_without_implicit_operation_retry(tmp_path):
    path = tmp_path / "busy.sqlite3"
    calls = []
    with closing(sqlite3.connect(path)) as blocker, closing(sqlite3.connect(path, timeout=0.01)) as other:
        blocker.execute("CREATE TABLE entries(value)")
        blocker.execute("BEGIN IMMEDIATE")

        def operation():
            calls.append(True)
            other.execute("BEGIN IMMEDIATE")

        with pytest.raises(HTTPException) as error:
            _call(operation)
        assert error.value.status_code == 503
        assert error.value.detail == "execution_storage_busy"
        assert error.value.headers == {"Retry-After": "1"}
        assert calls == [True]
        blocker.rollback()
    with pytest.raises(sqlite3.OperationalError, match="disk failure"):
        _call(lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk failure")))
