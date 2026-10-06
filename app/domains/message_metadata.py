"""Conversation labels and bounded, policy-scoped message context reads."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class ImportedConversationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    platform: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=256)
    conversation_type: str
    conversation_id: str = Field(min_length=1, max_length=512)
    capture_epoch: int = Field(ge=1)
    platform_name: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=512)
    cache_provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("conversation_type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        if value not in {"private", "group", "channel"}:
            raise ValueError("invalid conversation type")
        return value

    @field_validator("cache_provenance")
    @classmethod
    def bounded_provenance(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False).encode()) > 4096:
            raise ValueError("provenance too large")
        if any(not isinstance(k, str) or len(k) > 80 or not isinstance(v, str | int | bool | type(None))
               for k, v in value.items()):
            raise ValueError("provenance must contain bounded scalar values")
        return value


class MessageMetadataRevisionConflict(ValueError):
    pass


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class MessageMetadataMixin:
    def _ensure_metadata_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("""CREATE TABLE IF NOT EXISTS message_conversation_metadata (
            conversation_key TEXT PRIMARY KEY, platform_name TEXT, imported_display_name TEXT,
            cache_provenance_json TEXT NOT NULL DEFAULT '{}', capture_epoch INTEGER,
            user_alias TEXT, manual_display_name TEXT,
            revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""")

    def import_conversation_metadata(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("metadata batch must have at most 100 rows")
        models = [ImportedConversationMetadata.model_validate(item) for item in rows]
        accepted = rejected = 0
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_metadata_schema(conn)
            from app.domains.message_history import _conversation_key, _identity
            for item in models:
                key = _conversation_key(_identity(item.platform, item.account_id, item.conversation_type, item.conversation_id))
                policy = conn.execute("SELECT * FROM message_history_policies WHERE conversation_key=? AND record_enabled=1", (key,)).fetchone()
                # Metadata import can enrich an active whitelist only, and must belong to its current capture epoch.
                if policy is None or policy["capture_epoch"] != item.capture_epoch:
                    rejected += 1
                    continue
                conn.execute("""INSERT INTO message_conversation_metadata
                    (conversation_key,platform_name,imported_display_name,cache_provenance_json,capture_epoch,updated_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(conversation_key) DO UPDATE SET
                    platform_name=excluded.platform_name, imported_display_name=excluded.imported_display_name,
                    cache_provenance_json=excluded.cache_provenance_json,capture_epoch=excluded.capture_epoch,
                    updated_at=excluded.updated_at""",
                    (key, item.platform_name, item.display_name, json.dumps(item.cache_provenance, ensure_ascii=False), item.capture_epoch, _now()))
                accepted += 1
            conn.commit()
        return {"accepted": accepted, "rejected": rejected}

    def get_conversation_metadata(self, key: str, allowed_sources: list[str] | None = None,
                                  allowed_accounts: list[str] | None = None) -> dict[str, Any]:
        with self._connection() as conn:
            policies = self._read_scope(conn, key, allowed_sources, allowed_accounts)
            if not policies:
                raise PermissionError("message_history_not_allowed")
            self._ensure_metadata_schema(conn)
            return self._metadata(conn, policies[0])

    def _metadata(self, conn: sqlite3.Connection, policy: sqlite3.Row) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM message_conversation_metadata WHERE conversation_key=?", (policy["conversation_key"],)).fetchone()
        base = {"conversation_key": policy["conversation_key"], "platform": policy["platform"],
                "account_id": policy["account_id"], "conversation_type": policy["conversation_type"],
                "conversation_id": policy["conversation_id"], "group_id": policy["conversation_id"] if policy["conversation_type"] == "group" else None}
        values = dict(row) if row else {}
        if values.get("capture_epoch") != policy["capture_epoch"]:
            values = {**values, "platform_name": None, "imported_display_name": None,
                      "cache_provenance_json": "{}"}
        alias = values.get("user_alias")
        manual = values.get("manual_display_name")
        imported = values.get("imported_display_name")
        base.update({"platform_name": values.get("platform_name"), "cache_provenance": json.loads(values.get("cache_provenance_json") or "{}"),
                     "cached_display_name": imported, "manual_display_name": manual,
                     "user_alias": alias, "display_name": alias or manual or imported or policy["display_name"] or policy["conversation_id"],
                     "revision": values.get("revision", 0), "updated_at": values.get("updated_at")})
        return base

    def update_conversation_metadata(self, key: str, *, expected_revision: int, user_alias: str | None = None,
                                     display_name: str | None = None, allowed_sources: list[str] | None = None,
                                     allowed_accounts: list[str] | None = None) -> dict[str, Any]:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        for value in (user_alias, display_name):
            if value is not None and (not isinstance(value, str) or len(value) > 512 or not value.strip()):
                raise ValueError("metadata labels must be nonblank and bounded")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policies = self._read_scope(conn, key, allowed_sources, allowed_accounts)
            if not policies:
                raise PermissionError("message_history_not_allowed")
            self._ensure_metadata_schema(conn)
            row = conn.execute("SELECT revision FROM message_conversation_metadata WHERE conversation_key=?", (key,)).fetchone()
            actual = row[0] if row else 0
            if actual != expected_revision:
                conn.rollback()
                raise MessageMetadataRevisionConflict("metadata_revision_conflict")
            old = conn.execute("SELECT user_alias,manual_display_name FROM message_conversation_metadata WHERE conversation_key=?", (key,)).fetchone()
            alias = user_alias if user_alias is not None else (old[0] if old else None)
            name = display_name if display_name is not None else (old[1] if old else None)
            conn.execute("""INSERT INTO message_conversation_metadata
                (conversation_key,user_alias,manual_display_name,revision,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(conversation_key) DO UPDATE SET user_alias=excluded.user_alias,
                manual_display_name=excluded.manual_display_name,revision=excluded.revision,updated_at=excluded.updated_at""",
                (key, alias, name, actual + 1, _now()))
            result = self._metadata(conn, policies[0])
            conn.commit()
            return result

    def resolve_conversations(self, query: str, *, allowed_sources: list[str] | None = None,
                              allowed_accounts: list[str] | None = None, limit: int = 8) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 256 or not 1 <= limit <= 20:
            raise ValueError("invalid conversation resolve query")
        needle = _normalize(query)
        with self._connection() as conn:
            self._ensure_metadata_schema(conn)
            policies = self._read_scope(conn, None, allowed_sources, allowed_accounts)
            matches = []
            for policy in policies:
                meta = self._metadata(conn, policy)
                aliases = {meta["display_name"], meta["conversation_id"], meta["group_id"],
                           meta["cached_display_name"], meta["manual_display_name"], meta["user_alias"]}
                if any(value and _normalize(value) == needle for value in aliases):
                    matches.append(meta)
            has_more = len(matches) > limit
            matches = matches[:limit]
            return {"matches": matches, "ambiguous": len(matches) > 1 or has_more,
                    "has_more": has_more}

    def get_message(self, message_id: str, allowed_sources: list[str] | None = None,
                    allowed_accounts: list[str] | None = None) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM message_history_messages WHERE internal_message_id=?", (message_id,)).fetchone()
            if row is None:
                raise PermissionError("message_history_not_allowed")
            policies = self._read_scope(conn, row["conversation_key"], allowed_sources, allowed_accounts)
            if not policies:
                raise PermissionError("message_history_not_allowed")
            policy = policies[0]
            if row["capture_epoch"] != policy["capture_epoch"]:
                raise PermissionError("message_history_not_allowed")
            value = self._resolved_message(conn, row)
            value["conversation_metadata"] = self._metadata(conn, policy)
            value["untrusted"] = True
            return value

    def get_message_context(self, message_id: str, *, before: int = 10, after: int = 10,
                            allowed_sources: list[str] | None = None,
                            allowed_accounts: list[str] | None = None) -> dict[str, Any]:
        if type(before) is not int or type(after) is not int or not 0 <= before <= 25 or not 0 <= after <= 25:
            raise ValueError("context bounds must be between 0 and 25")
        with self._connection() as conn:
            anchor = conn.execute("SELECT * FROM message_history_messages WHERE internal_message_id=?", (message_id,)).fetchone()
            if anchor is None:
                raise PermissionError("message_history_not_allowed")
            policies = self._read_scope(conn, anchor["conversation_key"], allowed_sources, allowed_accounts)
            if not policies:
                raise PermissionError("message_history_not_allowed")
            policy = policies[0]
            if anchor["capture_epoch"] != policy["capture_epoch"]:
                raise PermissionError("message_history_not_allowed")
            key, seq, epoch = anchor["conversation_key"], anchor["seq"], policy["capture_epoch"]
            left = conn.execute("SELECT * FROM message_history_messages WHERE conversation_key=? AND capture_epoch=? AND seq<? ORDER BY seq DESC LIMIT ?", (key, epoch, seq, before + 1)).fetchall()
            right = conn.execute("SELECT * FROM message_history_messages WHERE conversation_key=? AND capture_epoch=? AND seq>? ORDER BY seq LIMIT ?", (key, epoch, seq, after + 1)).fetchall()
            left = list(reversed(left))
            selected = [*(left[-before:] if before else []), anchor, *right[:after]]
            messages = [self._resolved_message(conn, row) for row in selected]
            for message in messages:
                message["untrusted"] = True
            coverage = conn.execute("SELECT COUNT(*) AS count,MIN(seq) AS first_seq,MAX(seq) AS last_seq FROM message_history_messages WHERE conversation_key=? AND capture_epoch=?",
                                    (key, epoch)).fetchone()
            return {"anchor_message_id": message_id, "conversation_key": key, "messages": messages,
                    "gap_before": bool(left and seq - left[-1]["seq"] > 1),
                    "gap_after": bool(right and right[0]["seq"] - seq > 1),
                    "has_more_before": len(left) > before, "has_more_after": len(right) > after,
                    "coverage": {"capture_epoch": epoch, "message_count": coverage["count"],
                                 "first_seq": coverage["first_seq"], "last_seq": coverage["last_seq"],
                                 "page_only": True, "complete_platform_history": False},
                    "untrusted": True}
