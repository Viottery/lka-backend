"""Real-engine full-text checks using only uniquely identified synthetic rows."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from app.domains.knowledge import KnowledgeService
from app.domains.mail import MailService
from app.domains.matters import MatterService
from app.storage.mysql import MySQLConfig, connect_mysql
from app.storage.mysql_fulltext import _FULLTEXT_V9_NATIVE_SQL, execute_fulltext
from tests.test_mysql_owner_live import _config


def _migration_config() -> MySQLConfig:
    filename = os.environ.get("LKA_MYSQL_TEST_MIGRATION_CONFIG")
    if not filename:
        pytest.skip("dedicated MySQL migration config not supplied")
    config = MySQLConfig(**json.loads(Path(filename).read_text()))
    if not any(part in config.database.lower() for part in ("test", "lab")):
        pytest.fail("live MySQL tests require an explicitly named test/lab database")
    return config


def _han_nonce() -> str:
    # Random Han characters make each test term unique even if a populated lab
    # database is reused. No existing application text is fetched or inspected.
    return "".join(chr(0x4E00 + secrets.randbelow(0x5200)) for _ in range(10))


@pytest.fixture
def fulltext_store():
    conn = connect_mysql(_config())
    prefix = "lkafts_" + uuid4().hex
    ids = {
        "memory": prefix + "_memory",
        "memory_bulk_a": prefix + "_memory_bulk_a",
        "memory_bulk_b": prefix + "_memory_bulk_b",
        "memory_bulk_denied": prefix + "_memory_bulk_denied",
        "message": prefix + "_message",
        "domain_message": prefix + "_domain_message",
        "account": prefix + "_account",
        "matter_live": prefix + "_matter_live",
        "matter_deleted": prefix + "_matter_deleted",
        "source": prefix + "_source",
        "doc_active": prefix + "_doc_active",
        "doc_inactive": prefix + "_doc_inactive",
        "chunk_active": prefix + "_chunk_active",
        "chunk_inactive": prefix + "_chunk_inactive",
        "session": prefix + "_session",
    }
    try:
        yield conn, ids
    finally:
        conn.rollback()
        # Every predicate is an exact synthetic identifier created above.
        conn.execute_native("DELETE FROM agent_session_context_messages WHERE session_id=%s", (ids["session"],))
        conn.execute_native("DELETE FROM conversation_history_dirty WHERE session_id=%s", (ids["session"],))
        conn.execute_native(
            "DELETE FROM conversation_history_fts WHERE rowid IN "
            "(SELECT rowid FROM conversation_history_fts_map WHERE session_id=%s)", (ids["session"],),
        )
        conn.execute_native("DELETE FROM conversation_history_fts_map WHERE session_id=%s", (ids["session"],))
        conn.execute_native("DELETE FROM memory_entries WHERE memory_id=%s", (ids["memory"],))
        conn.execute_native("DELETE FROM memory_entries WHERE memory_id IN (%s,%s)",
                            (ids["memory_bulk_a"], ids["memory_bulk_b"]))
        conn.execute_native("DELETE FROM memory_entries WHERE memory_id=%s", (ids["memory_bulk_denied"],))
        conn.execute_native("DELETE FROM memory_entries_fts WHERE memory_id IN (%s,%s)",
                            (ids["memory_bulk_a"], ids["memory_bulk_b"]))
        conn.execute_native("DELETE FROM memory_entries_fts WHERE memory_id=%s", (ids["memory_bulk_denied"],))
        conn.execute_native("DELETE FROM memory_entries_fts WHERE memory_id=%s", (ids["memory"],))
        conn.execute_native("DELETE FROM mail_messages_fts WHERE message_id=%s", (ids["message"],))
        conn.execute_native("DELETE FROM mail_messages_fts WHERE message_id=%s", (ids["domain_message"],))
        conn.execute_native("DELETE FROM mail_messages WHERE message_id=%s", (ids["domain_message"],))
        conn.execute_native("DELETE FROM mail_accounts WHERE account_id=%s", (ids["account"],))
        conn.execute_native("DELETE FROM matters_fts WHERE matter_id IN (%s,%s)",
                            (ids["matter_live"], ids["matter_deleted"]))
        conn.execute_native("DELETE FROM matters WHERE matter_id IN (%s,%s)",
                            (ids["matter_live"], ids["matter_deleted"]))
        conn.execute_native("DELETE FROM knowledge_chunks_fts WHERE chunk_id IN (%s,%s)",
                            (ids["chunk_active"], ids["chunk_inactive"]))
        conn.execute_native("DELETE FROM knowledge_chunks WHERE chunk_id IN (%s,%s)",
                            (ids["chunk_active"], ids["chunk_inactive"]))
        conn.execute_native("DELETE FROM knowledge_documents WHERE document_id IN (%s,%s)",
                            (ids["doc_active"], ids["doc_inactive"]))
        conn.execute_native("DELETE FROM knowledge_sources WHERE source_id=%s", (ids["source"],))
        conn.commit()
        conn.close()


def _search(conn, table: str, query: str, extra_sql: str = "", params=()):
    cursor = execute_fulltext(
        conn,
        f"SELECT {table}.rowid FROM {table} WHERE {table} MATCH ? {extra_sql} ORDER BY rank LIMIT 20",
        (f'"{query}"', *params),
    )
    assert cursor is not None
    return [row[0] for row in cursor.fetchall()]


def _memory_ids(conn, phrase: str) -> set[str]:
    cursor = execute_fulltext(
        conn,
        "SELECT e.memory_id FROM memory_entries_fts f JOIN memory_entries e "
        "ON e.memory_id=f.memory_id WHERE memory_entries_fts MATCH ? "
        "AND e.scope='global' AND e.status='active' "
        "AND (e.expires_at IS NULL OR e.expires_at>?) ORDER BY rank LIMIT 20",
        (f'"{phrase}"', "9999-12-31"),
    )
    assert cursor is not None
    return {str(row[0]) for row in cursor.fetchall()}


def test_native_ngram_delete_insert_vs_update_in_place():
    """Prove the InnoDB ngram behavior on one isolated temporary test table."""
    conn = connect_mysql(_migration_config())
    table = "lka_fts_v8_proof_" + uuid4().hex[:16]
    replace_id, update_id = "delete_insert", "update_in_place"
    original = "旧内容替换测试原文"
    replacement = "难肞丁区焾閫訂芓啽炗"

    def search(term):
        return [row[0] for row in conn.execute_native(
            f"SELECT memory_id FROM `{table}` WHERE MATCH(content,memory_type) "
            "AGAINST(%s IN BOOLEAN MODE)", (f'"{term}"',),
        ).fetchall()]

    try:
        conn.execute_native(
            f"CREATE TABLE `{table}` (rowid BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,"
            "memory_id VARCHAR(191) COLLATE utf8mb4_0900_bin NOT NULL UNIQUE,"
            "content LONGTEXT,memory_type VARCHAR(191),"
            "FULLTEXT KEY proof_ngram(content,memory_type) WITH PARSER ngram) "
            "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin",
        )
        for memory_id in (replace_id, update_id):
            conn.execute_native(
                f"INSERT INTO `{table}`(memory_id,content,memory_type) VALUES(%s,%s,'fact')",
                (memory_id, original),
            )
        conn.commit()
        assert set(search(original)) == {replace_id, update_id}

        conn.execute_native(f"DELETE FROM `{table}` WHERE memory_id=%s", (replace_id,))
        conn.execute_native(
            f"INSERT INTO `{table}`(memory_id,content,memory_type) VALUES(%s,%s,'fact')",
            (replace_id, replacement),
        )
        conn.execute_native(f"UPDATE `{table}` SET content=%s WHERE memory_id=%s",
                            (replacement, update_id))
        conn.commit()
        replacement_hits = set(search(replacement))
        original_hits = set(search(original))
        assert replacement_hits == {replace_id, update_id}, (
            f"delete_insert={replace_id in replacement_hits}; "
            f"update_in_place={update_id in replacement_hits}; old_hits={original_hits!r}"
        )
        assert not original_hits
    finally:
        conn.rollback()
        conn.execute_native(f"DROP TABLE IF EXISTS `{table}`")
        conn.close()


def test_v9_memory_trigger_bulk_and_repeated_updates_keep_ngram_tokens_visible():
    migration_file = os.environ.get("LKA_MYSQL_TEST_MIGRATION_CONFIG")
    if not migration_file:
        pytest.skip("privileged migration config required for isolated trigger proof")
    config = MySQLConfig(**json.loads(Path(migration_file).read_text(encoding="utf-8")))
    conn = connect_mysql(config)
    suffix = uuid4().hex[:12]
    fts, source_table = (f"lkafts8_{suffix}_{name}" for name in ("fts", "source"))
    trigger = f"lkafts8_{suffix}_update"
    delete_trigger = f"lkafts8_{suffix}_delete"
    source_ids = (f"{suffix}_source1", f"{suffix}_source2")
    original, replacement, replacement2 = "旧内容替换测试原文", "难肞丁区焾閫訂芓啽炗", "连续更新二次索引"
    rolled_back = "回滚内容不可被索引"

    def search(term):
        return {row[0] for row in conn.execute_native(
            f"SELECT memory_id FROM `{fts}` WHERE MATCH(content,memory_type) AGAINST(%s IN BOOLEAN MODE)",
            (f'"{term}"',),
        ).fetchall()}

    try:
        conn.execute_native(
            f"CREATE TABLE `{fts}` (rowid BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,"
            "memory_id VARCHAR(191) COLLATE utf8mb4_0900_bin NOT NULL UNIQUE,content LONGTEXT,"
            "memory_type VARCHAR(191),FULLTEXT KEY proof_ngram(content,memory_type) WITH PARSER ngram) "
            "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin",
        )
        conn.execute_native(
            f"CREATE TABLE `{source_table}` (memory_id VARCHAR(191) COLLATE utf8mb4_0900_bin PRIMARY KEY,"
            "content LONGTEXT,memory_type VARCHAR(191))",
        )
        conn.execute_native(
            f"CREATE TRIGGER `{trigger}` AFTER UPDATE ON `{source_table}` FOR EACH ROW "
            + _FULLTEXT_V9_NATIVE_SQL.split(" FOR EACH ROW", 1)[1]
            .replace("memory_entries_fts", f"`{fts}`")
            .replace("memory_entries", f"`{source_table}`")
            .replace("memory_id", "memory_id"),
        )
        conn.execute_native(
            f"CREATE TRIGGER `{delete_trigger}` AFTER DELETE ON `{source_table}` FOR EACH ROW "
            f"DELETE FROM `{fts}` WHERE memory_id=OLD.memory_id",
        )
        for source_id in source_ids:
            conn.execute_native(f"INSERT INTO `{source_table}` VALUES(%s,%s,'fact')", (source_id, original))
            conn.execute_native(
                f"INSERT INTO `{fts}`(memory_id,content,memory_type) VALUES(%s,%s,'fact')",
                (source_id, original),
            )
        conn.commit()
        assert search(original) == set(source_ids)
        conn.execute_native(f"UPDATE `{source_table}` SET content=%s", (replacement,))
        conn.commit()
        assert not search(original)
        assert search(replacement) == set(source_ids)
        conn.execute_native(f"UPDATE `{source_table}` SET content=%s WHERE memory_id=%s",
                            (replacement2, source_ids[0]))
        conn.commit()
        assert search(replacement) == {source_ids[1]}
        assert search(replacement2) == {source_ids[0]}
        conn.execute_native(f"UPDATE `{source_table}` SET content=%s WHERE memory_id=%s",
                            (replacement2, source_ids[1]))
        # Exercise multiple independent row-level updates within one transaction.
        conn.commit()
        assert not search(replacement)
        assert search(replacement2) == set(source_ids)
        conn.execute_native(f"UPDATE `{source_table}` SET content=%s WHERE memory_id=%s",
                            (rolled_back, source_ids[0]))
        conn.rollback()
        assert not search(rolled_back)
        assert search(replacement2) == set(source_ids)
        conn.execute_native(f"DELETE FROM `{source_table}` WHERE memory_id=%s", (source_ids[0],))
        conn.commit()
        assert search(replacement2) == {source_ids[1]}
    finally:
        conn.rollback()
        conn.execute_native(f"DROP TRIGGER IF EXISTS `{trigger}`")
        conn.execute_native(f"DROP TRIGGER IF EXISTS `{delete_trigger}`")
        conn.execute_native(f"DROP TABLE IF EXISTS `{source_table}`")
        conn.execute_native(f"DROP TABLE IF EXISTS `{fts}`")
        conn.close()


def test_memory_trigger_insert_update_delete_and_cjk_search(fulltext_store):
    conn, ids = fulltext_store
    phrase, replacement = _han_nonce(), _han_nonce()
    values = (ids["memory"], "fact", phrase, "global", None, "active", 0.9, "private",
              None, ids["memory"], 1, None, None, "{}", "2026-10-29", "2026-10-29")
    conn.execute_native(
        "INSERT INTO memory_entries(memory_id,memory_type,content,scope,project_id,status,confidence,"
        "sensitivity,expires_at,dedupe_key,version,supersedes_id,extraction_model,metadata,created_at,updated_at) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", values,
    )
    conn.commit()
    assert _search(conn, "memory_entries_fts", phrase)

    conn.execute_native("UPDATE memory_entries SET content=%s WHERE memory_id=%s", (replacement, ids["memory"]))
    conn.commit()
    assert not _search(conn, "memory_entries_fts", phrase)
    assert _search(conn, "memory_entries_fts", replacement)

    conn.execute_native("DELETE FROM memory_entries WHERE memory_id=%s", (ids["memory"],))
    conn.commit()
    assert not _search(conn, "memory_entries_fts", replacement)


def test_memory_trigger_updates_reported_cjk_phrase(fulltext_store):
    """Regression for the exact rare-CJK replacement phrase seen in live CI."""
    conn, ids = fulltext_store
    original = "旧内容替换测试原文"
    replacement = "难肞丁区焾閫訂芓啽炗"
    values = (ids["memory"], "fact", original, "global", None, "active", 0.9, "private",
              None, ids["memory"], 1, None, None, "{}", "2026-10-29", "2026-10-29")
    conn.execute_native(
        "INSERT INTO memory_entries(memory_id,memory_type,content,scope,project_id,status,confidence,"
        "sensitivity,expires_at,dedupe_key,version,supersedes_id,extraction_model,metadata,created_at,updated_at) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", values,
    )
    conn.commit()
    assert _search(conn, "memory_entries_fts", original)

    conn.execute_native("UPDATE memory_entries SET content=%s WHERE memory_id=%s",
                        (replacement, ids["memory"]))
    conn.commit()
    assert _search(conn, "memory_entries_fts", replacement)
    conn.execute_native("UPDATE memory_entries SET content=%s WHERE memory_id=%s",
                        (original, ids["memory"]))
    conn.commit()
    assert not _search(conn, "memory_entries_fts", replacement)
    assert _search(conn, "memory_entries_fts", original)
    conn.execute_native("DELETE FROM memory_entries WHERE memory_id=%s", (ids["memory"],))
    conn.commit()
    assert not _search(conn, "memory_entries_fts", original)


def test_memory_trigger_bulk_update_and_multirow_transaction(fulltext_store):
    conn, ids = fulltext_store
    original = "旧内容替换测试原文"
    replacement = "难肞丁区焾閫訂芓啽炗"
    second_replacement = "同一事务连续写入索引"
    rollback_term = "回滚后不能成为索引内容"
    memory_ids = (ids["memory_bulk_a"], ids["memory_bulk_b"])
    for memory_id in memory_ids:
        conn.execute_native(
            "INSERT INTO memory_entries(memory_id,memory_type,content,scope,project_id,status,confidence,"
            "sensitivity,expires_at,dedupe_key,version,supersedes_id,extraction_model,metadata,created_at,updated_at) "
            "VALUES(%s,'fact',%s,'global',NULL,'active',0.9,'private',NULL,%s,1,NULL,NULL,'{}',"
            "'2026-10-29','2026-10-29')", (memory_id, original, memory_id),
        )
    denied_id = ids["memory_bulk_denied"]
    conn.execute_native(
        "INSERT INTO memory_entries(memory_id,memory_type,content,scope,project_id,status,confidence,"
        "sensitivity,expires_at,dedupe_key,version,supersedes_id,extraction_model,metadata,created_at,updated_at) "
        "VALUES(%s,'fact',%s,'global',NULL,'candidate',0.9,'private',NULL,%s,1,NULL,NULL,'{}',"
        "'2026-10-29','2026-10-29')", (denied_id, original, denied_id),
    )
    conn.commit()
    assert _memory_ids(conn, original) == set(memory_ids)

    conn.execute_native(
        "UPDATE memory_entries SET content=%s WHERE memory_id IN (%s,%s)",
        (replacement, *memory_ids),
    )
    conn.commit()
    assert not _memory_ids(conn, original)
    assert _memory_ids(conn, replacement) == set(memory_ids)

    for memory_id in memory_ids:
        conn.execute_native("UPDATE memory_entries SET content=%s WHERE memory_id=%s",
                            (second_replacement, memory_id))
    conn.commit()
    assert not _memory_ids(conn, replacement)
    assert _memory_ids(conn, second_replacement) == set(memory_ids)

    conn.execute_native("UPDATE memory_entries SET content=%s WHERE memory_id=%s",
                        (rollback_term, memory_ids[0]))
    conn.rollback()
    assert not _memory_ids(conn, rollback_term)
    assert _memory_ids(conn, second_replacement) == set(memory_ids)

    conn.execute_native("DELETE FROM memory_entries WHERE memory_id IN (%s,%s)", memory_ids)
    conn.commit()
    assert not _memory_ids(conn, second_replacement)


def test_mail_fts_search_update_delete_and_filter_boundary(fulltext_store):
    conn, ids = fulltext_store
    phrase, replacement = _han_nonce(), _han_nonce()
    conn.execute_native(
        "INSERT INTO mail_messages_fts(message_id,subject,sender,folder,body_text) VALUES(%s,%s,%s,%s,%s)",
        (ids["message"], phrase, "synthetic@example.invalid", "synthetic-folder", phrase),
    )
    conn.commit()
    sql = ("SELECT message_id FROM mail_messages_fts WHERE mail_messages_fts MATCH ? "
           "AND folder=? ORDER BY rank LIMIT 20")
    found = execute_fulltext(conn, sql, (f'"{phrase}"', "synthetic-folder")).fetchall()
    assert [row[0] for row in found] == [ids["message"]]
    assert not execute_fulltext(conn, sql, (f'"{phrase}"', "other-folder")).fetchall()

    conn.execute_native("UPDATE mail_messages_fts SET subject=%s,body_text=%s WHERE message_id=%s",
                        (replacement, replacement, ids["message"]))
    conn.commit()
    assert not execute_fulltext(conn, sql, (f'"{phrase}"', "synthetic-folder")).fetchall()
    assert execute_fulltext(conn, sql, (f'"{replacement}"', "synthetic-folder")).fetchall()
    conn.execute_native("DELETE FROM mail_messages_fts WHERE message_id=%s", (ids["message"],))
    conn.commit()
    assert not execute_fulltext(conn, sql, (f'"{replacement}"', "synthetic-folder")).fetchall()


def test_mysql_v12_native_fulltext_unicode_recall_and_casefold(fulltext_store):
    conn, ids = fulltext_store
    # Exercise native MATCH directly so an adapter fallback cannot hide an
    # index/tokenizer regression. All rows and terms in this test are synthetic.
    terms = ["复瘀刭猠龰辣凊翈崀", "渤饜鿓晕馔灆肺侢墶陥", "窽叹鿰鱮穋樷舚賚恆扵"] + [
        _han_nonce() for _ in range(18)
    ]
    message_ids = [ids["message"] + f"_{index}" for index in range(len(terms))]
    page_ids = [ids["message"] + "_page_a", ids["message"] + "_page_b"]
    try:
        native_misses = []
        adapted_misses = []
        for index, (message_id, term) in enumerate(zip(message_ids, terms, strict=True)):
            subject = "CaseProbeAlpha" if index == 0 else term
            conn.execute_native(
                "INSERT INTO mail_messages_fts(message_id,subject,sender,folder,body_text) "
                "VALUES(%s,%s,'synthetic@example.invalid','synthetic-folder',%s)",
                (message_id, subject, term),
            )
        for message_id in page_ids:
            conn.execute_native(
                "INSERT INTO mail_messages_fts(message_id,subject,sender,folder,body_text) "
                "VALUES(%s,%s,'synthetic@example.invalid','synthetic-folder',%s)",
                (message_id, terms[1], terms[1]),
            )
        conn.commit()
        plan = conn.execute_native(
            "EXPLAIN FORMAT=JSON SELECT f.message_id FROM mail_messages_fts f "
            "WHERE MATCH(f.subject,f.sender,f.folder,f.body_text) AGAINST(%s IN BOOLEAN MODE) "
            "AND (INSTR(LOWER(f.subject),LOWER(%s))>0 OR INSTR(LOWER(f.sender),LOWER(%s))>0 "
            "OR INSTR(LOWER(f.folder),LOWER(%s))>0 OR INSTR(LOWER(f.body_text),LOWER(%s))>0)",
            (terms[1], terms[1], terms[1], terms[1], terms[1]),
        ).fetchone()
        plan_text = json.dumps(plan[0], ensure_ascii=False).casefold()
        assert "ft_mail_messages_fts" in plan_text and "fulltext" in plan_text, plan_text

        page_statements = []
        native_execute = conn.execute_native
        conn.execute_native = lambda statement, parameters=(): (
            page_statements.append(statement), native_execute(statement, parameters),
        )[1]
        page_sql = (
            "SELECT mail_messages_fts.message_id FROM mail_messages_fts "
            "WHERE mail_messages_fts MATCH ? ORDER BY rank,mail_messages_fts.message_id LIMIT ? OFFSET ?"
        )
        try:
            first_page = execute_fulltext(conn, page_sql, (f'"{terms[1]}"', 1, 0)).fetchall()
            second_page = execute_fulltext(conn, page_sql, (f'"{terms[1]}"', 1, 1)).fetchall()
        finally:
            conn.execute_native = native_execute
        assert len(first_page) == 1 and len(second_page) == 1
        assert first_page[0][0] != second_page[0][0]
        assert first_page[0][0] == message_ids[1]
        assert second_page[0][0] in set(page_ids)
        assert any("LIMIT %(lka_param_1)s OFFSET %(lka_param_2)s" in sql for sql in page_statements)

        end_page_statements = []
        native_execute = conn.execute_native
        conn.execute_native = lambda statement, parameters=(): (
            end_page_statements.append(statement), native_execute(statement, parameters),
        )[1]
        try:
            native_page_end = execute_fulltext(
                conn,
                "SELECT mail_messages_fts.message_id FROM mail_messages_fts "
                "WHERE mail_messages_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?",
                ('"caseprobealpha"', 1, 1),
            ).fetchall()
        finally:
            conn.execute_native = native_execute
        assert native_page_end == []
        assert len(end_page_statements) == 2
        assert "LIMIT 1 OFFSET 0" in end_page_statements[1]
        assert all("lka_candidate_query_" not in sql for sql in end_page_statements)

        for index, (message_id, term) in enumerate(zip(message_ids, terms, strict=True)):
            query = "caseprobealpha" if index == 0 else term
            rows = conn.execute_native(
                "SELECT message_id FROM mail_messages_fts WHERE MATCH(subject,sender,folder,body_text) "
                "AGAINST(%s IN BOOLEAN MODE)",
                (f'"{query}"',),
            ).fetchall()
            if message_id not in {row[0] for row in rows}:
                loose = conn.execute_native(
                    "SELECT message_id FROM mail_messages_fts WHERE MATCH(subject,sender,folder,body_text) "
                    "AGAINST(%s IN BOOLEAN MODE)", (term,),
                ).fetchall()
                candidate = conn.execute_native(
                    "SELECT message_id FROM mail_messages_fts WHERE "
                    "MATCH(subject,sender,folder,body_text) AGAINST(%s IN BOOLEAN MODE) "
                    "AND (INSTR(LOWER(subject),LOWER(%s))>0 OR INSTR(LOWER(body_text),LOWER(%s))>0)",
                    (term, term, term),
                ).fetchall()
                literal = conn.execute_native(
                    "SELECT message_id FROM mail_messages_fts WHERE "
                    "INSTR(LOWER(subject),LOWER(%s))>0 OR INSTR(LOWER(body_text),LOWER(%s))>0",
                    (term, term),
                ).fetchall()
                native_misses.append((index, term, bool(loose), bool(candidate), bool(literal)))

            adapted = execute_fulltext(
                conn,
                "SELECT mail_messages_fts.message_id FROM mail_messages_fts "
                "WHERE mail_messages_fts MATCH ? ORDER BY rank LIMIT 20",
                (f'"{query}"',),
            ).fetchall()
            if message_id not in {row[0] for row in adapted}:
                adapted_misses.append((index, term))
        assert not adapted_misses, (
            f"indexed literal candidate missed synthetic terms {adapted_misses!r}; "
            f"native quoted misses observed: {native_misses!r}"
        )
    finally:
        conn.execute_native(
            "DELETE FROM mail_messages_fts WHERE message_id IN (" + ",".join(["%s"] * len(message_ids + page_ids)) + ")",
            tuple(message_ids + page_ids),
        )
        conn.commit()


def test_mail_domain_search_uses_translated_snippet_on_native_candidate(fulltext_store):
    conn, ids = fulltext_store
    phrase = "渤饜鿓晕馔灆肺侢墶陥"
    now = "2026-10-29T00:00:00+00:00"
    conn.execute_native(
        "INSERT INTO mail_accounts(account_id,provider,email_address,display_name,created_at,updated_at) "
        "VALUES(%s,'synthetic',%s,'Synthetic account',%s,%s)",
        (ids["account"], ids["account"] + "@example.invalid", now, now),
    )
    conn.execute_native(
        "INSERT INTO mail_messages(message_id,account_id,external_id,folder,subject,sender,recipients,cc,received_at,"
        "body_text,created_at,updated_at) VALUES(%s,%s,%s,'inbox',%s,'sender@example.invalid','[]','[]',%s,%s,%s,%s)",
        (ids["domain_message"], ids["account"], "synthetic-message", phrase, now, phrase, now, now),
    )
    conn.execute_native(
        "INSERT INTO mail_messages_fts(message_id,subject,sender,folder,body_text) "
        "VALUES(%s,%s,'sender@example.invalid','inbox',%s)",
        (ids["domain_message"], phrase, phrase),
    )
    conn.commit()
    statements = []

    def factory():
        service_conn = connect_mysql(_config())
        service_conn.row_factory = sqlite3.Row
        native_execute = service_conn.execute_native
        service_conn.execute_native = lambda statement, parameters=(): (
            statements.append((statement, parameters)), native_execute(statement, parameters),
        )[1]
        return service_conn

    try:
        result = MailService(factory).search_messages(query=phrase, limit=10)
        assert result.returned_count == 1
        assert result.messages[0].message_id == ids["domain_message"]
        assert phrase in result.messages[0].snippet
        assert any("lka_candidate_query_" in statement for statement, _ in statements)
        assert any("lka_snippet_" in statement for statement, _ in statements)
    finally:
        conn.execute_native("DELETE FROM mail_messages_fts WHERE message_id=%s", (ids["domain_message"],))
        conn.execute_native("DELETE FROM mail_messages WHERE message_id=%s", (ids["domain_message"],))
        conn.execute_native("DELETE FROM mail_accounts WHERE account_id=%s", (ids["account"],))
        conn.commit()


def test_matters_deleted_filter_and_knowledge_active_source_filter(fulltext_store):
    conn, ids = fulltext_store
    matter_phrase = "窽叹鿰鱮穋樷舚賚恆扵"
    for matter_id, deleted in ((ids["matter_live"], None), (ids["matter_deleted"], "2026-10-29")):
        conn.execute_native(
            "INSERT INTO matters(matter_id,title,summary,status,priority,tags,metadata,created_at,updated_at,deleted_at) "
            "VALUES(%s,%s,%s,'open','normal','[]','{}','2026-10-29','2026-10-29',%s)",
            (matter_id, matter_phrase, matter_phrase, deleted),
        )
        conn.execute_native(
            "INSERT INTO matters_fts(matter_id,title,summary,tags) VALUES(%s,%s,%s,'[]')",
            (matter_id, matter_phrase, matter_phrase),
        )
    conn.commit()
    matter_rows = execute_fulltext(
        conn,
        "SELECT m.matter_id FROM matters_fts JOIN matters m ON m.matter_id=matters_fts.matter_id "
        "WHERE matters_fts MATCH ? AND m.deleted_at IS NULL ORDER BY rank,m.matter_id LIMIT 20",
        (f'"{matter_phrase}"',),
    ).fetchall()
    assert [row[0] for row in matter_rows] == [ids["matter_live"]]

    matter_statements = []

    def matter_factory():
        service_conn = connect_mysql(_config())
        service_conn.row_factory = sqlite3.Row
        native_execute = service_conn.execute_native
        service_conn.execute_native = lambda statement, parameters=(): (
            matter_statements.append((statement, parameters)), native_execute(statement, parameters),
        )[1]
        return service_conn

    matter_service = object.__new__(MatterService)
    matter_service._conn_factory = matter_factory
    matter_service.internal_items_enabled = lambda: False
    domain_matters = matter_service.search_matters(query=matter_phrase, limit=10, visibility="user")
    assert [item.matter_id for item in domain_matters.matters] == [ids["matter_live"]]
    matter_candidate = next(sql for sql, _ in matter_statements if "lka_candidate_query_" in sql)
    assert "deleted_at IS NULL" in matter_candidate
    assert "visibility = %(lka_param_1)s" in matter_candidate
    assert "snippet(" not in matter_candidate and "lka_snippet_" in matter_candidate

    case_id = ids["matter_live"] + "_case"
    now = "2026-10-29"
    try:
        conn.execute_native(
            "INSERT INTO matters(matter_id,title,summary,status,priority,tags,metadata,created_at,updated_at) "
            "VALUES(%s,'CaseProbeAlpha','Synthetic casefold probe','open','normal','[]','{}',%s,%s)",
            (case_id, now, now),
        )
        conn.execute_native(
            "INSERT INTO matters_fts(matter_id,title,summary,tags) VALUES(%s,'CaseProbeAlpha','Synthetic casefold probe','[]')",
            (case_id,),
        )
        conn.commit()
        native_case_rows = conn.execute_native(
            "SELECT matter_id FROM matters_fts WHERE MATCH(title,summary,tags) AGAINST(%s IN BOOLEAN MODE)",
            ('"caseprobealpha"',),
        ).fetchall()
        assert case_id in {row[0] for row in native_case_rows}
        adapted_case_rows = execute_fulltext(
            conn,
            "SELECT matter_id FROM matters_fts WHERE matters_fts MATCH ? ORDER BY rank LIMIT 20",
            ('"caseprobealpha"',),
        ).fetchall()
        assert case_id in {row[0] for row in adapted_case_rows}
    finally:
        conn.execute_native("DELETE FROM matters_fts WHERE matter_id=%s", (case_id,))
        conn.execute_native("DELETE FROM matters WHERE matter_id=%s", (case_id,))
        conn.commit()

    now, source_id = "2026-10-29", ids["source"]
    conn.execute_native(
        "INSERT INTO knowledge_sources(source_id,source_type,display_name,uri,metadata,status,sensitivity,"
        "remote_policy,access_scope,created_at,updated_at) "
        "VALUES(%s,'synthetic','Synthetic fulltext fixture',NULL,'{}','active','personal','allow','test',%s,%s)",
        (source_id, now, now),
    )
    docs = ((ids["doc_active"], "active"), (ids["doc_inactive"], "archived"))
    for document_id, status in docs:
        conn.execute_native(
            "INSERT INTO knowledge_documents(document_id,source_id,source_type,title,uri,checksum,mime_type,status,"
            "sensitivity,remote_policy,source_ref,created_at,updated_at,indexed_at,metadata) "
            "VALUES(%s,%s,'synthetic','Synthetic fixture',%s,%s,'text/plain',%s,'personal','allow',%s,%s,%s,%s,'{}')",
            (document_id, source_id, document_id, document_id, status, document_id, now, now, now),
        )
    knowledge_phrase = "渤饜鿓晕馔灆肺侢墶陥"
    for chunk_id, document_id in ((ids["chunk_active"], ids["doc_active"]),
                                  (ids["chunk_inactive"], ids["doc_inactive"])):
        conn.execute_native(
            "INSERT INTO knowledge_chunks(chunk_id,document_id,chunk_index,text,char_count,token_estimate,source_ref,"
            "sensitivity,remote_policy,metadata,created_at) VALUES(%s,%s,0,%s,%s,10,%s,'personal','allow','{}',%s)",
            (chunk_id, document_id, knowledge_phrase, len(knowledge_phrase), document_id, now),
        )
        conn.execute_native(
            "INSERT INTO knowledge_chunks_fts(chunk_id,document_id,title,text,source_type) "
            "VALUES(%s,%s,'Synthetic fixture',%s,'synthetic')",
            (chunk_id, document_id, knowledge_phrase),
        )
    conn.commit()
    knowledge_rows = execute_fulltext(
        conn,
        "SELECT c.chunk_id FROM knowledge_chunks_fts JOIN knowledge_chunks c "
        "ON c.chunk_id=knowledge_chunks_fts.chunk_id JOIN knowledge_documents d "
        "ON d.document_id=c.document_id WHERE knowledge_chunks_fts MATCH ? AND d.status='active' "
        "AND d.source_type=? ORDER BY rank,c.chunk_id LIMIT ?",
        (f'"{knowledge_phrase}"', "synthetic", 20),
    ).fetchall()
    assert [row[0] for row in knowledge_rows] == [ids["chunk_active"]]

    knowledge_statements = []

    def knowledge_factory():
        service_conn = connect_mysql(_config())
        service_conn.row_factory = sqlite3.Row
        native_execute = service_conn.execute_native
        service_conn.execute_native = lambda statement, parameters=(): (
            knowledge_statements.append((statement, parameters)), native_execute(statement, parameters),
        )[1]
        return service_conn

    knowledge_service = KnowledgeService(knowledge_factory)
    domain_knowledge = None
    try:
        domain_knowledge = knowledge_service.search(
            query=knowledge_phrase, limit=10, source_types=["synthetic"],
            source_ids=[source_id], mode="keyword",
        )
        assert [item.chunk_id for item in domain_knowledge.results] == [ids["chunk_active"]]
        knowledge_candidate = next(sql for sql, _ in knowledge_statements if "lka_candidate_query_" in sql)
        assert "d.status = 'active'" in knowledge_candidate
        assert "d.source_type IN" in knowledge_candidate
        assert "snippet(" not in knowledge_candidate and "lka_snippet_" in knowledge_candidate
    finally:
        if domain_knowledge is not None:
            conn.execute_native(
                "DELETE FROM knowledge_access_audit WHERE query_id=%s", (domain_knowledge.query_id,),
            )
            conn.commit()


def test_conversation_history_dirty_indexing_and_session_filter(fulltext_store):
    conn, ids = fulltext_store
    # Captured MySQL ngram miss: exact text is present, but native MATCH on
    # `+"复瘀刭猠龰辣凊翈骶崀"` returns no row on the rollout lab.
    phrase, replacement = "复瘀刭猠龰辣凊翈骶崀", _han_nonce()
    conn.execute_native(
        "INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) "
        "VALUES(%s,1,'user',%s,'2026-10-29',NULL)", (ids["session"], phrase),
    )
    conn.commit()
    assert conn.execute_native(
        "SELECT 1 FROM conversation_history_dirty WHERE session_id=%s AND seq=1", (ids["session"],),
    ).fetchone()
    fts_row = conn.execute_native("INSERT INTO conversation_history_fts(content) VALUES(%s)", (phrase,))
    conn.execute_native(
        "INSERT INTO conversation_history_fts_map(rowid,session_id,seq,source_hash) VALUES(%s,%s,1,%s)",
        (fts_row.lastrowid, ids["session"], "synthetic-hash"),
    )
    conn.commit()
    conn.execute_native(
        "SELECT f.rowid FROM conversation_history_fts f JOIN conversation_history_fts_map m "
        "ON m.rowid=f.rowid WHERE MATCH(f.content) AGAINST(%s IN BOOLEAN MODE) "
        "AND m.session_id=%s LIMIT 10",
        (f'"{phrase}"', ids["session"]),
    ).fetchall()
    # Native ngram may miss this captured phrase; the scoped adapter regression
    # below must still return it without weakening the session filter.
    search = ("SELECT f.rowid,m.seq FROM conversation_history_fts f "
              "JOIN conversation_history_fts_map m ON m.rowid=f.rowid "
              "WHERE conversation_history_fts MATCH ? AND m.session_id=? ORDER BY m.seq DESC LIMIT ? OFFSET ?")
    assert len(execute_fulltext(conn, search, (f'"{phrase}"', ids["session"], 10, 0)).fetchall()) == 1
    assert not execute_fulltext(conn, search, (f'"{phrase}"', ids["session"] + "-other", 10, 0)).fetchall()

    conn.execute_native("UPDATE agent_session_context_messages SET content=%s WHERE session_id=%s AND seq=1",
                        (replacement, ids["session"]))
    conn.commit()
    assert conn.execute_native(
        "SELECT 1 FROM conversation_history_dirty WHERE session_id=%s AND seq=1", (ids["session"],),
    ).fetchone()
    conn.execute_native("UPDATE conversation_history_fts SET content=%s WHERE rowid=%s",
                        (replacement, fts_row.lastrowid))
    conn.commit()
    assert not execute_fulltext(conn, search, (f'"{phrase}"', ids["session"], 10, 0)).fetchall()
    assert len(execute_fulltext(conn, search, (f'"{replacement}"', ids["session"], 10, 0)).fetchall()) == 1

    conn.execute_native("DELETE FROM agent_session_context_messages WHERE session_id=%s AND seq=1",
                        (ids["session"],))
    conn.commit()
    assert not execute_fulltext(conn, search, (f'"{replacement}"', ids["session"], 10, 0)).fetchall()
    assert conn.execute_native(
        "SELECT 1 FROM conversation_history_fts_map WHERE session_id=%s", (ids["session"],),
    ).fetchone() is None
