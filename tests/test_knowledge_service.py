from __future__ import annotations

import sqlite3
from pathlib import Path

from app.domains.knowledge import (
    KnowledgeDocumentInput,
    KnowledgeService,
    KnowledgeSourceInput,
)
from app.storage.db import connect, get_db_path, init_db


def _service(tmp_path: Path) -> KnowledgeService:
    db_path = get_db_path(tmp_path / "data")
    init_db(db_path)
    return KnowledgeService(lambda: connect(db_path))


def test_knowledge_import_search_and_load_chunks(tmp_path):
    service = _service(tmp_path)

    imported = service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                source_type="local_document",
                display_name="minecraft_wiki.md",
                uri="/knowledge_samples/minecraft_wiki.md",
                sensitivity="public",
                remote_policy="allow",
            ),
            title="minecraft_wiki.md",
            uri="/knowledge_samples/minecraft_wiki.md",
            text=(
                "Minecraft Wiki 是有关 Minecraft 的中文资料库。\n\n"
                "红石、方块、生物群系和合成配方可以作为知识库搜索样本。"
            ),
            mime_type="text/markdown",
            sensitivity="public",
            remote_policy="allow",
        )
    )

    assert imported.imported_chunks >= 1
    assert imported.secret_chunks_redacted == 0

    search = service.search(query="红石 方块", limit=3)

    assert search.filtered_count == 0
    assert len(search.results) == 1
    assert search.results[0].source_type == "local_document"
    assert search.results[0].untrusted_data is True
    assert search.results[0].policy_decision == "allowed"
    assert "红石" in search.results[0].snippet

    loaded = service.load_chunks(chunk_ids=[search.results[0].chunk_id])

    assert loaded.filtered_count == 0
    assert len(loaded.chunks) == 1
    assert loaded.chunks[0].document_id == imported.document_id
    assert "合成配方" in loaded.chunks[0].text


def test_knowledge_search_falls_back_to_chinese_substring_matching(tmp_path):
    service = _service(tmp_path)
    service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                display_name="operators.md",
                uri="/knowledge_samples/operators.md",
                sensitivity="public",
                remote_policy="allow",
            ),
            title="operators.md",
            uri="/knowledge_samples/operators.md",
            text="干员档案和敌人档案可以作为本地知识库的中文检索样本。",
            sensitivity="public",
            remote_policy="allow",
        )
    )

    search = service.search(query="干员", limit=3)

    assert len(search.results) == 1
    assert "干员档案" in search.results[0].snippet


def test_knowledge_secret_like_content_is_not_returned_or_indexed_raw(tmp_path):
    service = _service(tmp_path)

    imported = service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                source_type="local_document",
                display_name="secrets.txt",
                uri="/workspace/secrets.txt",
                sensitivity="personal",
                remote_policy="redact",
            ),
            title="secrets.txt",
            uri="/workspace/secrets.txt",
            text="normal note\napi_key = sk-test-secret-token-value-abcdefg\n",
        )
    )

    assert imported.secret_chunks_redacted == 1

    search = service.search(query="secret token", limit=5)

    assert search.results == []
    assert search.filtered_count == 1

    document = service.load_document(document_id=imported.document_id, include_text=True)

    assert document.text == ""
    assert document.policy_decision == "redacted"
    assert document.truncated is True


def test_knowledge_deny_policy_filters_prompt_context(tmp_path):
    service = _service(tmp_path)

    imported = service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                source_type="local_document",
                display_name="private.md",
                uri="/workspace/private.md",
                sensitivity="secret",
                remote_policy="deny",
            ),
            title="private.md",
            uri="/workspace/private.md",
            text="PRTS confidential operator planning notes",
            sensitivity="secret",
            remote_policy="deny",
        )
    )

    search = service.search(query="PRTS operator", limit=5)
    document = service.load_document(document_id=imported.document_id)
    loaded = service.load_chunks(chunk_ids=document.chunk_ids)

    assert search.results == []
    assert search.filtered_count == 1
    assert loaded.chunks == []


def test_knowledge_access_audit_records_search(tmp_path):
    data_dir = tmp_path / "data"
    db_path = get_db_path(data_dir)
    init_db(db_path)
    service = KnowledgeService(lambda: connect(db_path))
    service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                display_name="audit.md",
                uri="/workspace/audit.md",
                sensitivity="public",
                remote_policy="allow",
            ),
            title="audit.md",
            uri="/workspace/audit.md",
            text="audit searchable text",
            sensitivity="public",
            remote_policy="allow",
        )
    )

    service.search(query="audit", limit=1, tool_name="knowledge.search")

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT tool_name, action, result_count, remote_data_sent FROM knowledge_access_audit"
        ).fetchall()
    finally:
        conn.close()

    assert rows == [("knowledge.search", "search", 1, 0)]
