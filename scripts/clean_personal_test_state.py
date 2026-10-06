"""Backed-up, explicit-ID soft cleanup; never resets message/QQ storage.

Operator script, not an Agent tool. Run with the native production interpreter
for a Windows SQLite database. Default is read-only preview. Audit and raw
messages are retained for recovery, but deleted sessions cannot enter prompts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.instruction_files import GLOBAL_TEMPLATE, InstructionFiles
from app.core.memory_files import MemoryFiles
from app.domains.memory import MemoryService, MemorySourceInput


def protected_snapshot(conn):
    """Exact in-transaction content hashes, not merely counts of raw messages."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND "
        "(name GLOB 'message_*' OR name GLOB 'qq_*') ORDER BY name"
    )]
    output = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        rows = sorted((tuple(row) for row in conn.execute(f"SELECT * FROM {quoted}")), key=repr)
        output[table] = hashlib.sha256(repr(rows).encode()).hexdigest()
    for table, where in (("background_jobs", "kind NOT IN ('memory_extract','context_compact')"),):
        rows = sorted((tuple(row) for row in conn.execute(f"SELECT * FROM {table} WHERE {where}")), key=repr)
        output[table] = hashlib.sha256(repr(rows).encode()).hexdigest()
    return output


def clean(db, *, sessions, memories, apply=False, guidance_sha=None):
    db = Path(db).resolve()
    if not db.is_file() or db.name != "lka.sqlite3":
        raise ValueError("An existing explicit lka.sqlite3 path is required")
    if not sessions and not memories:
        raise ValueError("Explicit session or memory IDs are required; no blanket reset")
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        selected_sessions = []
        for sid in sessions:
            row = conn.execute("SELECT session_id,title,status FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {sid}")
            selected_sessions.append(dict(row))
            if conn.execute("SELECT 1 FROM agent_runs WHERE session_id=? AND status IN ('queued','running','waiting_confirmation','waiting_user')", (sid,)).fetchone():
                raise ValueError(f"Session still has active work: {sid}")
        selected_memories = []
        for mid in memories:
            row = conn.execute("SELECT memory_id,scope,project_id,status,version FROM memory_entries WHERE memory_id=?", (mid,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown memory: {mid}")
            selected_memories.append(dict(row))
        plan = {"sessions": selected_sessions, "memories": selected_memories,
                "mode": "apply" if apply else "preview", "recovery": "soft_delete_and_retract"}
        if guidance_sha:
            path = db.parent / "instructions/AGENTS.md"
            if hashlib.sha256(path.read_bytes()).hexdigest() != guidance_sha:
                raise ValueError("Guidance changed since inspection; no reset")
        if not apply:
            return plan
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = db.parent.parent / "deployment_backups" / ("preference-cleanup-" + stamp)
        backup.mkdir(parents=True, exist_ok=False)
        with sqlite3.connect(backup / "lka.sqlite3") as saved:
            conn.backup(saved)
        for folder in ("instructions", "memory"):
            path = db.parent / folder
            if path.exists():
                shutil.copytree(path, backup / folder)
    finally:
        conn.close()
    service = MemoryService(db)
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = protected_snapshot(conn)
        changed_scopes = {(row["scope"], row["project_id"]) for row in selected_memories}
        for sid in sessions:
            if conn.execute("SELECT 1 FROM agent_runs WHERE session_id=? AND status IN ('queued','running','waiting_confirmation','waiting_user')", (sid,)).fetchone():
                raise ValueError(f"Session became active: {sid}")
            for row in conn.execute("SELECT message_id,content FROM agent_session_messages WHERE session_id=? AND role='user'", (sid,)).fetchall():
                changed_scopes.update((linked["scope"], linked["project_id"]) for linked in conn.execute(
                    "SELECT e.scope,e.project_id FROM memory_entries e "
                    "JOIN memory_entry_sources es USING(memory_id) JOIN memory_sources s USING(source_id) "
                    "WHERE s.source_type='user_message' AND s.source_ref=?", (row["message_id"],),
                ))
                source = service.register_source_in_transaction(conn, MemorySourceInput(
                    source_type="user_message", source_ref=row["message_id"],
                    checksum=hashlib.sha256(row["content"].encode()).hexdigest(),
                ))
                # Legacy/user-created sources may omit a checksum. Revoke every
                # identity for this exact message, plus the canonical marker that
                # fences late extraction, not just today's checksum identity.
                sources = [item[0] for item in conn.execute(
                    "SELECT source_id FROM memory_sources WHERE source_type='user_message' AND source_ref=?",
                    (row["message_id"],),
                )]
                for source in sources:
                    service.revoke_source_in_transaction(conn, source)
            conn.execute("UPDATE agent_sessions SET status='deleted',updated_at=? WHERE session_id=?",
                         (datetime.now(UTC).isoformat(), sid))
        for row in selected_memories:
            current = conn.execute("SELECT status,version FROM memory_entries WHERE memory_id=?", (row["memory_id"],)).fetchone()
            if current["status"] == "retracted":
                continue  # Session provenance revocation already did this.
            service._transition(conn, row["memory_id"], "retracted", "operator_test_cleanup", {}, row["version"])
        service._refresh_conflict_metadata(conn)
        if protected_snapshot(conn) != before:
            raise RuntimeError("Protected message/QQ content changed; rolling back")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    file_results = []
    for scope, project in changed_scopes:
        MemoryFiles(service, db.parent).generate(scope=scope, project_id=project)
        file_results.append({"scope": scope, "project_id": project, "status": "synced"})
    if guidance_sha:
        files = InstructionFiles(db.parent, [])
        try:
            files.update("global", content=GLOBAL_TEMPLATE, expected_sha256=guidance_sha)
        finally:
            files.close()
    plan.update(backup_path=str(backup), protected_tables=len(before),
                protected_content_unchanged=True, memory_files=file_results)
    (backup / "cleanup_manifest.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--session-id", action="append", default=[])
    parser.add_argument("--memory-id", action="append", default=[])
    parser.add_argument("--reset-global-guidance-sha256")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(clean(args.db, sessions=args.session_id, memories=args.memory_id,
                           apply=args.apply, guidance_sha=args.reset_global_guidance_sha256),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
