"""Database-level replication of memory published by explicitly trusted nodes.

The grant trusts backend publication state. It conveys no human confirmation or
execution authority. Only bounded memory/provenance projections cross the wire;
ordinary conversations and model configuration remain on their original node.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domains.memory import MemoryInput, MemorySourceInput, _valid_conflict_hints
from app.domains.shared_sync import SyncConflictError, canonical


def _now():
    return datetime.now(UTC).isoformat()


def _live_expiry(value):
    if value is None:
        return True
    try:
        instant = datetime.fromisoformat(value)
        return instant.tzinfo is not None and instant > datetime.fromisoformat(_now())
    except (ValueError, TypeError):
        return False


def _canonical_claim(text):
    # Same conservative spelling normalization used for local exact discovery;
    # this is never a semantic merge of different claims or conditions.
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip().rstrip("。.! ")


def _fingerprint(record):
    return hashlib.sha256(canonical([
        _canonical_claim(record["content"]), record["memory_type"], record["scope"],
        record.get("project_id"), record["sensitivity"], record.get("expires_at"),
    ]).encode()).hexdigest()


def _safe_checksum(value):
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"(?:sha(?:1|256|512):)?[a-fA-F0-9]{32,128}", value):
        return value
    # Old integrations sometimes placed an opaque reference in this field.
    # Retain its stable identity without exporting that private reference.
    return "opaque-sha256:" + hashlib.sha256(str(value).encode()).hexdigest()


class ReplicaSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = Field(min_length=1, max_length=200)
    source_type: str = Field(min_length=1, max_length=120)
    checksum: str | None = Field(default=None, max_length=256)
    status: Literal["active", "revoked"]
    expires_at: str | None = Field(default=None, max_length=80)

    @field_validator("expires_at")
    @classmethod
    def canonical_expiry(cls, value):
        return _validate_expiry(value)


class ReplicaMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str = Field(min_length=1, max_length=8000)
    memory_type: str = Field(min_length=1, max_length=80)
    scope: Literal["global", "project"]
    project_id: str | None = Field(default=None, max_length=200)
    status: Literal["active", "candidate"]
    sensitivity: Literal["normal", "public"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_at: str | None = Field(default=None, max_length=80)
    origin_version: int = Field(ge=1)
    supersedes_id: str | None = Field(default=None, max_length=200)
    extraction_model: str | None = Field(default=None, max_length=120)
    conflict_hints: list[dict[str, str]] = Field(default_factory=list, max_length=8)
    needs_review: bool = False

    @field_validator("expires_at")
    @classmethod
    def canonical_expiry(cls, value):
        return _validate_expiry(value)

    @model_validator(mode="after")
    def valid_record(self):
        if not self.content.strip() or not _canonical_claim(self.content):
            raise ValueError("memory content cannot be blank")
        if (self.scope == "project" and not self.project_id
                or self.scope == "global" and self.project_id is not None):
            raise ValueError("memory project scope mismatch")
        if self.conflict_hints != _valid_conflict_hints(self.conflict_hints):
            raise ValueError("invalid conflict hints")
        _validate_expiry(self.expires_at)
        return self


def _validate_expiry(value):
    if value is not None:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            raise ValueError("replicated expiry requires a timezone")
        return instant.astimezone(UTC).isoformat()
    return None


class MemoryReplicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    hub_id: str = Field(min_length=1, max_length=120)
    mutation_id: str = Field(min_length=1, max_length=120, pattern=r"^[\w.-]+$")
    origin_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1)
    operation: Literal["upsert", "withdraw"]
    record: ReplicaMemory | None = None
    sources: list[ReplicaSource] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def valid_operation(self):
        if self.operation == "upsert" and (self.record is None or not self.sources):
            raise ValueError("replicated publication requires memory and provenance")
        if self.operation == "withdraw" and (self.record is not None or self.sources):
            raise ValueError("withdrawals contain identities only")
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate replica source")
        for source in self.sources:
            _validate_expiry(source.expires_at)
        return self


class MemoryReplicationService:
    def __init__(self, memory_service, sync_service):
        self.memory, self.sync = memory_service, sync_service
        with self._connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS memory_replication_origins(
                node_id TEXT NOT NULL, origin_id TEXT NOT NULL, revision INTEGER NOT NULL,
                memory_id TEXT, source_ids TEXT NOT NULL, payload TEXT NOT NULL,
                lifecycle TEXT NOT NULL, parent_origin_id TEXT,
                PRIMARY KEY(node_id,origin_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS memory_replication_claims(
                fingerprint TEXT PRIMARY KEY, memory_id TEXT NOT NULL,
                managed_version INTEGER NOT NULL, protected INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS memory_replication_receipts(
                node_id TEXT NOT NULL,mutation_id TEXT NOT NULL,payload TEXT NOT NULL,receipt TEXT NOT NULL,
                PRIMARY KEY(node_id,mutation_id))""")

    @contextmanager
    def _connection(self):
        conn = self.memory._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _find_claim(self, conn, record, fingerprint):
        claim = conn.execute("SELECT * FROM memory_replication_claims WHERE fingerprint=?", (fingerprint,)).fetchone()
        if claim:
            return claim
        rows = conn.execute(
            "SELECT * FROM memory_entries WHERE scope=? AND project_id IS ? AND memory_type=? "
            "AND sensitivity=? AND expires_at IS ? ORDER BY created_at,memory_id",
            (record["scope"], record.get("project_id"), record["memory_type"], record["sensitivity"], record.get("expires_at")),
        )
        for row in rows:
            if _canonical_claim(row["content"]) == _canonical_claim(record["content"]):
                conn.execute("INSERT INTO memory_replication_claims VALUES(?,?,?,1)",
                             (fingerprint, row["memory_id"], row["version"]))
                return conn.execute("SELECT * FROM memory_replication_claims WHERE fingerprint=?", (fingerprint,)).fetchone()
        return None

    def _lineage_fenced(self, conn, node_id, parent, origin_id=None):
        seen = {origin_id} if origin_id else set()
        for _ in range(64):
            if not parent:
                return False
            if parent in seen:
                return True
            seen.add(parent)
            row = conn.execute("SELECT memory_id,parent_origin_id,lifecycle FROM memory_replication_origins "
                               "WHERE node_id=? AND origin_id=?", (node_id, parent)).fetchone()
            if row is None:
                return False
            if row["lifecycle"] == "conflict" or row["memory_id"] and conn.execute(
                "SELECT 1 FROM memory_replication_claims WHERE memory_id=? AND protected=1", (row["memory_id"],),
            ).fetchone():
                return True
            parent = row["parent_origin_id"]
        return True

    def _result(self, conn, memory_id):
        row = conn.execute("SELECT * FROM memory_entries WHERE memory_id=?", (memory_id,)).fetchone() if memory_id else None
        if row is None:
            return None
        record = self.memory._record(conn, row)
        return {"memory_id": record.memory_id, "version": record.version, "status": record.status,
                "needs_review": bool(record.metadata.get("needs_review"))}

    def list_conflicts(self, limit=100, offset=0):
        """Review current fenced proposals without exposing historical snapshots."""
        if (type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int
                or not 0 <= offset <= 1_000_000):
            raise ValueError("invalid replication conflict page")
        config = self.sync.config
        if not (config.enabled and config.role == "hub" and getattr(config, "memory_aggregation_enabled", False)):
            raise PermissionError("automatic memory aggregation disabled")
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM memory_replication_origins WHERE lifecycle='conflict' "
                                "ORDER BY node_id,origin_id LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
            items = []
            for row in rows[:limit]:
                current = MemoryReplicationInput.model_validate_json(row["payload"])
                # Latest withdrawals replace this row and its payload. Old
                # immutable receipt bodies never enter this review projection.
                items.append({"node_id": row["node_id"], "origin_id": row["origin_id"], "revision": row["revision"],
                              "proposal": current.record.model_dump(mode="json") if current.record else None,
                              "current": self._result(conn, row["memory_id"])})
            return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}

    def submit(self, principal, payload):
        item = MemoryReplicationInput.model_validate(payload)
        config = self.sync.config
        if not (config.enabled and config.role == "hub" and getattr(config, "memory_aggregation_enabled", False)):
            raise PermissionError("automatic memory aggregation disabled")
        if not isinstance(principal, dict) or not principal.get("node_id"):
            raise PermissionError("authenticated backend required")
        encoded, node_id = canonical(item.model_dump(mode="json")), principal["node_id"]
        with self._connection() as conn:
            self.sync._authorize(conn, principal, "sync.memory.aggregate")
            hub = conn.execute("SELECT value FROM shared_sync_meta WHERE key='hub_id'").fetchone()
            if hub is None or hub[0] != item.hub_id:
                raise PermissionError("memory replication hub identity mismatch")
            prior = conn.execute("SELECT payload,receipt FROM memory_replication_receipts WHERE node_id=? AND mutation_id=?",
                                 (node_id, item.mutation_id)).fetchone()
            if prior:
                if prior["payload"] != encoded:
                    raise SyncConflictError("replication mutation identity has different content")
                return json.loads(prior["receipt"])
            origin = conn.execute("SELECT * FROM memory_replication_origins WHERE node_id=? AND origin_id=?",
                                  (node_id, item.origin_id)).fetchone()
            receipt = {"hub_id": item.hub_id, "node_id": node_id, "mutation_id": item.mutation_id,
                       "origin_id": item.origin_id, "revision": item.revision}
            if origin and item.revision <= origin["revision"]:
                if item.revision == origin["revision"]:
                    raise SyncConflictError("origin revision already has an immutable mutation")
                receipt.update(status="stale", result=self._result(conn, origin["memory_id"]))
            else:
                # Any change outside this service fences later automatic writes.
                # Tracking is version-based, not a caller supplied confirmation.
                conn.execute("UPDATE memory_replication_claims SET protected=1 WHERE EXISTS(SELECT 1 "
                             "FROM memory_entries e WHERE e.memory_id=memory_replication_claims.memory_id "
                             "AND e.version!=memory_replication_claims.managed_version)")
                event_floor = conn.execute("SELECT coalesce(max(rowid),0) FROM memory_events").fetchone()[0]
                old_sources = json.loads(origin["source_ids"]) if origin else []
                target_id = origin["memory_id"] if origin else None
                current_sources, parent = [], item.record.supersedes_id if item.record else (origin["parent_origin_id"] if origin else None)
                if item.operation == "withdraw":
                    for source_id in old_sources:
                        self.memory.revoke_source_in_transaction(conn, source_id)
                    if target_id:
                        claim = conn.execute("SELECT protected FROM memory_replication_claims WHERE memory_id=?", (target_id,)).fetchone()
                        if claim and not claim[0]:
                            self.memory.reconcile_replica_status_in_transaction(conn, target_id)
                    receipt.update(status="withdrawn", result=self._result(conn, target_id))
                else:
                    record = item.record.model_dump(mode="json")
                    if record["scope"] == "project" and not conn.execute("SELECT 1 FROM memory_projects WHERE project_id=?",
                                                                           (record["project_id"],)).fetchone():
                        raise PermissionError("replicated project is not registered")
                    fingerprint = _fingerprint(record)
                    claim = self._find_claim(conn, record, fingerprint)
                    row = conn.execute("SELECT * FROM memory_entries WHERE memory_id=?", (claim["memory_id"],)).fetchone() if claim else None
                    old_target = conn.execute("SELECT e.status,c.protected FROM memory_entries e "
                                              "LEFT JOIN memory_replication_claims c USING(memory_id) WHERE e.memory_id=?",
                                              (origin["memory_id"],)).fetchone() if origin and origin["memory_id"] else None
                    fenced = (self._lineage_fenced(conn, node_id, parent, item.origin_id)
                              or bool(old_target and (old_target["protected"] or old_target["status"] not in {"active", "candidate"})))
                    inactive = row is not None and row["status"] not in {"active", "candidate"}
                    expired = record["expires_at"] is not None and record["expires_at"] <= _now()
                    if fenced or inactive or expired:
                        # A newer backend state retires its old contribution
                        # even when the replacement cannot overwrite cloud
                        # state. Other origins and native user sources survive.
                        for source_id in old_sources:
                            self.memory.revoke_source_in_transaction(conn, source_id)
                        target_id = claim["memory_id"] if claim else target_id
                        receipt.update(status="conflict", reason="authoritative_target_changed" if fenced or inactive else "origin_expired",
                                       result=self._result(conn, target_id))
                    else:
                        # Every origin contributes separate revocable provenance;
                        # one node cannot revoke another node's identical claim.
                        for source in item.sources:
                            if source.status != "active" or source.expires_at and source.expires_at <= _now():
                                continue
                            source_id = self.memory.register_source_in_transaction(conn, MemorySourceInput(
                                source_type="node_memory_replica",
                                source_ref=canonical([item.hub_id, node_id, item.origin_id, fingerprint, source.source_id]),
                                checksum=source.checksum, expires_at=source.expires_at, trusted_source=False,
                                metadata={"node_id": node_id, "origin_id": item.origin_id,
                                          "origin_source_id": source.source_id, "origin_source_type": source.source_type,
                                          "published_status": record["status"], "needs_review": record["needs_review"],
                                          "origin_version": record["origin_version"]},
                            ))
                            current_sources.append(source_id)
                        if not current_sources:
                            raise ValueError("replicated publication has no live provenance")
                        memory = self.memory.publish_replica_in_transaction(conn, MemoryInput(
                            content=record["content"], memory_type=record["memory_type"], scope=record["scope"],
                            project_id=record["project_id"], source_id=current_sources[0], confidence=record["confidence"],
                            sensitivity=record["sensitivity"], expires_at=record["expires_at"],
                            dedupe_key="replica:" + fingerprint, user_confirmed=False,
                            extraction_model=record["extraction_model"],
                            metadata={"node_backend_replica": True, "conflict_hints": record["conflict_hints"],
                                      **({"needs_review": True} if record["needs_review"] else {})},
                        ), source_ids=current_sources, target_memory_id=claim["memory_id"] if claim else None,
                           preserve_status=bool(claim and claim["protected"]))
                        target_id = memory.memory_id
                        if claim is None:
                            conn.execute("INSERT INTO memory_replication_claims VALUES(?,?,?,0)",
                                         (fingerprint, target_id, memory.version))
                        for source_id in set(old_sources) - set(current_sources):
                            self.memory.revoke_source_in_transaction(conn, source_id)
                        if origin and origin["memory_id"] and origin["memory_id"] != target_id:
                            old_claim = conn.execute("SELECT protected FROM memory_replication_claims WHERE memory_id=?",
                                                     (origin["memory_id"],)).fetchone()
                            if old_claim and not old_claim[0]:
                                self.memory.reconcile_replica_status_in_transaction(conn, origin["memory_id"])
                        receipt.update(status="accepted", result=self._result(conn, target_id))
                conn.execute("INSERT INTO memory_replication_origins VALUES(?,?,?,?,?,?,?,?) "
                             "ON CONFLICT(node_id,origin_id) DO UPDATE SET revision=excluded.revision,memory_id=excluded.memory_id,"
                             "source_ids=excluded.source_ids,payload=excluded.payload,lifecycle=excluded.lifecycle,parent_origin_id=excluded.parent_origin_id",
                             (node_id, item.origin_id, item.revision, target_id,
                              canonical(current_sources), encoded, receipt["status"], parent))
                conn.execute("UPDATE memory_replication_claims SET managed_version=(SELECT version FROM memory_entries e "
                             "WHERE e.memory_id=memory_replication_claims.memory_id) WHERE memory_id IN "
                             "(SELECT memory_id FROM memory_events WHERE rowid>?)", (event_floor,))
            self.sync._authorize(conn, principal, "sync.memory.aggregate")
            conn.execute("INSERT INTO memory_replication_receipts VALUES(?,?,?,?)",
                         (node_id, item.mutation_id, encoded, canonical(receipt)))
            return receipt


class NodeMemoryReplicationJournal:
    """Transactional dirty references, immutable snapshots and mirror ownership."""

    def __init__(self, conn_factory, *, hub_id, node_id, project_bindings=None, cache=None):
        self._conn_factory, self.hub_id, self.node_id = conn_factory, hub_id, node_id
        self.cache, self.project_bindings = cache, dict(project_bindings or (cache.project_bindings if cache else {}))
        if not hub_id or not node_id:
            raise ValueError("memory replication requires pinned hub and node identities")
        with self._connection(write=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS node_memory_replication_identity(slot INTEGER PRIMARY KEY,hub_id TEXT,node_id TEXT)")
            conn.execute("INSERT OR IGNORE INTO node_memory_replication_identity VALUES(1,?,?)", (hub_id, node_id))
            if tuple(conn.execute("SELECT hub_id,node_id FROM node_memory_replication_identity WHERE slot=1").fetchone()) != (hub_id, node_id):
                raise SyncConflictError("memory journal belongs to another hub/node")
            conn.execute("""CREATE TABLE IF NOT EXISTS node_memory_replication_heads(
                origin_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,collected_revision INTEGER NOT NULL DEFAULT 0,
                last_operation TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS node_memory_replication_outbox(
                mutation_id TEXT PRIMARY KEY,origin_id TEXT NOT NULL,revision INTEGER NOT NULL,
                payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',receipt TEXT,
                retry_order INTEGER NOT NULL DEFAULT 0,UNIQUE(origin_id,revision))""")
            if "retry_order" not in {row["name"] for row in conn.execute("PRAGMA table_info(node_memory_replication_outbox)")}:
                conn.execute("ALTER TABLE node_memory_replication_outbox ADD COLUMN retry_order INTEGER NOT NULL DEFAULT 0")
            conn.execute("""CREATE TABLE IF NOT EXISTS node_memory_replication_ownership(
                origin_id TEXT PRIMARY KEY,hub_memory_id TEXT,revision INTEGER NOT NULL,
                owned INTEGER NOT NULL DEFAULT 0,blocked INTEGER NOT NULL DEFAULT 0,publication_status TEXT)""")
            if "publication_status" not in {row["name"] for row in conn.execute("PRAGMA table_info(node_memory_replication_ownership)")}:
                conn.execute("ALTER TABLE node_memory_replication_ownership ADD COLUMN publication_status TEXT")
            conn.execute("CREATE TABLE IF NOT EXISTS node_memory_replication_config(slot INTEGER PRIMARY KEY,project_bindings TEXT NOT NULL)")
            bindings = canonical(self.project_bindings)
            previous_bindings = conn.execute("SELECT project_bindings FROM node_memory_replication_config WHERE slot=1").fetchone()
            if previous_bindings and previous_bindings[0] != bindings:
                conn.execute("UPDATE node_memory_replication_heads SET revision=revision+1")
            conn.execute("INSERT INTO node_memory_replication_config VALUES(1,?) ON CONFLICT(slot) "
                         "DO UPDATE SET project_bindings=excluded.project_bindings", (bindings,))
            self._install_triggers(conn)
            # Turning this feature on is the explicit backend replication opt-in.
            conn.execute("INSERT OR IGNORE INTO node_memory_replication_heads(origin_id,revision) "
                         "SELECT memory_id,1 FROM memory_entries")

    @contextmanager
    def _connection(self, *, write=False):
        conn = self._conn_factory()
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            if write:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _install_triggers(conn):
        for action, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS memory_replication_entries_{action.lower()}
                AFTER {action} ON memory_entries BEGIN
                INSERT INTO node_memory_replication_heads(origin_id,revision) VALUES({ref}.memory_id,1)
                ON CONFLICT(origin_id) DO UPDATE SET revision=revision+1; END""")
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS memory_replication_links_{action.lower()}
                AFTER {action} ON memory_entry_sources BEGIN
                INSERT INTO node_memory_replication_heads(origin_id,revision) VALUES({ref}.memory_id,1)
                ON CONFLICT(origin_id) DO UPDATE SET revision=revision+1; END""")
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS memory_replication_sources_{action.lower()}
                AFTER {action} ON memory_sources BEGIN
                INSERT INTO node_memory_replication_heads(origin_id,revision)
                SELECT memory_id,1 FROM memory_entry_sources WHERE source_id={ref}.source_id
                ON CONFLICT(origin_id) DO UPDATE SET revision=revision+1; END""")

    def _snapshot(self, conn, origin_id, revision):
        row = conn.execute("SELECT * FROM memory_entries WHERE memory_id=?", (origin_id,)).fetchone()
        base = {"hub_id": self.hub_id, "origin_id": origin_id, "revision": revision,
                "operation": "withdraw", "record": None, "sources": []}
        if row is not None:
            sources = conn.execute("SELECT s.* FROM memory_entry_sources l JOIN memory_sources s USING(source_id) "
                                   "WHERE l.memory_id=? ORDER BY s.source_id LIMIT 33", (origin_id,)).fetchall()
            project = self.project_bindings.get(row["project_id"]) if row["scope"] == "project" else None
            eligible = (row["status"] in {"active", "candidate"} and row["sensitivity"] in {"normal", "public"}
                        and len(row["content"]) <= 8000 and len(sources) <= 32
                        and (row["scope"] == "global" or project is not None)
                        and _live_expiry(row["expires_at"])
                        and any(s["status"] == "active" and _live_expiry(s["expires_at"]) for s in sources))
            if eligible:
                metadata = json.loads(row["metadata"])
                base.update(operation="upsert", record={
                    "content": row["content"], "memory_type": row["memory_type"], "scope": row["scope"],
                    "project_id": project, "status": row["status"], "sensitivity": row["sensitivity"],
                    "confidence": row["confidence"], "expires_at": row["expires_at"], "origin_version": row["version"],
                    "supersedes_id": row["supersedes_id"], "extraction_model": row["extraction_model"],
                    "conflict_hints": _valid_conflict_hints(metadata.get("conflict_hints"))[:8],
                    "needs_review": bool(metadata.get("needs_review")),
                }, sources=[{**{key: source[key] for key in ("source_id", "source_type", "status", "expires_at")},
                             "checksum": _safe_checksum(source["checksum"])}
                            for source in sources])
        mutation_id = "replica_" + hashlib.sha256(canonical(base).encode()).hexdigest()
        return MemoryReplicationInput.model_validate({**base, "mutation_id": mutation_id}).model_dump(mode="json")

    def collect(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid memory collection limit")
        collected = 0
        with self._connection(write=True) as conn:
            conn.create_function("memory_replica_live", 1, _live_expiry)
            # Expiry does not produce a SQLite write; invalidate exported heads
            # once so the next immutable snapshot carries an identity withdrawal.
            conn.execute("UPDATE node_memory_replication_heads SET revision=revision+1 WHERE last_operation='upsert' "
                         "AND revision=collected_revision AND EXISTS(SELECT 1 FROM memory_entries e WHERE e.memory_id=origin_id "
                         "AND (NOT memory_replica_live(e.expires_at) OR NOT EXISTS(SELECT 1 FROM memory_entry_sources l JOIN memory_sources s USING(source_id) "
                         "WHERE l.memory_id=e.memory_id AND s.status='active' AND memory_replica_live(s.expires_at))))")
            heads = conn.execute("SELECT * FROM node_memory_replication_heads WHERE revision>collected_revision "
                                 "ORDER BY rowid LIMIT ?", (limit,)).fetchall()
            for head in heads:
                try:
                    snapshot = self._snapshot(conn, head["origin_id"], head["revision"])
                except (ValueError, TypeError):
                    # Malformed or oversize private domain data cannot starve
                    # later records or leak through an unvalidated snapshot.
                    base = {"hub_id": self.hub_id, "origin_id": head["origin_id"], "revision": head["revision"],
                            "operation": "withdraw", "record": None, "sources": []}
                    snapshot = {**base, "mutation_id": "replica_" + hashlib.sha256(canonical(base).encode()).hexdigest()}
                known = conn.execute("SELECT 1 FROM node_memory_replication_outbox WHERE origin_id=?",
                                     (head["origin_id"],)).fetchone()
                conn.execute("UPDATE node_memory_replication_outbox SET status='superseded' WHERE origin_id=? AND status IN ('queued','blocked')",
                             (head["origin_id"],))
                if snapshot["operation"] == "upsert" or known:
                    conn.execute("INSERT INTO node_memory_replication_outbox(mutation_id,origin_id,revision,payload) VALUES(?,?,?,?)",
                                 (snapshot["mutation_id"], snapshot["origin_id"], snapshot["revision"], canonical(snapshot)))
                    collected += 1
                conn.execute("UPDATE node_memory_replication_heads SET collected_revision=?,last_operation=? WHERE origin_id=?",
                             (head["revision"], snapshot["operation"], head["origin_id"]))
        return collected

    def pending(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid memory delivery limit")
        with self._connection(write=True) as conn:
            conn.execute("UPDATE node_memory_replication_outbox SET status='superseded' WHERE status IN ('queued','blocked') "
                         "AND EXISTS(SELECT 1 FROM node_memory_replication_heads h WHERE h.origin_id=node_memory_replication_outbox.origin_id "
                         "AND h.revision>node_memory_replication_outbox.revision)")
            blocked = conn.execute("SELECT mutation_id,payload FROM node_memory_replication_outbox WHERE status='blocked' "
                                   "ORDER BY retry_order,rowid LIMIT 2").fetchall()
            fresh_exists = conn.execute("SELECT 1 FROM node_memory_replication_outbox WHERE status='queued' LIMIT 1").fetchone()
            retry_budget = min(len(blocked), limit - 1 if fresh_exists else limit)
            fresh = conn.execute("SELECT payload FROM node_memory_replication_outbox WHERE status='queued' "
                                 "ORDER BY rowid LIMIT ?", (limit - retry_budget,)).fetchall()
            retry_order = conn.execute("SELECT coalesce(max(retry_order),0) FROM node_memory_replication_outbox").fetchone()[0]
            for index, row in enumerate(blocked[:retry_budget], 1):
                conn.execute("UPDATE node_memory_replication_outbox SET retry_order=? WHERE mutation_id=?",
                             (retry_order + index, row["mutation_id"]))
            return [json.loads(row["payload"]) for row in [*fresh, *blocked[:retry_budget]]]

    def acknowledge(self, payload, receipt):
        item = MemoryReplicationInput.model_validate(payload)
        if (not isinstance(receipt, dict) or receipt.get("hub_id") != self.hub_id or receipt.get("node_id") != self.node_id
                or receipt.get("mutation_id") != item.mutation_id or receipt.get("origin_id") != item.origin_id
                or receipt.get("revision") != item.revision or receipt.get("status") not in {"accepted", "withdrawn", "conflict", "stale"}):
            raise SyncConflictError("invalid memory replication receipt")
        result = receipt.get("result")
        if receipt["status"] == "accepted" and (not isinstance(result, dict) or not isinstance(result.get("memory_id"), str)):
            raise SyncConflictError("invalid memory replication result")
        with self._connection(write=True) as conn:
            old = conn.execute("SELECT payload,status,receipt FROM node_memory_replication_outbox WHERE mutation_id=?",
                               (item.mutation_id,)).fetchone()
            if old is None or old["payload"] != canonical(item.model_dump(mode="json")):
                raise SyncConflictError("memory snapshot changed before acknowledgment")
            local_block = old["receipt"] and json.loads(old["receipt"]).get("local_rejection") == "cloud_http_403"
            if old["receipt"] and not local_block:
                if old["receipt"] != canonical(receipt):
                    raise SyncConflictError("memory receipt changed")
                return
            conn.execute("UPDATE node_memory_replication_outbox SET status=?,receipt=? WHERE mutation_id=?",
                         (receipt["status"], canonical(receipt), item.mutation_id))
            previous = conn.execute("SELECT revision FROM node_memory_replication_ownership WHERE origin_id=?", (item.origin_id,)).fetchone()
            if previous is None or item.revision >= previous[0]:
                blocked = receipt["status"] == "conflict" or bool(result and result.get("needs_review"))
                conn.execute("INSERT INTO node_memory_replication_ownership(origin_id,hub_memory_id,revision,blocked,publication_status) VALUES(?,?,?,?,?) "
                             "ON CONFLICT(origin_id) DO UPDATE SET hub_memory_id=excluded.hub_memory_id,revision=excluded.revision,"
                             "blocked=max(blocked,excluded.blocked),publication_status=excluded.publication_status",
                             (item.origin_id, result.get("memory_id") if result else None, item.revision, int(blocked),
                              result.get("status") if result else None))

    def reject(self, payload, reason):
        if reason not in {"cloud_http_403", "cloud_http_409", "cloud_http_422"}:
            raise ValueError("invalid replication rejection category")
        with self._connection(write=True) as conn:
            changed = conn.execute("UPDATE node_memory_replication_outbox SET status=?,receipt=? "
                                   "WHERE mutation_id=? AND payload=? AND status IN ('queued','blocked')",
                                   ("blocked" if reason == "cloud_http_403" else "rejected",
                                    canonical({"local_rejection": reason}), payload["mutation_id"], canonical(payload))).rowcount
            if not changed:
                raise SyncConflictError("replication snapshot changed before rejection")

    def local_visible(self, record):
        if record.memory_id.startswith("cloud_"):
            return True
        pending_owners = []
        with self._connection() as conn:
            current, seen = record.memory_id, set()
            for _ in range(64):
                if not current:
                    break
                if current in seen:
                    return False
                seen.add(current)
                owner = conn.execute("SELECT * FROM node_memory_replication_ownership WHERE origin_id=?", (current,)).fetchone()
                if owner:
                    if owner["owned"] or owner["blocked"]:
                        return False
                    if self.cache is not None and owner["hub_memory_id"]:
                        pending_owners.append(dict(owner))
                row = conn.execute("SELECT supersedes_id FROM memory_entries WHERE memory_id=?", (current,)).fetchone()
                current = row[0] if row else None
            else:
                if current:
                    return False
        for owner in pending_owners:
            cloud = self.cache._records(scope=record.scope, project_id=record.project_id,
                                        memory_id=owner["hub_memory_id"], limit=1)
            with self.cache.replica._connection() as mirror:
                known = mirror.execute("SELECT deleted,payload FROM replica_records WHERE kind='memory_entries' AND entity_key=?",
                                       (canonical([owner["hub_memory_id"]]),)).fetchone()
            installed_active = bool(known and not known["deleted"] and known["payload"]
                                    and json.loads(known["payload"]).get("status") == "active")
            if cloud or installed_active or known and known["deleted"] and owner["publication_status"] == "active":
                with self._connection(write=True) as conn:
                    conn.execute("UPDATE node_memory_replication_ownership SET owned=1 WHERE origin_id=?", (owner["origin_id"],))
                return False
        return True

    def status(self):
        with self._connection() as conn:
            states = dict(conn.execute("SELECT status,count(*) FROM node_memory_replication_outbox GROUP BY status"))
            dirty = conn.execute("SELECT count(*) FROM node_memory_replication_heads WHERE revision>collected_revision").fetchone()[0]
        return {"hub_id": self.hub_id, "node_id": self.node_id, "dirty": dirty,
                **dict.fromkeys(("queued", "blocked", "accepted", "withdrawn", "conflict", "stale", "rejected", "superseded"), 0),
                **states}
