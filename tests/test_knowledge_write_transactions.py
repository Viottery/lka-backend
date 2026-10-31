from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.domains.knowledge import (
    KnowledgeDocumentInput,
    KnowledgeService,
    KnowledgeSourceInput,
)
from app.storage.db import connect, get_db_path, init_db


def _service(tmp_path: Path) -> tuple[KnowledgeService, Path]:
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    return KnowledgeService(lambda: connect(db_path)), db_path


def _payload(text: str, *, uri: str = "/workspace/note.md", metadata=None):
    return KnowledgeDocumentInput(
        source=KnowledgeSourceInput(
            display_name="note.md", uri=uri, sensitivity="personal", remote_policy="redact",
        ),
        title="note.md",
        uri=uri,
        text=text,
        metadata=metadata or {},
    )


def test_import_replaces_document_chunks_and_fts_atomically(tmp_path):
    service, db_path = _service(tmp_path)
    original = service.import_text_document(_payload("obsoleteword " * 80))
    replacement = service.import_text_document(_payload("currentword " * 80))

    with sqlite3.connect(db_path) as conn:
        documents = conn.execute(
            "SELECT document_id FROM knowledge_documents WHERE uri=?", ("/workspace/note.md",)
        ).fetchall()
        old_fts = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks_fts WHERE knowledge_chunks_fts MATCH 'obsoleteword'"
        ).fetchone()[0]
        new_fts = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks_fts WHERE knowledge_chunks_fts MATCH 'currentword'"
        ).fetchone()[0]
        child_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE document_id=?", (replacement.document_id,)
        ).fetchone()[0]

    assert original.document_id != replacement.document_id
    assert documents == [(replacement.document_id,)]
    assert old_fts == 0
    assert new_fts == child_count == replacement.imported_chunks


def test_failed_chunk_batch_rolls_back_document_and_fts_replacement(tmp_path):
    service, db_path = _service(tmp_path)
    original = service.import_text_document(_payload("stableword " * 80))
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TRIGGER reject_new_chunk BEFORE INSERT ON knowledge_chunks
            WHEN NEW.text LIKE 'rejectnewword%'
            BEGIN SELECT RAISE(ABORT, 'injected test failure'); END""")
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected test failure"):
        service.import_text_document(_payload("rejectnewword " * 80))

    with sqlite3.connect(db_path) as conn:
        documents = conn.execute(
            "SELECT document_id FROM knowledge_documents WHERE uri=?", ("/workspace/note.md",)
        ).fetchall()
        stable_fts = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks_fts WHERE knowledge_chunks_fts MATCH 'stableword'"
        ).fetchone()[0]
        failed_fts = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks_fts WHERE knowledge_chunks_fts MATCH 'rejectnewword'"
        ).fetchone()[0]

    assert documents == [(original.document_id,)]
    assert stable_fts == original.imported_chunks
    assert failed_fts == 0


def _install_chunk_write_audit(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE chunk_write_audit(action TEXT NOT NULL);
            CREATE TRIGGER audit_knowledge_chunk_insert AFTER INSERT ON knowledge_chunks
            BEGIN INSERT INTO chunk_write_audit(action) VALUES('insert'); END;
            CREATE TRIGGER audit_knowledge_chunk_delete AFTER DELETE ON knowledge_chunks
            BEGIN INSERT INTO chunk_write_audit(action) VALUES('delete'); END;
        """)


def test_identical_reimport_revalidates_privacy_without_rebuilding_chunks(tmp_path, monkeypatch):
    service, db_path = _service(tmp_path)
    payload = _payload("repeatable knowledge " * 80)
    original = service.import_text_document(payload)
    _install_chunk_write_audit(db_path)
    privacy_checks = []
    original_privacy_check = service.privacy_gateway.contains_secret

    def count_privacy_check(text):
        privacy_checks.append(text)
        return original_privacy_check(text)

    monkeypatch.setattr(service.privacy_gateway, "contains_secret", count_privacy_check)
    repeated = service.import_text_document(payload)

    with sqlite3.connect(db_path) as conn:
        audit = conn.execute("SELECT action FROM chunk_write_audit").fetchall()
        chunks = conn.execute(
            "SELECT chunk_id,chunk_index,text,metadata FROM knowledge_chunks "
            "WHERE document_id=? ORDER BY chunk_index", (original.document_id,),
        ).fetchall()
        fts_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks_fts WHERE document_id=?", (original.document_id,),
        ).fetchone()[0]

    assert repeated.document_id == original.document_id
    assert repeated.imported_chunks == original.imported_chunks
    assert privacy_checks, "an unchanged checksum must not bypass privacy revalidation"
    assert audit == []
    assert len(chunks) == fts_count == original.imported_chunks


def test_title_and_acl_updates_keep_chunk_ids_and_refresh_required_projections(tmp_path):
    service, db_path = _service(tmp_path)
    payload = _payload("stable projected text " * 80)
    imported = service.import_text_document(payload)
    _install_chunk_write_audit(db_path)
    with sqlite3.connect(db_path) as conn:
        before_title = conn.execute(
            "SELECT chunk_id,chunk_index FROM knowledge_chunks WHERE document_id=? ORDER BY chunk_index",
            (imported.document_id,),
        ).fetchall()

    renamed = payload.model_copy(update={"title": "Renamed note"})
    service.import_text_document(renamed)
    with sqlite3.connect(db_path) as conn:
        after_title = conn.execute(
            "SELECT chunk_id,chunk_index FROM knowledge_chunks WHERE document_id=? ORDER BY chunk_index",
            (imported.document_id,),
        ).fetchall()
        titles = {row[0] for row in conn.execute(
            "SELECT DISTINCT title FROM knowledge_chunks_fts WHERE document_id=?",
            (imported.document_id,),
        )}
        title_audit = conn.execute("SELECT action FROM chunk_write_audit").fetchall()
        conn.execute("DELETE FROM chunk_write_audit")
        conn.commit()

    assert after_title == before_title
    assert titles == {"Renamed note"}
    assert title_audit == []

    changed_source = payload.source.model_copy(update={"access_scope": {"session_ids": ["allowed-session"]}})
    restricted = payload.model_copy(update={"source": changed_source, "title": "Renamed note"})
    updated = service.import_text_document(restricted)
    with sqlite3.connect(db_path) as conn:
        scope = conn.execute(
            "SELECT access_scope FROM knowledge_sources WHERE source_id=?", (imported.source_id,),
        ).fetchone()[0]
        acl_audit = conn.execute("SELECT action FROM chunk_write_audit").fetchall()
        stable_id = conn.execute(
            "SELECT document_id FROM knowledge_documents WHERE uri=?", (restricted.uri,),
        ).fetchone()[0]

    assert updated.document_id == imported.document_id == stable_id
    assert json.loads(scope) == {"session_ids": ["allowed-session"]}
    assert acl_audit == []
    assert service._source_access_allowed(
        scope, workspace_path=None, session_id="allowed-session",
    )
    assert not service._source_access_allowed(scope, workspace_path=None, session_id="other-session")

    sensitive_source = changed_source.model_copy(update={"sensitivity": "sensitive"})
    service.import_text_document(restricted.model_copy(update={"source": sensitive_source}))
    with sqlite3.connect(db_path) as conn:
        sensitivity = conn.execute(
            "SELECT DISTINCT sensitivity FROM knowledge_chunks WHERE document_id=?",
            (imported.document_id,),
        ).fetchall()
        sensitivity_audit = conn.execute("SELECT action FROM chunk_write_audit").fetchall()
    assert {row[0] for row in sensitivity} == {"sensitive"}
    assert sensitivity_audit


def test_extraction_projection_change_rebuilds_effective_chunk_metadata(tmp_path):
    service, db_path = _service(tmp_path)
    text = "page-aware source text " * 70
    first_payload = _payload(text, metadata={"extraction": {"page_spans": [
        {"page": 1, "start_char": 0, "end_char": len(text)},
    ]}})
    imported = service.import_text_document(first_payload)
    _install_chunk_write_audit(db_path)
    changed_payload = _payload(text, metadata={"extraction": {"page_spans": [
        {"page": 2, "start_char": 0, "end_char": len(text)},
    ]}})
    service.import_text_document(changed_payload)

    with sqlite3.connect(db_path) as conn:
        audit = conn.execute("SELECT action FROM chunk_write_audit").fetchall()
        metadata = [json.loads(row[0]) for row in conn.execute(
            "SELECT metadata FROM knowledge_chunks WHERE document_id=? ORDER BY chunk_index",
            (imported.document_id,),
        )]
    assert audit
    assert all(item.get("extraction", {}).get("pages") == [2] for item in metadata)


def test_chunk_preparation_happens_before_writer_and_inserts_are_batched(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    order = []
    batch_statements = []
    original_factory = service._conn_factory

    class RecordingConnection:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *args):
            result = self.inner.execute(sql, *args)
            if (sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                    and self.inner._write_started is not None and "write_lease" not in order):
                order.append("write_lease")
            return result

        def executemany(self, sql, rows):
            batch_statements.append(sql)
            return self.inner.executemany(sql, rows)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(service, "_conn_factory", lambda: RecordingConnection(original_factory()))
    original_chunker = service._chunk_text

    def record_chunking(*args):
        order.append("chunk")
        return original_chunker(*args)

    monkeypatch.setattr(service, "_chunk_text", record_chunking)
    original_privacy_check = service.privacy_gateway.contains_secret

    def record_privacy_check(text):
        order.append("privacy")
        return original_privacy_check(text)

    monkeypatch.setattr(service.privacy_gateway, "contains_secret", record_privacy_check)
    source_metadata_json = service._json

    def record_json(payload):
        order.append("json")
        return source_metadata_json(payload)

    monkeypatch.setattr(service, "_json", record_json)

    result = service.import_text_document(_payload("prepared word " * 600))

    assert result.imported_chunks >= 5
    assert "write_lease" in order
    assert all(item in {"chunk", "privacy", "json"} for item in order[:order.index("write_lease")])
    assert not any(item in {"chunk", "privacy", "json"} for item in order[order.index("write_lease") + 1:])
    assert len(batch_statements) == 2
    assert "INSERT INTO knowledge_chunks(" in batch_statements[0]
    assert "INSERT INTO knowledge_chunks_fts(" in batch_statements[1]


def test_redaction_and_extraction_chunk_provenance_are_preserved(tmp_path):
    service, db_path = _service(tmp_path)
    text = "ordinary paragraph.\n\napi_key = sk-test-secret-token-value-abcdefg\n\nlast section. " * 4
    imported = service.import_text_document(_payload(
        text,
        metadata={"extraction": {"page_spans": [{"page": 7, "start_char": 0,
                                                    "end_char": len(text)}]}},
    ))

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        chunks = conn.execute(
            "SELECT text,sensitivity,remote_policy,metadata FROM knowledge_chunks "
            "WHERE document_id=? ORDER BY chunk_index", (imported.document_id,),
        ).fetchall()
        fts_text = " ".join(row[0] for row in conn.execute(
            "SELECT text FROM knowledge_chunks_fts WHERE document_id=?", (imported.document_id,),
        ))

    assert imported.secret_chunks_redacted > 0
    assert all("api_key = sk-test-secret-token-value-abcdefg" not in row["text"] for row in chunks)
    assert "sk-test-secret-token-value-abcdefg" not in fts_text
    assert all(json.loads(row["metadata"]).get("extraction", {}).get("pages") == [7]
               for row in chunks)
    assert any(row["sensitivity"] == "secret" and row["remote_policy"] == "deny" for row in chunks)
