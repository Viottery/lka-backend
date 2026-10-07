"""Opt-in TTL media cache and durable metadata delivery, independent of providers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .models import CapturePolicy, MessageEnvelope


def attachment_key(message: MessageEnvelope, ordinal: int) -> str:
    raw = json.dumps(
        [message.platform, message.account_id, message.message_id, ordinal],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


class MediaCache:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.path = config.data_dir / "media.sqlite3"
        self._cleanup_cursor = 0
        self.directory = config.data_dir / "media"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS media (
                key TEXT PRIMARY KEY, parent_id TEXT NOT NULL, message TEXT NOT NULL,
                update_json TEXT NOT NULL, state TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0, conversation_key TEXT NOT NULL,
                epoch INTEGER NOT NULL, expires_at INTEGER, size_bytes INTEGER)""")

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # SELECT -> UPDATE reconciliation must reserve the write lock first;
            # otherwise concurrent cache writers can deadlock its transaction upgrade.
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                yield conn
        finally:
            conn.close()

    def enqueue(self, message: MessageEnvelope, policy: CapturePolicy):
        with self._connection() as conn:
            for ref in message.attachments:
                update = {
                    **message.conversation.model_dump(),
                    "message_id": message.message_id,
                    "ordinal": ref.ordinal,
                    "policy_revision": policy.revision,
                    "capture_epoch": message.capture_epoch,
                    "state": "pending",
                }
                conn.execute(
                    "INSERT OR IGNORE INTO media VALUES(?,?,?,?,?,0,0,?,?,NULL,NULL)",
                    (
                        attachment_key(message, ref.ordinal),
                        message.internal_id,
                        message.model_dump_json(),
                        json.dumps(update),
                        "pending",
                        message.key,
                        message.capture_epoch,
                    ),
                )

    def _rows(self, *, where="1=1", params=(), limit=100):
        with self._connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"SELECT rowid AS cursor,* FROM media WHERE {where} ORDER BY rowid LIMIT ?",
                    (*params, limit),
                )
            ]

    def reconcile(self, policies):
        """Fence EVERY observed media revocation before applying the new policy snapshot.

        Media permission can change without changing capture_epoch. Regrant never
        restores old media jobs, cache bytes, or previously expired references.
        """
        snapshot = {p.key: p for p in policies}
        with self._connection() as conn:
            keys = conn.execute("SELECT DISTINCT conversation_key,epoch FROM media WHERE revoked=0")
            for item in keys:
                policy = snapshot.get(item["conversation_key"])
                if (
                    not policy
                    or not policy.record_enabled
                    or not policy.media_enabled
                    or policy.capture_epoch != item["epoch"]
                ):
                    rows = conn.execute(
                        "SELECT key FROM media WHERE conversation_key=? AND epoch=? AND revoked=0",
                        (item["conversation_key"], item["epoch"]),
                    )
                    for row in rows:
                        self._remove_file(row["key"])
                    conn.execute(
                        "UPDATE media SET revoked=1 WHERE conversation_key=? AND epoch=?",
                        (item["conversation_key"], item["epoch"]),
                    )

    def _set(self, key, update, *, revoked=False):
        with self._connection() as conn:
            conn.execute(
                """UPDATE media SET update_json=?,state=?,delivered=0,
                revoked=MAX(revoked,?),expires_at=?,size_bytes=? WHERE key=?""",
                (
                    json.dumps(update),
                    update["state"],
                    int(revoked),
                    update.get("expires_at"),
                    update.get("size_bytes"),
                    key,
                ),
            )

    def _eligible(self, row):
        message = MessageEnvelope.model_validate_json(row["message"])
        policy = self.store.policy(message.conversation)
        if (
            row["revoked"]
            or not policy
            or not policy.record_enabled
            or not policy.media_enabled
            or policy.capture_epoch != message.capture_epoch
        ):
            return None
        try:
            parent = self.store.read_message(row["parent_id"])
        except PermissionError:
            return None
        return message if parent["sync_state"] == "acked" else False

    def cleanup(self):
        now = int(time.time())
        rows = self._rows(where="revoked=0 AND rowid>?", params=(self._cleanup_cursor,))
        self._cleanup_cursor = rows[-1]["cursor"] if rows else 0
        for row in rows:
            update = json.loads(row["update_json"])
            eligible = self._eligible(row)
            if eligible is None:
                self._remove_file(row["key"])
                if not row["revoked"]:
                    self._set(row["key"], {**update, "state": "unavailable"}, revoked=True)
            elif row["state"] == "cached":
                if (
                    update["expires_at"] <= now
                    or not (self.directory / (row["key"] + ".bin")).is_file()
                ):
                    self._remove_file(row["key"])
                    self._set(row["key"], {**update, "state": "expired"})
        # Evict in oldest-first bounded pages; only cached size metadata enters
        # the aggregate. No historical message bodies are loaded for accounting.
        with self._connection() as conn:
            total = conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM media WHERE state='cached' AND revoked=0"
            ).fetchone()[0]
        while total > self.config.media_max_bytes:
            with self._connection() as conn:
                oldest = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT key,update_json,size_bytes FROM media WHERE state='cached' AND revoked=0 ORDER BY expires_at LIMIT 100"
                    )
                ]
            if not oldest:
                break
            for row in oldest:
                if total <= self.config.media_max_bytes:
                    break
                self._remove_file(row["key"])
                self._set(row["key"], {**json.loads(row["update_json"]), "state": "expired"})
                total -= row["size_bytes"]

    def _remove_file(self, key):
        (self.directory / (key + ".bin")).unlink(missing_ok=True)

    async def tick(self, adapters, backend):
        await asyncio.to_thread(self.cleanup)
        if self.config.media_enabled:
            # One bounded download per tick; no model calls or unbounded tasks.
            for row in await asyncio.to_thread(self._rows, where="state='pending' AND revoked=0"):
                if row["state"] != "pending" or row["revoked"]:
                    continue
                message = await asyncio.to_thread(self._eligible, row)
                if not message:
                    continue
                adapter = next(
                    (
                        a
                        for a in adapters
                        if a.platform == message.platform and a.account_id == message.account_id
                    ),
                    None,
                )
                if adapter is None:
                    continue
                update = json.loads(row["update_json"])
                ref = next(r for r in message.attachments if r.ordinal == update["ordinal"])
                max_bytes = min(
                    self.config.media_max_bytes,
                    self.config.image_max_bytes
                    if ref.kind == "image"
                    else self.config.video_max_bytes,
                )
                target = self.directory / (row["key"] + ".part")
                try:
                    if target.is_symlink():
                        raise ValueError("unsafe_cache_path")
                    # A crashed exclusive writer may leave a partial file. This
                    # worker owns the capture session lease; abandoned bytes are
                    # never treated as a completed cache entry.
                    await asyncio.to_thread(target.unlink, missing_ok=True)
                    metadata = await adapter.download_attachment(
                        message, ref.ordinal, target, max_bytes
                    )
                    if not await asyncio.to_thread(self._eligible, row):
                        raise PermissionError("media_permission_changed")
                    size, digest = await asyncio.to_thread(_file_metadata, target)
                    if size != metadata.size_bytes or size > max_bytes:
                        raise ValueError("media_size_mismatch")
                    allowed_mimes = {
                        "image": {
                            "image/jpeg",
                            "image/png",
                            "image/gif",
                            "image/webp",
                            "image/bmp",
                            "image/avif",
                        },
                        "video": {
                            "video/mp4",
                            "video/webm",
                            "video/quicktime",
                            "video/x-msvideo",
                            "video/x-matroska",
                        },
                    }
                    if metadata.mime_type not in allowed_mimes[ref.kind]:
                        raise ValueError("unsupported_media_type")
                    await asyncio.to_thread(target.replace, self.directory / (row["key"] + ".bin"))
                    update.update(
                        state="cached",
                        mime_type=metadata.mime_type,
                        size_bytes=size,
                        sha256=digest,
                        expires_at=int(time.time()) + self.config.media_ttl_seconds,
                    )
                    for name in ("width", "height", "duration_ms"):
                        if getattr(metadata, name) is not None:
                            update[name] = getattr(metadata, name)
                except (RuntimeError, ValueError, PermissionError, OSError):
                    update.update(state="failed", error_code="media_download_failed")
                finally:
                    target.unlink(missing_ok=True)
                await asyncio.to_thread(self._set, row["key"], update)
                break
        await asyncio.to_thread(self.cleanup)
        rows = []
        for row in await asyncio.to_thread(self._rows, where="delivered=0 AND revoked=0"):
            if not row["delivered"] and not row["revoked"]:
                message = await asyncio.to_thread(self._eligible, row)
                if message:
                    rows.append(row)
                if len(rows) >= 100:
                    break
        if rows:
            updates = [json.loads(row["update_json"]) for row in rows]
            response = await backend.import_media(updates)
            await asyncio.to_thread(self._apply_ack, rows, response)

    def _apply_ack(self, rows, response):
        if not isinstance(response, dict) or set(response) != {"acknowledged", "rejected"}:
            raise ValueError("invalid_media_ack")
        fields = ("platform", "account_id", "message_id", "ordinal")
        originals = {tuple(json.loads(row["update_json"])[f] for f in fields): row for row in rows}
        seen, accepted, revoked = set(), [], []
        for category in ("acknowledged", "rejected"):
            if not isinstance(response[category], list):
                raise ValueError("invalid_media_ack")  # noqa: TRY004 -- wire protocol error
            for item in response[category]:
                if not isinstance(item, dict) or any(f not in item for f in fields):
                    raise ValueError("invalid_media_ack")
                identity = tuple(item[f] for f in fields)
                expected = set(fields) | (
                    {"reason", "permanent"} if category == "rejected" else set()
                )
                if (
                    set(item) != expected
                    or identity not in originals
                    or identity in seen
                    or type(item["ordinal"]) is not int
                ):
                    raise ValueError("invalid_media_ack")
                if category == "rejected" and (
                    type(item["permanent"]) is not bool or not isinstance(item["reason"], str)
                ):
                    raise ValueError("invalid_media_ack")
                seen.add(identity)
                if category == "acknowledged":
                    accepted.append(originals[identity])
                elif item["permanent"]:
                    revoked.append(originals[identity])
        with self._connection() as conn:
            for row in accepted:
                # A policy/TTL cleanup during HTTP cannot acknowledge a newer state.
                conn.execute(
                    "UPDATE media SET delivered=1 WHERE key=? AND update_json=? AND revoked=0",
                    (row["key"], row["update_json"]),
                )
            for row in revoked:
                conn.execute("UPDATE media SET revoked=1 WHERE key=?", (row["key"],))
        for row in revoked:
            self._remove_file(row["key"])

    def attachments(self, *, allowed_keys=None, conversation_key=None, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("invalid_page")
        items, total, cursor = [], 0, 0
        conditions, params = ["revoked=0"], []
        if conversation_key:
            conditions.append("conversation_key=?")
            params.append(conversation_key)
        if allowed_keys is not None:
            if not allowed_keys:
                return {"items": [], "total": 0, "next_offset": None}
            conditions.append("conversation_key IN (" + ",".join("?" for _ in allowed_keys) + ")")
            params.extend(sorted(allowed_keys))
        while True:
            rows = self._rows(
                where=" AND ".join([*conditions, "rowid>?"]), params=(*params, cursor)
            )
            if not rows:
                break
            for row in rows:
                cursor = row["cursor"]
                if not self._eligible(row):
                    continue
                total += 1
                if offset < total <= offset + limit:
                    items.append(self._public_attachment(row))
        return {
            "items": items,
            "total": total,
            "next_offset": offset + limit if total > offset + limit else None,
        }

    def _public_attachment(self, row):
        message = MessageEnvelope.model_validate_json(row["message"])
        update = json.loads(row["update_json"])
        if update["state"] == "cached" and update["expires_at"] <= int(time.time()):
            update["state"] = "expired"
        update.pop("error_code", None)
        return {
            **update,
            "attachment_id": "message_attachment_" + row["key"],
            "message_id": row["parent_id"],
            "provider_message_id": message.message_id,
            "conversation_key": message.key,
        }

    def read_chunk(self, attachment_id: str, *, offset=0, length=65536, allowed_keys=None):
        if (
            type(offset) is not int
            or offset < 0
            or type(length) is not int
            or not 1 <= length <= 65536
        ):
            raise ValueError("invalid_media_range")
        key = attachment_id.removeprefix("message_attachment_")
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise PermissionError("attachment_unavailable")
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM media WHERE key=?", (key,)).fetchone()
        if row is None:
            raise PermissionError("attachment_unavailable")
        message = MessageEnvelope.model_validate_json(row["message"])
        update = json.loads(row["update_json"])
        if (
            (allowed_keys is not None and message.key not in allowed_keys)
            or not self._eligible(row)
            or update["state"] != "cached"
            or update["expires_at"] <= int(time.time())
        ):
            raise PermissionError("attachment_unavailable")
        path = self.directory / (key + ".bin")
        if path.is_symlink():
            raise PermissionError("attachment_unavailable")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                if os.fstat(handle.fileno()).st_size != update["size_bytes"]:
                    raise PermissionError("attachment_unavailable")
                handle.seek(offset)
                content = handle.read(length)
        except OSError:
            raise PermissionError("attachment_unavailable") from None
        if not self._eligible(row):
            raise PermissionError("attachment_unavailable")
        return {
            "attachment_id": attachment_id,
            "mime_type": update["mime_type"],
            "sha256": update["sha256"],
            "size_bytes": update["size_bytes"],
            "offset": offset,
            "data_base64": base64.b64encode(content).decode(),
            "next_offset": offset + len(content)
            if offset + len(content) < update["size_bytes"]
            else None,
        }


def _file_metadata(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        size = 0
        for chunk in iter(lambda: handle.read(65536), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()
