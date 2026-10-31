from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeService, KnowledgeSourceInput
from app.domains.knowledge_retrieval import EmbeddingModelInfo, SemanticVectorRecord
from app.integrations.local_semantic import SQLiteVecSemanticIndex
from app.integrations.mysql_semantic import MySQLVectorSemanticIndex
from app.storage.db import connect, get_db_path, init_db
from app.storage.mysql_knowledge_vectors import (
    _CHECKSUM,
    install_knowledge_vector_blobs_v11,
)

MODEL = EmbeddingModelInfo("test", "vectors", 3, normalized=False)


def _db(tmp_path: Path):
    path = get_db_path(tmp_path / "data")
    init_db(path)
    conn = connect(path)
    conn.execute(
        "CREATE TABLE knowledge_vector_blobs(vector_id INTEGER PRIMARY KEY, embedding BLOB NOT NULL, "
        "FOREIGN KEY(vector_id) REFERENCES knowledge_embedding_records(vector_id) ON DELETE CASCADE)"
    )
    conn.commit()
    conn.close()
    return path


def _factory(path: Path):
    def make():
        conn = connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    return make


def _import(service: KnowledgeService, name: str, *, sensitivity="public", policy="allow"):
    return service.import_text_document(KnowledgeDocumentInput(
        source=KnowledgeSourceInput(
            source_type="local_document", display_name=name, uri=f"/{name}",
            sensitivity=sensitivity, remote_policy=policy,
        ),
        title=name, uri=f"/{name}", text=f"content for {name}",
        sensitivity=sensitivity, remote_policy=policy,
    ))


def _chunk_ids(path: Path, source_id: str) -> list[str]:
    conn = connect(path)
    try:
        return [str(row[0]) for row in conn.execute(
            "SELECT c.chunk_id FROM knowledge_chunks c JOIN knowledge_documents d "
            "ON d.document_id=c.document_id WHERE d.source_id=? ORDER BY c.chunk_index", (source_id,),
        ).fetchall()]
    finally:
        conn.close()


def test_mysql_index_matches_sqlite_vec_and_scopes_before_top_k(tmp_path):
    path = _db(tmp_path)
    factory = _factory(path)
    service = KnowledgeService(factory)
    allowed = _import(service, "allowed")
    also_allowed = _import(service, "also-allowed")
    denied = _import(service, "denied", sensitivity="secret", policy="deny")
    chunk_ids = (_chunk_ids(path, allowed.source_id) + _chunk_ids(path, also_allowed.source_id)
                 + _chunk_ids(path, denied.source_id))
    vectors = [[1, 0, 0], [0.9, 0.1, 0], [0, 1, 0]]
    records = [SemanticVectorRecord(cid, f"sum-{i}", vec) for i, (cid, vec) in enumerate(zip(chunk_ids, vectors))]
    mysql_index = MySQLVectorSemanticIndex(conn_factory=factory, model_info=MODEL)
    sqlite_index = SQLiteVecSemanticIndex(conn_factory=factory, model_info=MODEL)
    assert sqlite_index.upsert(records) == (3, 0)
    # Existing metadata is preserved and the MySQL-owned payload table is filled.
    assert mysql_index.upsert(records) == (0, 3)

    query = [0.8, 0.2, 0]
    expected = sqlite_index.search(query, 2)
    actual = mysql_index.search(query, 2)
    assert [hit.chunk_id for hit in actual] == [hit.chunk_id for hit in expected]
    assert [hit.score for hit in actual] == pytest.approx([hit.score for hit in expected], abs=1e-6)
    scoped = mysql_index.search_scoped(query, 5, source_ids=[allowed.source_id, also_allowed.source_id])
    assert {hit.chunk_id for hit in scoped} == set(chunk_ids[:2])
    assert mysql_index.search_scoped(query, 5, source_ids=[] ) == []
    assert all(hit.chunk_id != chunk_ids[2] for hit in mysql_index.search_scoped(query, 5))


def test_mysql_index_updates_removes_reconciles_and_reports_status(tmp_path):
    path = _db(tmp_path)
    factory = _factory(path)
    service = KnowledgeService(factory)
    first = _import(service, "lifecycle-a")
    second = _import(service, "lifecycle-b")
    ids = _chunk_ids(path, first.source_id) + _chunk_ids(path, second.source_id)
    index = MySQLVectorSemanticIndex(conn_factory=factory, model_info=MODEL)
    initial = [SemanticVectorRecord(ids[0], "same", [1, 0, 0]), SemanticVectorRecord(ids[1], "old", [0, 1, 0])]
    assert index.upsert(initial) == (2, 0)
    assert index.upsert(initial) == (0, 0)
    assert index.upsert([SemanticVectorRecord(ids[1], "new", [0, 0, 1])]) == (0, 1)
    assert index.status().indexed_chunks == 2
    assert index.remove([ids[0]]) == 1
    assert index.reconcile([]) == 1
    assert index.status().indexed_chunks == 0
    conn = connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_vector_blobs").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("vector", [[1, 2], [0, 0, 0], [float("nan"), 0, 1], [float("inf"), 0, 1]])
def test_mysql_index_rejects_invalid_vectors(tmp_path, vector):
    path = _db(tmp_path)
    index = MySQLVectorSemanticIndex(conn_factory=_factory(path), model_info=MODEL)
    with pytest.raises(ValueError):
        index.upsert([SemanticVectorRecord("missing", "checksum", vector)])
    with pytest.raises(ValueError):
        index.search(vector, 1)


def test_mysql_blob_table_is_only_created_by_explicit_migration(tmp_path):
    path = _db(tmp_path)
    index = MySQLVectorSemanticIndex(conn_factory=_factory(path), model_info=MODEL)
    # Index operations never issue schema DDL; if the table is absent they fail closed.
    conn = connect(path)
    conn.execute("DROP TABLE knowledge_vector_blobs")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.OperationalError):
        index.search([1, 0, 0], 1)


def test_v11_runtime_verification_issues_no_ddl():
    class ReadOnlyMySQL:
        in_transaction = False

        def __init__(self):
            self.statements = []

        def execute_native(self, sql, params=()):
            self.statements.append(sql)
            if "version=%s" in sql and params == (10,):
                rows = [(10,)]
            elif "version=%s" in sql and params == (11,):
                rows = [("knowledge_vector_blobs", _CHECKSUM)]
            elif "TABLE_NAME='knowledge_embedding_records'" in sql:
                rows = [("int", "int", "NO", "PRI", "auto_increment")]
            elif "SELECT ENGINE" in sql:
                rows = [("InnoDB",)]
            elif "COLUMN_NAME,DATA_TYPE,COLUMN_TYPE" in sql:
                rows = [
                    ("vector_id", "int", "int", "NO", "PRI", None, ""),
                    ("embedding", "longblob", "longblob", "NO", "", None, ""),
                ]
            elif "KEY_COLUMN_USAGE" in sql:
                rows = [("vector_id", "knowledge_embedding_records", "vector_id", "CASCADE")]
            else:  # pragma: no cover - unexpected query must fail closed
                raise AssertionError(f"unexpected verification query: {sql}")

            class Result:
                def fetchone(self):
                    return rows[0] if rows else None

                def fetchall(self):
                    return rows

            return Result()

    conn = ReadOnlyMySQL()
    install_knowledge_vector_blobs_v11(conn, install=False)
    assert all(not statement.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))
               for statement in conn.statements)


def test_live_mysql_vector_lifecycle_and_scope():
    from app.storage.mysql import connect_mysql
    from app.storage.mysql_knowledge_vectors import verify_knowledge_vector_blobs_v11
    from tests.test_mysql_owner_live import _config

    config = _config()
    factory = lambda: connect_mysql(config)
    index = MySQLVectorSemanticIndex(conn_factory=factory, model_info=MODEL)
    suffix = uuid.uuid4().hex
    source_ids = [f"sem-source-{suffix}-{i}" for i in range(3)]
    document_ids = [f"sem-doc-{suffix}-{i}" for i in range(3)]
    chunk_ids = [f"sem-chunk-{suffix}-{i}" for i in range(3)]
    now = "2026-10-30T00:00:00+00:00"
    try:
        with factory() as conn:
            verify_knowledge_vector_blobs_v11(conn)
            for i, source_id in enumerate(source_ids):
                conn.execute_native(
                    "INSERT INTO knowledge_sources(source_id,source_type,display_name,uri,metadata,status,"
                    "sensitivity,remote_policy,access_scope,created_at,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (source_id, "local_document", f"semantic test {i}", f"/{suffix}/{i}",
                     json.dumps({"account_id": "acct-b" if i == 1 else "acct-a"}),
                     "active", "public", "allow", "private", now, now),
                )
                conn.execute_native(
                    "INSERT INTO knowledge_documents(document_id,source_id,source_type,title,uri,checksum,"
                    "mime_type,status,sensitivity,remote_policy,source_ref,created_at,updated_at,indexed_at,metadata) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (document_ids[i], source_id, "local_document", f"semantic test {i}", f"/{suffix}/{i}",
                     f"sum-{suffix}-{i}", "text/plain", "active", "public", "allow", f"ref-{suffix}-{i}",
                     now, now, now,
                     (json.dumps({"account_id": None, "mail_account_id": None}) if i == 0
                      else json.dumps({"account_id": "acct-b"}) if i == 1 else "{}")),
                )
                conn.execute_native(
                    "INSERT INTO knowledge_chunks(chunk_id,document_id,chunk_index,text,char_count,token_estimate,"
                    "source_ref,sensitivity,remote_policy,metadata,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (chunk_ids[i], document_ids[i], 0, f"semantic content {i}", 18, 4, f"ref-{suffix}-{i}",
                     "secret" if i == 2 else "public", "deny" if i == 2 else "allow", "{}", now),
                )
        records = [
            SemanticVectorRecord(chunk_ids[0], "sum-a", [1, 0, 0]),
            SemanticVectorRecord(chunk_ids[1], "sum-b", [0.8, 0.2, 0]),
            SemanticVectorRecord(chunk_ids[2], "sum-c", [0, 1, 0]),
        ]
        assert index.upsert(records) == (3, 0)
        assert index.upsert(records) == (0, 0)
        assert [hit.chunk_id for hit in index.search_scoped([1, 0, 0], 5)] == chunk_ids[:2]
        assert [hit.chunk_id for hit in index.search_scoped([1, 0, 0], 5, account_ids=["acct-a"])] == [chunk_ids[0]]
        assert [hit.chunk_id for hit in index.search_scoped([1, 0, 0], 5, account_ids=["acct-b"])] == [chunk_ids[1]]
        assert index.search_scoped([1, 0, 0], 5, account_ids=["wrong-account"]) == []
        assert index.search_scoped([1, 0, 0], 5, source_ids=[source_ids[2]]) == []
        with factory() as conn:
            vector_id = conn.execute_native(
                "SELECT vector_id FROM knowledge_embedding_records WHERE index_key=%s AND chunk_id=%s",
                (MODEL.index_key, chunk_ids[0]),
            ).fetchone()[0]
            conn.execute_native("DELETE FROM knowledge_embedding_records WHERE vector_id=%s", (vector_id,))
        with factory() as conn:
            assert conn.execute_native(
                "SELECT 1 FROM knowledge_vector_blobs WHERE vector_id=%s", (vector_id,),
            ).fetchone() is None
        assert index.remove([chunk_ids[1]]) == 1
        assert index.reconcile([]) == 1
        assert index.status().indexed_chunks == 0
    finally:
        with factory() as conn:
            conn.execute_native(
                "DELETE FROM knowledge_embedding_records WHERE index_key=%s AND chunk_id IN (%s,%s,%s)",
                (MODEL.index_key, *chunk_ids),
            )
            conn.execute_native("DELETE FROM knowledge_chunks WHERE chunk_id IN (%s,%s,%s)", chunk_ids)
            conn.execute_native("DELETE FROM knowledge_documents WHERE document_id IN (%s,%s,%s)", document_ids)
            conn.execute_native("DELETE FROM knowledge_sources WHERE source_id IN (%s,%s,%s)", source_ids)


def test_semantic_builder_selects_mysql_without_opening_a_connection():
    from app.integrations.local_semantic import build_local_semantic_components

    def forbidden_connection():
        raise AssertionError("component construction must remain lazy")

    provider, index = build_local_semantic_components(
        conn_factory=forbidden_connection, enabled=True, provider_name="fastembed",
        index_provider_name="sqlite_vec", model_name="synthetic-model", dimensions=3,
        cache_dir="/unused", batch_size=2, query_prefix="", normalize_embeddings=True,
        local_files_only=True, storage_backend="mysql",
    )
    assert provider.model_info.dimensions == 3
    assert isinstance(index, MySQLVectorSemanticIndex)
