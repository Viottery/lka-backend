"""Explicit shared-state projections and transactional reference journal.

The journal contains identities, never historical content. Device sync.read is
an explicit grant to the entire shared projection, not workspace-scoped access.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SharedSyncConfig(BaseModel):
    enabled: bool = False
    role: Literal["hub", "replica"] = "hub"
    hub_url: str | None = None
    hub_id: str | None = Field(default=None, min_length=1, max_length=120)
    token_env: str = Field(default="LKA_NODE_TOKEN", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    poll_seconds: float = Field(default=15, ge=1, le=3600)
    page_size: int = Field(default=100, ge=1, le=500)
    request_timeout_seconds: float = Field(default=10, ge=1, le=120)
    max_page_bytes: int = Field(default=1048576, ge=131072, le=1048576)
    memory_project_bindings: dict[str, str] = Field(default_factory=dict)
    memory_cache_stale_seconds: float = Field(default=60, ge=1, le=604800)
    memory_write_enabled: bool = False
    memory_aggregation_enabled: bool = False

    @field_validator("hub_url")
    @classmethod
    def valid_url(cls, value):
        if value is not None:
            from urllib.parse import urlsplit

            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in ("", "/")
            ):
                raise ValueError("hub_url must be an HTTPS origin")
        return value


class SyncConflictError(ValueError):
    pass


# table -> (identity columns, published columns). No wildcard hydration.
PROJECTIONS = {
    "memory_entries": (
        ("memory_id",),
        "memory_id memory_type content scope project_id status confidence sensitivity expires_at version supersedes_id created_at updated_at",
    ),
    "memory_sources": (
        ("source_id",),
        "source_id source_type checksum status expires_at created_at updated_at",
    ),
    "memory_entry_sources": (("memory_id", "source_id"), "memory_id source_id"),
    "memory_projects": (("project_id",), "project_id created_at"),
    "agent_sessions": (("session_id",), "session_id kind status title created_at updated_at"),
    "manager_controllers": (("session_id",), "session_id status revision created_at updated_at"),
    "manager_defaults": (("slot",), "slot session_id"),
    "manager_goals": (
        ("goal_id",),
        "goal_id session_id status revision objective created_at updated_at",
    ),
    "matters": (
        ("matter_id",),
        "matter_id title summary status priority due_at tags created_at updated_at revision visibility kind parent_matter_id project_id scheduled_on scheduled_start scheduled_end inbox section sort_order sections deleted_at",
    ),
    "matter_source_links": (
        ("matter_id", "source_type", "source_id"),
        "matter_id source_type source_id reason created_at",
    ),
    "matter_management": (
        ("matter_id",),
        "matter_id manager_session_id purpose responsible_party next_action wait_condition management_state revision",
    ),
    "manager_work_relations": (
        ("manager_session_id", "source_matter_id", "target_matter_id", "relation"),
        "manager_session_id source_matter_id target_matter_id relation",
    ),
    "manager_goal_matter_links": (("goal_id",), "goal_id manager_session_id matter_id"),
    "manager_delegates": (
        ("delegate_id",),
        "delegate_id session_id parent_session_id objective state revision created_at updated_at",
    ),
    "manager_deliveries": (
        ("delivery_id",),
        "delivery_id session_id execution_id run_id message_id created_at",
    ),
}
PROJECTIONS = {kind: (keys, tuple(fields.split())) for kind, (keys, fields) in PROJECTIONS.items()}
MAX_RECORD_BYTES = 131072
MAX_PAGE_BYTES = 1048576


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class NodeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    proposal_id: str = Field(min_length=1, max_length=120, pattern=r"^[\w.-]+$")
    kind: Literal["memory_candidate", "assistant_note"]
    text: str = Field(min_length=1, max_length=8000)
    target_ref: str | None = Field(default=None, max_length=256)
    base_revision: int | None = Field(default=None, ge=1)


class SharedSyncService:
    def __init__(self, conn_factory, config: SharedSyncConfig, devices=None):
        self._conn_factory, self.config, self.devices = conn_factory, config, devices
        if config.enabled and config.role == "hub":
            self.ensure_schema()

    @contextmanager
    def _connection(self, *, readonly=False):
        conn = self._conn_factory()
        conn.row_factory = sqlite3.Row
        try:
            # Read endpoints use a deferred transaction so SQLite establishes
            # a consistent snapshot without reserving the single writer slot.
            # Schema, proposal, and publication paths retain the write lock.
            conn.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _enabled(self):
        if not self.config.enabled or self.config.role != "hub":
            raise PermissionError("shared sync hub disabled")

    def _authorize(self, conn, principal, scope="sync.read"):
        if principal is not None:
            if self.devices is None:
                raise PermissionError("device authentication unavailable")
            self.devices.require_node(conn, principal, scope)

    def _reauthorize_current(self, principal, scope="sync.read"):
        # A read transaction pins its data snapshot. Recheck on a fresh
        # connection so a revocation committed during a long page read is seen.
        if principal is not None:
            with self._connection(readonly=True) as conn:
                self._authorize(conn, principal, scope)

    def ensure_schema(self):
        with self._connection() as conn:
            for statement in (
                "CREATE TABLE IF NOT EXISTS shared_sync_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS shared_sync_heads(kind TEXT NOT NULL,entity_key TEXT NOT NULL,version INTEGER NOT NULL,deleted INTEGER NOT NULL,PRIMARY KEY(kind,entity_key))",
                "CREATE TABLE IF NOT EXISTS shared_sync_journal(sequence INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,entity_key TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS shared_sync_proposals(node_id TEXT NOT NULL,proposal_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(node_id,proposal_id))",
            ):
                conn.execute(statement)
            row = conn.execute("SELECT value FROM shared_sync_meta WHERE key='hub_id'").fetchone()
            if row and self.config.hub_id and row[0] != self.config.hub_id:
                raise SyncConflictError("configured hub identity differs from stored identity")
            conn.execute(
                "INSERT OR IGNORE INTO shared_sync_meta VALUES('hub_id',?)",
                (self.config.hub_id or "hub_" + uuid4().hex,),
            )
            for kind, (keys, fields) in PROJECTIONS.items():
                columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({kind})")}
                if not set(fields) <= columns:
                    continue  # Optional domain schema is absent; retry on next startup.
                for action, prefix, deleted in (
                    ("INSERT", "NEW", 0),
                    ("UPDATE", "NEW", 0),
                    ("DELETE", "OLD", 1),
                ):
                    key = "json_array(" + ",".join(f"{prefix}.{k}" for k in keys) + ")"
                    # All SQL identifiers come exclusively from the static allowlist.
                    condition = f" WHEN {prefix}.kind='manager'" if kind == "agent_sessions" else ""
                    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS shared_sync_{kind}_{action.lower()}
                        AFTER {action} ON {kind}{condition} BEGIN
                        INSERT INTO shared_sync_heads VALUES('{kind}',{key},1,{deleted})
                        ON CONFLICT(kind,entity_key) DO UPDATE SET version=version+1,deleted={deleted};
                        INSERT INTO shared_sync_journal(kind,entity_key) VALUES('{kind}',{key}); END""")
                old_key = "json_array(" + ",".join(f"OLD.{k}" for k in keys) + ")"
                new_key = "json_array(" + ",".join(f"NEW.{k}" for k in keys) + ")"
                extra = " AND OLD.kind='manager'" if kind == "agent_sessions" else ""
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS shared_sync_{kind}_identity_update
                    AFTER UPDATE ON {kind} WHEN {old_key}!={new_key}{extra} BEGIN
                    INSERT INTO shared_sync_heads VALUES('{kind}',{old_key},1,1)
                    ON CONFLICT(kind,entity_key) DO UPDATE SET version=version+1,deleted=1;
                    INSERT INTO shared_sync_journal(kind,entity_key) VALUES('{kind}',{old_key}); END""")
                marker = "bootstrap:" + kind
                if conn.execute("SELECT 1 FROM shared_sync_meta WHERE key=?", (marker,)).fetchone():
                    continue
                key = "json_array(" + ",".join(keys) + ")"
                where = " WHERE kind='manager'" if kind == "agent_sessions" else ""
                conn.execute(
                    f"INSERT OR IGNORE INTO shared_sync_heads SELECT '{kind}',{key},1,0 FROM {kind}{where}"
                )
                conn.execute(
                    f"INSERT INTO shared_sync_journal(kind,entity_key) SELECT '{kind}',{key} FROM {kind}{where} ORDER BY {key}"
                )
                conn.execute("INSERT INTO shared_sync_meta VALUES(?,?)", (marker, "1"))

            # Derived visibility changes must get new versions even when the
            # owning business row is unchanged. These are references only.
            dependencies = [
                (
                    "memory_sources",
                    "memory_entries",
                    "json_extract(entity_key,'$[0]') IN (SELECT memory_id FROM memory_entry_sources WHERE source_id={ref}.source_id)",
                ),
                (
                    "memory_entry_sources",
                    "memory_entries",
                    "json_extract(entity_key,'$[0]')={ref}.memory_id",
                ),
                (
                    "matters",
                    "manager_goals",
                    "json_extract(entity_key,'$[0]') IN (SELECT goal_id FROM manager_goal_matter_links WHERE matter_id={ref}.matter_id)",
                ),
                (
                    "manager_goal_matter_links",
                    "manager_goals",
                    "json_extract(entity_key,'$[0]')={ref}.goal_id",
                ),
            ]
            for target in (
                "matter_management",
                "matter_source_links",
                "manager_goal_matter_links",
                "manager_work_relations",
            ):
                fields = [
                    f
                    for f in ("matter_id", "source_matter_id", "target_matter_id")
                    if f in PROJECTIONS[target][1]
                ]
                condition = " OR ".join(
                    f"json_extract(entity_key,'$[0]') IN (SELECT {PROJECTIONS[target][0][0]} FROM {target} WHERE {f}={{ref}}.matter_id)"
                    for f in fields
                )
                dependencies.append(("matters", target, condition))
            for dependent in (
                "manager_controllers",
                "manager_goals",
                "manager_delegates",
                "manager_deliveries",
                "manager_defaults",
                "matter_management",
                "manager_work_relations",
                "manager_goal_matter_links",
            ):
                key_fields = [
                    f
                    for f in ("session_id", "manager_session_id", "parent_session_id")
                    if f in PROJECTIONS[dependent][1]
                ]
                condition = " OR ".join(
                    f"json_extract(entity_key,'$[0]') IN (SELECT {PROJECTIONS[dependent][0][0]} FROM {dependent} WHERE {f}={{ref}}.session_id)"
                    for f in key_fields
                )
                if condition:
                    dependencies.append(("agent_sessions", dependent, condition))
            for source, target, condition in dependencies:
                if (
                    not conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (source,)
                    ).fetchone()
                    or not conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (target,)
                    ).fetchone()
                ):
                    continue
                if (
                    source == "matters"
                    and not conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='manager_goal_matter_links'"
                    ).fetchone()
                ):
                    continue
                for action, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                    where = condition.format(ref=ref)
                    if action == "UPDATE":
                        where = "(" + where + ") OR (" + condition.format(ref="OLD") + ")"
                    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS shared_sync_invalidate_{source}_{target}_{action.lower()} AFTER {action} ON {source} BEGIN
                        UPDATE shared_sync_heads SET version=version+1 WHERE kind='{target}' AND ({where});
                        INSERT INTO shared_sync_journal(kind,entity_key) SELECT kind,entity_key FROM shared_sync_heads WHERE kind='{target}' AND ({where}); END""")

    @staticmethod
    def _project(conn, kind, record):
        if record is None:
            return None
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        source_metadata = "metadata" in {row["name"] for row in conn.execute("PRAGMA table_info(memory_sources)")}
        if kind == "memory_sources" and record.get("source_type") == "node_memory_replica":
            if not source_metadata:
                return None
            source = conn.execute("SELECT metadata FROM memory_sources WHERE source_id=?",
                                  (record["source_id"],)).fetchone()
            try:
                publication = json.loads(source[0]) if source else None
            except (TypeError, ValueError):
                return None
            # Candidate provenance remains in the hub, but must not prolong an
            # expired active origin in an offline recall mirror.
            if (not isinstance(publication, dict) or publication.get("published_status") != "active"
                    or publication.get("needs_review")):
                return None
        if kind == "memory_entries":
            # Conflict-review metadata is private, but its suppression must
            # survive the reduced shared projection used for ordinary recall.
            try:
                metadata = json.loads(record.pop("metadata", "{}"))
            except (TypeError, ValueError):
                return None
            if not isinstance(metadata, dict) or metadata.get("needs_review"):
                return None
            source_activation = "coalesce(s.source_type,'')!='node_memory_replica'"
            if source_metadata:
                source_activation += (
                    " OR (json_valid(s.metadata) AND json_extract(s.metadata,'$.published_status')='active' "
                    "AND coalesce(json_extract(s.metadata,'$.needs_review'),0)=0)"
                )
            if (
                record["status"] != "active"
                or record["expires_at"] is not None
                and record["expires_at"] <= now
            ):
                return None
            if (
                conn.execute(
                    "SELECT 1 FROM memory_entry_sources l JOIN memory_sources s USING(source_id) "
                    "WHERE l.memory_id=? AND s.status='active' AND (s.expires_at IS NULL OR s.expires_at>?) "
                    f"AND ({source_activation}) LIMIT 1",
                    (record["memory_id"], now),
                ).fetchone()
                is None
            ):
                return None
        if kind == "agent_sessions" and (
            record["kind"] != "manager" or record["status"] == "deleted"
        ):
            return None
        if kind == "matters" and record["deleted_at"] is not None:
            return None
        session_ids = [
            record[k]
            for k in ("session_id", "manager_session_id", "parent_session_id")
            if k in record and kind != "agent_sessions"
        ]
        for session_id in session_ids:
            if (
                conn.execute(
                    "SELECT 1 FROM agent_sessions WHERE session_id=? AND kind='manager' AND status!='deleted'",
                    (session_id,),
                ).fetchone()
                is None
            ):
                return None
        matter_ids = [
            record[k]
            for k in ("matter_id", "source_matter_id", "target_matter_id")
            if k in record and kind != "matters"
        ]
        for matter_id in matter_ids:
            if (
                conn.execute(
                    "SELECT 1 FROM matters WHERE matter_id=? AND deleted_at IS NULL", (matter_id,)
                ).fetchone()
                is None
            ):
                return None
        if kind == "manager_goals":
            link = (
                conn.execute(
                    "SELECT matter_id FROM manager_goal_matter_links WHERE goal_id=?",
                    (record["goal_id"],),
                ).fetchone()
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='manager_goal_matter_links'"
                ).fetchone()
                else None
            )
            if link:
                matter = conn.execute(
                    "SELECT title,summary,deleted_at FROM matters WHERE matter_id=?", (link[0],)
                ).fetchone()
                if matter is None or matter["deleted_at"] is not None:
                    return None
                record["objective"] = matter["title"] + (
                    "\n" + matter["summary"]
                    if matter["summary"] and matter["summary"] != matter["title"]
                    else ""
                )
        return record

    def status(self, principal=None):
        self._enabled()
        with self._connection(readonly=True) as conn:
            self._authorize(conn, principal)
            result = {
                "hub_id": conn.execute(
                    "SELECT value FROM shared_sync_meta WHERE key='hub_id'"
                ).fetchone()[0],
                "cursor": conn.execute(
                    "SELECT coalesce(max(sequence),0) FROM shared_sync_journal"
                ).fetchone()[0],
            }
            self._reauthorize_current(principal)
            return result

    def pull(self, after=0, limit=100, principal=None):
        self._enabled()
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("invalid sync cursor or limit")
        with self._connection(readonly=True) as conn:
            self._authorize(conn, principal)
            high = conn.execute(
                "SELECT coalesce(max(sequence),0) FROM shared_sync_journal"
            ).fetchone()[0]
            if after > high:
                raise SyncConflictError("cursor is ahead of hub")
            result = {
                "hub_id": conn.execute(
                    "SELECT value FROM shared_sync_meta WHERE key='hub_id'"
                ).fetchone()[0],
                "after": after,
                "cursor": after,
                "high_watermark": high,
                "has_more": False,
                "changes": [],
            }
            size = 0
            memory_has_metadata = any(
                column["name"] == "metadata"
                for column in conn.execute("PRAGMA table_info(memory_entries)")
            )
            for event in conn.execute(
                "SELECT * FROM shared_sync_journal WHERE sequence>? ORDER BY sequence LIMIT ?",
                (after, limit),
            ):
                kind, key = event["kind"], event["entity_key"]
                head = conn.execute(
                    "SELECT * FROM shared_sync_heads WHERE kind=? AND entity_key=?", (kind, key)
                ).fetchone()
                keys, fields = PROJECTIONS[kind]
                if kind == "memory_entries" and memory_has_metadata:
                    fields = (*fields, "metadata")
                record = None
                if not head["deleted"]:
                    record = conn.execute(
                        f"SELECT {','.join(fields)} FROM {kind} WHERE "
                        + " AND ".join(f"{k}=?" for k in keys),
                        json.loads(key),
                    ).fetchone()
                    record = dict(record) if record is not None else None
                record = self._project(conn, kind, record)
                change = {
                    "sequence": event["sequence"],
                    "kind": kind,
                    "key": json.loads(key),
                    "version": head["version"],
                    "deleted": bool(head["deleted"]) or record is None,
                    "record": record,
                }
                length = len(canonical(change).encode())
                if length > MAX_RECORD_BYTES:
                    raise ValueError("shared record exceeds sync size limit")
                if size + length > self.config.max_page_bytes - 8192:
                    if not result["changes"]:
                        raise ValueError("shared record exceeds configured page size limit")
                    break
                result["changes"].append(change)
                result["cursor"] = event["sequence"]
                size += length
            result["has_more"] = result["cursor"] < high
            self._reauthorize_current(principal)
            return result

    def propose(self, principal, payload):
        self._enabled()
        item = NodeProposal.model_validate(payload)
        data = canonical(item.model_dump())
        with self._connection() as conn:
            self._authorize(conn, principal, "sync.propose")
            node_id = principal["node_id"]
            old = conn.execute(
                "SELECT payload,status FROM shared_sync_proposals WHERE node_id=? AND proposal_id=?",
                (node_id, item.proposal_id),
            ).fetchone()
            if old and old["payload"] != data:
                raise SyncConflictError("proposal id already has different content")
            conn.execute(
                "INSERT OR IGNORE INTO shared_sync_proposals(node_id,proposal_id,payload) VALUES(?,?,?)",
                (node_id, item.proposal_id, data),
            )
            self._authorize(conn, principal, "sync.propose")
            return {
                "node_id": node_id,
                "proposal_id": item.proposal_id,
                "status": old["status"] if old else "pending",
            }

    def proposals(self, limit=100):
        self._enabled()
        if not 1 <= limit <= 500:
            raise ValueError("invalid limit")
        with self._connection(readonly=True) as conn:
            return {
                "items": [
                    {**dict(row), "payload": json.loads(row["payload"])}
                    for row in conn.execute(
                        "SELECT * FROM shared_sync_proposals ORDER BY node_id,proposal_id LIMIT ?",
                        (limit,),
                    )
                ]
            }

    def resolve_proposal(self, node_id, proposal_id, status):
        self._enabled()
        if status not in ("rejected", "reviewed"):
            raise ValueError(
                "proposal resolution is rejected or reviewed; activation is not supported"
            )
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status FROM shared_sync_proposals WHERE node_id=? AND proposal_id=?",
                (node_id, proposal_id),
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] not in ("pending", status):
                raise SyncConflictError("proposal already resolved")
            conn.execute(
                "UPDATE shared_sync_proposals SET status=? WHERE node_id=? AND proposal_id=?",
                (status, node_id, proposal_id),
            )
            return {"node_id": node_id, "proposal_id": proposal_id, "status": status}
