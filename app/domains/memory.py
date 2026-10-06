"""Deterministic, local long-term memory storage.

MemoryService owns only memory records and their provenance. It never calls an
LLM. Call :meth:`ensure_schema` once per database path before using a service.
Project identity is stable across path renames only when explicitly rebound;
paths and directory names are never implicitly merged.

Untrusted sources may create candidate records, but cannot activate them.
Activation requires ``trusted_source=True`` on source registration or an
explicit ``user_confirmed=True`` when creating/correcting a memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryScope = Literal["global", "project"]
MemoryStatus = Literal["candidate", "active", "retracted", "superseded"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_later_expiry(candidate: str, current: str) -> bool:
    try:
        new_value = datetime.fromisoformat(candidate)
        old_value = datetime.fromisoformat(current)
        if new_value.tzinfo is None or old_value.tzinfo is None:
            return False
        return new_value.astimezone(UTC) > old_value.astimezone(UTC)
    except (TypeError, ValueError):
        return False


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _valid_conflict_hints(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    valid: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        slot, polarity, condition = item.get("slot"), item.get("polarity"), item.get("condition", "")
        if (isinstance(slot, str) and slot and isinstance(polarity, str) and polarity
                and isinstance(condition, str) and len(slot) <= 160 and len(condition) <= 160):
            valid.append({"slot": slot, "polarity": polarity, "condition": condition})
    return valid


_CONFLICT_POLARITY_PAIRS = {
    "response_detail": {"concise", "detailed"},
    "response_language": {"chinese", "english"},
    "response_structure": {"bullets", "prose"},
}


def _opposite_hints(left: dict[str, str], right: dict[str, str]) -> bool:
    if left["slot"] != right["slot"] or left["condition"] != right["condition"]:
        return False
    allowed = _CONFLICT_POLARITY_PAIRS.get(left["slot"])
    if allowed is None and left["slot"].startswith("assertion:"):
        allowed = {"positive", "negative"}
    return allowed is not None and {left["polarity"], right["polarity"]} == allowed


def memory_target_matches(target: str, content: str) -> bool:
    terms = re.findall(r"[a-z0-9_+-]{2,}|[\u3400-\u9fff]{2,}", target.casefold())
    expanded = []
    for term in terms[:8]:
        expanded.append(term)
        if re.fullmatch(r"[\u3400-\u9fff]+", term):
            expanded.extend(term[i:i + 2] for i in range(len(term) - 1))
    terms = list(dict.fromkeys(expanded))[:24]
    # A shared framing bigram (e.g. a preference verb) is not a claim identity.
    # Require substantial overlap; vague or unrelated selectors fail closed.
    minimum = max(2, (len(terms) + 1) // 2)
    return sum(t in content.casefold() for t in terms) >= minimum


class MemorySourceInput(BaseModel):
    source_type: str
    source_ref: str
    checksum: str | None = None
    excerpt_start: int | None = Field(default=None, ge=0)
    excerpt_end: int | None = Field(default=None, ge=0)
    trusted_source: bool = False
    expires_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryInput(BaseModel):
    content: str = Field(min_length=1)
    memory_type: str = "fact"
    scope: MemoryScope = "global"
    project_id: str | None = None
    source_id: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    sensitivity: str = "personal"
    expires_at: str | None = None
    dedupe_key: str | None = None
    user_confirmed: bool = False
    extraction_model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryRecord(BaseModel):
    memory_id: str
    memory_type: str
    content: str
    scope: MemoryScope
    project_id: str | None
    status: MemoryStatus
    confidence: float | None
    sensitivity: str
    source_ids: list[str]
    expires_at: str | None
    version: int
    supersedes_id: str | None
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryService:
    """SQLite memory store with scope checks, provenance, and optimistic CAS."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def ensure_schema(self) -> None:
        """Create the isolated memory schema and optional FTS5 index."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS memory_projects(
                    project_id TEXT PRIMARY KEY, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_project_paths(
                    path_key TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES memory_projects(project_id)
                );
                CREATE TABLE IF NOT EXISTS memory_sources(
                    source_id TEXT PRIMARY KEY, source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL, checksum TEXT, excerpt_start INTEGER,
                    excerpt_end INTEGER, trusted_source INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active', expires_at TEXT,
                    metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(source_type, source_ref, checksum)
                );
                CREATE TABLE IF NOT EXISTS memory_entries(
                    memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, content TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('global','project')),
                    project_id TEXT, status TEXT NOT NULL CHECK(status IN
                      ('candidate','active','retracted','superseded')),
                    confidence REAL, sensitivity TEXT NOT NULL, expires_at TEXT,
                    dedupe_key TEXT NOT NULL, version INTEGER NOT NULL,
                    supersedes_id TEXT, extraction_model TEXT, metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES memory_projects(project_id),
                    FOREIGN KEY(supersedes_id) REFERENCES memory_entries(memory_id),
                    UNIQUE(scope, project_id, dedupe_key)
                );
                CREATE TABLE IF NOT EXISTS memory_entry_sources(
                    memory_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    PRIMARY KEY(memory_id, source_id),
                    FOREIGN KEY(memory_id) REFERENCES memory_entries(memory_id),
                    FOREIGN KEY(source_id) REFERENCES memory_sources(source_id)
                );
                CREATE TABLE IF NOT EXISTS memory_events(
                    event_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, version INTEGER NOT NULL,
                    event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memory_entries(memory_id),
                    UNIQUE(memory_id, version)
                );
                CREATE TABLE IF NOT EXISTS memory_snapshots(
                    snapshot_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, version INTEGER NOT NULL,
                    record TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memory_entries(memory_id),
                    UNIQUE(memory_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_entries_scope_status
                    ON memory_entries(scope, project_id, status, expires_at);
                CREATE TABLE IF NOT EXISTS memory_learning_policies(
                    scope TEXT NOT NULL, scope_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope, scope_id)
                );
                CREATE TABLE IF NOT EXISTS memory_correction_fences(
                    watermark INTEGER NOT NULL, scope_id TEXT NOT NULL,
                    selectors TEXT NOT NULL, hints TEXT NOT NULL,
                    PRIMARY KEY(watermark, scope_id)
                );
                """
            )
            try:
                conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts
                    USING fts5(memory_id UNINDEXED, content, memory_type)""")
                conn.executescript("""
                    CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memory_entries
                    BEGIN
                        INSERT INTO memory_entries_fts(memory_id,content,memory_type)
                        VALUES(new.memory_id,new.content,new.memory_type);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE OF content,memory_type ON memory_entries
                    BEGIN
                        DELETE FROM memory_entries_fts WHERE memory_id=old.memory_id;
                        INSERT INTO memory_entries_fts(memory_id,content,memory_type)
                        VALUES(new.memory_id,new.content,new.memory_type);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memory_entries
                    BEGIN
                        DELETE FROM memory_entries_fts WHERE memory_id=old.memory_id;
                    END;
                """)
                conn.execute(
                    "INSERT INTO memory_entries_fts(memory_id,content,memory_type) "
                    "SELECT e.memory_id,e.content,e.memory_type FROM memory_entries e "
                    "WHERE NOT EXISTS(SELECT 1 FROM memory_entries_fts f WHERE f.memory_id=e.memory_id)"
                )
            except sqlite3.OperationalError:
                pass  # Search has a deterministic LIKE fallback.
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def fence_prior_publication(
        self, *, source_message_id: str | None, user_input: str,
        selectors: list[str], hints: list[dict[str, str]],
        project_id: str | None,
    ) -> None:
        """Persist a correction before recall; later user sources remain eligible."""
        if source_message_id is None:
            # Legacy two-argument gates can retract published state, but lack
            # authority to fence an unidentified persisted user source.
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_session_messages'").fetchone():
                raise ValueError("Persisted correction source is unavailable")
            row = conn.execute(
                "SELECT rowid,role,content FROM agent_session_messages WHERE message_id=?",
                (source_message_id,),
            ).fetchone()
            if row is None or row["role"] != "user" or row["content"] != user_input:
                raise ValueError("Persisted correction source does not match user input")
            for scope_id in ["global", *([project_id] if project_id else [])]:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_correction_fences VALUES(?,?,?,?)",
                    (row[0], scope_id, _json(selectors), _json(hints)),
                )
            conn.commit()
        finally:
            conn.close()

    def _check_correction_fences(self, conn: sqlite3.Connection, source: sqlite3.Row, payload: MemoryInput) -> None:
        if source["source_type"] != "user_message":
            return
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_session_messages'").fetchone():
            return
        row = conn.execute("SELECT rowid FROM agent_session_messages WHERE message_id=? AND role='user'",
                           (source["source_ref"],)).fetchone()
        if row is None:
            return
        for fence in conn.execute(
            # Include the correction's own source: repeating a rejected claim
            # in a forget instruction is not a new positive assertion.
            "SELECT selectors,hints FROM memory_correction_fences WHERE scope_id=? AND watermark>=?",
            (payload.project_id or "global", row[0]),
        ):
            if (any(memory_target_matches(t, payload.content) for t in json.loads(fence["selectors"]))
                    or any(_opposite_hints(old, new)
                           for old in _valid_conflict_hints(payload.metadata.get("conflict_hints"))
                           for new in _valid_conflict_hints(json.loads(fence["hints"])))):
                raise MemoryPublicationSuppressed("Earlier source superseded by user correction")

    def set_learning_enabled(
        self, *, scope: MemoryScope, project_id: str | None, enabled: bool,
    ) -> None:
        if scope == "project" and not project_id:
            raise ValueError("project_id required for project learning policy")
        if scope == "global" and project_id is not None:
            raise ValueError("global learning policy cannot have project_id")
        scope_id = project_id or "global"
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO memory_learning_policies(scope,scope_id,enabled,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(scope,scope_id) DO UPDATE SET "
                "enabled=excluded.enabled,updated_at=excluded.updated_at",
                (scope, scope_id, int(enabled), _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def learning_enabled(self, *, scope: MemoryScope, project_id: str | None = None) -> bool:
        if scope == "project" and not project_id:
            return False
        scope_id = project_id or "global"
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT enabled FROM memory_learning_policies WHERE scope=? AND scope_id=?",
                (scope, scope_id),
            ).fetchone()
            return row is None or bool(row["enabled"])
        finally:
            conn.close()

    @staticmethod
    def _path_key(path: str | Path) -> str:
        resolved = str(Path(path).expanduser().resolve(strict=False))
        return resolved.casefold() if os.name == "nt" else resolved

    def resolve_project(self, workspace_path: str | Path, *, create: bool = True) -> str | None:
        """Resolve a path to its stable project ID; unrelated paths never merge."""
        key = self._path_key(workspace_path)
        conn = self._connect()
        try:
            if create:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT project_id FROM memory_project_paths WHERE path_key=? AND active=1", (key,)
            ).fetchone()
            if row:
                if create:
                    conn.commit()
                return str(row["project_id"])
            if not create:
                return None
            project_id = f"project_{uuid.uuid4().hex}"
            now = _now()
            conn.execute("INSERT INTO memory_projects VALUES(?,?)", (project_id, now))
            conn.execute("INSERT INTO memory_project_paths VALUES(?,?,1,?) ON CONFLICT(path_key) DO UPDATE SET project_id=excluded.project_id,active=1,created_at=excluded.created_at", (key, project_id, now))
            conn.commit()
            return project_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def bind_project_path(self, project_id: str, workspace_path: str | Path, *, old_path: str | Path | None = None) -> None:
        """Explicitly map a renamed path, optionally deactivating its old mapping."""
        key = self._path_key(workspace_path)
        now = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM memory_projects WHERE project_id=?", (project_id,)).fetchone() is None:
                raise KeyError(f"Unknown project: {project_id}")
            owner = conn.execute("SELECT project_id FROM memory_project_paths WHERE path_key=? AND active=1", (key,)).fetchone()
            if owner and owner["project_id"] != project_id:
                raise ValueError("Workspace path is already bound to another project")
            if old_path is not None:
                previous = conn.execute("SELECT project_id FROM memory_project_paths WHERE path_key=? AND active=1", (self._path_key(old_path),)).fetchone()
                if previous is None or previous["project_id"] != project_id:
                    raise ValueError("Old workspace no longer belongs to this project")
                conn.execute("UPDATE memory_project_paths SET active=0 WHERE path_key=? AND project_id=?", (self._path_key(old_path), project_id))
            conn.execute("""INSERT INTO memory_project_paths(path_key,project_id,active,created_at)
                VALUES(?,?,1,?) ON CONFLICT(path_key) DO UPDATE SET project_id=excluded.project_id,
                active=1""", (key, project_id, now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register_source(self, payload: MemorySourceInput) -> str:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            source_id = self.register_source_in_transaction(conn, payload)
            conn.commit()
            return source_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register_source_in_transaction(
        self, conn: sqlite3.Connection, payload: MemorySourceInput,
    ) -> str:
        """Register provenance inside a caller-owned write transaction."""
        now = _now()
        identity = _json([payload.source_type, payload.source_ref, payload.checksum])
        source_id = _stable_id("msrc", identity)
        conn.execute("""INSERT INTO memory_sources(source_id,source_type,source_ref,checksum,
                excerpt_start,excerpt_end,trusted_source,status,expires_at,metadata,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'active',?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                excerpt_start=excluded.excerpt_start, excerpt_end=excluded.excerpt_end,
                trusted_source=MAX(memory_sources.trusted_source,excluded.trusted_source),
                status=memory_sources.status, expires_at=excluded.expires_at, metadata=excluded.metadata,
                updated_at=excluded.updated_at""",
                (source_id,payload.source_type,payload.source_ref,payload.checksum,payload.excerpt_start,
                 payload.excerpt_end,int(payload.trusted_source),payload.expires_at,_json(payload.metadata),now,now))
        return source_id

    def revoke_source(self, source_id: str, *, expected_version: int | None = None) -> int:
        """Revoke provenance and retract claims lacking any other valid source."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = self.revoke_source_in_transaction(conn, source_id, expected_version=expected_version)
            conn.commit()
            return changed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def revoke_source_in_transaction(
        self, conn: sqlite3.Connection, source_id: str,
        *, expected_version: int | None = None,
    ) -> int:
        source = conn.execute(
            "SELECT status FROM memory_sources WHERE source_id=?", (source_id,),
        ).fetchone()
        if source is None:
            raise KeyError(f"Unknown source: {source_id}")
        if source["status"] == "revoked":
            return 0
        now = _now()
        conn.execute(
            "UPDATE memory_sources SET status='revoked',updated_at=? WHERE source_id=?",
            (now, source_id),
        )
        ids = [r["memory_id"] for r in conn.execute(
            "SELECT memory_id FROM memory_entry_sources WHERE source_id=?", (source_id,),
        )]
        changed = 0
        for memory_id in ids:
            memory = conn.execute(
                "SELECT status FROM memory_entries WHERE memory_id=?", (memory_id,),
            ).fetchone()
            if memory is None or memory["status"] in {"retracted", "superseded"}:
                continue
            other = conn.execute(
                "SELECT 1 FROM memory_entry_sources es JOIN memory_sources s USING(source_id) "
                "WHERE es.memory_id=? AND s.status='active' "
                "AND (s.expires_at IS NULL OR s.expires_at>?) LIMIT 1",
                (memory_id, now),
            ).fetchone()
            if other is not None:
                continue
            self._transition(
                conn, memory_id, "retracted", "source_revoked",
                {"source_id": source_id}, expected_version if len(ids) == 1 else None,
            )
            changed += 1
        self._refresh_conflict_metadata(conn)
        return changed

    @staticmethod
    def _literal_alias_source(conn: sqlite3.Connection, source: sqlite3.Row, evidence: str) -> bool:
        if source["source_type"] != "user_message" or source["trusted_source"]:
            return False
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_session_messages'").fetchone():
            return False
        message = conn.execute("SELECT content FROM agent_session_messages WHERE message_id=? AND role='user'",
                               (source["source_ref"],)).fetchone()
        return bool(message and evidence in message[0]
                    and source["checksum"] == hashlib.sha256(message[0].encode("utf-8")).hexdigest())

    def _publication_alias(
        self, conn: sqlite3.Connection, payload: MemoryInput, source: sqlite3.Row,
    ) -> sqlite3.Row | None:
        """Only one terminal Chinese sentence mark, proven by identical literal evidence.

        Called within the fenced publication transaction, never a migration or
        semantic merge. Both original strings and legacy IDs remain untouched.
        """
        evidence = payload.metadata.get("evidence")
        if (payload.extraction_model != "configured_llm_v1" or payload.user_confirmed
                or not isinstance(evidence, str) or not 2 <= len(evidence) <= 500
                or not evidence.endswith("。") or evidence.endswith("。。")
                or payload.content not in (evidence, evidence[:-1])
                or not self._literal_alias_source(conn, source, evidence)):
            return None
        matches = conn.execute(
            "SELECT * FROM memory_entries WHERE scope=? AND project_id IS ? AND memory_type=? "
            "AND extraction_model=? AND content IN (?,?) AND json_extract(metadata,'$.evidence')=? "
            "ORDER BY status IN ('retracted','superseded') DESC,memory_id LIMIT 3",
            (payload.scope, payload.project_id, payload.memory_type, payload.extraction_model,
             evidence, evidence[:-1], evidence),
        ).fetchall()
        if any(row["status"] in {"retracted", "superseded"} for row in matches):
            raise MemoryPublicationSuppressed("publication_alias_tombstone")
        if len(matches) > 1:
            raise MemoryPublicationSuppressed("publication_alias_ambiguous")
        if not matches:
            return None
        prior_sources = conn.execute(
            "SELECT s.* FROM memory_entry_sources es JOIN memory_sources s USING(source_id) WHERE es.memory_id=?",
            (matches[0]["memory_id"],),
        ).fetchall()
        if (not prior_sources or not all(self._literal_alias_source(conn, s, evidence) for s in prior_sources)
                or not any(self._source_valid(s, _now()) for s in prior_sources)):
            return None
        return matches[0]

    def create(
        self,
        payload: MemoryInput,
        *,
        publication_lease: tuple[str, str, int] | None = None,
    ) -> MemoryRecord:
        content = payload.content.strip()
        if not content:
            raise ValueError("Memory content cannot be blank")
        if payload.scope == "project" and not payload.project_id:
            raise ValueError("Project-scoped memory requires project_id")
        if payload.scope == "global" and payload.project_id is not None:
            raise ValueError("Global memory cannot have project_id")
        dedupe_key = payload.dedupe_key or hashlib.sha256(content.casefold().encode()).hexdigest()
        memory_id = _stable_id("mem", _json([payload.scope,payload.project_id,dedupe_key]))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if publication_lease is not None:
                self._validate_publication_lease(conn, publication_lease)
            now = _now()
            source = conn.execute("SELECT * FROM memory_sources WHERE source_id=?", (payload.source_id,)).fetchone()
            if source is None:
                raise KeyError(f"Unknown source: {payload.source_id}")
            if not self._source_valid(source, now):
                raise ValueError("Source is revoked or expired")
            self._check_correction_fences(conn, source, payload)
            alias = self._publication_alias(conn, payload, source) if publication_lease is not None else None
            if alias is not None:
                memory_id = alias["memory_id"]
            status: MemoryStatus = "active" if payload.user_confirmed or source["trusted_source"] else "candidate"
            existing = conn.execute("SELECT * FROM memory_entries WHERE memory_id=?", (memory_id,)).fetchone()
            if existing:
                prior_source = conn.execute(
                    "SELECT 1 FROM memory_entry_sources WHERE memory_id=? AND source_id=?",
                    (memory_id, payload.source_id),
                ).fetchone() is not None
                # Retried publication from one source is idempotent. The only
                # same-source state change permitted is a new explicit confirmation.
                if prior_source and not (existing["status"] == "candidate" and status == "active"):
                    if publication_lease is not None:
                        self._validate_publication_lease(conn, publication_lease)
                    conn.commit()
                    return self.get(memory_id)
                if existing["status"] in {"retracted", "superseded"}:
                    if publication_lease is not None:
                        self._validate_publication_lease(conn, publication_lease)
                    conn.commit()
                    return self.get(memory_id)
                if not prior_source:
                    conn.execute("INSERT INTO memory_entry_sources VALUES(?,?)", (memory_id,payload.source_id))
                self._snapshot(conn, existing)
                independent_user_evidence = conn.execute(
                    "SELECT COUNT(DISTINCT s.source_ref) FROM memory_entry_sources es "
                    "JOIN memory_sources s USING(source_id) WHERE es.memory_id=? "
                    "AND s.status='active' AND (s.expires_at IS NULL OR s.expires_at>?) "
                    "AND s.source_type IN ('user_message','conversation')",
                    (memory_id, now),
                ).fetchone()[0]
                promote_by_repeat = (
                    existing["status"] == "candidate"
                    and existing["memory_type"] == "preference"
                    and float(existing["confidence"] or 0) >= 0.75
                    and payload.confidence is not None and payload.confidence >= 0.75
                    and independent_user_evidence >= 2
                    and not prior_source
                    and not json.loads(existing["metadata"]).get("needs_review")
                )
                promote = (
                    existing["status"] == "candidate"
                    and not json.loads(existing["metadata"]).get("needs_review")
                    and (status == "active" or promote_by_repeat)
                )
                old_expiry = existing["expires_at"]
                expiry_extended = bool(
                    payload.user_confirmed and payload.expires_at and old_expiry
                    and _is_later_expiry(payload.expires_at, old_expiry)
                )
                effective_expiry = payload.expires_at if expiry_extended else old_expiry
                duplicate_version = max(int(existing["version"]), int(conn.execute(
                    "SELECT COALESCE(MAX(version),0) FROM memory_events WHERE memory_id=?",
                    (memory_id,),
                ).fetchone()[0])) + 1
                conn.execute(
                    "UPDATE memory_entries SET version=?,updated_at=?,status=?,expires_at=? WHERE memory_id=?",
                    (duplicate_version, now, "active" if promote else existing["status"], effective_expiry, memory_id),
                )
                event = (
                    "promoted_by_confirmation" if status == "active" and promote
                    else "promoted_by_independent_evidence" if promote_by_repeat
                    else "expiry_extended" if expiry_extended
                    else "duplicate_source_linked"
                )
                self._record_event(conn,memory_id,duplicate_version,event,{
                    "source_id":payload.source_id,
                    "independent_user_sources": int(independent_user_evidence),
                    **({"previous_expires_at": old_expiry, "expires_at": effective_expiry}
                       if expiry_extended else {}),
                    **({"publication_alias": "terminal_chinese_period",
                        "incoming_claim": payload.content,
                        "incoming_evidence": payload.metadata["evidence"]} if alias is not None else {}),
                })
                if publication_lease is not None:
                    self._validate_publication_lease(conn, publication_lease)
                conn.commit()
                return self.get(memory_id)
            metadata = dict(payload.metadata)
            hints = _valid_conflict_hints(metadata.get("conflict_hints")) if payload.memory_type == "preference" else []
            conflicts = self._find_preference_conflicts(
                conn, payload, hints, now,
            )
            if conflicts:
                metadata["needs_review"] = True
                metadata["conflict_ids"] = sorted({row["memory_id"] for row, _hint in conflicts})
                if not payload.user_confirmed:
                    status = "candidate"
            conn.execute("""INSERT INTO memory_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (memory_id,payload.memory_type,content,payload.scope,payload.project_id,status,payload.confidence,
                 payload.sensitivity,payload.expires_at,dedupe_key,1,None,payload.extraction_model,_json(metadata),now,now))
            conn.execute("INSERT INTO memory_entry_sources VALUES(?,?)", (memory_id,payload.source_id))
            self._record_event(conn,memory_id,1,"created",{"status":status,"source_id":payload.source_id,"content":content,
                **({"needs_review": True, "conflict_ids": metadata["conflict_ids"]} if conflicts else {})})
            for peer, hint in (conflicts if payload.user_confirmed else ()):
                peer_metadata = json.loads(peer["metadata"])
                peer_ids = sorted(set(peer_metadata.get("conflict_ids", [])) | {memory_id})
                peer_metadata.update(needs_review=True, conflict_ids=peer_ids)
                self._snapshot(conn, peer)
                version = int(peer["version"]) + 1
                conn.execute("UPDATE memory_entries SET metadata=?,version=?,updated_at=? WHERE memory_id=?",
                             (_json(peer_metadata), version, now, peer["memory_id"]))
                self._record_event(conn, peer["memory_id"], version, "conflict_detected", {
                    "with_memory_id": memory_id, "slot": hint["slot"], "condition": hint["condition"],
                })
            if publication_lease is not None:
                self._validate_publication_lease(conn, publication_lease)
            conn.commit()
            return self.get(memory_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _validate_publication_lease(
        conn: sqlite3.Connection, lease: tuple[str, str, int],
    ) -> None:
        if (
            not isinstance(lease, tuple) or len(lease) != 3
            or not isinstance(lease[0], str) or not lease[0]
            or not isinstance(lease[1], str) or not lease[1]
            or isinstance(lease[2], bool) or not isinstance(lease[2], int)
        ):
            raise MemoryPublicationLeaseError("publication_lease_invalid")
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(background_jobs)")}
        except sqlite3.Error as exc:
            raise MemoryPublicationLeaseError("publication_lease_unavailable") from exc
        required = {"job_id", "status", "lease_owner", "lease_epoch", "lease_expires_at", "deadline"}
        if not required.issubset(columns):
            raise MemoryPublicationLeaseError("publication_lease_unavailable")
        job_id, owner, epoch = lease
        row = conn.execute(
            "SELECT status,lease_owner,lease_epoch,lease_expires_at,deadline "
            "FROM background_jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if row is None:
            raise MemoryPublicationLeaseError("publication_lease_missing")
        now = _now()
        if row["status"] != "running":
            raise MemoryPublicationLeaseError("publication_lease_not_running")
        if row["lease_owner"] != owner or int(row["lease_epoch"]) != epoch:
            raise MemoryPublicationLeaseError("publication_lease_fenced")
        if not row["lease_expires_at"] or row["lease_expires_at"] <= now:
            raise MemoryPublicationLeaseError("publication_lease_expired")
        if row["deadline"] is not None and row["deadline"] <= now:
            raise MemoryPublicationLeaseError("publication_deadline_exceeded")

    def get(self, memory_id: str) -> MemoryRecord:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM memory_entries WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(f"Memory not found: {memory_id}")
            return self._record(conn,row)
        finally:
            conn.close()

    def get_active(self, memory_id: str) -> MemoryRecord | None:
        """Return current active state only while its entry and a source are live."""
        now = _now()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT e.* FROM memory_entries e WHERE e.memory_id=? AND e.status='active' "
                "AND (e.expires_at IS NULL OR e.expires_at>?) "
                "AND EXISTS (SELECT 1 FROM memory_entry_sources es "
                "JOIN memory_sources s USING(source_id) WHERE es.memory_id=e.memory_id "
                "AND s.status='active' AND (s.expires_at IS NULL OR s.expires_at>?))",
                (memory_id, now, now),
            ).fetchone()
            if row is None:
                return None
            record = self._record(conn, row)
            return None if record.metadata.get("needs_review") else record
        finally:
            conn.close()

    def get_recallable(self, memory_id: str) -> MemoryRecord | None:
        """Compatibility alias for exact-ID active/source-valid lookup."""
        return self.get_active(memory_id)

    def list(self, *, scope: MemoryScope | None = None, project_id: str | None = None,
             statuses: tuple[MemoryStatus, ...] = ("active",), include_expired: bool = False,
             limit: int = 100, offset: int = 0) -> list[MemoryRecord]:
        now = _now()
        clauses = ["1=1"]
        args: list[Any] = []
        if scope:
            clauses.append("scope=?"); args.append(scope)
        if scope == "project":
            clauses.append("project_id=?"); args.append(project_id or "")
        elif scope is None and project_id is not None:
            clauses.append("(scope='global' OR (scope='project' AND project_id=?))"); args.append(project_id)
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")"); args.extend(statuses)
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at>?)"); args.append(now)
            clauses.append("EXISTS (SELECT 1 FROM memory_entry_sources es JOIN memory_sources s USING(source_id) WHERE es.memory_id=memory_entries.memory_id AND s.status='active' AND (s.expires_at IS NULL OR s.expires_at>?))"); args.append(now)
        args.append(max(1,min(int(limit),1000)))
        args.append(max(0, int(offset)))
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM memory_entries WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC,memory_id LIMIT ? OFFSET ?", args).fetchall()
            return [self._record(conn,row) for row in rows]
        finally:
            conn.close()

    def search(self, query: str, *, scope: MemoryScope | None = None, project_id: str | None = None,
               limit: int = 20) -> list[MemoryRecord]:
        terms = query.strip()
        if not terms:
            return []
        if scope is None:
            # Compatibility with the domain-service API: an explicit project ID
            # selects that project; otherwise default to global only, never all
            # projects at once.
            scope = "project" if project_id else "global"
        if scope == "project" and not project_id:
            return []
        now = _now()
        conn = self._connect()
        try:
            fts_ids: list[str] = []
            try:
                # Quote every term as a literal FTS phrase; malformed input
                # must never widen the query or bypass the scope filter.
                fts_query = " ".join('"' + part.replace('"', '""') + '"'
                                     for part in terms.split()[:8])
                fts_ids = [row[0] for row in conn.execute(
                    "SELECT memory_id FROM memory_entries_fts WHERE memory_entries_fts MATCH ? LIMIT 200",
                    (fts_query,),
                )]
            except sqlite3.OperationalError:
                pass
            escaped = terms.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses = [
                "e.scope=?", "e.status='active'", "(e.expires_at IS NULL OR e.expires_at>?)",
                ("EXISTS(SELECT 1 FROM memory_entry_sources es "
                 "JOIN memory_sources s USING(source_id) WHERE es.memory_id=e.memory_id "
                 "AND s.status='active' AND (s.expires_at IS NULL OR s.expires_at>?))"),
            ]
            params: list[Any] = [scope, now, now]
            if scope == "project":
                clauses.append("e.project_id=?")
                params.append(project_id)
            match_clause = "e.content LIKE ? ESCAPE '\\'"
            params.append(f"%{escaped}%")
            if fts_ids:
                match_clause += " OR e.memory_id IN (" + ",".join("?" for _ in fts_ids) + ")"
                params.extend(fts_ids)
            clauses.append("(" + match_clause + ")")
            params.append(max(1, min(int(limit), 100)))
            rows = conn.execute(
                "SELECT e.* FROM memory_entries e WHERE " + " AND ".join(clauses)
                + " ORDER BY e.updated_at DESC LIMIT ?", params,
            ).fetchall()
            return [self._record(conn, row) for row in rows]
        finally:
            conn.close()

    def add_source(self, memory_id: str, *, source_id: str, expected_version: int,
                   publication_lease: tuple[str, str, int] | None = None,
                   user_confirmed: bool = False) -> MemoryRecord:
        """Link equivalent evidence without depending on model paraphrase hashes."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if publication_lease is not None:
                self._validate_publication_lease(conn, publication_lease)
            row = conn.execute("SELECT * FROM memory_entries WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(memory_id)
            if int(row["version"]) != expected_version:
                raise MemoryConflictError("Memory changed while reconciling its sources")
            if row["status"] in {"retracted", "superseded"}:
                raise MemoryPublicationSuppressed("Cannot revive an inactive memory")
            payload = MemoryInput(
                content=row["content"], memory_type=row["memory_type"], scope=row["scope"],
                project_id=row["project_id"], source_id=source_id, confidence=row["confidence"],
                sensitivity=row["sensitivity"], expires_at=row["expires_at"],
                dedupe_key=row["dedupe_key"], metadata=json.loads(row["metadata"]),
                user_confirmed=user_confirmed,
            )
            now = _now()
            source = conn.execute("SELECT * FROM memory_sources WHERE source_id=?", (source_id,)).fetchone()
            if source is None or not self._source_valid(source, now):
                raise MemoryPublicationSuppressed("Reconciliation source is unavailable")
            self._check_correction_fences(conn, source, payload)
            linked = conn.execute("SELECT 1 FROM memory_entry_sources WHERE memory_id=? AND source_id=?",
                                  (memory_id, source_id)).fetchone()
            promote = user_confirmed and row["status"] == "candidate" and not payload.metadata.get("needs_review")
            if not linked or promote:
                self._snapshot(conn, row)
                conn.execute("INSERT OR IGNORE INTO memory_entry_sources VALUES(?,?)", (memory_id, source_id))
                conn.execute("UPDATE memory_entries SET version=version+1,updated_at=?,status=? WHERE memory_id=?",
                             (now, "active" if promote else row["status"], memory_id))
                self._record_event(conn, memory_id, expected_version + 1,
                                   "promoted_by_confirmation" if promote else "duplicate_source_linked",
                                   {"source_id": source_id})
            if publication_lease is not None:
                self._validate_publication_lease(conn, publication_lease)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get(memory_id)

    def correct(self, memory_id: str, *, content: str, expected_version: int,
                source_id: str | None = None, user_confirmed: bool = True,
                publication_lease: tuple[str, str, int] | None = None,
                metadata: dict[str, Any] | None = None,
                expires_at: str | None = None, require_active: bool = False) -> MemoryRecord:
        """Create a new version and preserve the old claim as superseded history."""
        content = content.strip()
        if not content:
            raise ValueError("Corrected content cannot be blank")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if publication_lease is not None:
                self._validate_publication_lease(conn, publication_lease)
            old = conn.execute("SELECT * FROM memory_entries WHERE memory_id=?", (memory_id,)).fetchone()
            if old is None:
                raise KeyError(f"Memory not found: {memory_id}")
            if int(old["version"]) != expected_version:
                raise MemoryConflictError(f"Expected version {expected_version}, found {old['version']}")
            if require_active and old["status"] != "active":
                raise MemoryPublicationSuppressed("Reconciliation requires an active target")
            if publication_lease is not None and old["status"] in {"retracted", "superseded"}:
                raise MemoryPublicationSuppressed("Cannot revive an inactive memory")
            if source_id:
                source = conn.execute("SELECT * FROM memory_sources WHERE source_id=?", (source_id,)).fetchone()
                if source is None or not self._source_valid(source,_now()):
                    raise ValueError("Correction source is unavailable")
                if publication_lease is not None:
                    self._check_correction_fences(conn, source, MemoryInput(
                        content=content, source_id=source_id, scope=old["scope"],
                        project_id=old["project_id"], metadata=metadata or {},
                    ))
            old_record = self._record(conn,old)
            self._snapshot(conn,old)
            conn.execute("UPDATE memory_entries SET status='superseded',version=version+1,updated_at=? WHERE memory_id=? AND version=?", (_now(),memory_id,expected_version))
            self._record_event(conn,memory_id,expected_version+1,"corrected_by",{"content":content})
            self._clear_conflict_references(conn, memory_id)
            now = _now()
            new_id = uuid.uuid4().hex
            new_source = source_id or old_record.source_ids[0]
            source_row = conn.execute("SELECT trusted_source FROM memory_sources WHERE source_id=? AND status='active'", (new_source,)).fetchone()
            if source_row is None:
                raise ValueError("Correction source is unavailable")
            status = "active" if user_confirmed or source_row["trusted_source"] else "candidate"
            dedupe = hashlib.sha256(content.casefold().encode()).hexdigest()
            corrected_metadata = json.loads(old["metadata"])
            for key in ("needs_review", "conflict_ids", "conflict_hints"):
                corrected_metadata.pop(key, None)
            corrected_metadata.update(metadata or {})
            conn.execute("""INSERT INTO memory_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id,old["memory_type"],content,old["scope"],old["project_id"],status,old["confidence"],old["sensitivity"],expires_at if metadata is not None else old["expires_at"],dedupe,1,memory_id,old["extraction_model"],_json(corrected_metadata),now,now))
            conn.execute("INSERT INTO memory_entry_sources VALUES(?,?)",(new_id,new_source))
            self._record_event(conn,new_id,1,"created_by_correction",{"supersedes_id":memory_id,"source_id":new_source,"content":content})
            if publication_lease is not None:
                self._validate_publication_lease(conn, publication_lease)
            conn.commit()
            return self.get(new_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def retract(self, memory_id: str, *, expected_version: int) -> MemoryRecord:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._transition(conn,memory_id,"retracted","retracted",{},expected_version)
            self._clear_conflict_references(conn, memory_id)
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()
        return self.get(memory_id)

    def export(self, *, scope: MemoryScope | None = None, project_id: str | None = None,
               include_candidates: bool = False) -> dict[str, Any]:
        statuses: tuple[MemoryStatus,...] = ("active","candidate") if include_candidates else ("active",)
        records = self.list(scope=scope,project_id=project_id,statuses=statuses,limit=1000)
        return {"version":1,"exported_at":_now(),"memories":[r.model_dump(mode="json") for r in records]}

    def events(self, memory_id: str) -> list[dict[str, Any]]:
        conn=self._connect()
        try:
            rows=conn.execute("SELECT * FROM memory_events WHERE memory_id=? ORDER BY version",(memory_id,)).fetchall()
            return [{"event_id":r["event_id"],"version":r["version"],"event_type":r["event_type"],"payload":json.loads(r["payload"]),"created_at":r["created_at"]} for r in rows]
        finally: conn.close()

    def sources_for(self, memory_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT s.* FROM memory_sources s JOIN memory_entry_sources es USING(source_id) WHERE es.memory_id=? ORDER BY s.created_at,s.source_id", (memory_id,)).fetchall()
            return [{key: row[key] for key in ("source_id", "source_type", "source_ref", "checksum",
                                              "status", "expires_at", "created_at", "updated_at")}
                    for row in rows]
        finally:
            conn.close()

    def _transition(self, conn: sqlite3.Connection, memory_id: str, status: MemoryStatus,
                    event_type: str, payload: dict[str,Any], expected_version: int | None) -> None:
        row=conn.execute("SELECT * FROM memory_entries WHERE memory_id=?",(memory_id,)).fetchone()
        if row is None: raise KeyError(f"Memory not found: {memory_id}")
        if expected_version is not None and int(row["version"])!=expected_version:
            raise MemoryConflictError(f"Expected version {expected_version}, found {row['version']}")
        version=int(row["version"])+1
        self._snapshot(conn,row)
        changed=conn.execute("UPDATE memory_entries SET status=?,version=?,updated_at=? WHERE memory_id=? AND version=?",(status,version,_now(),memory_id,row["version"])).rowcount
        if changed != 1: raise MemoryConflictError("Memory changed concurrently")
        self._record_event(conn,memory_id,version,event_type,payload)

    @staticmethod
    def _record_event(conn: sqlite3.Connection,memory_id: str,version: int,event_type: str,payload: dict[str,Any]) -> None:
        conn.execute("INSERT INTO memory_events VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,memory_id,version,event_type,_json(payload),_now()))

    @staticmethod
    def _snapshot(conn: sqlite3.Connection,row: sqlite3.Row) -> None:
        conn.execute("INSERT OR IGNORE INTO memory_snapshots VALUES(?,?,?,?,?)",(uuid.uuid4().hex,row["memory_id"],row["version"],_json(dict(row)),_now()))

    @staticmethod
    def _find_preference_conflicts(conn: sqlite3.Connection, payload: MemoryInput,
                                   hints: list[dict[str, str]], now: str) -> list[tuple[sqlite3.Row, dict[str, str]]]:
        if not hints:
            return []
        rows = conn.execute(
            "SELECT e.* FROM memory_entries e WHERE e.memory_type='preference' AND e.status='active' "
            "AND e.scope=? AND e.project_id IS ? AND (e.expires_at IS NULL OR e.expires_at>?) "
            "AND EXISTS (SELECT 1 FROM memory_entry_sources es JOIN memory_sources s USING(source_id) "
            "WHERE es.memory_id=e.memory_id AND s.status='active' AND (s.expires_at IS NULL OR s.expires_at>?))",
            (payload.scope, payload.project_id, now, now),
        ).fetchall()
        matches: list[tuple[sqlite3.Row, dict[str, str]]] = []
        for row in rows:
            for old_hint in _valid_conflict_hints(json.loads(row["metadata"]).get("conflict_hints")):
                for hint in hints:
                    if _opposite_hints(old_hint, hint):
                        matches.append((row, hint))
                        break
                else:
                    continue
                break
        return matches

    @staticmethod
    def _clear_conflict_references(conn: sqlite3.Connection, withdrawn_id: str) -> None:
        rows = conn.execute("SELECT * FROM memory_entries WHERE status IN ('active','candidate')").fetchall()
        now = _now()
        for row in rows:
            metadata = json.loads(row["metadata"])
            ids = metadata.get("conflict_ids")
            if not metadata.get("needs_review") or not isinstance(ids, list) or withdrawn_id not in ids:
                continue
            remaining = sorted({str(item) for item in ids if item != withdrawn_id})
            if remaining:
                metadata["conflict_ids"] = remaining
            else:
                metadata.pop("conflict_ids", None)
                metadata.pop("needs_review", None)
            MemoryService._snapshot(conn, row)
            version = int(row["version"]) + 1
            conn.execute("UPDATE memory_entries SET metadata=?,version=?,updated_at=? WHERE memory_id=?",
                         (_json(metadata), version, now, row["memory_id"]))
            MemoryService._record_event(conn, row["memory_id"], version, "conflict_resolved_by_retraction",
                                        {"withdrawn_memory_id": withdrawn_id, "remaining_conflict_ids": remaining})
        MemoryService._refresh_conflict_metadata(conn)

    @staticmethod
    def _effective_metadata(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        metadata = json.loads(row["metadata"])
        conflict_ids = metadata.get("conflict_ids")
        if not metadata.get("needs_review") or not isinstance(conflict_ids, list):
            return metadata
        now = _now()
        own_hints = _valid_conflict_hints(metadata.get("conflict_hints"))
        valid_ids: list[str] = []
        for conflict_id in conflict_ids:
            peer = conn.execute(
                "SELECT * FROM memory_entries WHERE memory_id=? AND status='active' "
                "AND scope=? AND project_id IS ? AND (expires_at IS NULL OR expires_at>?) "
                "AND EXISTS (SELECT 1 FROM memory_entry_sources es JOIN memory_sources s USING(source_id) "
                "WHERE es.memory_id=memory_entries.memory_id AND s.status='active' "
                "AND (s.expires_at IS NULL OR s.expires_at>?))",
                (str(conflict_id), row["scope"], row["project_id"], now, now),
            ).fetchone()
            if peer is None:
                continue
            peer_hints = _valid_conflict_hints(json.loads(peer["metadata"]).get("conflict_hints"))
            if any(_opposite_hints(left, right) for left in own_hints for right in peer_hints):
                valid_ids.append(str(conflict_id))
        valid_ids = sorted(set(valid_ids))
        if valid_ids:
            metadata["conflict_ids"] = valid_ids
            metadata["needs_review"] = True
        else:
            metadata.pop("conflict_ids", None)
            metadata.pop("needs_review", None)
        return metadata

    @staticmethod
    def _refresh_conflict_metadata(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT * FROM memory_entries WHERE status IN ('active','candidate') AND metadata LIKE '%needs_review%'",
        ).fetchall()
        now = _now()
        for row in rows:
            before = json.loads(row["metadata"])
            after = MemoryService._effective_metadata(conn, row)
            if before == after:
                continue
            MemoryService._snapshot(conn, row)
            version = int(row["version"]) + 1
            conn.execute("UPDATE memory_entries SET metadata=?,version=?,updated_at=? WHERE memory_id=?",
                         (_json(after), version, now, row["memory_id"]))
            MemoryService._record_event(conn, row["memory_id"], version, "conflict_peers_refreshed",
                                        {"conflict_ids": after.get("conflict_ids", [])})

    @staticmethod
    def _source_valid(source: sqlite3.Row, now: str) -> bool:
        return source["status"]=="active" and (source["expires_at"] is None or source["expires_at"]>now)

    def _record(self,conn: sqlite3.Connection,row: sqlite3.Row) -> MemoryRecord:
        source_ids=[str(r[0]) for r in conn.execute("SELECT source_id FROM memory_entry_sources WHERE memory_id=? ORDER BY source_id",(row["memory_id"],))]
        return MemoryRecord(memory_id=row["memory_id"],memory_type=row["memory_type"],content=row["content"],scope=row["scope"],project_id=row["project_id"],status=row["status"],confidence=row["confidence"],sensitivity=row["sensitivity"],source_ids=source_ids,expires_at=row["expires_at"],version=row["version"],supersedes_id=row["supersedes_id"],created_at=row["created_at"],updated_at=row["updated_at"],metadata=self._effective_metadata(conn, row))


class MemoryPublicationSuppressed(ValueError):
    """Publication blocked by correction, tombstone, or ambiguous alias identity."""


class MemoryConflictError(RuntimeError):
    """Raised when a compare-and-swap version does not match."""


class MemoryPublicationLeaseError(ValueError):
    """A background writer attempted publication without its live fencing lease."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
