"""Deterministic local delivery preferences; capture and budgets are independent."""
from __future__ import annotations

import json
from contextlib import closing

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domains.management import ManagementConflictError
from app.domains.management_goals import ManagementGoalService, _instant
from app.storage.commit_signals import notify_after_commit


class DeliveryPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    muted: bool = False
    snooze_until: str | None = Field(default=None, max_length=80)
    allow_decisions: bool = True

    @field_validator("snooze_until")
    @classmethod
    def aware_time(cls, value):
        return _instant(value).isoformat() if value is not None else None


class ManagementDeliveryService:
    def __init__(self, conn_factory):
        self._conn = conn_factory

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS manager_delivery_policies (
                session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                muted INTEGER NOT NULL CHECK(muted IN (0,1)), snooze_until TEXT,
                allow_decisions INTEGER NOT NULL CHECK(allow_decisions IN (0,1)))""")

    @staticmethod
    def _get(conn, session_id):
        row = conn.execute("SELECT * FROM manager_delivery_policies WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.update(muted=bool(result["muted"]), allow_decisions=bool(result["allow_decisions"]))
        return result

    def get(self, session_id):
        with closing(self._conn()) as conn:
            ManagementGoalService._manager(conn, session_id)
            return self._get(conn, session_id)

    @notify_after_commit
    def configure(self, session_id, expected_revision, muted=False, snooze_until=None,
                  allow_decisions=True, *, authority_check=None):
        args = DeliveryPolicyInput.model_validate({"expected_revision": expected_revision, "muted": muted,
            "snooze_until": snooze_until, "allow_decisions": allow_decisions})
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if authority_check is not None:
                authority_check(conn)
            ManagementGoalService._manager(conn, session_id)
            prior = self._get(conn, session_id)
            if (prior["revision"] if prior else 0) != args.expected_revision:
                raise ManagementConflictError("Delivery policy revision changed")
            conn.execute("INSERT INTO manager_delivery_policies VALUES(?,?,?,?,?) "
                         "ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,muted=excluded.muted,"
                         "snooze_until=excluded.snooze_until,allow_decisions=excluded.allow_decisions",
                         (session_id, args.expected_revision + 1, int(args.muted), args.snooze_until,
                          int(args.allow_decisions)))
            if authority_check is not None:
                authority_check(conn)
            return self._get(conn, session_id)

    def can_deliver(self, conn, session_id, event, *, now=None):
        instant = _instant(now)
        try:
            ManagementGoalService._manager(conn, session_id)
        except KeyError:
            return False
        if event.get("session_id") != session_id:
            return False
        policy = self._get(conn, session_id)
        if policy is None:
            return True
        payload = event["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        if policy["allow_decisions"] and (payload.get("task_waiting") is True or payload.get("report_kind") == "needs_decision"):
            return True
        return not policy["muted"] and not (
            policy["snooze_until"] and _instant(policy["snooze_until"]) > instant)
