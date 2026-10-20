"""SQLite conversation records with a fixed-source CAS publication fence.

Only deterministic validated deltas reach this store. No provider or tool calls.
"""

import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4


def source_digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ContextRecordConflict(ValueError):
    pass


class ContextRecordStore:
    def __init__(self, conn_factory):
        self.connect = conn_factory

    def ensure_schema(self):
        with closing(self.connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS context_record_heads(
                    session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    covered_seq INTEGER NOT NULL, project_id TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS context_records(
                    session_id TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    content_json TEXT NOT NULL, PRIMARY KEY(session_id,record_id));
                CREATE TABLE IF NOT EXISTS context_record_events(
                    event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, head_revision INTEGER NOT NULL,
                    change_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS context_record_events_session
                    ON context_record_events(session_id,head_revision);
                CREATE TABLE IF NOT EXISTS context_record_pending(
                    session_id TEXT PRIMARY KEY, target_seq INTEGER NOT NULL);
            """)

    @staticmethod
    def mark_pending(conn, session_id, target_seq):
        conn.execute("INSERT INTO context_record_pending VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET "
                     "target_seq=MAX(context_record_pending.target_seq,excluded.target_seq)", (session_id, target_seq))

    @staticmethod
    def scope(conn, session_id):
        row = conn.execute("SELECT status,metadata FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not row or row["status"] != "active":
            raise ContextRecordConflict("Session unavailable")
        metadata = json.loads(row["metadata"])
        # All records remain session-local, even when the workspace changes.
        # Current project identity is a publication fence, never a model field.
        return metadata.get("project_id")

    def snapshot(self, session_id, *, target_seq=None, conn=None, include_history=True):
        owned = conn is None
        conn = conn or self.connect()
        try:
            if owned:
                conn.execute("BEGIN")
            project_id = self.scope(conn, session_id)
            head = conn.execute("SELECT * FROM context_record_heads WHERE session_id=?", (session_id,)).fetchone()
            records = [json.loads(row[0]) for row in conn.execute(
                "SELECT content_json FROM context_records WHERE session_id=? ORDER BY record_id", (session_id,))]
            covered = head["covered_seq"] if head else 0
            if include_history:
                rows = [dict(row) for row in conn.execute(
                    "SELECT * FROM agent_session_context_messages WHERE session_id=? AND seq<=? ORDER BY seq",
                    (session_id, max(target_seq, covered) if target_seq is not None else 2**63-1))]
            else:
                refs = sorted({s["seq"] for r in records for s in r["sources"]})
                rows = [dict(row) for row in conn.execute(
                    "SELECT * FROM agent_session_context_messages WHERE session_id=? "
                    "AND (seq>? OR seq IN (SELECT value FROM json_each(?))) ORDER BY seq",
                    (session_id, covered, json.dumps(refs)))]
            # A withdrawn or edited source invalidates the derived view, not just
            # a future publication. Never leak revoked text through old records.
            sources = {r["seq"]: r for r in rows}
            hashes = {r["seq"]: hashlib.sha256(r["content"].encode()).hexdigest() for r in rows}
            for record in records:
                for support in record["sources"]:
                    row = sources.get(support["seq"])
                    if (row is None or hashes[support["seq"]] != support["message_sha256"]
                        or row["role"] != support["role"] or row["trace_id"] != support["trace_id"]
                        or row["created_at"] != support["created_at"]):
                        raise ContextRecordConflict("Derived source changed or was withdrawn")
            return {"session_id": session_id, "project_id": project_id,
                    "revision": head["revision"] if head else 0, "covered_seq": covered,
                    "records": records, "messages": rows,
                    "source_digest": source_digest(rows) if include_history else None,
                    "updated_at": head["updated_at"] if head else None}
        finally:
            if owned:
                conn.close()

    def publish(self, snapshot, changes, *, lease=None):
        session_id = snapshot["session_id"]
        rows = snapshot["messages"]
        target = rows[-1]["seq"] if rows else snapshot["covered_seq"]
        now = datetime.now(UTC).isoformat()
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self.snapshot(session_id, target_seq=target, conn=conn)
            if (current["project_id"] != snapshot["project_id"]
                or current["revision"] != snapshot["revision"]
                or current["covered_seq"] != snapshot["covered_seq"]
                or current["source_digest"] != snapshot["source_digest"]):
                raise ContextRecordConflict("Source, scope or record revision changed")
            if not rows or [r["seq"] for r in rows] != list(range(1, target+1)) or target <= current["covered_seq"]:
                raise ContextRecordConflict("Publication requires a new continuous source prefix")
            if lease:
                valid = conn.execute("SELECT 1 FROM background_jobs WHERE job_id=? AND lease_owner=? AND lease_epoch=? "
                    "AND status='running' AND lease_expires_at>? AND (deadline IS NULL OR deadline>?)",
                    (*lease, now, now)).fetchone()
                if not valid:
                    raise ContextRecordConflict("Stale publication lease")
            existing = {r["record_id"]: r for r in current["records"]}
            touched = set()
            next_revision = current["revision"] + 1
            for change in changes:
                identity = change.get("record_id") or "ctx_" + uuid4().hex
                targets = [identity, *change.get("merge_ids", [])]
                if len(set(targets)) != len(targets) or touched.intersection(targets):
                    raise ContextRecordConflict("Overlapping changes must be reconciled in one operation")
                touched.update(targets)
                old = existing.get(identity)
                if change["op"] != "add" and (old is None or old["revision"] != change["expected_revision"]):
                    raise ContextRecordConflict("Target revision changed")
                if change["op"] == "add" and old is not None:
                    raise ContextRecordConflict("Record already exists")
                value = dict(old) if change["op"] == "archive" else dict(change["value"])
                if change["op"] == "archive":
                    value["validity"] = "archived"
                value.update(record_id=identity, revision=(old["revision"]+1 if old else 1))
                # Preserve exact prior supporting sources when replacing a record.
                if old:
                    supports = {json.dumps(s, sort_keys=True): s for s in [*old["sources"], *value["sources"]]}
                    value["sources"] = list(supports.values())
                merged_before = []
                for other_id, revision in zip(change.get("merge_ids", []), change.get("merge_revisions", []), strict=True):
                    other = existing.get(other_id)
                    if other is None or other["revision"] != revision:
                        raise ContextRecordConflict("Merge source revision changed")
                    if (other["topic"], other["subject"], other["kind"]) != (value["topic"], value["subject"], value["kind"]):
                        raise ContextRecordConflict("Merge must preserve subject, topic and kind")
                    value["sources"].extend(s for s in other["sources"] if s not in value["sources"])
                    merged_before.append(other)
                    other = {**other, "validity": "superseded", "revision": other["revision"]+1, "superseded_by": identity}
                    self._write(conn, session_id, other)
                value["last_source_seq"] = max(s["seq"] for s in value["sources"])
                self._write(conn, session_id, value)
                conn.execute("INSERT INTO context_record_events VALUES(?,?,?,?,?)",
                    (uuid4().hex, session_id, next_revision,
                     json.dumps({**change, "record_id": identity, "before": old, "after": value,
                                 "merged_before": merged_before}, ensure_ascii=False), now))
            conn.execute("INSERT INTO context_record_heads VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                         "revision=excluded.revision,covered_seq=excluded.covered_seq,project_id=excluded.project_id,updated_at=excluded.updated_at",
                         (session_id, next_revision, target, current["project_id"], now))
        return next_revision

    @staticmethod
    def _write(conn, session_id, value):
        conn.execute("INSERT INTO context_records VALUES(?,?,?,?) ON CONFLICT(session_id,record_id) DO UPDATE SET "
                     "revision=excluded.revision,content_json=excluded.content_json",
                     (session_id, value["record_id"], value["revision"], json.dumps(value, ensure_ascii=False)))
