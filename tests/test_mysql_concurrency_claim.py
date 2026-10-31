from __future__ import annotations

from app.core import agent_storage
from app.core.agent_storage import SqliteAgentRunStore
from app.core.background_jobs import BackgroundJobStore
from app.core.session_admission import SessionAdmissionStore


class _Cursor:
    def __init__(self, rows=(), *, rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _ClaimConnection:
    dialect = "mysql"
    row_factory = None

    def __init__(self):
        self.in_transaction = False
        self.statements = []
        self.scopes = [
            {"kind": "agent_dispatch", "scope_id": "locked"},
            {"kind": "agent_dispatch", "scope_id": "available"},
        ]
        self.job = {
            "job_id": "job-1", "kind": "agent_dispatch", "scope_id": "available",
            "idempotency_key": "run-1", "payload_json": '{"run_id":"run-1"}',
            "status": "queued", "priority": 0, "available_at": "2026-10-29T00:00:00+00:00",
            "max_attempts": 3, "attempts": 0, "deadline": None, "error_class": None,
            "lease_owner": "worker-1", "lease_expires_at": "2099-01-01T00:00:00+00:00",
            "lease_epoch": 1, "lease_recovery_count": 0, "created_at": "2026-10-29T00:00:00+00:00",
            "started_at": "2026-10-29T00:00:00+00:00", "finished_at": None,
            "updated_at": "2026-10-29T00:00:00+00:00",
        }

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if sql == "BEGIN":
            self.in_transaction = True
            return _Cursor()
        if "SELECT * FROM `session_admissions`" in sql:
            return _Cursor([{ "session_id": params[0], "run_id": None, "generation": 0 }])
        return _Cursor()

    def execute_native(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if sql.startswith("INSERT INTO session_admissions"):
            return _Cursor(rowcount=1)
        if "SELECT job_id FROM background_jobs" in sql and "deadline" in sql:
            return _Cursor()
        if "SELECT kind,scope_id,MAX(priority)" in sql:
            return _Cursor(self.scopes)
        if "FROM background_job_scopes" in sql:
            return _Cursor([] if params[1] == "locked" else [{"kind": params[0], "scope_id": params[1]}])
        if "status='running'" in sql and "lease_expires_at>%s" in sql:
            return _Cursor()
        if "FOR UPDATE SKIP LOCKED" in sql and "SELECT * FROM background_jobs" in sql:
            return _Cursor([self.job])
        if sql.startswith("UPDATE background_jobs SET status='running'"):
            self.job["status"] = "running"
            return _Cursor(rowcount=1)
        if sql.startswith("SELECT * FROM background_jobs WHERE job_id="):
            return _Cursor([self.job])
        raise AssertionError(f"Unexpected MySQL statement: {sql}")

    def commit(self):
        self.in_transaction = False

    def rollback(self):
        self.in_transaction = False


def test_mysql_claim_locks_scope_before_job_and_skips_busy_scope():
    conn = _ClaimConnection()
    store = BackgroundJobStore("unused")

    claim = store._claim_mysql(conn, "worker-1", "2099-01-01T00:00:00+00:00",
                               "2026-10-29T00:00:00+00:00", None)

    assert claim["job_id"] == "job-1"
    lock_scope = next(i for i, (sql, _) in enumerate(conn.statements)
                      if "FROM background_job_scopes" in sql)
    lock_job = next(i for i, (sql, _) in enumerate(conn.statements)
                    if "SELECT * FROM background_jobs" in sql and "FOR UPDATE SKIP LOCKED" in sql)
    update_job = next(i for i, (sql, _) in enumerate(conn.statements)
                      if sql.startswith("UPDATE background_jobs SET status='running'"))
    assert lock_scope < lock_job < update_job
    assert "SKIP LOCKED" in conn.statements[lock_scope][0]
    assert conn.statements[lock_scope][1] == ("agent_dispatch", "locked")
    assert conn.statements[lock_job][1][1] == "available"


def test_mysql_session_admission_creates_then_locks_anchor():
    conn = _ClaimConnection()
    conn.execute("BEGIN")
    SessionAdmissionStore.lock_session(conn, "session-1")

    assert len(conn.statements) == 3
    assert "ON DUPLICATE KEY UPDATE" in conn.statements[1][0]
    assert "FOR UPDATE" in conn.statements[2][0]
    assert conn.statements[1][1] == ("session-1",)


class _InvocationConnection(_ClaimConnection):
    def __init__(self):
        super().__init__()
        self.invocation = None
        self.inserts = 0

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if sql == "BEGIN":
            self.in_transaction = True
            return _Cursor()
        if "SELECT session_id,record_payload FROM agent_runs" in sql:
            return _Cursor()
        if "FROM `agent_runs`" in sql or "FROM `agent_tool_invocations`" in sql:
            table = "agent_runs" if "agent_runs" in sql else "agent_tool_invocations"
            row = None if table == "agent_runs" else self.invocation
            return _Cursor([row] if row else [])
        if sql.lstrip().startswith("INSERT INTO agent_tool_invocations"):
            self.inserts += 1
            self.invocation = {
                "invocation_id": params[0], "run_id": params[1], "tool_name": params[2],
                "input_hash": params[3], "status": "executing", "result_payload": None,
            }
            return _Cursor(rowcount=1)
        if sql.lstrip().startswith("UPDATE agent_tool_invocations"):
            self.invocation.update(status="completed", result_payload=params[0])
            return _Cursor(rowcount=1)
        if "SELECT status,result_payload FROM agent_tool_invocations" in sql:
            return _Cursor([self.invocation] if self.invocation else [])
        return _Cursor()


def test_mysql_uncertain_tool_effect_is_not_claimed_again(monkeypatch):
    conn = _InvocationConnection()
    monkeypatch.setattr(agent_storage, "connect", lambda _: conn)
    monkeypatch.setattr(agent_storage, "persist_sqlite", lambda connect, operation: operation(connect()))
    store = SqliteAgentRunStore.__new__(SqliteAgentRunStore)
    store.db_path = "unused"
    kwargs = {
        "invocation_id": "effect-1", "run_id": "run-1", "tool_name": "test.write",
        "tool_input": {"value": 1}, "claimed_at": "2026-10-29T00:00:00+00:00",
    }

    assert store.claim_tool_invocation(**kwargs) == {"status": "claimed"}
    assert store.claim_tool_invocation(**kwargs) == {"status": "uncertain"}
    assert conn.inserts == 1
    run_lock = next(i for i, (sql, _) in enumerate(conn.statements) if "FROM `agent_runs`" in sql)
    effect_lock = next(i for i, (sql, _) in enumerate(conn.statements)
                       if "FROM `agent_tool_invocations`" in sql)
    assert run_lock < effect_lock
