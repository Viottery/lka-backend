"""Synthetic real-engine checks for domain writes that replace FTS rows."""
from __future__ import annotations

import secrets
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.core.local_config import (
    AgentConfig,
    EmbeddingConfig,
    LLMProviderConfig,
    LocalAppConfig,
    ManagementConfig,
    MemoryConfig,
    MessageHistoryConfig,
    RerankerConfig,
)
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput
from app.domains.mail import MailAccountInput, MailMessageInput
from app.domains.matters import MatterCreateInput, MatterUpdateInput
from app.storage.connections import unregister_mysql
from app.storage.mysql_fulltext import execute_fulltext
from tests.test_mysql_owner_live import _config


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = _config()
    local = LocalAppConfig(
        llm=LLMProviderConfig(provider="mock"),
        agent=AgentConfig(unified_entry_enabled=True, orchestrator="langgraph", checkpoint_backend="sqlite"),
        management=ManagementConfig(enabled=True, autonomous_enabled=False),
        memory=MemoryConfig(enabled=False, background_enabled=False),
        message_history=MessageHistoryConfig(enabled=False, background_enabled=False),
        embedding=EmbeddingConfig(enabled=False), reranker=RerankerConfig(enabled=False),
    )
    data = tmp_path / "mysql-runtime"
    settings = Settings(
        _env_file=None, data_dir=data, storage_backend="mysql",
        mysql_host=config.host, mysql_port=config.port, mysql_user=config.user,
        mysql_password=config.password, mysql_database=config.database,
        workspace_roots=str(tmp_path), db_timing_enabled=False,
    )
    monkeypatch.setattr(Settings, "load_local_config", lambda _: local)
    instance = LocalKnowledgeAgentRuntime(settings)
    assert instance._mysql_config == settings.mysql_config()
    try:
        yield instance
    finally:
        instance.stop()
        unregister_mysql(instance.db_path, expected=instance._mysql_config)


def _han_nonce() -> str:
    return "".join(chr(0x4E00 + secrets.randbelow(0x5200)) for _ in range(10))


def _hits(runtime, table: str, key: str, phrase: str) -> list[tuple]:
    with runtime._conn() as conn:
        cursor = execute_fulltext(
            conn,
            f"SELECT {key} FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT 20",
            (f'"{phrase}"',),
        )
        assert cursor is not None
        return [tuple(row) for row in cursor.fetchall()]


def test_domain_replacements_keep_rare_cjk_terms_searchable(runtime):
    suffix = uuid4().hex
    old_phrase, new_phrase = _han_nonce(), _han_nonce()
    mail_external_id = f"mysql-domain-ft-{suffix}"
    mail_account = MailAccountInput(email_address=f"{suffix}@example.invalid")
    matter_id = f"mysql-domain-ft-{suffix}"
    knowledge_uri = f"synthetic://mysql-domain-ft/{suffix}"
    knowledge_source = KnowledgeSourceInput(display_name=suffix, uri=knowledge_uri)
    imported_documents: set[str] = set()
    account_id = None
    message_id = None
    try:
        first = runtime.mail_service.import_messages(
            account=mail_account,
            messages=[MailMessageInput(
                external_id=mail_external_id, subject=old_phrase,
                body_text="Synthetic message " + old_phrase,
                received_at="2026-10-29T00:00:00+00:00",
            )],
        )
        account_id = first.account_id
        with runtime._conn() as conn:
            message_id = conn.execute(
                "SELECT message_id FROM mail_messages WHERE account_id=? AND external_id=?",
                (account_id, mail_external_id),
            ).fetchone()[0]
        assert [item[0] for item in _hits(runtime, "mail_messages_fts", "message_id", old_phrase)] == [message_id]

        runtime.mail_service.import_messages(
            account=mail_account,
            messages=[MailMessageInput(
                external_id=mail_external_id, subject=new_phrase,
                body_text="Synthetic message " + new_phrase,
                received_at="2026-10-29T00:00:00+00:00",
            )],
        )
        assert not _hits(runtime, "mail_messages_fts", "message_id", old_phrase)
        assert [item[0] for item in _hits(runtime, "mail_messages_fts", "message_id", new_phrase)] == [message_id]

        created = runtime.matter_service.create_matter(
            MatterCreateInput(title=old_phrase, summary="Synthetic matter " + old_phrase),
            matter_id=matter_id,
        )
        assert [item[0] for item in _hits(runtime, "matters_fts", "matter_id", old_phrase)] == [matter_id]
        runtime.matter_service.update_matter(
            matter_id=matter_id,
            payload=MatterUpdateInput(
                title=new_phrase, summary="Synthetic matter " + new_phrase,
                expected_revision=created.revision,
            ),
        )
        assert not _hits(runtime, "matters_fts", "matter_id", old_phrase)
        assert [item[0] for item in _hits(runtime, "matters_fts", "matter_id", new_phrase)] == [matter_id]

        first_doc = runtime.knowledge_service.import_text_document(KnowledgeDocumentInput(
            source=knowledge_source, title="Synthetic knowledge " + old_phrase,
            text="Synthetic knowledge text " + old_phrase, uri=knowledge_uri,
        ))
        imported_documents.add(first_doc.document_id)
        assert _hits(runtime, "knowledge_chunks_fts", "document_id", old_phrase)
        second_doc = runtime.knowledge_service.import_text_document(KnowledgeDocumentInput(
            source=knowledge_source, title="Synthetic knowledge " + new_phrase,
            text="Synthetic knowledge text " + new_phrase, uri=knowledge_uri,
        ))
        imported_documents.add(second_doc.document_id)
        assert not _hits(runtime, "knowledge_chunks_fts", "document_id", old_phrase)
        assert {item[0] for item in _hits(
            runtime, "knowledge_chunks_fts", "document_id", new_phrase,
        )} == {second_doc.document_id}
    finally:
        with runtime._conn() as conn:
            if message_id:
                conn.execute("DELETE FROM mail_messages_fts WHERE message_id=?", (message_id,))
                conn.execute("DELETE FROM mail_chunks WHERE message_id=?", (message_id,))
                conn.execute("DELETE FROM mail_attachments WHERE message_id=?", (message_id,))
                conn.execute("DELETE FROM mail_messages WHERE message_id=?", (message_id,))
            if account_id:
                conn.execute("DELETE FROM mail_accounts WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM matters_fts WHERE matter_id=?", (matter_id,))
            conn.execute("DELETE FROM matter_changes WHERE matter_id=?", (matter_id,))
            conn.execute("DELETE FROM matters WHERE matter_id=?", (matter_id,))
            for document_id in imported_documents:
                conn.execute("DELETE FROM knowledge_chunks_fts WHERE document_id=?", (document_id,))
                conn.execute("DELETE FROM knowledge_chunks WHERE document_id=?", (document_id,))
                conn.execute("DELETE FROM knowledge_documents WHERE document_id=?", (document_id,))
            if imported_documents:
                conn.execute("DELETE FROM knowledge_sources WHERE source_id=?", (first_doc.source_id,))
