"""Bounded, page-local reads for the shared sync hub pull path."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.domains.shared_sync import PROJECTIONS, canonical
from app.storage.connections import is_mysql

READ_BATCH_EVENTS = 32
_MAX_BINDS = 900


def _chunks(values, size):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _select_by_keys(conn, table, keys, fields, key_values):
    """Fetch bounded composite-key sets with only allowlisted identifiers."""
    key_values = list(dict.fromkeys(tuple(value) for value in key_values))
    if not key_values:
        return {}
    width = len(keys)
    batch_size = max(1, _MAX_BINDS // width)
    result = {}
    for batch in _chunks(key_values, batch_size):
        clauses = ["(" + " AND ".join(f"{key}=?" for key in keys) + ")" for _ in batch]
        params = [value for key in batch for value in key]
        rows = conn.execute(
            f"SELECT {','.join(fields)} FROM {table} WHERE " + " OR ".join(clauses), params
        )
        for row in rows:
            record = dict(row)
            result[tuple(record[key] for key in keys)] = record
    return result


def _select_by_values(conn, table, key, fields, values):
    values = list(dict.fromkeys(value for value in values if value is not None))
    result = {}
    for batch in _chunks(values, _MAX_BINDS):
        for row in conn.execute(
            f"SELECT {','.join(fields)} FROM {table} WHERE {key} IN ({','.join('?' for _ in batch)})",
            batch,
        ):
            result[row[key]] = dict(row)
    return result


def table_columns(conn, table):
    if is_mysql(conn):
        return {
            row["name"]
            for row in conn.execute_native(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name=%s",
                (table,),
            )
        }
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def table_exists(conn, table):
    if is_mysql(conn):
        return (
            conn.execute_native(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name=%s",
                (table,),
            ).fetchone()
            is not None
        )
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def load_changes(conn, events, schema):
    """Return sequence-aligned change candidates for one bounded event chunk."""
    if not events:
        return []
    by_kind = {}
    for event in events:
        by_kind.setdefault(event["kind"], []).append((event, json.loads(event["entity_key"])))

    heads_by_identity = {}
    records_by_identity = {}
    has_memory_metadata = schema["memory_metadata"]
    for kind, entries in by_kind.items():
        entity_keys = list(dict.fromkeys(event["entity_key"] for event, _key in entries))
        for batch in _chunks(entity_keys, _MAX_BINDS - 1):
            for row in conn.execute(
                "SELECT kind,entity_key,version,deleted FROM shared_sync_heads "
                f"WHERE kind=? AND entity_key IN ({','.join('?' for _ in batch)})",
                (kind, *batch),
            ):
                head = dict(row)
                heads_by_identity[(kind, head["entity_key"])] = head

        keys, base_fields = PROJECTIONS[kind]
        fields = base_fields + (
            ("metadata",) if kind == "memory_entries" and has_memory_metadata else ()
        )
        live_keys = [
            key
            for event, key in entries
            if not heads_by_identity[(kind, event["entity_key"])]["deleted"]
        ]
        records = _select_by_keys(conn, kind, keys, fields, live_keys)
        for key, record in records.items():
            records_by_identity[(kind, canonical(list(key)))] = record

    # Resolve all projection dependencies in a handful of bounded queries.
    now = datetime.now(UTC).isoformat()
    source_metadata = schema["source_metadata"]
    source_ids = []
    for (kind, _key), record in records_by_identity.items():
        if kind == "memory_sources" and record.get("source_type") == "node_memory_replica":
            source_ids.append(record["source_id"])
    source_publications = {}
    if source_metadata and source_ids:
        source_publications = _select_by_values(
            conn, "memory_sources", "source_id", ("source_id", "metadata"), source_ids
        )

    entry_ids = [
        record["memory_id"]
        for (kind, _key), record in records_by_identity.items()
        if kind == "memory_entries"
    ]
    active_entries = set()
    if entry_ids:
        source_activation = "COALESCE(s.source_type,'')!='node_memory_replica'"
        if source_metadata:
            if is_mysql(conn):
                needs_review = "JSON_EXTRACT(s.metadata,'$.needs_review')"
                needs_review_type = f"JSON_TYPE({needs_review}) COLLATE utf8mb4_0900_bin"
                needs_review_text = f"JSON_UNQUOTE({needs_review}) COLLATE utf8mb4_0900_bin"
                numeric_zero = (
                    f"{needs_review_text} REGEXP "
                    "'^-?0+([.]0+)?([eE][+-]?[0-9]+)?$' COLLATE utf8mb4_0900_bin"
                )
                source_activation += (
                    " OR (JSON_VALID(s.metadata) "
                    "AND JSON_UNQUOTE(JSON_EXTRACT(s.metadata,'$.published_status')) COLLATE utf8mb4_0900_bin="
                    "'active' COLLATE utf8mb4_0900_bin "
                    f"AND ({needs_review_type} IS NULL OR {needs_review_type}='NULL' COLLATE utf8mb4_0900_bin "
                    f"OR ({needs_review_type}='BOOLEAN' COLLATE utf8mb4_0900_bin "
                    f"AND {needs_review_text}='false' COLLATE utf8mb4_0900_bin) "
                    f"OR ({needs_review_type} IN ('INTEGER' COLLATE utf8mb4_0900_bin, "
                    "'DOUBLE' COLLATE utf8mb4_0900_bin, 'DECIMAL' COLLATE utf8mb4_0900_bin) "
                    f"AND {numeric_zero})))"
                )
            else:
                source_activation += (
                    " OR (json_valid(s.metadata) "
                    "AND json_extract(s.metadata,'$.published_status')='active' "
                    "AND COALESCE(json_extract(s.metadata,'$.needs_review'),0)=0)"
                )
        for batch in _chunks(entry_ids, _MAX_BINDS - 1):
            if is_mysql(conn):
                rows = conn.execute_native(
                    "SELECT DISTINCT l.memory_id "
                    "FROM memory_entry_sources l JOIN memory_sources s USING(source_id) "
                    f"WHERE l.memory_id IN ({','.join('%s' for _ in batch)}) "
                    "AND s.status='active' AND (s.expires_at IS NULL OR s.expires_at>%s) "
                    f"AND ({source_activation})",
                    (*batch, now),
                )
            else:
                rows = conn.execute(
                    "SELECT DISTINCT l.memory_id "
                    "FROM memory_entry_sources l JOIN memory_sources s USING(source_id) "
                    f"WHERE l.memory_id IN ({','.join('?' for _ in batch)}) "
                    "AND s.status='active' AND (s.expires_at IS NULL OR s.expires_at>?) "
                    f"AND ({source_activation})",
                    (*batch, now),
                )
            for row in rows:
                active_entries.add(row["memory_id"])

    session_ids = []
    matter_ids = []
    for (kind, _key), record in records_by_identity.items():
        if kind != "agent_sessions":
            session_ids.extend(
                record[field]
                for field in ("session_id", "manager_session_id", "parent_session_id")
                if field in record
            )
        if kind != "matters":
            matter_ids.extend(
                record[field]
                for field in ("matter_id", "source_matter_id", "target_matter_id")
                if field in record
            )
    goal_links = {}
    if any(kind == "manager_goals" for kind, _key in records_by_identity) and schema["goal_links"]:
        goal_ids = [
            record["goal_id"]
            for (kind, _key), record in records_by_identity.items()
            if kind == "manager_goals"
        ]
        for batch in _chunks(goal_ids, _MAX_BINDS):
            for row in conn.execute(
                "SELECT goal_id,matter_id FROM manager_goal_matter_links "
                f"WHERE goal_id IN ({','.join('?' for _ in batch)}) ORDER BY goal_id,matter_id",
                batch,
            ):
                goal_links.setdefault(row["goal_id"], row["matter_id"])
                matter_ids.append(row["matter_id"])

    managers = _select_by_values(
        conn, "agent_sessions", "session_id", ("session_id", "kind", "status"), session_ids
    )
    matters = _select_by_values(
        conn, "matters", "matter_id", ("matter_id", "deleted_at", "title", "summary"), matter_ids
    )

    projected = {}
    for identity, source in records_by_identity.items():
        kind, key = identity
        record = dict(source)
        if kind == "memory_sources" and record.get("source_type") == "node_memory_replica":
            try:
                publication = json.loads(source_publications[record["source_id"]]["metadata"])
            except (KeyError, TypeError, ValueError):
                publication = None
            if (
                not isinstance(publication, dict)
                or publication.get("published_status") != "active"
                or publication.get("needs_review")
            ):
                projected[identity] = None
                continue
        if kind == "memory_entries":
            try:
                metadata = json.loads(record.pop("metadata", "{}"))
            except (TypeError, ValueError):
                projected[identity] = None
                continue
            if (
                not isinstance(metadata, dict)
                or metadata.get("needs_review")
                or record["status"] != "active"
                or (record["expires_at"] is not None and record["expires_at"] <= now)
                or record["memory_id"] not in active_entries
            ):
                projected[identity] = None
                continue
        if kind == "agent_sessions" and (
            record["kind"] != "manager" or record["status"] == "deleted"
        ):
            projected[identity] = None
            continue
        if kind == "matters" and record["deleted_at"] is not None:
            projected[identity] = None
            continue
        refs_ok = all(
            managers.get(session_id, {}).get("kind") == "manager"
            and managers[session_id]["status"] != "deleted"
            for session_id in (
                record[field]
                for field in ("session_id", "manager_session_id", "parent_session_id")
                if field in record and kind != "agent_sessions"
            )
        ) and all(
            matter_id in matters and matters[matter_id]["deleted_at"] is None
            for matter_id in (
                record[field]
                for field in ("matter_id", "source_matter_id", "target_matter_id")
                if field in record and kind != "matters"
            )
        )
        if not refs_ok:
            projected[identity] = None
            continue
        if kind == "manager_goals" and record["goal_id"] in goal_links:
            matter = matters.get(goal_links[record["goal_id"]])
            if matter is None or matter["deleted_at"] is not None:
                projected[identity] = None
                continue
            record["objective"] = matter["title"] + (
                "\n" + matter["summary"]
                if matter["summary"] and matter["summary"] != matter["title"]
                else ""
            )
        projected[identity] = record

    changes = []
    for event in events:
        kind, key = event["kind"], event["entity_key"]
        head = heads_by_identity[(kind, key)]
        record = projected.get((kind, key))
        changes.append(
            {
                "sequence": event["sequence"],
                "kind": kind,
                "key": json.loads(key),
                "version": head["version"],
                "deleted": bool(head["deleted"]) or record is None,
                "record": dict(record) if record is not None else None,
            }
        )
    return changes
