"""Deterministic lifecycle of management sessions; no model or worker execution."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4


class ManagementConflictError(ValueError):
    """The requested transition conflicts with persisted management state."""


_UNSET = object()
_TERMINAL_RUNS = ("completed", "failed", "cancelled", "timed_out")


class ManagementService:
    def __init__(self, conn_factory, session_service, *, goal_service=None):
        self._conn = conn_factory
        self.session_service = session_service
        self.goal_service = goal_service

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS manager_controllers (
                    session_id TEXT PRIMARY KEY REFERENCES agent_sessions(session_id),
                    status TEXT NOT NULL DEFAULT 'paused' CHECK(status IN ('active','paused')),
                    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                    llm_client_name TEXT, llm_model TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manager_defaults (
                    slot INTEGER PRIMARY KEY CHECK(slot = 1),
                    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id)
                );
                CREATE TABLE IF NOT EXISTS manager_goals (
                    goal_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES manager_controllers(session_id),
                    status TEXT NOT NULL CHECK(status IN
                        ('active','waiting','paused','completed','cancelled')),
                    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                    objective TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS manager_goals_session_status
                    ON manager_goals(session_id,status);
            """)

    @staticmethod
    def _title(title):
        if title is None:
            return "管理会话"
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 40:
            raise ValueError("title must contain 1 to 40 non-whitespace characters")
        return title.strip()

    @staticmethod
    def _model_value(value):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Model settings must be non-empty strings or null")
        return value.strip() if value is not None else None

    def _create(self, conn, *, title, make_default, llm_client_name, llm_model):
        session_id = "session_" + uuid4().hex
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO agent_sessions(session_id,title,status,metadata,kind,created_at,updated_at) "
            "VALUES(?,?,'active','{}','manager',?,?)", (session_id, title, now, now),
        )
        conn.execute(
            "INSERT INTO manager_controllers(session_id,llm_client_name,llm_model,created_at,updated_at) "
            "VALUES(?,?,?,?,?)", (session_id, llm_client_name, llm_model, now, now),
        )
        if make_default:
            conn.execute("INSERT INTO manager_defaults(slot,session_id) VALUES(1,?) "
                         "ON CONFLICT(slot) DO UPDATE SET session_id=excluded.session_id", (session_id,))
        return session_id

    def create(self, title=None, make_default=False, llm_client_name=None, llm_model=None):
        title = self._title(title)
        client = self._model_value(llm_client_name)
        model = self._model_value(llm_model)
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            session_id = self._create(conn, title=title, make_default=make_default,
                                      llm_client_name=client, llm_model=model)
        return self.get(session_id)

    def get_default(self):
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT session_id FROM manager_defaults WHERE slot=1").fetchone()
            if row:
                session_id = row["session_id"]
                session = conn.execute("SELECT kind,status FROM agent_sessions WHERE session_id=?",
                                       (session_id,)).fetchone()
                if not session or session["kind"] != "manager" or session["status"] == "deleted":
                    raise ManagementConflictError("Default management session is unavailable; restore or explicitly replace it")
            else:
                session_id = self._create(conn, title=self._title(None), make_default=True,
                                          llm_client_name=None, llm_model=None)
        return self.get(session_id)

    def get(self, session_id):
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT c.* FROM manager_controllers c JOIN agent_sessions s "
                               "ON s.session_id=c.session_id WHERE c.session_id=? "
                               "AND s.kind='manager' AND s.status!='deleted'", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        session = self.session_service.get_session_or_none(session_id=session_id)
        if session is None:
            raise KeyError(session_id)
        return {"session": session.model_dump(mode="json"), "controller": dict(row)}

    def list(self, limit=50, offset=0):
        if not isinstance(limit, int) or not 1 <= limit <= 200 or not isinstance(offset, int) or offset < 0:
            raise ValueError("limit must be 1..200 and offset non-negative")
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT c.session_id FROM manager_controllers c JOIN agent_sessions s "
                                "ON s.session_id=c.session_id WHERE s.kind='manager' AND s.status!='deleted' "
                                "ORDER BY s.updated_at DESC,s.session_id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [self.get(row["session_id"]) for row in rows]

    def update(self, session_id, expected_revision, status=None, llm_client_name=_UNSET, llm_model=_UNSET):
        if status is not None and status not in {"active", "paused"}:
            raise ValueError("status must be active or paused")
        values = {}
        if status is not None:
            values["status"] = status
        for field, value in (("llm_client_name", llm_client_name), ("llm_model", llm_model)):
            if value is not _UNSET:
                values[field] = self._model_value(value)
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT c.* FROM manager_controllers c JOIN agent_sessions s "
                               "ON s.session_id=c.session_id WHERE c.session_id=? "
                               "AND s.kind='manager' AND s.status!='deleted'", (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            if row["revision"] != expected_revision:
                raise ManagementConflictError("Management revision changed")
            assignments = [f"{name}=?" for name in values]
            assignments.extend(["revision=revision+1", "updated_at=?"])
            conn.execute("UPDATE manager_controllers SET " + ",".join(assignments) + " WHERE session_id=?",
                         [*values.values(), datetime.now(UTC).isoformat(), session_id])
        return self.get(session_id)

    @staticmethod
    def _is_manager(conn, session_id):
        row = conn.execute("SELECT kind FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
        return row is not None and row["kind"] == "manager"

    def guard_delete(self, conn, session_id, only_if_empty=False):
        if not self._is_manager(conn, session_id):
            return
        if only_if_empty:
            raise ManagementConflictError("Management sessions cannot be automatically cleaned up")
        delegates = getattr(self, "delegates", None)
        if delegates is not None and conn.execute(
            "SELECT 1 FROM manager_delegates WHERE parent_session_id=? AND state='active' LIMIT 1", (session_id,),
        ).fetchone():
            raise ManagementConflictError("Resolve active delegates before deleting their parent")
        controller = conn.execute("SELECT status FROM manager_controllers WHERE session_id=?", (session_id,)).fetchone()
        if controller is None or controller["status"] != "paused":
            raise ManagementConflictError("Pause management before deleting its session")
        marks = ",".join("?" for _ in _TERMINAL_RUNS)
        if conn.execute(f"SELECT 1 FROM agent_runs WHERE session_id=? AND status NOT IN ({marks}) LIMIT 1",
                        (session_id, *_TERMINAL_RUNS)).fetchone():
            raise ManagementConflictError("Management session has unfinished runs")
        goals = conn.execute("SELECT goal_id FROM manager_goals WHERE session_id=? AND status IN ('active','waiting')",
                             (session_id,))
        if any(self.goal_service is None or self.goal_service.enabled_in_transaction(conn, goal["goal_id"])
               for goal in goals):
            raise ManagementConflictError("Pause or transfer active goals before deletion")

    def _pause_lifecycle(self, conn, session_id):
        if self._is_manager(conn, session_id):
            conn.execute("UPDATE manager_controllers SET status='paused',revision=revision+1,updated_at=? "
                         "WHERE session_id=?", (datetime.now(UTC).isoformat(), session_id))

    def after_delete(self, conn, session_id):
        self._pause_lifecycle(conn, session_id)

    def after_restore(self, conn, session_id):
        self._pause_lifecycle(conn, session_id)
