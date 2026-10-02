"""Deterministic project profiles backed by canonical memory project identities."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ProjectConflictError(ValueError):
    """Raised when a project rename loses its optimistic revision check."""


class ProjectService:
    """Store user-facing project names without owning project path identity."""

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection], memory_service: Any) -> None:
        self._conn_factory = conn_factory
        self._memory_service = memory_service

    def ensure_schema(self) -> None:
        conn = self._conn_factory()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS project_profiles(
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_profiles_name "
                "ON project_profiles(name, project_id)"
            )
            conn.commit()
        finally:
            conn.close()

    def register(self, path: str | Path, name: str | None = None) -> dict[str, Any]:
        """Resolve canonical project identity and create its profile only once."""
        if name is not None and (not name.strip() or len(name.strip()) > 120):
            raise ValueError("Project name must be 1..120 characters")
        project_id = self._memory_service.resolve_project(path)
        if not project_id:
            raise RuntimeError("MemoryService did not resolve a project identity")
        now = _now()
        initial_name = (name or Path(path).name or str(path)).strip()[:120] or "Project"
        conn = self._conn_factory()
        try:
            conn.execute(
                "INSERT INTO project_profiles(project_id,name,revision,created_at,updated_at) "
                "VALUES(?,?,1,?,?) ON CONFLICT(project_id) DO NOTHING",
                (project_id, initial_name, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get(project_id)

    def get(self, project_id: str) -> dict[str, Any]:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """SELECT p.project_id,p.name,p.revision,p.created_at,p.updated_at,
                          (SELECT mp.path_key FROM memory_project_paths mp
                           WHERE mp.project_id=p.project_id AND mp.active=1
                           ORDER BY mp.path_key LIMIT 1) AS workspace_path
                   FROM project_profiles p WHERE p.project_id=?""",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown project: {project_id}")
            result = self._record(row)
            result["session_count"] = self._session_count(conn, project_id, result["workspace_path"])
            return result
        finally:
            conn.close()

    def list(
        self, limit: int = 50, offset: int = 0, q: str | None = None,
    ) -> dict[str, Any]:
        page_size = max(1, min(int(limit), 500))
        start = max(0, int(offset))
        query = (q or "").strip()
        conn = self._conn_factory()
        try:
            where = "WHERE p.name LIKE ? ESCAPE '\\' OR p.project_id LIKE ? ESCAPE '\\'" if query else ""
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params: list[Any] = ([f"%{escaped}%", f"%{escaped}%"] if query else [])
            params.extend((page_size + 1, start))
            rows = conn.execute(
                f"""SELECT p.project_id,p.name,p.revision,p.created_at,p.updated_at,
                           (SELECT mp.path_key FROM memory_project_paths mp
                            WHERE mp.project_id=p.project_id AND mp.active=1
                            ORDER BY mp.path_key LIMIT 1) AS workspace_path
                    FROM project_profiles p {where}
                    ORDER BY p.name COLLATE NOCASE,p.project_id LIMIT ? OFFSET ?""",
                params,
            ).fetchall()
            has_more = len(rows) > page_size
            records = []
            for row in rows[:page_size]:
                record = self._record(row)
                record["session_count"] = self._session_count(
                    conn, record["project_id"], record["workspace_path"]
                )
                records.append(record)
            return {"projects": records, "next_offset": start + page_size if has_more else None}
        finally:
            conn.close()

    def rename(self, project_id: str, name: str, expected_revision: int) -> dict[str, Any]:
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("Project name must be 1..120 characters")
        conn = self._conn_factory()
        try:
            now = _now()
            cursor = conn.execute(
                "UPDATE project_profiles SET name=?,revision=revision+1,updated_at=? "
                "WHERE project_id=? AND revision=?",
                (normalized, now, project_id, expected_revision),
            )
            if cursor.rowcount == 0:
                exists = conn.execute(
                    "SELECT 1 FROM project_profiles WHERE project_id=?", (project_id,)
                ).fetchone()
                conn.rollback()
                if exists is None:
                    raise KeyError(f"Unknown project: {project_id}")
                raise ProjectConflictError(f"Project revision conflict: {project_id}")
            conn.commit()
        finally:
            conn.close()
        return self.get(project_id)

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "project_id": row["project_id"],
            "name": row["name"],
            "revision": row["revision"],
            "workspace_path": row["workspace_path"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _session_count(conn: sqlite3.Connection, project_id: str, workspace_path: str | None) -> int:
        try:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM agent_sessions s
                   WHERE s.status!='deleted' AND (
                     json_extract(s.metadata, '$.project_id')=? OR
                     (json_extract(s.metadata, '$.project_id') IS NULL AND
                      json_extract(s.metadata, '$.workspace.backend_path') IN (
                        SELECT path_key FROM memory_project_paths WHERE project_id=? AND active=1
                      ))
                   )""",
                (project_id, project_id),
            ).fetchone()
            return int(row["n"])
        except sqlite3.OperationalError as exc:
            # The minimal domain may be used with a database before session schema exists.
            if "no such table: agent_sessions" in str(exc):
                return 0
            raise
