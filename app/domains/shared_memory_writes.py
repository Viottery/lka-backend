"""Explicit human shared-memory edits and a durable node mutation outbox.

A write-authorized node attests its authenticated human control action. Ordinary
model proposals and local learning do not enter this path. The hub remains the
only authority; queued edits never modify the recall cache or private memory.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domains.memory import MemoryInput, MemorySourceInput
from app.domains.memory_feedback import MemoryFeedbackInput
from app.domains.shared_sync import SyncConflictError, canonical
from app.storage.db import connect


class HumanMemoryConsent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    authorization_epoch: int = Field(default=1, ge=1)
    confirmed: Literal[True]
    origin: Literal["human_ui"] = "human_ui"


class LocalMemoryMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mutation_id: str = Field(min_length=1, max_length=120, pattern=r"^[\w.-]+$")
    operation: Literal["create", "update", "delete"]
    memory_id: str | None = Field(default=None, min_length=1, max_length=512)
    expected_version: int = Field(ge=0)
    scope: Literal["global", "project"] = "global"
    project_id: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=500)
    memory_type: Literal["preference", "project_decision", "user_fact"] = "preference"
    sensitivity: Literal["normal", "personal"] = "normal"
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_at: str | None = None
    confirmed: Literal[True]

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("memory content cannot be blank")
        return value.strip() if value is not None else None

    @field_validator("expires_at")
    @classmethod
    def expiry(cls, value):
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("memory expiry requires a timezone")
        return parsed.astimezone(UTC).isoformat()

    @model_validator(mode="after")
    def operation_fields(self):
        if self.scope == "project" and not self.project_id:
            raise ValueError("project scope requires project_id")
        if self.scope == "global" and self.project_id is not None:
            raise ValueError("global memory cannot have project_id")
        if self.operation == "create":
            if self.memory_id is not None or self.expected_version != 0:
                raise ValueError("create requires no target and expected_version=0")
        elif self.memory_id is None or self.expected_version < 1:
            raise ValueError("update/delete require a target and expected_version>=1")
        if self.operation != "delete" and self.content is None:
            raise ValueError("create/update require content")
        if self.operation == "delete" and self.content is not None:
            raise ValueError("delete cannot contain replacement content")
        return self


class SharedMemoryMutation(LocalMemoryMutation):
    hub_id: str = Field(min_length=1, max_length=120)
    consent: HumanMemoryConsent


class SharedMemoryWriteService:
    """CAS domain mutations and idempotency receipts share one hub transaction."""

    def __init__(self, memory_service, sync_service):
        self.memory, self.sync = memory_service, sync_service
        with closing(self.memory._connect()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS shared_memory_mutations(
                node_id TEXT NOT NULL, mutation_id TEXT NOT NULL, user_id TEXT NOT NULL,
                payload TEXT NOT NULL, receipt TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(node_id, mutation_id))""")

    def _current(self, conn, row, *, depth=0):
        if row is None:
            return None
        record = self.memory._record(conn, row)
        result = record.model_dump(mode="json")
        now = datetime.now(UTC).isoformat()
        # An inactive/purged source never becomes recall evidence through a
        # conflict response. IDs, status and versions suffice to explain it.
        if (record.status not in {"active", "candidate"}
                or record.sensitivity not in {"normal", "public"}
                or record.expires_at and record.expires_at <= now
                or not self.memory._has_live_source_ids(conn, record.source_ids, now)):
            result = {key: result[key] for key in
                      ("memory_id", "scope", "project_id", "status", "version", "supersedes_id")}
            result["content_unavailable"] = True
        replacement = conn.execute(
            "SELECT * FROM memory_entries WHERE supersedes_id=? ORDER BY created_at DESC,memory_id LIMIT 1",
            (record.memory_id,),
        ).fetchone()
        if replacement is not None:
            if depth < 8:
                result["replacement"] = self._current(conn, replacement, depth=depth + 1)
            else:
                result["replacement_ref"] = replacement["memory_id"]
        return result

    def submit(self, principal: dict, payload: SharedMemoryMutation | dict) -> dict:
        item = SharedMemoryMutation.model_validate(payload)
        if not isinstance(principal, dict) or not principal.get("node_id"):
            raise PermissionError("authenticated node required")
        config = self.sync.config
        if not (config.enabled and config.role == "hub"
                and getattr(config, "memory_write_enabled", False)):
            raise PermissionError("shared memory writes disabled")
        encoded = canonical(item.model_dump(mode="json"))
        with closing(self.memory._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            # Revocation and grant changes serialize with the domain commit,
            # including retries of a previously accepted mutation.
            self.sync._authorize(conn, principal, "sync.memory.write")
            hub = conn.execute("SELECT value FROM shared_sync_meta WHERE key='hub_id'").fetchone()
            if not hub or item.hub_id != hub[0]:
                raise PermissionError("shared memory hub identity mismatch")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_accounts'").fetchone():
                raise PermissionError("active human account required")
            account = conn.execute("SELECT disabled FROM auth_accounts WHERE user_id=?",
                                   (item.consent.user_id,)).fetchone()
            if account is None or account[0]:
                raise PermissionError("active human account required")
            node_id = principal["node_id"]
            old = conn.execute("SELECT payload,receipt FROM shared_memory_mutations "
                               "WHERE node_id=? AND mutation_id=?", (node_id, item.mutation_id)).fetchone()
            if old is not None:
                if old["payload"] != encoded:
                    raise SyncConflictError("mutation identity already has different content")
                receipt = json.loads(old["receipt"])
                # Refresh only visibility on replay; the original resolution is
                # durable and a deleted source cannot be exposed by an old ACK.
                if receipt["status"] == "conflict":
                    current = conn.execute("SELECT * FROM memory_entries WHERE memory_id=?",
                                           (item.memory_id,)).fetchone()
                    receipt["current"] = self._current(conn, current)
                return receipt
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                            "name='device_connection_authorizations'").fetchone():
                authorization = conn.execute(
                    "SELECT authorization_epoch FROM device_connection_authorizations WHERE node_id=? AND user_id=?",
                    (node_id, item.consent.user_id),
                ).fetchone()
                if item.consent.authorization_epoch != (authorization[0] if authorization else 1):
                    raise PermissionError("human node authorization revoked")
            row = None
            reason = None
            if item.operation != "create":
                row = conn.execute("SELECT * FROM memory_entries WHERE memory_id=?",
                                   (item.memory_id,)).fetchone()
                if row is None:
                    reason = "target_missing"
                elif row["scope"] != item.scope or row["project_id"] != item.project_id:
                    raise PermissionError("memory scope mismatch")
                elif row["version"] != item.expected_version:
                    reason = "version_conflict"
                elif row["status"] != "active":
                    reason = "target_inactive"
                elif not self.memory._has_live_source_ids(
                        conn, self.memory._record(conn, row).source_ids, datetime.now(UTC).isoformat()):
                    reason = "source_unavailable"
                elif row["expires_at"] and row["expires_at"] <= datetime.now(UTC).isoformat():
                    reason = "target_expired"
            if item.scope == "project" and conn.execute(
                    "SELECT 1 FROM memory_projects WHERE project_id=?", (item.project_id,),
            ).fetchone() is None:
                raise PermissionError("unknown shared project")
            receipt = {"hub_id": item.hub_id, "node_id": node_id, "mutation_id": item.mutation_id,
                       "status": "conflict" if reason else "accepted"}
            if reason:
                receipt.update(reason=reason, current=self._current(conn, row),
                               proposal=item.model_dump(mode="json"))
            else:
                # No caller-controlled source/trusted flag or model evidence is
                # admitted. This provenance records the original human action.
                source_id = self.memory.register_source_in_transaction(conn, MemorySourceInput(
                    source_type="user_api",
                    source_ref=f"shared-human:{item.hub_id}:{node_id}:{item.mutation_id}",
                    checksum=hashlib.sha256(encoded.encode()).hexdigest(), trusted_source=False,
                    metadata={"user_id": item.consent.user_id, "session_id": item.consent.session_id,
                              "origin": "human_ui", "node_id": node_id},
                )) if item.operation != "delete" else None
                if item.operation == "create":
                    record = self.memory.create_in_transaction(conn, MemoryInput(
                        content=item.content, memory_type=item.memory_type, scope=item.scope,
                        project_id=item.project_id, source_id=source_id, user_confirmed=True,
                        sensitivity=item.sensitivity, confidence=item.confidence, expires_at=item.expires_at,
                        dedupe_key=f"shared-human:{node_id}:{item.mutation_id}",
                    ))
                elif item.operation == "update":
                    record = self.memory.correct_in_transaction(
                        conn, item.memory_id, content=item.content, source_id=source_id,
                        user_confirmed=True, expected_version=item.expected_version, require_active=True,
                        user_feedback=MemoryFeedbackInput(origin="user_api", reason="unspecified"),
                    )
                else:
                    record = self.memory.retract_in_transaction(
                        conn, item.memory_id, expected_version=item.expected_version,
                        user_feedback=MemoryFeedbackInput(origin="user_api", reason="unspecified"),
                    )
                receipt["result"] = {"memory_id": record.memory_id, "version": record.version,
                                     "status": record.status, "supersedes_id": record.supersedes_id}
            conn.execute("INSERT INTO shared_memory_mutations VALUES(?,?,?,?,?,?)",
                         (node_id, item.mutation_id, item.consent.user_id, encoded, canonical(receipt),
                          datetime.now(UTC).isoformat()))
            return receipt


class SharedMemoryMutationOutbox:
    """Pending/conflicting edits are explicit UI state, never recall memories."""

    def __init__(self, path: Path, *, hub_id: str, node_id: str, cache):
        self.path, self.hub_id, self.node_id, self.cache = Path(path), hub_id, node_id, cache
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(self.path)) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS shared_memory_outbox_identity(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), hub_id TEXT NOT NULL, node_id TEXT NOT NULL)""")
            conn.execute("INSERT OR IGNORE INTO shared_memory_outbox_identity VALUES(1,?,?)", (hub_id, node_id))
            if tuple(conn.execute("SELECT hub_id,node_id FROM shared_memory_outbox_identity").fetchone()) != (hub_id, node_id):
                raise SyncConflictError("shared memory outbox belongs to a different hub/node")
            conn.execute("""CREATE TABLE IF NOT EXISTS shared_memory_outbox(
                mutation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', receipt TEXT, created_at TEXT NOT NULL,
                rejection_reason TEXT)""")
            if "rejection_reason" not in {row["name"] for row in conn.execute("PRAGMA table_info(shared_memory_outbox)")}:
                conn.execute("ALTER TABLE shared_memory_outbox ADD COLUMN rejection_reason TEXT")

    def enqueue(self, payload: LocalMemoryMutation | dict, principal: dict) -> dict:
        item = LocalMemoryMutation.model_validate(payload)
        if (not principal.get("user_id") or not principal.get("session_id")
                or principal.get("node_id") != self.node_id
                or type(principal.get("authorization_epoch")) is not int
                or principal["authorization_epoch"] < 1):
            raise PermissionError("authenticated human session bound to this node required")
        values = item.model_dump(mode="json")
        if item.scope == "project":
            shared_project = self.cache.project_bindings.get(item.project_id)
            if not shared_project:
                raise PermissionError("local project has no explicit shared memory binding")
            values["project_id"] = shared_project
        if item.memory_id:
            target = self.cache._original_id(item.memory_id)
            if target is None:
                raise PermissionError("shared edit requires a memory ID from this hub cache")
            values["memory_id"] = target
        values.update(hub_id=self.hub_id, consent=HumanMemoryConsent(
            user_id=principal["user_id"], session_id=principal["session_id"], confirmed=True,
            authorization_epoch=principal["authorization_epoch"],
        ).model_dump())
        shared = SharedMemoryMutation.model_validate(values)
        encoded = canonical(shared.model_dump(mode="json"))
        with closing(connect(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM shared_memory_outbox WHERE mutation_id=?",
                               (item.mutation_id,)).fetchone()
            if old:
                if old["payload"] != encoded:
                    raise SyncConflictError("mutation identity already has different content")
                return self._entry(old)
            if item.memory_id:
                current = self.cache.get(item.memory_id)
                if current.scope != item.scope or current.project_id != item.project_id:
                    raise PermissionError("shared memory scope mismatch")
            conn.execute("INSERT OR IGNORE INTO shared_memory_outbox(mutation_id,user_id,payload,created_at) "
                         "VALUES(?,?,?,?)", (item.mutation_id, principal["user_id"], encoded,
                                             datetime.now(UTC).isoformat()))
        return self.get(item.mutation_id, principal["user_id"])

    def pending(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid mutation limit")
        with closing(connect(self.path)) as conn:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM shared_memory_outbox WHERE status='queued' ORDER BY rowid LIMIT ?", (limit,),
            )]

    def acknowledge(self, payload: dict, receipt: dict) -> None:
        item = SharedMemoryMutation.model_validate(payload)
        if (not isinstance(receipt, dict) or receipt.get("hub_id") != self.hub_id
                or receipt.get("node_id") != self.node_id or receipt.get("mutation_id") != item.mutation_id
                or receipt.get("status") not in {"accepted", "conflict"}):
            raise SyncConflictError("invalid shared memory receipt")
        if receipt["status"] == "accepted" and (
                not isinstance(receipt.get("result"), dict)
                or not isinstance(receipt["result"].get("memory_id"), str)
                or type(receipt["result"].get("version")) is not int):
            raise SyncConflictError("invalid shared memory result")
        with closing(connect(self.path)) as conn, conn:
            changed = conn.execute("UPDATE shared_memory_outbox SET status=?,receipt=? "
                                   "WHERE mutation_id=? AND payload=? AND status='queued'",
                                   (receipt["status"], canonical(receipt), item.mutation_id,
                                    canonical(item.model_dump(mode="json")))).rowcount
            if not changed:
                old = conn.execute("SELECT receipt FROM shared_memory_outbox WHERE mutation_id=? AND payload=?",
                                   (item.mutation_id, canonical(item.model_dump(mode="json")))).fetchone()
                if not old or old[0] != canonical(receipt):
                    raise SyncConflictError("mutation changed before receipt")

    def reject(self, payload: dict, reason: str) -> None:
        """A definitive HTTP rejection is local terminal state, never a hub ACK."""
        if reason not in {"cloud_http_403", "cloud_http_409", "cloud_http_422"}:
            raise ValueError("mutation rejection must be a definitive server category")
        item = SharedMemoryMutation.model_validate(payload)
        with closing(connect(self.path)) as conn, conn:
            changed = conn.execute(
                "UPDATE shared_memory_outbox SET status='rejected',rejection_reason=? "
                "WHERE mutation_id=? AND payload=? AND status='queued'",
                (reason, item.mutation_id, canonical(item.model_dump(mode="json"))),
            ).rowcount
            if not changed:
                raise SyncConflictError("mutation changed before rejection")

    def _entry(self, row):
        receipt = json.loads(row["receipt"]) if row["receipt"] else None
        if receipt and receipt.get("status") == "conflict":
            receipt["current"] = self._visible_current(receipt.get("current"))
        return {"mutation_id": row["mutation_id"], "status": row["status"],
                "proposal": json.loads(row["payload"]),
                "receipt": receipt,
                "rejection_reason": row["rejection_reason"],
                "created_at": row["created_at"], "recall_effective": False}

    def _visible_current(self, current):
        """Known mirror withdrawals also hide old conflict-response content."""
        if not isinstance(current, dict):
            return current
        current = dict(current)
        if "replacement" in current:
            current["replacement"] = self._visible_current(current["replacement"])
        now = datetime.now(UTC).isoformat()
        withdrawn = bool(current.get("expires_at") and current["expires_at"] <= now)
        try:
            with self.cache.replica._connection() as conn:
                known = conn.execute(
                    "SELECT deleted,payload FROM replica_records WHERE kind='memory_entries' AND entity_key=?",
                    (canonical([current.get("memory_id")]),),
                ).fetchone()
                if known:
                    record = json.loads(known["payload"]) if known["payload"] else {}
                    withdrawn = withdrawn or bool(known["deleted"] or record.get("status") != "active")
        except (sqlite3.Error, OSError, ValueError):
            withdrawn = True
        if withdrawn:
            current = {key: value for key, value in current.items() if key in {
                "memory_id", "scope", "project_id", "status", "version", "supersedes_id",
                "replacement", "replacement_ref",
            }}
            current["content_unavailable"] = True
        return current

    def get(self, mutation_id: str, user_id: str) -> dict:
        with closing(connect(self.path)) as conn:
            row = conn.execute("SELECT * FROM shared_memory_outbox WHERE mutation_id=? AND user_id=?",
                               (mutation_id, user_id)).fetchone()
            if row is None:
                raise KeyError(mutation_id)
            return self._entry(row)

    def list(self, user_id: str, limit: int = 100) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid mutation limit")
        with closing(connect(self.path)) as conn:
            return {"items": [self._entry(row) for row in conn.execute(
                "SELECT * FROM shared_memory_outbox WHERE user_id=? ORDER BY rowid DESC LIMIT ?", (user_id, limit),
            )]}

    def status(self, user_id: str | None = None) -> dict:
        with closing(connect(self.path)) as conn:
            rows = conn.execute("SELECT status,count(*) FROM shared_memory_outbox"
                                + (" WHERE user_id=?" if user_id is not None else "")
                                + " GROUP BY status", (user_id,) if user_id is not None else ()).fetchall()
        return {"hub_id": self.hub_id, "node_id": self.node_id,
                **dict.fromkeys(("queued", "accepted", "conflict", "rejected"), 0), **dict(rows)}
