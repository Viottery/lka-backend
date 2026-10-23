"""Separate read materialization: never executes synchronized business state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.domains.shared_sync import (
    MAX_PAGE_BYTES,
    MAX_RECORD_BYTES,
    PROJECTIONS,
    SyncConflictError,
    canonical,
)
from app.storage.sqlite_timing import connect_sqlite


class SharedReplicaStore:
    def __init__(self, path, hub_id):
        if not isinstance(hub_id, str) or not hub_id or len(hub_id) > 120:
            raise ValueError("replica requires an expected hub identity")
        self.path, self.hub_id = Path(path).resolve(), hub_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS replica_meta(slot INTEGER PRIMARY KEY CHECK(slot=1),hub_id TEXT NOT NULL,cursor INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS replica_records(kind TEXT NOT NULL,entity_key TEXT NOT NULL,version INTEGER NOT NULL,deleted INTEGER NOT NULL,payload TEXT,PRIMARY KEY(kind,entity_key))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS replica_pages(after_cursor INTEGER PRIMARY KEY,end_cursor INTEGER NOT NULL,digest TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS replica_sync_state("
                "slot INTEGER PRIMARY KEY CHECK(slot=1),last_pull_at TEXT,high_watermark INTEGER)"
            )
            conn.execute("INSERT OR IGNORE INTO replica_sync_state VALUES(1,NULL,NULL)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS replica_memory_scope ON replica_records("
                "deleted,json_extract(payload,'$.scope'),json_extract(payload,'$.project_id'),"
                "json_extract(payload,'$.status')) WHERE kind='memory_entries'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS replica_memory_links ON replica_records("
                "json_extract(payload,'$.memory_id'),deleted) WHERE kind='memory_entry_sources'"
            )
            conn.execute("INSERT OR IGNORE INTO replica_meta VALUES(1,?,0)", (hub_id,))
            if conn.execute("SELECT hub_id FROM replica_meta").fetchone()[0] != hub_id:
                raise SyncConflictError("replica belongs to another hub")

    @contextmanager
    def _connection(self, write=False):
        conn = (
            connect_sqlite(self.path)
            if write
            else sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        )
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def status(self):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM replica_meta").fetchone()
            return {"hub_id": row["hub_id"], "cursor": row["cursor"]}

    def memory_cache_status(self):
        """Local observations only; a cursor never asserts current cloud availability."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT m.hub_id,m.cursor,s.last_pull_at,s.high_watermark "
                "FROM replica_meta m CROSS JOIN replica_sync_state s"
            ).fetchone()
            return dict(row)

    @staticmethod
    def _validate(page):
        if not isinstance(page, dict) or set(page) != {
            "hub_id",
            "after",
            "cursor",
            "high_watermark",
            "has_more",
            "changes",
        }:
            raise ValueError("invalid sync page fields")
        if not isinstance(page["hub_id"], str) or type(page["has_more"]) is not bool:
            raise ValueError("invalid page identity or continuation")
        for field in ("after", "cursor", "high_watermark"):
            if type(page[field]) is not int or not 0 <= page[field] <= 9223372036854775807:
                raise ValueError("invalid page cursor")
        if not isinstance(page["changes"], list) or len(page["changes"]) > 500:
            raise ValueError("invalid page changes")
        after, cursor, high = page["after"], page["cursor"], page["high_watermark"]
        if (
            cursor != after + len(page["changes"])
            or high < cursor
            or page["has_more"] != (cursor < high)
        ):
            raise ValueError("noncontiguous sync page")
        if page["has_more"] and not page["changes"]:
            raise ValueError("empty continuation page")
        if len(canonical(page).encode()) > MAX_PAGE_BYTES:
            raise ValueError("sync page exceeds size limit")
        for sequence, change in enumerate(page["changes"], after + 1):
            if not isinstance(change, dict) or set(change) != {
                "sequence",
                "kind",
                "key",
                "version",
                "deleted",
                "record",
            }:
                raise ValueError("invalid change fields")
            if type(change["sequence"]) is not int or change["sequence"] != sequence:
                raise ValueError("sync journal gap")
            kind = change["kind"]
            if not isinstance(kind, str) or kind not in PROJECTIONS:
                raise ValueError("unsupported entity kind")
            keys, fields = PROJECTIONS[kind]
            if (
                not isinstance(change["key"], list)
                or len(change["key"]) != len(keys)
                or any(
                    type(k) not in (str, int) or isinstance(k, str) and (not k or len(k) > 1024)
                    for k in change["key"]
                )
            ):
                raise ValueError("invalid entity identity")
            if (
                type(change["version"]) is not int
                or not 1 <= change["version"] <= 9223372036854775807
                or type(change["deleted"]) is not bool
            ):
                raise ValueError("invalid entity version")
            record = change["record"]
            if change["deleted"]:
                if record is not None:
                    raise ValueError("tombstone contains content")
            elif (
                not isinstance(record, dict)
                or set(record) != set(fields)
                or [record[k] for k in keys] != change["key"]
            ):
                raise ValueError("invalid projection fields or identity")
            elif any(type(value) not in (str, int, float, type(None)) for value in record.values()):
                raise ValueError("invalid projection field value")
            if record is not None:
                for field, value in record.items():
                    numeric = field in ("confidence", "sort_order")
                    integer = field in ("slot", "revision", "version", "inbox")
                    allowed = (int, float) if numeric else (int,) if integer else (str,)
                    if value is not None and type(value) not in allowed:
                        raise ValueError("invalid projection field type")
            if len(canonical(change).encode()) > MAX_RECORD_BYTES:
                raise ValueError("shared record exceeds size limit")

    def apply_page(self, page):
        self._validate(page)
        if page["hub_id"] != self.hub_id:
            raise SyncConflictError("unexpected hub identity")
        digest = hashlib.sha256(canonical(page).encode()).hexdigest()
        with self._connection(write=True) as conn:
            cursor = conn.execute("SELECT cursor FROM replica_meta").fetchone()[0]
            if page["after"] != cursor:
                old = conn.execute(
                    "SELECT * FROM replica_pages WHERE after_cursor=?", (page["after"],)
                ).fetchone()
                if old and old["digest"] == digest and old["end_cursor"] <= cursor:
                    return {"hub_id": self.hub_id, "cursor": cursor}
                raise SyncConflictError("page does not continue replica cursor")
            for change in page["changes"]:
                kind, key = change["kind"], canonical(change["key"])
                payload = canonical(change["record"]) if not change["deleted"] else None
                old = conn.execute(
                    "SELECT * FROM replica_records WHERE kind=? AND entity_key=?", (kind, key)
                ).fetchone()
                if old:
                    if change["version"] < old["version"]:
                        raise SyncConflictError("entity version regressed")
                    if change["version"] == old["version"]:
                        if change["deleted"] and not old["deleted"]:
                            conn.execute(
                                "UPDATE replica_records SET deleted=1,payload=NULL WHERE kind=? AND entity_key=?",
                                (kind, key),
                            )
                            continue
                        if old["payload"] != payload or bool(old["deleted"]) != change["deleted"]:
                            raise SyncConflictError("same entity version has different content")
                        continue
                conn.execute(
                    "INSERT INTO replica_records VALUES(?,?,?,?,?) ON CONFLICT(kind,entity_key) DO UPDATE SET version=excluded.version,deleted=excluded.deleted,payload=excluded.payload",
                    (kind, key, change["version"], int(change["deleted"]), payload),
                )
            conn.execute("UPDATE replica_meta SET cursor=?", (page["cursor"],))
            conn.execute(
                "UPDATE replica_sync_state SET last_pull_at=?,high_watermark=? WHERE slot=1",
                (datetime.now(UTC).isoformat(), page["high_watermark"]),
            )
            # Empty polls do not replace hashes of prior nonempty pages.
            if page["cursor"] > page["after"]:
                conn.execute(
                    "INSERT INTO replica_pages VALUES(?,?,?)",
                    (page["after"], page["cursor"], digest),
                )
        return self.status()

    def read_memories(self, *, scope, project_id=None, query=None, memory_id=None,
                      limit=100, offset=0):
        """Read a bounded source-valid memory projection in one local snapshot.

        Unlike the generic inspection API, this query never walks all source
        links for every memory and never exposes another project's records.
        Expiry remains enforced even while disconnected from the hub.
        """
        if (scope not in {"global", "project"}
                or type(limit) is not int or not 1 <= limit <= 100
                or type(offset) is not int or offset < 0
                or scope == "project" and not project_id):
            raise ValueError("invalid memory cache query")
        now = datetime.now(UTC)

        def unexpired(value):
            if value is None:
                return True
            try:
                instant = datetime.fromisoformat(value)
                return instant.tzinfo is not None and instant > now
            except (TypeError, ValueError):
                return False

        with self._connection() as conn:
            conn.execute("BEGIN")
            conn.create_function("replica_unexpired", 1, unexpired)
            conn.create_function("replica_casefold", 1, lambda value: (value or "").casefold())
            valid_source = (
                "s.kind='memory_sources' AND s.deleted=0 "
                "AND json_extract(s.payload,'$.status')='active' "
                "AND replica_unexpired(json_extract(s.payload,'$.expires_at'))"
            )
            links = (
                "FROM replica_records l JOIN replica_records s "
                "ON s.entity_key=json_array(json_extract(l.payload,'$.source_id')) "
                "WHERE l.kind='memory_entry_sources' AND l.deleted=0 "
                "AND json_extract(l.payload,'$.memory_id')=json_extract(e.payload,'$.memory_id') "
                "AND " + valid_source
            )
            clauses = [
                "e.kind='memory_entries'", "e.deleted=0",
                "json_extract(e.payload,'$.scope')=?",
                "json_extract(e.payload,'$.status')='active'",
                "replica_unexpired(json_extract(e.payload,'$.expires_at'))",
                "json_extract(e.payload,'$.sensitivity') IN ('normal','public')",
                "EXISTS(SELECT 1 " + links + ")",
            ]
            args = [scope]
            if scope == "project":
                clauses.append("json_extract(e.payload,'$.project_id')=?")
                args.append(project_id)
                clauses.append(
                    "EXISTS(SELECT 1 FROM replica_records p WHERE p.kind='memory_projects' "
                    "AND p.deleted=0 AND p.entity_key=json_array(?))"
                )
                args.append(project_id)
            else:
                clauses.append("json_extract(e.payload,'$.project_id') IS NULL")
            if memory_id is not None:
                clauses.append("e.entity_key=?")
                args.append(canonical([memory_id]))
            if query is not None:
                terms = [term.casefold() for term in query[:2048].split()[:8]]
                if not terms:
                    return []
                clauses.append("(" + " OR ".join(
                    "instr(replica_casefold(json_extract(e.payload,'$.content')),?)>0"
                    for _ in terms
                ) + ")")
                args.extend(terms)
            args.extend([limit, offset])
            rows = conn.execute(
                "SELECT e.payload FROM replica_records e WHERE " + " AND ".join(clauses)
                + " ORDER BY json_extract(e.payload,'$.updated_at') DESC,e.entity_key LIMIT ? OFFSET ?",
                args,
            ).fetchall()
            items = []
            for row in rows:
                record = json.loads(row[0])
                source_rows = conn.execute(
                    "SELECT json_extract(s.payload,'$.source_id') "
                    + links.replace(
                        "json_extract(e.payload,'$.memory_id')", "?"
                    ) + " ORDER BY s.entity_key LIMIT 33",
                    (record["memory_id"],),
                ).fetchall()
                record["source_ids"] = [source[0] for source in source_rows[:32]]
                record["sources_truncated"] = len(source_rows) > 32
                items.append(record)
            return items

    def list_records(self, kind, limit=100, offset=0):
        if (
            kind not in PROJECTIONS
            or type(limit) is not int
            or not 1 <= limit <= 500
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("invalid record query")
        now = datetime.now(UTC)
        with self._connection() as conn:

            def get(entity_kind, *key):
                row = conn.execute(
                    "SELECT payload FROM replica_records WHERE kind=? AND entity_key=? AND deleted=0",
                    (entity_kind, canonical(list(key))),
                ).fetchone()
                return json.loads(row[0]) if row else None

            def session_valid(session_id):
                session = get("agent_sessions", session_id)
                return (
                    session is not None
                    and session["kind"] == "manager"
                    and session["status"] != "deleted"
                )

            def unexpired(value):
                if value is None:
                    return True
                try:
                    instant = datetime.fromisoformat(value)
                    return instant.tzinfo is not None and instant > now
                except (TypeError, ValueError):
                    return False

            def valid(record, entity_kind):
                if entity_kind == "memory_entries":
                    if record["status"] != "active" or not unexpired(record["expires_at"]):
                        return False
                    for row in conn.execute(
                        "SELECT payload FROM replica_records WHERE kind='memory_entry_sources' AND deleted=0"
                    ):
                        link = json.loads(row[0])
                        if link["memory_id"] == record["memory_id"]:
                            source = get("memory_sources", link["source_id"])
                            if (
                                source
                                and source["status"] == "active"
                                and unexpired(source["expires_at"])
                            ):
                                return True
                    return False
                if entity_kind == "agent_sessions":
                    return record["kind"] == "manager" and record["status"] != "deleted"
                if entity_kind == "matters":
                    return record["deleted_at"] is None
                if entity_kind == "manager_work_relations":
                    return session_valid(record["manager_session_id"]) and all(
                        (m := get("matters", record[k])) is not None and m["deleted_at"] is None
                        for k in ("source_matter_id", "target_matter_id")
                    )
                if entity_kind in ("matter_management", "manager_goal_matter_links"):
                    matter = get("matters", record["matter_id"])
                    return (
                        session_valid(record["manager_session_id"])
                        and matter is not None
                        and matter["deleted_at"] is None
                    )
                if entity_kind.startswith("manager_"):
                    if not session_valid(record["session_id"]):
                        return False
                    if entity_kind == "manager_delegates" and not session_valid(
                        record["parent_session_id"]
                    ):
                        return False
                    if entity_kind == "manager_goals":
                        link = get("manager_goal_matter_links", record["goal_id"])
                        if link:
                            matter = get("matters", link["matter_id"])
                            if matter is None or matter["deleted_at"] is not None:
                                return False
                            record["objective"] = matter["title"] + (
                                "\n" + matter["summary"]
                                if matter["summary"] and matter["summary"] != matter["title"]
                                else ""
                            )
                    return True
                if entity_kind == "matter_source_links":
                    matter = get("matters", record["matter_id"])
                    return matter is not None and matter["deleted_at"] is None
                return True

            items = []
            eligible = 0
            for row in conn.execute(
                "SELECT payload FROM replica_records WHERE kind=? AND deleted=0 ORDER BY entity_key",
                (kind,),
            ):
                record = json.loads(row[0])
                if valid(record, kind):
                    if eligible >= offset:
                        items.append(record)
                    eligible += 1
                    if len(items) >= limit:
                        break
            return {"kind": kind, "items": items, "offset": offset, "limit": limit, **self.status()}
