"""Explicit human-marked, local-only evaluation copies; no model or network work."""

from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domains.message_history import _now
from app.domains.message_matter_proposals import HumanControlPrincipal


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


MessageId = Annotated[str, Field(min_length=1, max_length=256)]


class BadcaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_message_ids: list[MessageId] = Field(min_length=1, max_length=20)
    expected_evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    label: Literal[
        "missed_importance", "false_positive", "topic_split", "topic_merge",
        "deadline_correction", "evidence_error",
    ]
    note: str = Field(default="", max_length=2000)
    local_copy_consent: bool

    @field_validator("local_copy_consent")
    @classmethod
    def explicit_consent(cls, value):
        if value is not True:
            raise ValueError("explicit_local_copy_consent_required")
        return value


class BadcaseConflict(ValueError):
    """The reviewed quotes or capture permissions no longer match."""


class MessageReadingEvaluationService:
    def __init__(self, message_history):
        self.message_history = message_history
        self.ensure_schema()

    @contextmanager
    def _transaction(self, conn=None):
        owns = conn is None
        if owns:
            conn = self.message_history._connect()
            conn.execute("BEGIN IMMEDIATE")
        elif not conn.in_transaction:
            raise ValueError("badcase_caller_transaction_required")
        try:
            yield conn
            if owns:
                conn.commit()
        except BaseException:
            if owns:
                conn.rollback()
            raise
        finally:
            if owns:
                conn.close()

    def ensure_schema(self):
        with self._transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS message_reading_badcases (
                badcase_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL,
                label TEXT NOT NULL, note TEXT NOT NULL, evidence_json TEXT NOT NULL,
                evidence_digest TEXT NOT NULL, fences_json TEXT NOT NULL,
                local_copy_consent INTEGER NOT NULL CHECK(local_copy_consent=1),
                created_at TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reading_badcases_feed ON message_reading_badcases(created_at DESC,badcase_id DESC)")

    @staticmethod
    def _human(principal):
        if (not isinstance(principal, HumanControlPrincipal)
                or principal.kind != "human_control"
                or not isinstance(principal.principal_id, str)
                or not principal.principal_id.strip()):
            raise PermissionError("human_control_identity_required")

    def _evidence(self, conn, ids, allowed_sources, allowed_accounts):
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 20
                or any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in ids)):
            raise ValueError("invalid_badcase_evidence")
        evidence, fences = [], {}
        for message_id in sorted(set(ids)):
            row = conn.execute("SELECT * FROM message_history_messages WHERE internal_message_id=?", (message_id,)).fetchone()
            if row is None:
                raise PermissionError("badcase_unavailable")
            policies = self.message_history._read_scope(conn, row["conversation_key"], allowed_sources, allowed_accounts)
            if not policies:
                raise PermissionError("badcase_unavailable")
            policy = policies[0]
            evidence.append({"source_type": "message_history_message", "source_id": policy["source_id"],
                             "account_scope_id": policy["account_scope_id"],
                             **self.message_history._resolved_message(conn, row)})
            fences[row["conversation_key"]] = policy["capture_epoch"]
        return evidence, fences

    @staticmethod
    def _evidence_digest(evidence, fences):
        return _digest({"evidence": evidence, "capture_epochs": fences})

    def preview(self, evidence_message_ids, *, principal, allowed_sources=None, allowed_accounts=None):
        self._human(principal)
        with self._transaction() as conn:
            evidence, fences = self._evidence(conn, evidence_message_ids, allowed_sources, allowed_accounts)
            return {"evidence_message_ids": [item["message_id"] for item in evidence],
                    "evidence_digest": self._evidence_digest(evidence, fences), "evidence": evidence,
                    "local_copy_requires_consent": True, "local_only": True, "untrusted_data": True}

    def _visible(self, conn, row, allowed_sources, allowed_accounts):
        saved = json.loads(row["evidence_json"])
        _, fences = self._evidence(conn, [item["message_id"] for item in saved], allowed_sources, allowed_accounts)
        # Reauthorizing capture does not silently resurrect copies from a revoked generation.
        if fences != json.loads(row["fences_json"]):
            raise PermissionError("badcase_unavailable")
        return {"badcase_id": row["badcase_id"], "label": row["label"], "note": row["note"],
                "evidence": saved, "evidence_digest": row["evidence_digest"],
                "local_copy_consent": True, "created_at": row["created_at"],
                "local_only": True, "untrusted_data": True}

    def save(self, payload: BadcaseInput | dict, *, principal, allowed_sources=None, allowed_accounts=None, conn=None):
        self._human(principal)
        data = BadcaseInput.model_validate(payload.model_dump() if isinstance(payload, BadcaseInput) else payload)
        with self._transaction(conn) as transaction:
            evidence, fences = self._evidence(transaction, data.evidence_message_ids, allowed_sources, allowed_accounts)
            digest = self._evidence_digest(evidence, fences)
            if digest != data.expected_evidence_digest:
                raise BadcaseConflict("badcase_evidence_conflict")
            identity = {"principal_id": principal.principal_id, "evidence_digest": digest,
                        "label": data.label, "note": data.note}
            badcase_id = "message_badcase_" + _digest(identity)
            transaction.execute("""INSERT OR IGNORE INTO message_reading_badcases
                VALUES(?,?,?,?,?,?,?,1,?)""", (badcase_id, principal.principal_id, data.label, data.note,
                    _json(evidence), digest, _json(fences), _now()))
            row = transaction.execute("SELECT * FROM message_reading_badcases WHERE badcase_id=?", (badcase_id,)).fetchone()
            return self._visible(transaction, row, allowed_sources, allowed_accounts)

    def get_badcase(self, badcase_id, *, principal, allowed_sources=None, allowed_accounts=None):
        self._human(principal)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM message_reading_badcases WHERE badcase_id=?", (badcase_id,)).fetchone()
            if row is None:
                raise PermissionError("badcase_unavailable")
            return self._visible(conn, row, allowed_sources, allowed_accounts)

    def list_badcases(self, *, principal, limit=20, cursor=None, allowed_sources=None, allowed_accounts=None):
        self._human(principal)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid_badcase_page")
        scope = _digest({"principal": principal.principal_id,
                         "sources": sorted(allowed_sources) if allowed_sources is not None else None,
                         "accounts": sorted(allowed_accounts) if allowed_accounts is not None else None})
        params, where = [], ""
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError()
                page = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if set(page) != {"scope", "created_at", "badcase_id"} or page["scope"] != scope:
                    raise ValueError()
                if not isinstance(page["created_at"], str) or not isinstance(page["badcase_id"], str):
                    raise TypeError()
                where = " WHERE (created_at,badcase_id)<(?,?)"
                params.extend([page["created_at"], page["badcase_id"]])
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise ValueError("invalid_badcase_cursor") from None
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM message_reading_badcases" + where
                                + " ORDER BY created_at DESC,badcase_id DESC LIMIT ?", [*params, limit + 1]).fetchall()
            items = []
            for row in rows[:limit]:
                try:
                    items.append(self._visible(conn, row, allowed_sources, allowed_accounts))
                except PermissionError:
                    continue
            has_more = len(rows) > limit
            last = rows[limit - 1] if has_more else None
            next_cursor = base64.urlsafe_b64encode(_json({"scope": scope, "created_at": last["created_at"],
                                                        "badcase_id": last["badcase_id"]}).encode()).decode() if last else None
            return {"badcases": items, "next_cursor": next_cursor, "has_more": has_more, "local_only": True}
