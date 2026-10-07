"""Independent durable capture queue and permission-filtered local history.

Sequence numbers here are local cursors, never backend sequence numbers.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import CaptureBackpressure, CapturePolicy, ConversationRef, MessageEnvelope


class OutboxFull(CaptureBackpressure):
    """Capture must pause until the bounded durable queue has space."""


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bound(value: int, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid_bound")
    return value


_VISIBLE = "p.enabled=1 AND m.epoch=p.epoch AND m.quarantine=0"
_JOIN = "messages m JOIN policies p ON p.key=m.conversation_key"


class Store:
    def __init__(self, path: Path, max_pending: int = 10000):
        self.path = Path(path)
        self.max_pending = _bound(max_pending, 1, 100000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS policies (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    enabled INTEGER NOT NULL, epoch INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, conversation_key TEXT NOT NULL,
                    seq INTEGER NOT NULL, epoch INTEGER NOT NULL,
                    payload TEXT NOT NULL, payload_bytes INTEGER NOT NULL,
                    text TEXT NOT NULL, sender_id TEXT, timestamp INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', quarantine INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(conversation_key,seq));
                CREATE INDEX IF NOT EXISTS message_history ON messages(conversation_key,seq);
                CREATE INDEX IF NOT EXISTS message_outbox ON messages(state,quarantine);
            """)

    @contextmanager
    def _connection(self, *, write: bool = False):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def replace_policies(self, policies: list[CapturePolicy]) -> None:
        snapshot = {p.key: p for p in policies}
        if len(snapshot) != len(policies):
            raise ValueError("duplicate_policy")
        with self._connection(write=True) as conn:
            old = {row["key"]: row for row in conn.execute("SELECT * FROM policies")}
            for key, previous in old.items():
                current = snapshot.get(key)
                if (
                    current is None
                    or not current.record_enabled
                    or current.capture_epoch != previous["epoch"]
                ):
                    # Revocation is irreversible for captured rows, even on regrant.
                    conn.execute(
                        "UPDATE messages SET quarantine=1 WHERE conversation_key=?", (key,)
                    )
            conn.execute("DELETE FROM policies")
            conn.executemany(
                "INSERT INTO policies VALUES(?,?,?,?)",
                [
                    (p.key, _json(p.model_dump()), int(p.record_enabled), p.capture_epoch)
                    for p in policies
                ],
            )

    def policy(self, ref: ConversationRef) -> CapturePolicy | None:
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM policies WHERE key=?", (ref.key,)).fetchone()
            return CapturePolicy.model_validate_json(row[0]) if row else None

    def policies(self) -> list[CapturePolicy]:
        with self._connection() as conn:
            return [
                CapturePolicy.model_validate_json(row[0])
                for row in conn.execute("SELECT payload FROM policies ORDER BY key")
            ]

    def enqueue(self, message: MessageEnvelope) -> bool:
        payload = _json(message.model_dump())
        with self._connection(write=True) as conn:
            policy = conn.execute("SELECT * FROM policies WHERE key=?", (message.key,)).fetchone()
            if not policy or not policy["enabled"] or policy["epoch"] != message.capture_epoch:
                raise PermissionError("message_unavailable")
            existing = conn.execute(
                "SELECT payload,quarantine FROM messages WHERE id=?", (message.internal_id,)
            ).fetchone()
            if existing:
                if existing["quarantine"]:
                    raise PermissionError("message_unavailable")
                previous = json.loads(existing["payload"])
                captured = message.model_dump()
                # Reconnect/backfill changes the local observation time. Preserve
                # the first durable timestamp; every provider content field stays immutable.
                previous.pop("received_at")
                captured.pop("received_at")
                if previous != captured:
                    raise ValueError("identity_conflict")
                return False
            count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE state='pending' AND quarantine=0"
            ).fetchone()[0]
            if count >= self.max_pending:
                raise OutboxFull("outbox_full")
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE conversation_key=?",
                (message.key,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO messages
                (id,conversation_key,seq,epoch,payload,payload_bytes,text,sender_id,timestamp)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    message.internal_id,
                    message.key,
                    seq,
                    message.capture_epoch,
                    payload,
                    len(payload.encode()),
                    message.text,
                    message.sender_id,
                    message.sent_at or message.received_at,
                ),
            )
            return True

    def pending(self, limit: int = 100, max_bytes: int = 2 * 1024 * 1024) -> list[dict]:
        _bound(limit, 1, 100)
        _bound(max_bytes, 1, 16 * 1024 * 1024)
        result, size = [], len(b'{"schema_version":2,"messages":[]}')
        with self._connection() as conn:
            for row in conn.execute(
                f"SELECT m.* FROM {_JOIN} WHERE {_VISIBLE} AND m.state='pending' ORDER BY m.rowid LIMIT ?",
                (limit,),
            ):
                additional = row["payload_bytes"] + (1 if result else 0)
                if size + additional > max_bytes:
                    break
                result.append(json.loads(row["payload"]))
                size += additional
        return result

    @staticmethod
    def _identity(item: dict) -> tuple[str, str, str]:
        keys = ("platform", "account_id", "message_id")
        if not isinstance(item, dict) or any(
            type(item.get(k)) is not str or not item[k] for k in keys
        ):
            raise ValueError("invalid_acknowledgement")
        return tuple(item[k] for k in keys)

    def apply_ack(self, submitted: list[dict], response: dict) -> None:
        if not isinstance(response, dict) or set(response) != {"acknowledged", "rejected"}:
            raise ValueError("invalid_acknowledgement")
        originals = {}
        for raw in submitted:
            item = MessageEnvelope.model_validate(raw)
            identity = self._identity(raw)
            if identity in originals:
                raise ValueError("duplicate_submission")
            originals[identity] = item
        updates, seen = [], set()
        for category in ("acknowledged", "rejected"):
            entries = response[category]
            if not isinstance(entries, list):
                raise ValueError("invalid_acknowledgement")  # noqa: TRY004 -- one protocol error type
            for raw in entries:
                identity = self._identity(raw)
                expected = {"platform", "account_id", "message_id"}
                if category == "rejected":
                    expected |= {"reason", "permanent"}
                    if (
                        type(raw.get("permanent")) is not bool
                        or not isinstance(raw.get("reason"), str)
                        or not raw["reason"]
                    ):
                        raise ValueError("invalid_acknowledgement")
                if set(raw) != expected or identity not in originals or identity in seen:
                    raise ValueError("invalid_acknowledgement")
                seen.add(identity)
                updates.append((originals[identity], category, raw.get("permanent", False)))
        with self._connection(write=True) as conn:
            # Validate every submission against durable content before any mutations.
            for item in originals.values():
                row = conn.execute(
                    "SELECT payload FROM messages WHERE id=?", (item.internal_id,)
                ).fetchone()
                if row is None or row[0] != _json(item.model_dump()):
                    raise ValueError("invalid_submission")
            for item, category, permanent in updates:
                if category == "acknowledged":
                    conn.execute(
                        "UPDATE messages SET state='acked' WHERE id=? AND quarantine=0",
                        (item.internal_id,),
                    )
                elif permanent:
                    conn.execute("UPDATE messages SET quarantine=1 WHERE id=?", (item.internal_id,))

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        item = json.loads(row["payload"])
        item.update(
            provider_message_id=item["message_id"],
            message_id=row["id"],
            conversation_key=row["conversation_key"],
            seq=row["seq"],
            sync_state=row["state"],
        )
        return item

    @staticmethod
    def _grant(conn, key: str) -> None:
        row = conn.execute("SELECT enabled FROM policies WHERE key=?", (key,)).fetchone()
        if row is None or not row[0]:
            raise PermissionError("message_unavailable")

    @staticmethod
    def _page(items: list, total: int, limit: int, offset: int) -> dict:
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + len(items) if offset + len(items) < total else None,
        }

    @staticmethod
    def _scope(allowed_keys: set[str] | None) -> tuple[str, list]:
        if allowed_keys is None:
            return "", []
        if not isinstance(allowed_keys, set) or any(not isinstance(k, str) for k in allowed_keys):
            raise ValueError("invalid_scope")
        if not allowed_keys:
            return " AND 0", []
        keys = sorted(allowed_keys)
        return " AND m.conversation_key IN (" + ",".join("?" for _ in keys) + ")", keys

    @staticmethod
    def _scope_grant(key: str, allowed_keys: set[str] | None) -> None:
        if allowed_keys is not None and key not in allowed_keys:
            raise PermissionError("message_unavailable")

    def conversations(
        self, limit: int = 50, offset: int = 0, *, allowed_keys: set[str] | None = None
    ) -> dict:
        _bound(limit, 1, 200)
        _bound(offset, 0, 2**31 - 1)
        scope, scope_params = self._scope(allowed_keys)
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT p.payload,m.conversation_key,COUNT(*) AS message_count,
                MAX(m.seq) AS latest_seq FROM {_JOIN} WHERE {_VISIBLE}{scope}
                GROUP BY m.conversation_key ORDER BY MAX(m.rowid) DESC LIMIT ? OFFSET ?""",
                [*scope_params, limit, offset],
            ).fetchall()
            total = conn.execute(
                f"SELECT COUNT(DISTINCT m.conversation_key) FROM {_JOIN} WHERE {_VISIBLE}{scope}",
                scope_params,
            ).fetchone()[0]
            items = []
            for row in rows:
                item = json.loads(row["payload"])
                item.update(
                    conversation_key=row["conversation_key"],
                    message_count=row["message_count"],
                    latest_seq=row["latest_seq"],
                )
                items.append(item)
            return self._page(items, total, limit, offset)

    def search(
        self,
        query: str,
        conversation_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
        since: int | None = None,
        until: int | None = None,
        sender_id: str | None = None,
        *,
        allowed_keys: set[str] | None = None,
    ) -> dict:
        _bound(limit, 1, 200)
        _bound(offset, 0, 2**31 - 1)
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValueError("invalid_query")
        conditions, params = [_VISIBLE, "m.text LIKE ? ESCAPE '\\'"], []
        params.append(
            "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        if conversation_key is not None:
            conditions.append("m.conversation_key=?")
            params.append(conversation_key)
        for value, operator in ((since, ">="), (until, "<=")):
            if value is not None:
                _bound(value, 0, 253402300799)
                conditions.append(f"m.timestamp{operator}?")
                params.append(value)
        if since is not None and until is not None and since > until:
            raise ValueError("invalid_time_range")
        if sender_id is not None:
            if not isinstance(sender_id, str) or not sender_id or len(sender_id) > 512:
                raise ValueError("invalid_sender")
            conditions.append("m.sender_id=?")
            params.append(sender_id)
        scope, scope_params = self._scope(allowed_keys)
        where = " AND ".join(conditions) + scope
        params.extend(scope_params)
        with self._connection() as conn:
            if conversation_key is not None:
                self._scope_grant(conversation_key, allowed_keys)
                self._grant(conn, conversation_key)
            total = conn.execute(f"SELECT COUNT(*) FROM {_JOIN} WHERE {where}", params).fetchone()[
                0
            ]
            rows = conn.execute(
                f"SELECT m.* FROM {_JOIN} WHERE {where} ORDER BY m.rowid DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            return self._page([self._row(row) for row in rows], total, limit, offset)

    def history(
        self,
        conversation_key: str,
        before_seq: int | None = None,
        limit: int = 50,
        *,
        allowed_keys: set[str] | None = None,
    ) -> dict:
        _bound(limit, 1, 200)
        if before_seq is not None:
            _bound(before_seq, 1, 2**63 - 1)
        self._scope(allowed_keys)
        self._scope_grant(conversation_key, allowed_keys)
        with self._connection() as conn:
            self._grant(conn, conversation_key)
            where = f"{_VISIBLE} AND m.conversation_key=?"
            params = [conversation_key]
            if before_seq is not None:
                where += " AND m.seq<?"
                params.append(before_seq)
            total = conn.execute(f"SELECT COUNT(*) FROM {_JOIN} WHERE {where}", params).fetchone()[
                0
            ]
            rows = conn.execute(
                f"SELECT m.* FROM {_JOIN} WHERE {where} ORDER BY m.seq DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            items = [self._row(row) for row in reversed(rows)]
            page = self._page(items, total, limit, 0)
            page["next_before_seq"] = items[0]["seq"] if len(items) < total else None
            return page

    def _message(self, conn, message_id: str, allowed_keys: set[str] | None = None):
        scope, params = self._scope(allowed_keys)
        row = conn.execute(
            f"SELECT m.* FROM {_JOIN} WHERE {_VISIBLE} AND m.id=?{scope}", [message_id, *params]
        ).fetchone()
        if row is None:
            raise PermissionError("message_unavailable")
        return row

    def read_message(self, message_id: str, *, allowed_keys: set[str] | None = None) -> dict:
        with self._connection() as conn:
            return self._row(self._message(conn, message_id, allowed_keys))

    def context(
        self,
        message_id: str,
        before: int = 10,
        after: int = 10,
        *,
        allowed_keys: set[str] | None = None,
    ) -> dict:
        _bound(before, 0, 100)
        _bound(after, 0, 100)
        with self._connection() as conn:
            row = self._message(conn, message_id, allowed_keys)
            parts = {}
            for label, operator, direction, limit in (
                ("before", "<", "DESC", before),
                ("after", ">", "ASC", after),
            ):
                rows = conn.execute(
                    f"SELECT m.* FROM {_JOIN} WHERE {_VISIBLE} AND m.conversation_key=? AND m.seq{operator}? ORDER BY m.seq {direction} LIMIT ?",
                    (row["conversation_key"], row["seq"], limit),
                ).fetchall()
                parts[label] = [
                    self._row(r) for r in (reversed(rows) if label == "before" else rows)
                ]
            return {"message": self._row(row), **parts}

    def status(self) -> dict:
        with self._connection() as conn:
            return {
                "pending": conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE state='pending' AND quarantine=0"
                ).fetchone()[0],
                "acked": conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE state='acked' AND quarantine=0"
                ).fetchone()[0],
                "quarantined": conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE quarantine=1"
                ).fetchone()[0],
                "active_conversations": conn.execute(
                    "SELECT COUNT(*) FROM policies WHERE enabled=1"
                ).fetchone()[0],
            }
