"""Deterministic, local message history and analysis publication store.

Message text is treated as untrusted data. This module performs no network or LLM
work; callers own those workflows and may publish only fenced analysis results.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.domains.message_attachments import AttachmentInput, MessageAttachmentMixin
from app.domains.message_dossiers import MessageDossierMixin
from app.domains.message_metadata import MessageMetadataMixin
from app.domains.message_reading_intelligence import MessageReadingIntelligenceMixin
from app.domains.message_reading_results import (
    MessageReadingResultsMixin,
    ReadingFinding,
    TopicUpdate,
)
from app.domains.message_reading_runtime import MessageReadingRuntimeMixin

ANALYSIS_JOB_KIND = "message_analysis"
_FACT_KINDS = {"fact", "event", "decision", "task_candidate", "question", "correction"}
_FACT_CERTAINTY = {"explicit", "inferred"}
_MAX_PAGE = 200


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _identity(platform: str, account_id: str, conversation_type: str, conversation_id: str) -> dict[str, str]:
    return {
        "platform": platform,
        "account_id": account_id,
        "conversation_type": conversation_type,
        "conversation_id": conversation_id,
    }


def _conversation_key(identity: dict[str, str]) -> str:
    return "message_conversation_" + _digest(identity)


def _account_scope_id(platform: str, account_id: str) -> str:
    return "message_account_" + _digest({"platform": platform, "account_id": account_id})


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    platform: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=256)
    message_id: str = Field(min_length=1, max_length=512)
    conversation_type: Literal["private", "group", "channel"]
    conversation_id: str = Field(min_length=1, max_length=512)
    sender_id: str | None = Field(default=None, max_length=512)
    sender_name: str | None = Field(default=None, max_length=512)
    text: str = Field(default="", max_length=16384)
    sent_at: int | None = None
    received_at: int
    content_kind: Literal["text", "unsupported"] = "text"
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_attachment_ordinals(self):
        if len({item.ordinal for item in self.attachments}) != len(self.attachments):
            raise ValueError("attachment ordinals must be unique")
        return self

    @field_validator("platform", "account_id", "message_id", "conversation_id")
    @classmethod
    def nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identity fields must not be blank")
        return value

    @field_validator("received_at")
    @classmethod
    def valid_received_at(cls, value: int) -> int:
        if not 0 <= value <= 253402300799:
            raise ValueError("received_at must be a bounded Unix timestamp")
        return value


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    platform: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=256)
    conversation_type: Literal["private", "group", "channel"]
    conversation_id: str = Field(min_length=1, max_length=512)
    display_name: str | None = Field(default=None, max_length=512)
    record_enabled: bool = False
    analysis_enabled: bool = False
    proposals_enabled: bool = False
    local_signals_enabled: bool = True
    minimum_import_version: Literal[1, 2] = 1
    processing_schema_version: int = Field(default=1, ge=1)
    media_enabled: bool = False
    batch_size: int = Field(default=20, ge=1, le=200)
    min_interval_seconds: int = Field(default=300, ge=0)
    max_wait_seconds: int = Field(default=900, ge=0)
    max_batch_messages: int = Field(default=200, ge=1, le=200)
    auto_analyze: bool = True
    # One-shot initial cutover: leave all messages already captured outside the
    # analysis queue and begin with the next sequence allocated after this write.
    start_from_now: bool = False
    timezone: str = "Asia/Shanghai"
    expected_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_schedule(self):
        complete = self.expected_revision == 0 or {"max_wait_seconds", "min_interval_seconds"}.issubset(self.model_fields_set)
        if complete and self.max_wait_seconds < self.min_interval_seconds:
            raise ValueError("max_wait_seconds must be >= min_interval_seconds")
        return self

    @field_validator("platform", "account_id", "conversation_id")
    @classmethod
    def nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identity fields must not be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class PolicyRevisionConflict(ValueError):
    """Raised when a conversation policy update loses its revision CAS."""


class NativeMention(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["user", "all"]
    user_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def valid_target(self):
        if (self.kind == "user") != (self.user_id is not None):
            raise ValueError("mention target does not match kind")
        return self


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["text", "mention", "reply", "unsupported"]
    text: str | None = Field(default=None, max_length=16384)
    mention: NativeMention | None = None
    message_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def valid_content(self):
        expected = {"text": "text", "mention": "mention", "reply": "message_id"}.get(self.kind)
        present = {name for name in ("text", "mention", "message_id") if getattr(self, name) is not None}
        if present != ({expected} if expected else set()):
            raise ValueError("content part payload does not match kind")
        return self


class MetadataCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mentions: Literal["supported", "not_provided", "unknown"] = "unknown"
    reply: Literal["supported", "not_provided", "unknown"] = "unknown"
    thread: Literal["supported", "not_provided", "unknown"] = "unknown"
    content_parts: Literal["supported", "not_provided", "unknown"] = "unknown"


class MessageInputV2(MessageInput):
    capture_epoch: int = Field(ge=1)
    mentions: list[NativeMention] = Field(default_factory=list, max_length=100)
    reply_to_message_id: str | None = Field(default=None, min_length=1, max_length=512)
    thread_id: str | None = Field(default=None, min_length=1, max_length=512)
    content_parts: list[ContentPart] = Field(default_factory=list, max_length=100)
    adapter_id: str = Field(min_length=1, max_length=120)
    adapter_version: str = Field(min_length=1, max_length=80)
    metadata_capabilities: MetadataCapabilities

    @model_validator(mode="after")
    def bounded_metadata(self):
        if len(self.model_dump_json().encode("utf-8")) > 131072:
            raise ValueError("message metadata exceeds byte bound")
        for field, capability in (("mentions", "mentions"), ("reply_to_message_id", "reply"),
                                  ("thread_id", "thread"), ("content_parts", "content_parts")):
            if getattr(self, field) and getattr(self.metadata_capabilities, capability) != "supported":
                raise ValueError("metadata requires a supported capability")
        return self


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["fact", "event", "decision", "task_candidate", "question", "correction"]
    text: str = Field(min_length=1, max_length=2000)
    source_message_ids: list[str] = Field(min_length=1, max_length=30)
    certainty: Literal["explicit", "inferred"]
    actor: str | None = Field(default=None, max_length=512)
    time_text: str | None = Field(default=None, max_length=512)
    supersedes_fact_ids: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fact text must not be blank")
        return value

    @model_validator(mode="after")
    def explicit_corrections_only(self):
        if self.supersedes_fact_ids and (self.kind != "correction" or self.certainty != "explicit"):
            raise ValueError("only an explicit correction may supersede previous facts")
        return self


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    batch_summary: str = Field(min_length=1, max_length=4000)
    summary: str = Field(min_length=1, max_length=8000)
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=100)
    client_name: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    generation_calls: int = Field(default=0, ge=0, le=100)
    usage_tokens: int | None = Field(default=None, ge=0, le=9223372036854775807)
    prompt_version: str = Field(default="message-analysis-v1", max_length=80)
    schema_version: Literal[1, 2, 3] = 1
    topic_updates: list[TopicUpdate] = Field(default_factory=list, max_length=40)
    highlights: list[ReadingFinding] = Field(default_factory=list, max_length=50)
    importance_findings: list[ReadingFinding] = Field(default_factory=list, max_length=50)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    reading_snapshot: dict[str, Any] | None = None
    reading_intelligence: dict[str, Any] | None = None
    reading_manifest: dict[str, Any] | None = None

    @model_validator(mode="after")
    def generation_member_version(self):
        if self.schema_version != 3 and any(getattr(topic, "member_message_ids", None) is not None for topic in self.topic_updates):
            raise ValueError("reading_members_require_schema_v3")
        return self

    @field_validator("warnings")
    @classmethod
    def bounded_warnings(cls, value):
        if any(len(item) > 512 for item in value):
            raise ValueError("reading_warning_too_long")
        return value

    @field_validator("batch_summary", "summary")
    @classmethod
    def nonblank_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be blank")
        return value


class MessageHistoryService(MessageMetadataMixin, MessageDossierMixin, MessageReadingIntelligenceMixin, MessageReadingResultsMixin, MessageReadingRuntimeMixin, MessageAttachmentMixin):
    """SQLite-backed message history with whitelist-gated reads and durable batches."""

    def __init__(self, db_path: Path | str, jobs: Any) -> None:
        self.db_path = str(db_path)
        self.jobs = jobs
        self.configure_reading({})

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        self.jobs.ensure_schema()
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_history_policies (
                    conversation_key TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE,
                    account_scope_id TEXT NOT NULL, platform TEXT NOT NULL, account_id TEXT NOT NULL,
                    conversation_type TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    display_name TEXT, record_enabled INTEGER NOT NULL, analysis_enabled INTEGER NOT NULL,
                    batch_size INTEGER NOT NULL, timezone TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(platform, account_id, conversation_type, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS message_history_conversations (
                    conversation_key TEXT PRIMARY KEY, next_seq INTEGER NOT NULL DEFAULT 1,
                    covered_seq INTEGER NOT NULL DEFAULT 0, summary_revision INTEGER NOT NULL DEFAULT 0,
                    rolling_summary TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS message_history_messages (
                    internal_message_id TEXT PRIMARY KEY, platform TEXT NOT NULL, account_id TEXT NOT NULL,
                    provider_message_id TEXT NOT NULL, conversation_key TEXT NOT NULL, seq INTEGER NOT NULL,
                    sender_id TEXT, sender_name TEXT, text TEXT NOT NULL, content_kind TEXT NOT NULL,
                    sent_at INTEGER, timestamp_quality TEXT NOT NULL, received_at INTEGER NOT NULL,
                    ingested_at TEXT NOT NULL,
                    UNIQUE(platform, account_id, provider_message_id),
                    UNIQUE(conversation_key, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_message_history_conversation_seq
                    ON message_history_messages(conversation_key, seq DESC);
                CREATE INDEX IF NOT EXISTS idx_message_history_sender
                    ON message_history_messages(conversation_key, sender_id, seq DESC);
                CREATE TABLE IF NOT EXISTS message_history_batches (
                    job_id TEXT PRIMARY KEY, conversation_key TEXT NOT NULL, start_seq INTEGER NOT NULL,
                    end_seq INTEGER NOT NULL, policy_revision INTEGER NOT NULL,
                    expected_summary_revision INTEGER NOT NULL, batch_summary TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    client_name TEXT, model TEXT, generation_calls INTEGER NOT NULL DEFAULT 0,
                    usage_tokens INTEGER, prompt_version TEXT NOT NULL DEFAULT 'message-analysis-v1',
                    created_at TEXT NOT NULL, UNIQUE(conversation_key,start_seq,end_seq,policy_revision)
                );
                CREATE TABLE IF NOT EXISTS message_history_facts (
                    fact_id TEXT PRIMARY KEY, conversation_key TEXT NOT NULL, batch_job_id TEXT NOT NULL,
                    kind TEXT NOT NULL, text TEXT NOT NULL, source_message_ids_json TEXT NOT NULL,
                    certainty TEXT NOT NULL, actor TEXT, time_text TEXT,
                    supersedes_fact_ids_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_message_history_facts_conversation
                    ON message_history_facts(conversation_key, created_at DESC, fact_id);
                CREATE TABLE IF NOT EXISTS message_history_fact_supersessions (
                    conversation_key TEXT NOT NULL, prior_fact_id TEXT NOT NULL, correction_fact_id TEXT NOT NULL,
                    PRIMARY KEY(conversation_key,prior_fact_id,correction_fact_id)
                );
                """
            )

            self._ensure_attachment_schema(conn)
            self._ensure_metadata_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(message_history_policies)")}
            if "media_enabled" not in columns:
                conn.execute("ALTER TABLE message_history_policies ADD COLUMN media_enabled INTEGER NOT NULL DEFAULT 0")
            for table, additions in {
                "message_history_policies": {
                    "capture_epoch": "INTEGER NOT NULL DEFAULT 1", "analysis_epoch": "INTEGER NOT NULL DEFAULT 1",
                    "proposals_epoch": "INTEGER NOT NULL DEFAULT 1", "processing_revision": "INTEGER NOT NULL DEFAULT 1",
                    "schedule_revision": "INTEGER NOT NULL DEFAULT 1", "proposals_enabled": "INTEGER NOT NULL DEFAULT 0",
                    "local_signals_enabled": "INTEGER NOT NULL DEFAULT 1", "minimum_import_version": "INTEGER NOT NULL DEFAULT 1",
                    "processing_schema_version": "INTEGER NOT NULL DEFAULT 1"},
                "message_history_messages": {"schema_version": "INTEGER NOT NULL DEFAULT 1", "capture_epoch": "INTEGER",
                    "metadata_json": "TEXT NOT NULL DEFAULT '{}'"},
                "message_history_conversations": {"pipeline_version": "TEXT NOT NULL DEFAULT 'legacy-v1'",
                    "baseline_start_seq": "INTEGER", "local_signal_start_seq": "INTEGER", "local_signal_seq": "INTEGER NOT NULL DEFAULT 0"},
            }.items():
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                for name, declaration in additions.items():
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            # Preserve already-authorized legacy jobs without promoting stale ones.
            for job in conn.execute("SELECT job_id,scope_id,payload_json FROM background_jobs WHERE kind=? "
                                    "AND status IN ('queued','running','retry_wait','failed','cancelled')",
                                    (ANALYSIS_JOB_KIND,)).fetchall():
                payload = json.loads(job["payload_json"])
                policy = conn.execute("SELECT * FROM message_history_policies WHERE conversation_key=?",
                                      (job["scope_id"],)).fetchone()
                if policy is not None and "capture_epoch" not in payload and payload.get("revision") == policy["revision"]:
                    payload.update({name: policy[name] for name in ("capture_epoch", "analysis_epoch", "processing_revision")})
                    conn.execute("UPDATE background_jobs SET payload_json=? WHERE job_id=?", (_json(payload), job["job_id"]))
            self._ensure_reading_schema(conn)
            self._ensure_reading_results_schema(conn)
            self._ensure_reading_intelligence_schema(conn)
            conn.commit()

    @staticmethod
    def _policy(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "conversation_key": row["conversation_key"], "source_id": row["source_id"],
            "account_scope_id": row["account_scope_id"], "platform": row["platform"],
            "account_id": row["account_id"], "conversation_type": row["conversation_type"],
            "conversation_id": row["conversation_id"], "display_name": row["display_name"],
            "record_enabled": bool(row["record_enabled"]),
            "media_enabled": bool(row["media_enabled"]),
            "analysis_enabled": bool(row["analysis_enabled"]), "batch_size": row["batch_size"],
            "timezone": row["timezone"], "revision": row["revision"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            **{name: row[name] for name in ("capture_epoch", "analysis_epoch", "proposals_epoch",
               "processing_revision", "schedule_revision", "minimum_import_version", "processing_schema_version")},
            "proposals_enabled": bool(row["proposals_enabled"]),
            "local_signals_enabled": bool(row["local_signals_enabled"]),
            **{name: row[name] for name in ("min_interval_seconds", "max_wait_seconds", "max_batch_messages")},
            "auto_analyze": bool(row["auto_analyze"]),
        }

    def set_policy(self, payload: PolicyInput | dict[str, Any]) -> dict[str, Any]:
        policy = payload if isinstance(payload, PolicyInput) else PolicyInput.model_validate(payload)
        identity = _identity(policy.platform, policy.account_id, policy.conversation_type, policy.conversation_id)
        key = _conversation_key(identity)
        source = "message_source_" + _digest(identity)
        account_scope = _account_scope_id(policy.platform, policy.account_id)
        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM message_history_policies WHERE conversation_key=?", (key,)).fetchone()
            actual = old["revision"] if old else 0
            if actual != policy.expected_revision:
                conn.rollback()
                raise PolicyRevisionConflict("policy_revision_conflict")
            # Older UI clients omit this field. Preserve an existing grant unless
            # the caller explicitly changes it; newly created policies stay off.
            media_enabled = (bool(old["media_enabled"]) if old is not None
                             and "media_enabled" not in policy.model_fields_set else policy.media_enabled)
            if old is not None:
                # New defaults must not revoke existing grants when an older client
                # submits an otherwise complete policy object.
                for name in ("record_enabled", "analysis_enabled", "proposals_enabled", "local_signals_enabled",
                             "minimum_import_version", "processing_schema_version", "timezone", "batch_size", "display_name"):
                    if name not in policy.model_fields_set:
                        setattr(policy, name, bool(old[name]) if name.endswith("enabled") else old[name])
                for name in ("min_interval_seconds", "max_wait_seconds", "max_batch_messages", "auto_analyze"):
                    if name not in policy.model_fields_set:
                        setattr(policy, name, bool(old[name]) if name == "auto_analyze" else old[name])
                if policy.minimum_import_version < old["minimum_import_version"]:
                    raise ValueError("minimum_import_version cannot be downgraded")
            enabling_analysis = policy.analysis_enabled and (old is None or not bool(old["analysis_enabled"]))
            if policy.start_from_now and not enabling_analysis:
                raise ValueError("start_from_now_only_allowed_on_initial_analysis_enable")
            if policy.max_wait_seconds < policy.min_interval_seconds:
                raise ValueError("max_wait_seconds must be >= min_interval_seconds")
            fence = old is not None and (bool(old["record_enabled"]) != policy.record_enabled
                    or bool(old["analysis_enabled"]) != policy.analysis_enabled
                    or old["timezone"] != policy.timezone
                    or old["processing_schema_version"] != policy.processing_schema_version)
            revision = actual + 1
            if old is None:
                conn.execute(
                    """INSERT INTO message_history_policies
                       (conversation_key,source_id,account_scope_id,platform,account_id,conversation_type,
                        conversation_id,display_name,record_enabled,analysis_enabled,batch_size,timezone,
                        revision,created_at,updated_at,media_enabled)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, source, account_scope, policy.platform, policy.account_id, policy.conversation_type,
                     policy.conversation_id, policy.display_name, int(policy.record_enabled),
                     int(policy.analysis_enabled), policy.batch_size, policy.timezone, revision, now, now,
                     int(media_enabled)),
                )
                conn.execute(
                    "INSERT INTO message_history_conversations(conversation_key,updated_at) VALUES(?,?)",
                    (key, now),
                )
            else:
                conn.execute(
                    """UPDATE message_history_policies SET display_name=?,record_enabled=?,analysis_enabled=?,
                       batch_size=?,timezone=?,revision=?,updated_at=?,media_enabled=? WHERE conversation_key=? AND revision=?""",
                    (policy.display_name, int(policy.record_enabled), int(policy.analysis_enabled),
                     policy.batch_size, policy.timezone, revision, now, int(media_enabled), key, actual),
                )
                # Permission and semantic changes fence results; scheduling changes
                # preserve the fixed range and its ownership.
                if fence:
                    conn.execute(
                        """UPDATE background_jobs SET status='cancelled',finished_at=?,updated_at=?,
                           lease_owner=NULL,lease_expires_at=NULL WHERE kind=? AND scope_id=?
                           AND status IN ('queued','retry_wait','running')""",
                        (now, now, ANALYSIS_JOB_KIND, key),
                    )
            epochs = {
                "capture_epoch": old["capture_epoch"] + int(bool(old["record_enabled"]) != policy.record_enabled) if old else 1,
                "analysis_epoch": old["analysis_epoch"] + int(bool(old["analysis_enabled"]) != policy.analysis_enabled) if old else 1,
                "proposals_epoch": old["proposals_epoch"] + int(bool(old["proposals_enabled"]) != policy.proposals_enabled) if old else 1,
                "processing_revision": old["processing_revision"] + int(old["timezone"] != policy.timezone or old["processing_schema_version"] != policy.processing_schema_version) if old else 1,
                "schedule_revision": old["schedule_revision"] + int(any(old[name] != getattr(policy, name) for name in
                    ("batch_size", "min_interval_seconds", "max_wait_seconds", "max_batch_messages", "auto_analyze"))) if old else 1,
            }
            conn.execute("UPDATE message_history_policies SET capture_epoch=?,analysis_epoch=?,proposals_epoch=?,"
                         "processing_revision=?,schedule_revision=?,proposals_enabled=?,local_signals_enabled=?,"
                         "minimum_import_version=?,processing_schema_version=? WHERE conversation_key=?",
                         (*epochs.values(), int(policy.proposals_enabled), int(policy.local_signals_enabled),
                          policy.minimum_import_version, policy.processing_schema_version, key))
            conn.execute("UPDATE message_history_policies SET min_interval_seconds=?,max_wait_seconds=?,"
                         "max_batch_messages=?,auto_analyze=? WHERE conversation_key=?",
                         (policy.min_interval_seconds, policy.max_wait_seconds, policy.max_batch_messages,
                          int(policy.auto_analyze), key))
            state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (key,)).fetchone()
            initialized = bool(state["analysis_baseline_initialized"])
            if policy.start_from_now and initialized:
                raise ValueError("start_from_now_only_allowed_on_initial_analysis_enable")
            if enabling_analysis:
                # BEGIN IMMEDIATE serializes this floor with imports, which also
                # allocate next_seq under a write transaction. The floor is
                # immutable after first analysis enable, even across disable/re-enable.
                if policy.start_from_now:
                    conn.execute("UPDATE message_history_conversations SET analysis_baseline_floor_seq=next_seq-1 WHERE conversation_key=?", (key,))
                conn.execute("UPDATE message_history_conversations SET analysis_baseline_initialized=1 WHERE conversation_key=?", (key,))
            if fence:
                conn.execute("DELETE FROM message_reading_checkpoints WHERE family_id IN "
                             "(SELECT family_id FROM message_reading_families WHERE conversation_key=?)", (key,))
            if not policy.record_enabled and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_matter_proposals'").fetchone():
                conn.execute("UPDATE message_matter_proposals SET state=CASE WHEN state='pending' THEN 'revoked' ELSE state END,frozen_reason='record_revoked' WHERE conversation_key=?", (key,))
            self._refresh_reading_schedule(conn, key)
            row = conn.execute("SELECT * FROM message_history_policies WHERE conversation_key=?", (key,)).fetchone()
            conn.commit()
        if policy.record_enabled and policy.analysis_enabled:
            self.schedule_pending(key)
        return self._policy(row)

    def list_policies(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM message_history_policies ORDER BY platform,account_id,conversation_id").fetchall()
            return [self._policy(row) for row in rows]

    def import_messages(self, messages: list[MessageInput | dict[str, Any]], *, schema_version: int = 1) -> dict[str, list[dict[str, Any]]]:
        if type(schema_version) is not int or schema_version not in (1, 2):
            raise ValueError("unsupported_schema_version")
        if not isinstance(messages, list) or len(messages) > 1000:
            raise ValueError("messages must be a list with at most 1000 entries")
        if schema_version == 2 and len(_json([m.model_dump() if isinstance(m, BaseModel) else m for m in messages]).encode("utf-8")) > 2097152:
            raise ValueError("batch exceeds byte bound")
        acknowledged: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        touched: set[str] = set()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for raw in messages:
                identity = self._safe_identity(raw)
                try:
                    model = MessageInputV2 if schema_version == 2 else MessageInput
                    item = model.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)
                except ValidationError:
                    rejected.append(self._rejection(identity, "invalid_message"))
                    continue
                public_identity = {"platform": item.platform, "account_id": item.account_id, "message_id": item.message_id}
                identity = _identity(item.platform, item.account_id, item.conversation_type, item.conversation_id)
                key = _conversation_key(identity)
                policy = conn.execute(
                    "SELECT * FROM message_history_policies WHERE conversation_key=?", (key,)
                ).fetchone()
                if policy is None or not policy["record_enabled"]:
                    rejected.append(self._rejection(public_identity, "not_whitelisted"))
                    continue
                if schema_version < policy["minimum_import_version"]:
                    rejected.append(self._rejection(public_identity, "import_version_required"))
                    continue
                if schema_version == 2 and item.capture_epoch != policy["capture_epoch"]:
                    rejected.append(self._rejection(public_identity, "capture_epoch_conflict"))
                    continue
                metadata = ({name: getattr(item, name) for name in (
                    "mentions", "reply_to_message_id", "thread_id", "content_parts", "adapter_id",
                    "adapter_version", "metadata_capabilities")} if schema_version == 2 else {})
                metadata = {name: [v.model_dump() for v in value] if isinstance(value, list)
                            else value.model_dump() if isinstance(value, BaseModel) else value
                            for name, value in metadata.items()}
                text = item.text if item.content_kind == "text" else ""
                sent_valid = item.sent_at is not None and 0 < item.sent_at <= 253402300799
                sent_at = item.sent_at if sent_valid else None
                message_digest = _digest({"platform": item.platform, "account_id": item.account_id,
                                           "message_id": item.message_id})
                internal_id = "message_" + message_digest
                existing = conn.execute(
                    """SELECT * FROM message_history_messages WHERE platform=? AND account_id=?
                       AND provider_message_id=?""",
                    (item.platform, item.account_id, item.message_id),
                ).fetchone()
                comparable = (key, item.sender_id, item.sender_name, text, item.content_kind, sent_at)
                if existing is not None:
                    prior = (existing["conversation_key"], existing["sender_id"], existing["sender_name"],
                             existing["text"], existing["content_kind"], existing["sent_at"])
                    metadata_matches = (schema_version == 1 or
                        existing["schema_version"] == 2 and existing["capture_epoch"] == item.capture_epoch
                        and existing["metadata_json"] == _json(metadata))
                    if comparable == prior and metadata_matches and self._attachment_refs_match(conn, internal_id, item):
                        acknowledged.append(public_identity)
                    else:
                        rejected.append(self._rejection(public_identity, "identity_conflict"))
                    continue
                state = conn.execute(
                    "SELECT next_seq FROM message_history_conversations WHERE conversation_key=?", (key,)
                ).fetchone()
                if state is None:
                    # Defensive against a policy created by an older schema version.
                    conn.execute("INSERT INTO message_history_conversations(conversation_key,updated_at) VALUES(?,?)", (key, _now()))
                    sequence = 1
                else:
                    sequence = state["next_seq"]
                conn.execute(
                    """INSERT INTO message_history_messages(internal_message_id,platform,account_id,
                       provider_message_id,conversation_key,seq,sender_id,sender_name,text,content_kind,
                       sent_at,timestamp_quality,received_at,ingested_at,schema_version,capture_epoch,metadata_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (internal_id, item.platform, item.account_id, item.message_id, key, sequence,
                     item.sender_id, item.sender_name, text, item.content_kind,
                     sent_at, "valid" if sent_valid else "invalid",
                     item.received_at, _now(), schema_version,
                     item.capture_epoch if schema_version == 2 else policy["capture_epoch"], _json(metadata)),
                )
                conn.execute(
                    "UPDATE message_history_conversations SET next_seq=?,updated_at=? WHERE conversation_key=?",
                    (sequence + 1, _now(), key),
                )
                self._insert_attachment_refs(conn, internal_id, key, item)
                acknowledged.append(public_identity)
                touched.add(key)
            for key in touched:
                self._observe_reading_activity(conn, key)
                self._refresh_reading_schedule(conn, key)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        for key in sorted(touched):
            try:
                self.scan_local_signals(key, limit=1000)
                self.schedule_pending(key)
            except sqlite3.Error:
                # The message commit is the import acknowledgement boundary. A
                # periodic pending scan can recover enqueue work after a DB fault.
                continue
        return {"acknowledged": acknowledged, "rejected": rejected}

    @staticmethod
    def _safe_identity(raw: Any) -> dict[str, str] | None:
        if not isinstance(raw, dict):
            return None
        keys = ("platform", "account_id", "message_id")
        if all(isinstance(raw.get(key), str) for key in keys):
            return {key: raw[key][:512] for key in keys}
        return None

    @staticmethod
    def _rejection(identity: dict[str, str] | None, reason: str) -> dict[str, Any]:
        value = identity or {}
        return {"platform": value.get("platform"), "account_id": value.get("account_id"),
                "message_id": value.get("message_id"), "reason": reason, "permanent": True}

    def _read_scope(
        self, conn: sqlite3.Connection, conversation_key: str | None,
        allowed_sources: list[str] | None, allowed_accounts: list[str] | None,
    ) -> list[sqlite3.Row]:
        rows = conn.execute(
            "SELECT * FROM message_history_policies WHERE record_enabled=1 ORDER BY conversation_key"
        ).fetchall()
        if allowed_sources == [] or allowed_accounts == []:
            return []
        result = []
        for row in rows:
            if conversation_key is not None and row["conversation_key"] != conversation_key:
                continue
            if allowed_sources is not None and row["source_id"] not in allowed_sources:
                continue
            if allowed_accounts is not None and row["account_scope_id"] not in allowed_accounts:
                continue
            result.append(row)
        if conversation_key is not None and not result:
            raise PermissionError("message_history_not_allowed")
        return result

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        if isinstance(limit, bool) or isinstance(offset, bool) or limit < 1 or offset < 0:
            raise ValueError("invalid pagination")
        return min(int(limit), _MAX_PAGE), int(offset)

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "message_id": row["internal_message_id"], "platform": row["platform"],
            "account_id": row["account_id"], "provider_message_id": row["provider_message_id"],
            "conversation_key": row["conversation_key"], "seq": row["seq"],
            "sender_id": row["sender_id"], "sender_name": row["sender_name"], "text": row["text"],
            "content_kind": row["content_kind"], "sent_at": row["sent_at"],
            "timestamp_quality": row["timestamp_quality"], "received_at": row["received_at"],
            "ingested_at": row["ingested_at"],
            "schema_version": row["schema_version"], "capture_epoch": row["capture_epoch"],
            "mentions": [], "reply_to_message_id": None, "thread_id": None, "content_parts": [],
            "adapter_id": None, "adapter_version": None,
            "metadata_capabilities": MetadataCapabilities().model_dump(),
            **json.loads(row["metadata_json"]),
        }

    def resolve_message_source(self, internal_message_id: str, *, allowed_sources: list[str] | None = None,
                               allowed_accounts: list[str] | None = None) -> dict[str, Any]:
        """Resolve a real internal ID only, rechecking both independent grants."""
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM message_history_messages WHERE internal_message_id=?",
                               (internal_message_id,)).fetchone()
            if row is None:
                raise PermissionError("message_history_not_allowed")
            policies = self._read_scope(conn, row["conversation_key"], allowed_sources, allowed_accounts)
            if not policies:
                raise PermissionError("message_history_not_allowed")
            return {"source_type": "message_history_message", "source_id": policies[0]["source_id"],
                    "account_scope_id": policies[0]["account_scope_id"], **self._resolved_message(conn, row)}

    def _resolved_message(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = self._message(row)
        reply = result["reply_to_message_id"]
        target = conn.execute("SELECT internal_message_id FROM message_history_messages WHERE platform=? "
                              "AND account_id=? AND conversation_key=? AND provider_message_id=?",
                              (row["platform"], row["account_id"], row["conversation_key"], reply)).fetchone() if reply else None
        result["reply_to_internal_message_id"] = target[0] if target else None
        result["reply_resolution"] = "resolved" if target else "unresolved" if reply else "none"
        return result

    def coverage(self, conversation_key: str, *, allowed_sources: list[str] | None = None,
                 allowed_accounts: list[str] | None = None) -> dict[str, Any]:
        with self._connection() as conn:
            if not self._read_scope(conn, conversation_key, allowed_sources, allowed_accounts):
                raise PermissionError("message_history_not_allowed")
            return self._reading_coverage(conn, conversation_key)

    def list_conversations(
        self, *, allowed_sources: list[str] | None = None, allowed_accounts: list[str] | None = None,
        limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        size, start = self._page(limit, offset)
        with self._connection() as conn:
            policies = self._read_scope(conn, None, allowed_sources, allowed_accounts)
            records = []
            for policy in policies:
                state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (policy["conversation_key"],)).fetchone()
                count = conn.execute("SELECT COUNT(*) FROM message_history_messages WHERE conversation_key=?", (policy["conversation_key"],)).fetchone()[0]
                current_count = conn.execute(
                    "SELECT COUNT(*) FROM message_history_messages WHERE conversation_key=? AND capture_epoch=?",
                    (policy["conversation_key"], policy["capture_epoch"]),
                ).fetchone()[0]
                latest = conn.execute("SELECT received_at FROM message_history_messages WHERE conversation_key=? ORDER BY seq DESC LIMIT 1", (policy["conversation_key"],)).fetchone()
                value = self._policy(policy)
                value.update({"message_count": count, "current_message_count": current_count,
                              "covered_seq": state["covered_seq"],
                              "summary_revision": state["summary_revision"],
                              "latest_received_at": latest[0] if latest else None,
                              "legacy_pending_count": max(0, state["next_seq"] - 1 - state["covered_seq"]),
                              "pending_count": max(0, state["next_seq"] - 1 - self._reading_watermark(state)),
                              "analysis_job": self._latest_analysis_job(conn, policy["conversation_key"], policy)})
                records.append(value)
            records.sort(key=lambda item: (item["latest_received_at"] or 0, item["conversation_key"]), reverse=True)
            page = records[start:start + size + 1]
            more = len(page) > size
            return {"conversations": page[:size], "next_offset": start + size if more else None}

    def _messages(
        self, *, conversation_key: str | None, where: str, params: list[Any], limit: int, offset: int,
        allowed_sources: list[str] | None, allowed_accounts: list[str] | None,
        current_capture_only: bool = False,
        order_by: str = "received_at DESC,seq DESC",
    ) -> dict[str, Any]:
        size, start = self._page(limit, offset)
        with self._connection() as conn:
            policies = self._read_scope(conn, conversation_key, allowed_sources, allowed_accounts)
            keys = [row["conversation_key"] for row in policies]
            if not keys:
                return {"messages": [], "next_offset": None, "has_more": False}
            if current_capture_only:
                epoch_clause = " OR ".join(
                    "(conversation_key=? AND capture_epoch=?)" for _ in policies
                )
                where += f" AND ({epoch_clause})"
                for policy in policies:
                    params.extend((policy["conversation_key"], policy["capture_epoch"]))
            marks = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT * FROM message_history_messages WHERE conversation_key IN ({marks}) {where} "
                f"ORDER BY {order_by} LIMIT ? OFFSET ?",
                [*keys, *params, size + 1, start],
            ).fetchall()
            more = len(rows) > size
            return {"messages": [self._resolved_message(conn, row) for row in rows[:size]],
                    "next_offset": start + size if more else None, "has_more": more}

    def recent(
        self, conversation_key: str | None = None, *, limit: int = 50, offset: int = 0,
        since: int | None = None, sender_id: str | None = None,
        allowed_sources: list[str] | None = None, allowed_accounts: list[str] | None = None,
        current_capture_only: bool = False,
    ) -> dict[str, Any]:
        where, params = self._message_filters(since, sender_id)
        return self._messages(conversation_key=conversation_key, where=where, params=params, limit=limit, offset=offset,
                              allowed_sources=allowed_sources, allowed_accounts=allowed_accounts,
                              current_capture_only=current_capture_only)

    def search(
        self, query: str, conversation_key: str | None = None, *, limit: int = 50, offset: int = 0,
        since: int | None = None, until: int | None = None, sender_id: str | None = None,
        sender: str | None = None,
        allowed_sources: list[str] | None = None, allowed_accounts: list[str] | None = None,
        current_capture_only: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValueError("query must contain 1..256 characters")
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where, params = self._message_filters(since, sender_id, until=until, sender=sender)
        where += " AND text LIKE ? ESCAPE '\\'"
        params.append(f"%{escaped}%")
        return self._messages(conversation_key=conversation_key, where=where,
                              params=params, limit=limit, offset=offset,
                              allowed_sources=allowed_sources, allowed_accounts=allowed_accounts,
                              current_capture_only=current_capture_only)

    @staticmethod
    def _message_filters(since: int | None, sender_id: str | None, *, until: int | None = None,
                         sender: str | None = None) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if since is not None:
            if isinstance(since, bool) or not isinstance(since, int) or since < 0:
                raise ValueError("since must be a non-negative integer")
            conditions.append("received_at>=?")
            params.append(since)
        if until is not None:
            if isinstance(until, bool) or not isinstance(until, int) or until < 0:
                raise ValueError("until must be a non-negative integer")
            conditions.append("received_at<=?")
            params.append(until)
        if sender_id is not None:
            if not isinstance(sender_id, str) or len(sender_id) > 512:
                raise ValueError("sender_id must be at most 512 characters")
            conditions.append("sender_id=?")
            params.append(sender_id)
        if sender is not None:
            if not isinstance(sender, str) or not sender.strip() or len(sender) > 256:
                raise ValueError("sender must be 1..256 characters")
            conditions.append("sender_name LIKE ? ESCAPE '\\'")
            params.append("%" + sender.strip().replace("%", "\\%").replace("_", "\\_") + "%")
        return ("AND " + " AND ".join(conditions) if conditions else ""), params

    def history(
        self, conversation_key: str, *, limit: int = 100, before_seq: int | None = None,
        offset: int = 0, sender_id: str | None = None,
        allowed_sources: list[str] | None = None, allowed_accounts: list[str] | None = None,
    ) -> dict[str, Any]:
        where, params = self._message_filters(None, sender_id)
        if before_seq is not None:
            if isinstance(before_seq, bool) or not isinstance(before_seq, int) or before_seq < 1:
                raise ValueError("before_seq must be a positive integer")
            where += " AND seq<?"
            params.append(before_seq)
        result = self._messages(conversation_key=conversation_key, where=where, params=params, limit=limit,
                                offset=offset, allowed_sources=allowed_sources, allowed_accounts=allowed_accounts,
                                order_by="seq DESC")
        result["next_before_seq"] = result["messages"][-1]["seq"] if result["has_more"] and result["messages"] else None
        return result

    def summary(
        self, conversation_key: str, *, allowed_sources: list[str] | None = None,
        allowed_accounts: list[str] | None = None,
    ) -> dict[str, Any] | None:
        with self._connection() as conn:
            policy_rows = self._read_scope(conn, conversation_key, allowed_sources, allowed_accounts)
            if not policy_rows:
                return None
            policy = self._policy(policy_rows[0])
            state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (conversation_key,)).fetchone()
            if state is None:
                return None
            batch = conn.execute(
                "SELECT batch_summary FROM message_history_batches WHERE conversation_key=? ORDER BY end_seq DESC LIMIT 1",
                (conversation_key,),
            ).fetchone()
            legacy_pending_count = max(0, state["next_seq"] - 1 - state["covered_seq"])
            analysis_pending_count = max(0, state["next_seq"] - 1 - self._reading_watermark(state))
            tail_rows = conn.execute(
                "SELECT * FROM message_history_messages WHERE conversation_key=? AND seq>? ORDER BY seq DESC LIMIT 20",
                (conversation_key, state["covered_seq"]),
            ).fetchall()
            tail: list[dict[str, Any]] = []
            budget = 8000
            tail_truncated = False
            for row in tail_rows:
                message = self._resolved_message(conn, row)
                text = message["text"]
                if len(text) > budget:
                    message["text"] = text[:budget]
                    message["truncated"] = True
                    tail_truncated = True
                budget -= len(message["text"])
                tail.append(message)
                if budget <= 0:
                    break
            tail.reverse()
            latest_job = self._latest_analysis_job(conn, conversation_key, policy_rows[0])
            total_count = state["next_seq"] - 1
            coverage = {
                "mode": "inbound_only", "covered_seq": state["covered_seq"],
                "total_count": total_count, "pending_count": legacy_pending_count,
                "legacy_pending_count": legacy_pending_count,
                "analysis_pending_count": analysis_pending_count,
                "tail_returned_count": len(tail),
                "tail_omitted_count": max(0, legacy_pending_count - len(tail)),
                "tail_truncated": tail_truncated,
                "pipeline_version": state["pipeline_version"], "baseline_start_seq": state["baseline_start_seq"],
                "legacy_only": True, "legacy_covered_seq": state["covered_seq"], "complete_for_platform": False,
            }
            return {"conversation_key": conversation_key, "summary": state["rolling_summary"],
                    "covered_seq": state["covered_seq"], "summary_revision": state["summary_revision"],
                    "policy_revision": policy["revision"],
                    "batch_summary": batch[0] if batch else "", "pending_count": legacy_pending_count,
                    "legacy_pending_count": legacy_pending_count,
                    "analysis_pending_count": analysis_pending_count,
                    "raw_tail": tail, "coverage": coverage, "analysis_job": latest_job}

    def _latest_analysis_job(
        self, conn: sqlite3.Connection, conversation_key: str, policy: sqlite3.Row,
    ) -> dict[str, Any] | None:
        rows = conn.execute(
            "SELECT job_id,status,updated_at,error_class,payload_json FROM background_jobs "
            "WHERE kind=? AND scope_id=? ORDER BY created_at DESC LIMIT 10",
            (ANALYSIS_JOB_KIND, conversation_key),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if self._job_policy_matches(payload, policy):
                return {"job_id": row["job_id"], "status": row["status"],
                        "updated_at": row["updated_at"], "error_class": row["error_class"]}
        return None

    def facts(
        self, conversation_key: str | None = None, *, limit: int = 50, offset: int = 0,
        allowed_sources: list[str] | None = None, allowed_accounts: list[str] | None = None,
    ) -> dict[str, Any]:
        size, start = self._page(limit, offset)
        with self._connection() as conn:
            policies = self._read_scope(conn, conversation_key, allowed_sources, allowed_accounts)
            keys = [row["conversation_key"] for row in policies]
            if not keys:
                return {"facts": [], "next_offset": None}
            marks = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT * FROM message_history_facts WHERE conversation_key IN ({marks}) "
                "ORDER BY created_at DESC,fact_id LIMIT ? OFFSET ?", [*keys, size + 1, start],
            ).fetchall()
            values = []
            for row in rows[:size]:
                message_ids = json.loads(row["source_message_ids_json"])
                source_rows = conn.execute(
                    f"SELECT internal_message_id,seq,sent_at,timestamp_quality FROM message_history_messages "
                    f"WHERE conversation_key=? AND internal_message_id IN ({','.join('?' for _ in message_ids)})",
                    [row["conversation_key"], *message_ids],
                ).fetchall() if message_ids else []
                source_map = {item["internal_message_id"]: item for item in source_rows}
                superseded_by = [item[0] for item in conn.execute(
                    "SELECT correction_fact_id FROM message_history_fact_supersessions "
                    "WHERE conversation_key=? AND prior_fact_id=? ORDER BY correction_fact_id",
                    (row["conversation_key"], row["fact_id"]),
                )]
                values.append({
                    "fact_id": row["fact_id"], "conversation_key": row["conversation_key"],
                    "batch_job_id": row["batch_job_id"], "kind": row["kind"], "text": row["text"],
                    "source_message_ids": message_ids,
                    "source_refs": [{"message_id": message_id, "seq": source_map[message_id]["seq"],
                                     "sent_at": source_map[message_id]["sent_at"],
                                     "timestamp_quality": source_map[message_id]["timestamp_quality"]}
                                    for message_id in message_ids if message_id in source_map],
                    "certainty": row["certainty"], "actor": row["actor"], "time_text": row["time_text"],
                    "supersedes_fact_ids": json.loads(row["supersedes_fact_ids_json"]),
                    "superseded_by": superseded_by, "active": not superseded_by,
                    "created_at": row["created_at"],
                })
            return {"facts": values, "next_offset": start + size if len(rows) > size else None}

    def source_inventory(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            return [{"source_id": row[0], "platform": row[1], "conversation_type": row[2],
                     "display_name": row[3]} for row in conn.execute(
                         "SELECT source_id,platform,conversation_type,display_name FROM message_history_policies "
                         "WHERE record_enabled=1 ORDER BY platform,conversation_type,display_name,source_id")]

    def account_inventory(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            return [{"account_scope_id": row[0], "platform": row[1], "account_id": row[2]}
                    for row in conn.execute(
                        "SELECT DISTINCT account_scope_id,platform,account_id FROM message_history_policies "
                        "WHERE record_enabled=1 ORDER BY platform,account_id")]

    def _enqueue_next(
        self, conn: sqlite3.Connection, policy: sqlite3.Row, *, force: bool,
        ignore_job_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self._enqueue_reading(conn, policy, force=force, ignore_job_id=ignore_job_id)

    @staticmethod
    def _job_policy_matches(payload: dict[str, Any], policy: sqlite3.Row) -> bool:
        names = ("capture_epoch", "analysis_epoch", "processing_revision")
        if all(name in payload for name in names):
            return all(type(payload[name]) is int and payload[name] == policy[name] for name in names)
        return payload.get("revision") == policy["revision"] and not any(name in payload for name in names)

    @staticmethod
    def _valid_analysis_payload(payload: dict[str, Any]) -> bool:
        legacy = {"conversation_id", "start_seq", "end_seq", "revision"}
        names = {"capture_epoch", "analysis_epoch", "processing_revision"}
        return set(payload) in (legacy, legacy | names, legacy | names | {"work_family_id"})

    def schedule_pending(self, conversation_key: str | None = None, force: bool = False) -> list[dict[str, Any]]:
        if not isinstance(force, bool):
            raise TypeError("force must be boolean")
        self.scan_local_signals(conversation_key)
        jobs: list[dict[str, Any]] = []
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._catch_up_participant_activity(conn, conversation_key)
            if conversation_key is None:
                policies = conn.execute(
                    "SELECT * FROM message_history_policies WHERE record_enabled=1 AND analysis_enabled=1 "
                    "ORDER BY COALESCE((SELECT last_scan_at FROM message_reading_schedules s "
                    "WHERE s.conversation_key=message_history_policies.conversation_key),''),"
                    "COALESCE((SELECT pending_since FROM message_reading_schedules s "
                    "WHERE s.conversation_key=message_history_policies.conversation_key),updated_at),conversation_key LIMIT ?",
                    (self._reading_config.get("due_scan_limit", 32),)
                ).fetchall()
            else:
                policy = conn.execute(
                    "SELECT * FROM message_history_policies WHERE conversation_key=?", (conversation_key,)
                ).fetchone()
                policies = [policy] if policy is not None else []
            for policy in policies:
                job = self._enqueue_next(conn, policy, force=force)
                if job is None:
                    continue
                jobs.append(job)
            conn.commit()
        return jobs

    def retry_analysis(self, conversation_key: str, expected_updated_at: str,
                       *, allow_checkpoint_restart: bool = False) -> dict[str, Any]:
        """CAS-retry the current revision's failed batch for one conversation."""
        if type(allow_checkpoint_restart) is not bool:
            raise ValueError("invalid_restart_authorization")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policy = conn.execute(
                "SELECT * FROM message_history_policies WHERE conversation_key=? "
                "AND record_enabled=1 AND analysis_enabled=1", (conversation_key,),
            ).fetchone()
            if policy is None:
                return {"status": "not_allowed", "job": None}
            state = conn.execute(
                "SELECT * FROM message_history_conversations WHERE conversation_key=?",
                (conversation_key,),
            ).fetchone()
            rows = conn.execute(
                "SELECT * FROM background_jobs WHERE kind=? AND scope_id=? AND status IN ('failed','cancelled') "
                "ORDER BY created_at DESC", (ANALYSIS_JOB_KIND, conversation_key),
            ).fetchall()
            row = next((candidate for candidate in rows
                        if self._job_policy_matches(json.loads(candidate["payload_json"]), policy)
                        and json.loads(candidate["payload_json"]).get("start_seq") == self._reading_watermark(state) + 1), None)
            if row is not None and row["updated_at"] == expected_updated_at:
                from app.domains.message_reading_runtime import _RECOVERABLE_READING_ERRORS
                if self._reading_config.get("reading_algorithm", "legacy") != "legacy" and row["error_class"] in _RECOVERABLE_READING_ERRORS:
                    recovered = self._recover_failed_reading(conn, row, policy,
                                                             authorized_restart=allow_checkpoint_restart)
                    conn.commit()
                    return {"status": "retried" if recovered else "unsupported", "job": recovered or self.jobs._public(row)}
        if row is None:
            return {"status": "missing", "job": None}
        if row["updated_at"] != expected_updated_at:
            return {"status": "conflict", "job": self.jobs.get(row["job_id"])}
        if row["error_class"] == "work_budget_exhausted":
            return {"status": "unsupported", "job": self.jobs.get(row["job_id"])}
        status, job = self.jobs.retry_controlled(
            row["job_id"], expected_updated_at, {ANALYSIS_JOB_KIND}
        )
        # A concurrent policy revocation fences publication. Cancel the retried job if
        # its original revision ceased to be current while the queue CAS ran.
        with self._connection() as conn:
            current = conn.execute(
                "SELECT * FROM message_history_policies WHERE conversation_key=?",
                (conversation_key,),
            ).fetchone()
        payload = json.loads(row["payload_json"])
        if (current is None or not current["record_enabled"] or not current["analysis_enabled"]
                or not self._job_policy_matches(payload, current)):
            if job:
                self.jobs.cancel(job["job_id"])
            return {"status": "not_allowed", "job": job}
        return {"status": status, "job": job}

    @staticmethod
    def _lease_values(job: dict[str, Any]) -> tuple[str, int, dict[str, Any]] | None:
        if not isinstance(job, dict):
            return None
        job_id = job.get("job_id")
        owner = job.get("lease_owner")
        epoch = job.get("lease_epoch")
        payload = job.get("payload")
        if not (isinstance(job_id, str) and isinstance(owner, str) and isinstance(epoch, int)
                and isinstance(payload, dict)):
            return None
        return job_id, epoch, {"job_id": job_id, "lease_owner": owner, "lease_epoch": epoch, "payload": payload}

    def load_analysis_batch(self, job: dict[str, Any]) -> dict[str, Any] | None:
        lease = self._lease_values(job)
        if lease is None:
            return None
        job_id, epoch, canonical = lease
        payload = canonical["payload"]
        if not self._valid_analysis_payload(payload):
            return None
        now = _now()
        with self._connection() as conn:
            control = conn.execute("SELECT paused FROM message_reading_control WHERE service='message_reading'").fetchone()
            if control[0]:
                return None
            valid = conn.execute(
                """SELECT payload_json FROM background_jobs WHERE job_id=? AND kind=? AND scope_id=? AND status='running'
                   AND lease_owner=? AND lease_epoch=? AND lease_expires_at>? AND (deadline IS NULL OR deadline>?)""",
                (job_id, ANALYSIS_JOB_KIND, payload["conversation_id"], canonical["lease_owner"], epoch, now, now),
            ).fetchone()
            policy = conn.execute(
                "SELECT * FROM message_history_policies WHERE conversation_key=? "
                "AND record_enabled=1 AND analysis_enabled=1",
                (payload["conversation_id"],),
            ).fetchone()
            state = conn.execute(
                "SELECT * FROM message_history_conversations WHERE conversation_key=?", (payload["conversation_id"],)
            ).fetchone()
            if valid is None or json.loads(valid[0]) != payload or policy is None or state is None or not self._job_policy_matches(payload, policy):
                return None
            if payload["start_seq"] != self._reading_watermark(state) + 1 or payload["end_seq"] < payload["start_seq"]:
                return None
            messages = conn.execute(
                "SELECT * FROM message_history_messages WHERE conversation_key=? AND seq BETWEEN ? AND ? ORDER BY seq",
                (payload["conversation_id"], payload["start_seq"], payload["end_seq"]),
            ).fetchall()
            if len(messages) != payload["end_seq"] - payload["start_seq"] + 1:
                return None
            previous_facts = conn.execute(
                "SELECT fact_id,kind,text,source_message_ids_json,certainty FROM message_history_facts "
                "WHERE conversation_key=? AND NOT EXISTS (SELECT 1 FROM message_history_fact_supersessions s "
                "WHERE s.conversation_key=message_history_facts.conversation_key "
                "AND s.prior_fact_id=message_history_facts.fact_id) "
                "ORDER BY created_at DESC,fact_id LIMIT 100",
                (payload["conversation_id"],),
            ).fetchall()
            return {
                "conversation_key": payload["conversation_id"], "start_seq": payload["start_seq"],
                "end_seq": payload["end_seq"], "policy": self._policy(policy),
                "messages": [self._resolved_message(conn, row) for row in messages],
                "previous_summary": state["rolling_summary"],
                "expected_summary_revision": state["summary_revision"],
                "previous_facts": [{"fact_id": row["fact_id"], "kind": row["kind"], "text": row["text"],
                                    "source_message_ids": json.loads(row["source_message_ids_json"]),
                                    "certainty": row["certainty"]} for row in reversed(previous_facts)],
                **self._reading_input_context(conn, payload["conversation_id"]),
                "intelligence_snapshot": self.snapshot_context(payload["conversation_id"], payload["start_seq"] - 1),
            }

    def publish_analysis(self, job: dict[str, Any], result: AnalysisResult | dict[str, Any], *, service_epoch: int | None = None) -> bool:
        lease = self._lease_values(job)
        if lease is None:
            return False
        try:
            analysis = result if isinstance(result, AnalysisResult) else AnalysisResult.model_validate(result)
        except Exception as exc:
            raise ValueError("invalid_analysis_result") from exc
        job_id, epoch, canonical = lease
        payload = canonical["payload"]
        if not self._valid_analysis_payload(payload):
            return False
        now = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Re-evaluate the lease after any wait for SQLite's writer lock.
            now = _now()
            control = conn.execute("SELECT * FROM message_reading_control WHERE service='message_reading'").fetchone()
            if control["paused"] or (service_epoch is not None and control["service_epoch"] != service_epoch):
                conn.rollback()
                return False
            current_job = conn.execute(
                """SELECT payload_json FROM background_jobs WHERE job_id=? AND kind=? AND scope_id=? AND status='running'
                   AND lease_owner=? AND lease_epoch=? AND lease_expires_at>? AND (deadline IS NULL OR deadline>?)""",
                (job_id, ANALYSIS_JOB_KIND, payload["conversation_id"], canonical["lease_owner"], epoch, now, now),
            ).fetchone()
            policy = conn.execute(
                "SELECT * FROM message_history_policies WHERE conversation_key=? "
                "AND record_enabled=1 AND analysis_enabled=1",
                (payload["conversation_id"],),
            ).fetchone()
            state = conn.execute(
                "SELECT * FROM message_history_conversations WHERE conversation_key=?", (payload["conversation_id"],)
            ).fetchone()
            if current_job is None or json.loads(current_job[0]) != payload or policy is None or state is None or not self._job_policy_matches(payload, policy):
                conn.rollback()
                return False
            if (payload["start_seq"] != self._reading_watermark(state) + 1
                    or payload["end_seq"] < payload["start_seq"]
                    or state["summary_revision"] < 0):
                conn.rollback()
                return False
            source_rows = conn.execute(
                "SELECT * FROM message_history_messages WHERE conversation_key=? AND seq BETWEEN ? AND ?",
                (payload["conversation_id"], payload["start_seq"], payload["end_seq"]),
            ).fetchall()
            valid_sources = {row["internal_message_id"] for row in source_rows}
            if len(valid_sources) != payload["end_seq"] - payload["start_seq"] + 1:
                conn.rollback()
                return False
            prior_fact_ids = {row[0] for row in conn.execute(
                "SELECT fact_id FROM message_history_facts WHERE conversation_key=? AND NOT EXISTS "
                "(SELECT 1 FROM message_history_fact_supersessions s WHERE "
                "s.conversation_key=message_history_facts.conversation_key "
                "AND s.prior_fact_id=message_history_facts.fact_id)", (payload["conversation_id"],)
            )}
            new_fact_ids: set[str] = set()
            normalized_facts: list[tuple[str, ExtractedFact]] = []
            for index, fact in enumerate(analysis.facts):
                if not set(fact.source_message_ids).issubset(valid_sources):
                    raise ValueError("fact_source_outside_batch")
                if not set(fact.supersedes_fact_ids).issubset(prior_fact_ids):
                    raise ValueError("fact_supersedes_outside_conversation")
                fact_id = "message_fact_" + _digest({"job_id": job_id, "index": index})
                if fact_id in new_fact_ids:
                    raise ValueError("duplicate_fact_id")
                new_fact_ids.add(fact_id)
                normalized_facts.append((fact_id, fact))
            conn.execute(
                """INSERT INTO message_history_batches(job_id,conversation_key,start_seq,end_seq,policy_revision,
                   expected_summary_revision,batch_summary,summary,client_name,model,generation_calls,usage_tokens,
                   prompt_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, payload["conversation_id"], payload["start_seq"], payload["end_seq"],
                 payload["revision"], state["summary_revision"], analysis.batch_summary, analysis.summary,
                 analysis.client_name, analysis.model, analysis.generation_calls, analysis.usage_tokens,
                 analysis.prompt_version, now),
            )
            for fact_id, fact in normalized_facts:
                conn.execute(
                    """INSERT INTO message_history_facts VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (fact_id, payload["conversation_id"], job_id, fact.kind, fact.text,
                     _json(fact.source_message_ids), fact.certainty, fact.actor, fact.time_text,
                     _json(fact.supersedes_fact_ids), now),
                )
                for prior_fact_id in fact.supersedes_fact_ids:
                    conn.execute(
                        "INSERT INTO message_history_fact_supersessions VALUES(?,?,?)",
                        (payload["conversation_id"], prior_fact_id, fact_id),
                    )
            selected = (analysis.reading_manifest or {}).get("coverage_mode") == "selected_text"
            conn.execute(
                """UPDATE message_history_conversations SET covered_seq=?,generation_published_seq=?,summary_revision=summary_revision+1,
                   rolling_summary=?,updated_at=? WHERE conversation_key=? AND generation_published_seq=? AND summary_revision=?""",
                (state["covered_seq"] if selected else payload["end_seq"], max(state["generation_published_seq"], payload["end_seq"]), analysis.summary, now, payload["conversation_id"],
                 state["generation_published_seq"], state["summary_revision"]),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                conn.rollback()
                return False
            self._publish_reading_results(conn, canonical, analysis, policy, state, now)
            self._publish_reading_intelligence(conn, canonical, source_rows, analysis)
            completed = conn.execute(
                """UPDATE background_jobs SET status='succeeded',finished_at=?,updated_at=?,
                   lease_owner=NULL,lease_expires_at=NULL WHERE job_id=? AND status='running'
                   AND lease_owner=? AND lease_epoch=? AND lease_expires_at>?
                   AND (deadline IS NULL OR deadline>?)""",
                (now, now, job_id, canonical["lease_owner"], epoch, now, now),
            )
            if completed.rowcount != 1:
                conn.rollback()
                return False
            fresh_policy = conn.execute(
                "SELECT * FROM message_history_policies WHERE conversation_key=?", (payload["conversation_id"],)
            ).fetchone()
            if fresh_policy is not None:
                self._refresh_reading_schedule(conn, payload["conversation_id"], clear_active=True)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
