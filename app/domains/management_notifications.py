"""Per-execution notification proposals, separate from internal publication."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime

from app.domains.management import ManagementConflictError


class ManagementNotificationService:
    def __init__(self, conn_factory, tasks):
        self._conn, self.tasks = conn_factory, tasks

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS manager_notification_proposals (
                execution_id TEXT PRIMARY KEY REFERENCES manager_executions(execution_id),
                session_id TEXT NOT NULL, run_id TEXT NOT NULL, command_id TEXT NOT NULL,
                notify_user INTEGER NOT NULL CHECK(notify_user IN (0,1)), reason TEXT NOT NULL,
                created_at TEXT NOT NULL, applied_at TEXT
            )""")

    @staticmethod
    def _execution(conn, record):
        metadata = record.get("metadata", {})
        execution_id = metadata.get("manager_execution_id")
        envelope = metadata.get("input_envelope", {})
        if (not execution_id or record.get("parent_run_id") or envelope.get("origin") != "background"):
            return None
        return conn.execute(
            "SELECT * FROM manager_executions WHERE execution_id=? AND run_id=? AND session_id=?",
            (execution_id, record["run_id"], record["session_id"]),
        ).fetchone()

    def projection(self, conn, record):
        execution = self._execution(conn, record)
        if execution is None:
            return None
        row = conn.execute(
            "SELECT notify_user,reason FROM manager_notification_proposals "
            "WHERE execution_id=? AND run_id=? AND session_id=?",
            (execution["execution_id"], record["run_id"], record["session_id"]),
        ).fetchone()
        # The creation-time flag preserves publication behavior for runs already
        # queued/checkpointed before explicit notification decisions existed.
        required = record.get("metadata", {}).get("manager_notification_required") is True
        notification = {
            "notify_user": bool(row["notify_user"]) if row else not required,
            "reason": row["reason"] if row else (
                "No explicit notification decision was recorded." if required else
                "Legacy execution retains automatic notification."
            ),
            "decision_source": "explicit" if row else (
                "missing_decision_default_silent" if required else "legacy_default_notify"
            ),
        }
        delivery = getattr(self, "delivery", None)
        if delivery is not None and notification["notify_user"]:
            event_ids = json.loads(execution["event_ids"])
            events = []
            for event_id in event_ids:
                event = conn.execute(
                    "SELECT * FROM manager_inbox WHERE event_id=? AND session_id=?",
                    (event_id, record["session_id"]),
                ).fetchone()
                if event is not None:
                    event = dict(event)
                    event["payload"] = json.loads(event["payload"])
                    events.append(event)
            # Central inspection remains independent of delivery preferences.
            # Existing structured decision events in a mixed batch retain their
            # allow_decisions exception; a model's prose cannot grant it.
            central = any(event["payload"].get("attention_sweep") is True for event in events)
            if central and not any(delivery.can_deliver(conn, record["session_id"], event) for event in events):
                notification = {
                    "notify_user": False,
                    "reason": "Delivery preferences suppress this notification; internal supervision continues.",
                    "decision_source": "delivery_policy",
                    "requested_notify_user": notification["notify_user"],
                    "requested_reason": notification["reason"],
                }
        return {"notification": notification, "user_visible": notification["notify_user"]}

    def for_run(self, run_id):
        """Visibility hook; ordinary user answers have no management override."""
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            return self.projection(conn, json.loads(row[0])) if row else None

    def decide_notification(self, context, *, command_id, notify_user, reason):
        if not context.safety_review_approved:
            raise ValueError("Notification decisions require the tool safety review gate")
        if type(notify_user) is not bool:
            raise ValueError("notify_user must be a boolean")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("reason must contain 1..2000 characters")
        reason = reason.strip()
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self.tasks.caller(context, mutate=True, conn=conn)
            execution = self._execution(conn, run.model_dump(mode="json"))
            if execution is None or execution["status"] != "bound":
                raise ManagementConflictError("Notification decisions require this run's bound background execution")
            args = {"execution_id": execution["execution_id"], "notify_user": notify_user, "reason": reason}
            fingerprint, prior = self.tasks._command(conn, context, command_id, "decide_notification", args)
            if prior:
                return prior
            if conn.execute("SELECT 1 FROM manager_notification_proposals WHERE execution_id=?",
                            (execution["execution_id"],)).fetchone():
                raise ManagementConflictError("Only one notification decision is allowed per execution")
            now = datetime.now(UTC).isoformat()
            conn.execute("INSERT INTO manager_notification_proposals "
                         "(execution_id,session_id,run_id,command_id,notify_user,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                         (execution["execution_id"], context.session_id, context.run_id, command_id, int(notify_user), reason, now))
            receipt = {"command_id": command_id, "action": "decide_notification", "status": "proposed",
                       **args, "run_id": context.run_id}
            conn.execute("INSERT INTO management_commands "
                         "(manager_session_id,command_id,fingerprint,action,receipt,created_at) VALUES(?,?,?,?,?,?)",
                         (context.session_id, command_id, fingerprint, "decide_notification", json.dumps(receipt), now))
        return receipt

    def apply(self, conn, record):
        """Mark the proposal applied only inside the successful publication transaction."""
        if not conn.in_transaction:
            raise ValueError("Notification publication requires a transaction")
        projection = self.projection(conn, record)
        if projection is None:
            raise ManagementConflictError("Notification publication requires a bound background execution")
        conn.execute("UPDATE manager_notification_proposals SET applied_at=? "
                     "WHERE execution_id=? AND run_id=? AND session_id=? AND applied_at IS NULL",
                     (datetime.now(UTC).isoformat(), record["metadata"]["manager_execution_id"],
                      record["run_id"], record["session_id"]))
        return projection
