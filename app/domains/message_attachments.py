"""Message-bound media index. Downloading remains the collector's responsibility."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SAFE_MEDIA_TYPES = {
    "image": {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/avif"},
    "video": {"video/mp4", "video/webm", "video/quicktime", "video/x-msvideo", "video/x-matroska"},
}


def cache_key(platform: str, account_id: str, message_id: str, ordinal: int) -> str:
    raw = json.dumps([platform, account_id, message_id, ordinal], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class AttachmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ordinal: int = Field(ge=0, le=1023)
    kind: Literal["image", "video"]
    file_name: str | None = Field(default=None, max_length=512)


class MediaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    platform: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=256)
    message_id: str = Field(min_length=1, max_length=512)
    conversation_type: Literal["private", "group", "channel"]
    conversation_id: str = Field(min_length=1, max_length=512)
    policy_revision: int = Field(ge=1)
    capture_epoch: int | None = Field(default=None, ge=1)
    ordinal: int = Field(ge=0, le=1023)
    state: Literal["pending", "cached", "failed", "expired", "unavailable"]
    mime_type: str | None = Field(default=None, max_length=120)
    size_bytes: int | None = Field(default=None, ge=0, le=2_000_000_000)
    sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    width: int | None = Field(default=None, ge=1, le=100000)
    height: int | None = Field(default=None, ge=1, le=100000)
    duration_ms: int | None = Field(default=None, ge=0, le=604800000)
    expires_at: int | None = Field(default=None, ge=0, le=253402300799)
    error_code: str | None = Field(default=None, max_length=120, pattern="^[a-zA-Z0-9_.-]+$")

    @model_validator(mode="after")
    def cached_metadata(self):
        if self.state == "cached" and any(value is None for value in
                (self.mime_type, self.size_bytes, self.sha256, self.expires_at)):
            raise ValueError("cached media requires MIME, size, digest, expiry")
        return self


class MessageAttachmentMixin:
    @staticmethod
    def _ensure_attachment_schema(conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_history_attachments (
                attachment_id TEXT PRIMARY KEY, cache_key TEXT NOT NULL UNIQUE,
                internal_message_id TEXT NOT NULL, conversation_key TEXT NOT NULL,
                ordinal INTEGER NOT NULL, kind TEXT NOT NULL, file_name TEXT,
                state TEXT NOT NULL DEFAULT 'pending', mime_type TEXT, size_bytes INTEGER,
                sha256 TEXT, width INTEGER, height INTEGER, duration_ms INTEGER,
                expires_at INTEGER, error_code TEXT, updated_at TEXT NOT NULL,
                UNIQUE(internal_message_id,ordinal)
            );
            CREATE INDEX IF NOT EXISTS idx_message_attachments_conversation
                ON message_history_attachments(conversation_key,updated_at DESC);
        """)

    @staticmethod
    def _attachment_refs_match(conn, internal_id, item):
        # Old schema-v1 retries omit this optional field and cannot erase refs.
        if "attachments" not in item.model_fields_set:
            return True
        rows = conn.execute("SELECT ordinal,kind,file_name FROM message_history_attachments WHERE internal_message_id=? ORDER BY ordinal", (internal_id,)).fetchall()
        return [tuple(row) for row in rows] == sorted((ref.ordinal, ref.kind, ref.file_name) for ref in item.attachments)

    @staticmethod
    def _insert_attachment_refs(conn, internal_id, conversation_key, item):
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        for ref in item.attachments:
            key = cache_key(item.platform, item.account_id, item.message_id, ref.ordinal)
            conn.execute("""INSERT INTO message_history_attachments
                (attachment_id,cache_key,internal_message_id,conversation_key,ordinal,kind,file_name,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""", ("message_attachment_" + key, key, internal_id,
                        conversation_key, ref.ordinal, ref.kind, ref.file_name, now))

    @staticmethod
    def _attachment(row):
        value = dict(row)
        # Expiry is enforced on every read even if the cleanup worker is delayed.
        if value["state"] == "cached" and (value["expires_at"] or 0) <= time.time():
            value["state"] = "expired"
        value.pop("cache_key", None)
        return value

    def attachments(self, conversation_key=None, *, kind=None, query=None, limit=50, offset=0,
                    allowed_sources=None, allowed_accounts=None):
        size, start = self._page(limit, offset)
        if kind not in (None, "image", "video"):
            raise ValueError("invalid attachment kind")
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 256):
            raise ValueError("invalid attachment query")
        with self._connection() as conn:
            policies = self._read_scope(conn, conversation_key, allowed_sources, allowed_accounts)
            keys = [row["conversation_key"] for row in policies if row["media_enabled"]]
            if conversation_key and not keys:
                raise PermissionError("media_disabled")
            if not keys:
                return {"attachments": [], "next_offset": None, "has_more": False}
            conditions = ["a.conversation_key IN (" + ",".join("?" for _ in keys) + ")"]
            params = list(keys)
            if kind:
                conditions.append("a.kind=?")
                params.append(kind)
            if query:
                conditions.append("(a.file_name LIKE ? ESCAPE '\\' OR m.text LIKE ? ESCAPE '\\')")
                escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.extend(["%" + escaped + "%"] * 2)
            rows = conn.execute("SELECT a.*,m.platform,m.account_id,m.provider_message_id,m.received_at FROM message_history_attachments a JOIN message_history_messages m USING(internal_message_id) WHERE " + " AND ".join(conditions) + " ORDER BY m.received_at DESC,a.attachment_id LIMIT ? OFFSET ?", [*params, size + 1, start]).fetchall()
            more = len(rows) > size
            return {"attachments": [self._attachment(row) for row in rows[:size]],
                    "next_offset": start + size if more else None, "has_more": more}

    def attachment(self, attachment_id, *, allowed_sources=None, allowed_accounts=None):
        with self._connection() as conn:
            row = conn.execute("SELECT a.*,m.platform,m.account_id,m.provider_message_id,m.received_at FROM message_history_attachments a JOIN message_history_messages m USING(internal_message_id) WHERE attachment_id=?", (attachment_id,)).fetchone()
            if row is None:
                raise PermissionError("attachment_unavailable")
            policies = self._read_scope(conn, row["conversation_key"], allowed_sources, allowed_accounts)
            if not policies or not policies[0]["media_enabled"]:
                raise PermissionError("attachment_unavailable")
            return self._attachment(row)

    def update_media(self, rows, *, schema_version=1):
        if type(schema_version) is not int or schema_version not in (1, 2):
            raise ValueError("unsupported_schema_version")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("invalid media batch")
        ack, rejected = [], []
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for raw in rows:
                identity = {key: raw.get(key) for key in ("platform", "account_id", "message_id", "ordinal")} if isinstance(raw, dict) else {}
                try:
                    update = MediaUpdate.model_validate(raw)
                    key = cache_key(update.platform, update.account_id, update.message_id, update.ordinal)
                    row = conn.execute("""SELECT a.*,p.record_enabled,p.media_enabled,p.revision,p.capture_epoch,p.minimum_import_version,
                        p.conversation_type,p.conversation_id FROM message_history_attachments a
                        JOIN message_history_policies p USING(conversation_key) WHERE a.cache_key=?""", (key,)).fetchone()
                    if row is None:
                        raise ValueError("missing_attachment")
                    if schema_version < row["minimum_import_version"]:
                        raise ValueError("import_version_required")
                    if schema_version == 2 and update.capture_epoch is None:
                        raise ValueError("invalid_media")
                    permission_matches = (row["capture_epoch"] == update.capture_epoch if schema_version == 2
                                          else row["revision"] == update.policy_revision)
                    if (not row["record_enabled"] or not row["media_enabled"] or not permission_matches
                            or row["conversation_type"] != update.conversation_type or row["conversation_id"] != update.conversation_id):
                        raise ValueError("policy_fenced")
                    expired = row["state"] == "expired" or (row["state"] == "cached" and (row["expires_at"] or 0) <= time.time())
                    transitions = {"pending": {"pending", "cached", "failed", "expired", "unavailable"},
                                   "cached": {"cached", "expired"}, "failed": {"failed", "expired"},
                                   "unavailable": {"unavailable", "expired"}, "expired": {"expired"}}
                    if update.state not in transitions["expired" if expired else row["state"]]:
                        raise ValueError("state_conflict")
                    if update.state == "cached":
                        if update.mime_type not in SAFE_MEDIA_TYPES[row["kind"]] or update.expires_at <= time.time():
                            raise ValueError("invalid_cached_media")
                        if row["state"] == "cached" and any(row[field] != getattr(update, field) for field in ("sha256", "size_bytes", "mime_type", "expires_at")):
                            raise ValueError("immutable_cached_media")
                    fields = ("state", "mime_type", "size_bytes", "sha256", "width", "height", "duration_ms", "expires_at", "error_code")
                    values = [row[field] if update.state == "expired" and field not in update.model_fields_set
                              else getattr(update, field) for field in fields]
                    conn.execute("UPDATE message_history_attachments SET " + ",".join(field + "=?" for field in fields) + ",updated_at=? WHERE cache_key=?", [*values, datetime.now(UTC).isoformat(timespec="microseconds"), key])
                    ack.append(identity)
                except (ValidationError, ValueError) as exc:
                    reason = "invalid_media" if isinstance(exc, ValidationError) else str(exc)
                    rejected.append({**identity, "reason": reason, "permanent": True})
            conn.commit()
        return {"acknowledged": ack, "rejected": rejected}
