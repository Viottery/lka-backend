"""One durable periodic pulse for all of a manager's unfinished attention goals."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import timedelta

from app.domains.management_goals import _instant


class ManagementSupervisionService:
    def __init__(self, conn_factory, *, goals, management, config):
        self._conn = conn_factory
        self.goals, self.management, self.config = goals, management, config

    def ensure_schema(self):
        self.goals.ensure_schema()
        with closing(self._conn()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS manager_attention_sweep_state (
                session_id TEXT PRIMARY KEY, next_due_at TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS manager_attention_sweep_pending "
                         "ON manager_inbox(session_id,goal_id,goal_revision) "
                         "WHERE kind='timer' AND consumed_execution_id IS NULL")

    def _enabled(self):
        return (self.config.enabled and self.config.autonomous_enabled
                and self.config.attention_sweep_interval_seconds > 0)

    @staticmethod
    def _top_level(conn, alias="c"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_delegates'").fetchone():
            return f" AND NOT EXISTS (SELECT 1 FROM manager_delegates d WHERE d.session_id={alias}.session_id)"
        return ""

    def tick(self, now=None):
        """Coalesce missed periods and pending goals; capture also while paused."""
        instant = _instant(now)
        if not self._enabled():
            return 0
        stamp = instant.isoformat()
        upcoming = (instant + timedelta(seconds=self.config.attention_sweep_interval_seconds)).isoformat()
        captured = 0
        with closing(self._conn()) as probe:
            due_exists = probe.execute(
                "SELECT 1 FROM manager_controllers c JOIN agent_sessions s ON s.session_id=c.session_id "
                "LEFT JOIN manager_attention_sweep_state p ON p.session_id=c.session_id "
                "WHERE s.kind='manager' AND s.status!='deleted' "
                "AND (p.next_due_at IS NULL OR p.next_due_at<=?)" + self._top_level(probe) + " LIMIT 1",
                (stamp,),
            ).fetchone() is not None
        if not due_exists:
            return 0
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            managers = conn.execute(
                "SELECT c.session_id,p.next_due_at,p.generation FROM manager_controllers c "
                "JOIN agent_sessions s ON s.session_id=c.session_id "
                "LEFT JOIN manager_attention_sweep_state p ON p.session_id=c.session_id "
                "WHERE s.kind='manager' AND s.status!='deleted'" + self._top_level(conn)
                + " ORDER BY c.session_id")
            while page := managers.fetchmany(50):
                for manager in page:
                    sid = manager["session_id"]
                    if manager["next_due_at"] is None:
                        conn.execute("INSERT INTO manager_attention_sweep_state VALUES(?,?,0)", (sid, upcoming))
                        continue
                    if _instant(manager["next_due_at"]) > instant:
                        continue
                    generation = manager["generation"] + 1
                    goals = conn.execute("SELECT goal_id,revision FROM manager_goals WHERE session_id=? "
                                         "AND status IN ('active','waiting') ORDER BY goal_id", (sid,))
                    while goal_page := goals.fetchmany(50):
                        for goal in goal_page:
                            if not self.goals.enabled_in_transaction(conn, goal["goal_id"]):
                                continue
                            pending = conn.execute(
                                "SELECT 1 FROM manager_inbox WHERE session_id=? AND goal_id=? AND goal_revision=? "
                                "AND kind='timer' AND consumed_execution_id IS NULL "
                                "AND json_extract(payload,'$.attention_sweep')=1 LIMIT 1",
                                (sid, goal["goal_id"], goal["revision"])).fetchone()
                            if pending:
                                continue
                            captured += self.goals._event(
                                conn, sid, f"attention_sweep:{goal['goal_id']}:{goal['revision']}:{generation}",
                                "timer", {"attention_sweep": True, "generation": generation}, stamp,
                                goal["goal_id"], goal["revision"])
                    conn.execute("UPDATE manager_attention_sweep_state SET next_due_at=?,generation=? WHERE session_id=?",
                                 (upcoming, generation, sid))
        return captured

    def validate_event(self, conn, event):
        """Old pending generations remain valid while newer pulses coalesce."""
        if not self._enabled():
            return False
        payload = event["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        if (not isinstance(payload, dict) or payload.get("attention_sweep") is not True
                or type(payload.get("generation")) is not int or payload["generation"] < 1
                or event.get("kind") != "timer" or not event.get("goal_id")):
            return False
        return conn.execute(
            "SELECT 1 FROM manager_controllers c JOIN agent_sessions s ON s.session_id=c.session_id "
            "JOIN manager_attention_sweep_state p ON p.session_id=c.session_id "
            "WHERE c.session_id=? AND s.kind='manager' AND s.status!='deleted' AND p.generation>=?"
            + self._top_level(conn), (event["session_id"], payload["generation"])).fetchone() is not None
