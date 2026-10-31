from __future__ import annotations

import pytest

from app.storage.mysql_fulltext import (
    _SOURCE_TRIGGER_SQL,
    MySQLFullTextError,
    backfill_fulltext_indexes,
    bootstrap_fulltext,
    execute_fulltext,
)

pytest.importorskip("sqlglot")

class _Cursor:
    def __init__(self, row=None, rows=(), *, rowcount=None):
        self.row = row
        self.rows = list(rows)
        self.rowcount = rowcount
        self.closed = False

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class _Connection:
    dialect = "mysql"

    def __init__(self):
        self.in_transaction = False
        self._schema = None
        self.statements = []

    def execute_native(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        if "INFORMATION_SCHEMA" in statement:
            return _Cursor()
        if statement.startswith("SELECT matter_id,title,summary,tags"):
            return _Cursor(rows=[])
        return _Cursor()

    def executemany(self, statement, parameters):
        self.statements.append((statement, list(parameters)))
        return _Cursor()

    def commit(self):
        self.statements.append(("COMMIT", ()))


def test_mail_match_snippet_and_rank_translate_to_indexed_mysql_query():
    conn = _Connection()
    cursor = execute_fulltext(
        conn,
        """SELECT m.message_id, snippet(mail_messages_fts,4,'[',']','...',12) AS snippet
        FROM mail_messages_fts JOIN mail_messages m ON m.message_id=mail_messages_fts.message_id
        WHERE mail_messages_fts MATCH ? ORDER BY rank LIMIT ?""",
        ('"alpha" OR "beta"', 9),
    )
    assert cursor is not None
    sql, params = conn.statements[-1]
    assert "MATCH(mail_messages_fts.subject, mail_messages_fts.sender, mail_messages_fts.folder, mail_messages_fts.body_text)" in sql
    assert "AGAINST(%(lka_param_0)s IN BOOLEAN MODE)" in sql
    assert "LEFT(mail_messages_fts.body_text, 960)" in sql
    assert "[" in sql and "]" in sql
    assert "ORDER BY" in sql and "AGAINST(%(lka_param_0)s IN BOOLEAN MODE) DESC" in sql
    assert params == {
        "lka_param_0": '"alpha" "beta"', "lka_param_1": 9,
        "lka_snippet_2": "alpha",
    }


def test_conversation_match_rewrites_named_qmarks_once_and_keeps_rowid_join():
    conn = _Connection()
    sql = """SELECT f.rowid,m.seq,m.source_hash,f.content
    FROM conversation_history_fts f JOIN conversation_history_fts_map m ON m.rowid=f.rowid
    WHERE conversation_history_fts MATCH ? AND m.session_id=?
    ORDER BY m.seq DESC LIMIT ? OFFSET ?"""
    execute_fulltext(conn, sql, ('"quoted phrase"', "session-1", 8, 4))
    translated, params = conn.statements[-1]
    assert "MATCH(f.content) AGAINST(%(lka_param_0)s IN BOOLEAN MODE)" in translated
    assert "f.rowid" in translated and "m.rowid = f.rowid" in translated
    assert params == {
        "lka_param_0": '+"quoted phrase"', "lka_param_1": "session-1",
        "lka_param_2": 8, "lka_param_3": 4,
    }


class _ConversationFallbackConnection(_Connection):
    def __init__(self, *, session_rows=(), candidate_rowcount=0, probe_rowcount=None):
        super().__init__()
        self.session_rows = list(session_rows)
        self.candidate_rowcount = candidate_rowcount
        self.probe_rowcount = probe_rowcount
        self.native_cursor = _Cursor(rowcount=0)

    def execute_native(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        if "LIMIT 1 OFFSET 0" in statement:
            return _Cursor(rowcount=self.probe_rowcount)
        if statement.startswith("SELECT seq FROM conversation_history_fts_map"):
            return _Cursor(rows=self.session_rows)
        if "INSTR(" in statement and "AGAINST(" in statement:
            return _Cursor(rowcount=self.candidate_rowcount)
        if "INSTR(" in statement:
            return _Cursor(rowcount=1)
        return self.native_cursor


def test_conversation_zero_hit_fallback_preserves_scope_and_page():
    conn = _ConversationFallbackConnection(session_rows=[(3,), (2,), (1,)])
    sql = """SELECT f.rowid,m.seq,m.source_hash,f.content
    FROM conversation_history_fts f JOIN conversation_history_fts_map m ON m.rowid=f.rowid
    JOIN documents d ON d.document_id=m.session_id
    WHERE conversation_history_fts MATCH ? AND m.session_id=? AND d.visibility='public'
    ORDER BY m.seq DESC LIMIT ? OFFSET ?"""
    cursor = execute_fulltext(conn, sql, ('"alpha" OR "beta"', "session-1", 7, 0))

    assert cursor is not conn.native_cursor
    assert conn.native_cursor.closed
    assert len(conn.statements) == 3
    precheck, precheck_params = conn.statements[1]
    assert "conversation_history_fts_map" in precheck and "LIMIT %s" in precheck
    assert precheck_params == ("session-1", 5001)
    fallback, params = conn.statements[2]
    assert "INSTR(LOWER(f.content), LOWER(%(lka_backstop_" in fallback
    assert "m.session_id = %(lka_param_1)s" in fallback
    assert "d.visibility = 'public'" in fallback
    assert "ORDER BY m.seq DESC LIMIT %(lka_param_2)s OFFSET %(lka_param_3)s" in fallback
    assert params["lka_param_1"] == "session-1"
    assert params["lka_param_2"] == 7 and params["lka_param_3"] == 0
    assert params["lka_backstop_4_0"] == "alpha"
    assert params["lka_backstop_5_1"] == "beta"
    assert " OR " in fallback


def test_conversation_candidate_uses_native_tokens_and_exact_literal():
    conn = _ConversationFallbackConnection(candidate_rowcount=1)
    sql = ("SELECT f.rowid,m.seq FROM conversation_history_fts f "
           "JOIN conversation_history_fts_map m ON m.rowid=f.rowid "
           "WHERE conversation_history_fts MATCH ? AND m.session_id=? "
           "ORDER BY m.seq DESC LIMIT ? OFFSET ?")
    cursor = execute_fulltext(conn, sql, ('"CaseProbeAlpha"', "session-1", 8, 0))
    candidate, params = conn.statements[1]
    assert cursor is not conn.native_cursor
    assert "AGAINST(%(lka_candidate_query_4)s IN BOOLEAN MODE)" in candidate
    assert "INSTR(LOWER(f.content), LOWER(%(lka_candidate_exact_5)s))" in candidate
    assert params["lka_param_0"] == '+"CaseProbeAlpha"'
    assert params["lka_candidate_query_4"] == "CaseProbeAlpha"
    assert params["lka_candidate_exact_5"] == "CaseProbeAlpha"
    assert "STRAIGHT_JOIN conversation_history_fts_map" in candidate
    assert "m.session_id = %(lka_param_1)s" in candidate
    assert "LIMIT %(lka_param_2)s OFFSET %(lka_param_3)s" in candidate
    assert len(conn.statements) == 2


class _NativeCandidateConnection(_Connection):
    def __init__(self, *, candidate_rowcount=0, probe_rowcount=0):
        super().__init__()
        self.native_cursor = _Cursor(rowcount=0)
        self.candidate_rowcount = candidate_rowcount
        self.probe_rowcount = probe_rowcount

    def execute_native(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        if "LIMIT 1 OFFSET 0" in statement:
            return _Cursor(rowcount=self.probe_rowcount)
        if "INSTR(" in statement:
            return _Cursor(rowcount=self.candidate_rowcount)
        return self.native_cursor


@pytest.mark.parametrize(
    ("table", "fields", "sql"),
    [
        (
            "mail_messages_fts",
            ("subject", "sender", "folder", "body_text"),
            ("SELECT f.message_id,m.account_id,snippet(mail_messages_fts,4,'[',']','...',12) AS snippet "
             "FROM mail_messages_fts f "
             "JOIN mail_messages m ON m.message_id=f.message_id "
             "WHERE mail_messages_fts MATCH ? AND m.account_id=? AND m.visibility=? "
             "ORDER BY rank LIMIT ? OFFSET ?"),
        ),
        (
            "matters_fts",
            ("title", "summary", "tags"),
            ("SELECT f.matter_id,m.visibility,snippet(matters_fts,2,'[',']','...',12) AS snippet "
             "FROM matters_fts f "
             "JOIN matters m ON m.matter_id=f.matter_id "
             "WHERE matters_fts MATCH ? AND m.visibility=? ORDER BY rank LIMIT ? OFFSET ?"),
        ),
        (
            "knowledge_chunks_fts",
            ("title", "text", "source_type"),
            ("SELECT f.chunk_id,d.status FROM knowledge_chunks_fts f "
             "JOIN knowledge_chunks c ON c.chunk_id=f.chunk_id "
             "JOIN knowledge_documents d ON d.document_id=c.document_id "
             "WHERE knowledge_chunks_fts MATCH ? AND d.status='active' AND d.account_id=? "
             "ORDER BY rank LIMIT ? OFFSET ?"),
        ),
    ],
)
def test_nonconversation_zero_hit_uses_indexed_candidate_and_preserves_scope(table, fields, sql):
    conn = _NativeCandidateConnection(candidate_rowcount=1)
    if table == "mail_messages_fts":
        params = ('"CaseProbeAlpha"', "account-1", "private", 20, 0)
    elif table == "matters_fts":
        params = ('"CaseProbeAlpha"', "private", 20, 0)
    else:
        params = ('"CaseProbeAlpha"', "account-1", 20, 0)
    cursor = execute_fulltext(conn, sql, params)
    candidate, binds = conn.statements[1]

    assert cursor is not conn.native_cursor
    assert "STRAIGHT_JOIN" in candidate
    candidate_index = {"mail_messages_fts": 6, "matters_fts": 5, "knowledge_chunks_fts": 4}[table]
    assert "INSTR(LOWER(" in candidate
    assert "AND (INSTR(" in candidate
    assert ")) AND " in candidate
    assert f"AGAINST(%(lka_candidate_query_{candidate_index})s IN BOOLEAN MODE)" in candidate
    assert "AGAINST(%(lka_param_0)s IN BOOLEAN MODE) DESC" in candidate
    assert all(f"LOWER(f.{field})" in candidate for field in fields)
    assert "LIMIT " in candidate and "OFFSET " in candidate
    if table == "mail_messages_fts":
        assert "m.account_id = %(lka_param_1)s" in candidate
        assert "m.visibility = %(lka_param_2)s" in candidate
        assert "CaseProbeAlpha" == binds["lka_candidate_exact_7"]
        assert "ELSE LEFT(f.body_text, 960) END AS snippet" in candidate
        assert binds["lka_snippet_5"] == "CaseProbeAlpha"
    elif table == "matters_fts":
        assert "m.visibility = %(lka_param_1)s" in candidate
        assert binds["lka_candidate_exact_6"] == "CaseProbeAlpha"
        assert "ELSE LEFT(f.summary, 960) END AS snippet" in candidate
        assert binds["lka_snippet_4"] == "CaseProbeAlpha"
    else:
        assert "d.status = 'active'" in candidate
        assert "d.account_id = %(lka_param_1)s" in candidate
        assert binds["lka_candidate_exact_5"] == "CaseProbeAlpha"


def test_nonconversation_zero_candidate_stays_empty_without_literal_table_scan():
    conn = _NativeCandidateConnection(candidate_rowcount=0)
    sql = ("SELECT f.message_id FROM mail_messages_fts f "
           "JOIN mail_messages m ON m.message_id=f.message_id "
           "WHERE mail_messages_fts MATCH ? AND m.account_id=? ORDER BY rank LIMIT ? OFFSET ?")
    cursor = execute_fulltext(conn, sql, ('"thereisnosuchsyntheticphrase"', "account-1", 20, 0))
    assert cursor.rowcount == 0
    assert len(conn.statements) == 2
    assert "AGAINST(" in conn.statements[1][0] and "INSTR(" in conn.statements[1][0]


def test_nonconversation_multi_phrase_and_offset_do_not_expand_candidate():
    sql = ("SELECT f.message_id FROM mail_messages_fts f WHERE mail_messages_fts MATCH ? "
           "ORDER BY rank LIMIT ? OFFSET ?")
    conn = _NativeCandidateConnection(candidate_rowcount=1)
    cursor = execute_fulltext(conn, sql, ('"alpha" "beta"', 20, 0))
    assert cursor is conn.native_cursor
    assert len(conn.statements) == 1


def test_offset_recovery_probes_first_native_page_then_keeps_requested_page():
    conn = _NativeCandidateConnection(candidate_rowcount=1, probe_rowcount=0)
    sql = ("SELECT f.message_id FROM mail_messages_fts f "
           "JOIN mail_messages m ON m.message_id=f.message_id "
           "WHERE mail_messages_fts MATCH ? AND m.account_id=? ORDER BY rank LIMIT ? OFFSET ?")
    cursor = execute_fulltext(conn, sql, ('"RareFixedProbe"', "account-1", 1, 1))
    assert cursor is not conn.native_cursor
    assert len(conn.statements) == 3
    probe, probe_params = conn.statements[1]
    candidate, candidate_params = conn.statements[2]
    assert "LIMIT 1 OFFSET 0" in probe
    assert probe_params["lka_param_1"] == "account-1"
    assert "LIMIT %(lka_param_2)s OFFSET %(lka_param_3)s" in candidate
    assert candidate_params["lka_param_2"] == 1 and candidate_params["lka_param_3"] == 1


def test_offset_native_page_end_does_not_run_candidate_fallback():
    conn = _NativeCandidateConnection(candidate_rowcount=1, probe_rowcount=1)
    sql = "SELECT mail_messages_fts.message_id FROM mail_messages_fts WHERE mail_messages_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?"
    cursor = execute_fulltext(conn, sql, ('"ExistingNativeResult"', 1, 1))
    assert cursor is conn.native_cursor
    assert len(conn.statements) == 2
    assert "LIMIT 1 OFFSET 0" in conn.statements[1][0]
    assert "INSTR(" not in conn.statements[1][0]


def test_conversation_literal_fallback_preserves_and_semantics():
    conn = _ConversationFallbackConnection()
    sql = ("SELECT f.rowid,m.seq FROM conversation_history_fts f "
           "JOIN conversation_history_fts_map m ON m.rowid=f.rowid "
           "WHERE conversation_history_fts MATCH ? AND m.session_id=? "
           "ORDER BY m.seq DESC LIMIT ? OFFSET ?")
    execute_fulltext(conn, sql, ('"alpha" "beta"', "session-1", 8, 0))
    fallback = conn.statements[-1][0]
    assert " AND " in fallback
    assert " OR " not in fallback


def test_conversation_fallback_requires_bound_session_scope():
    conn = _ConversationFallbackConnection()
    sql = ("SELECT f.rowid,m.seq FROM conversation_history_fts f "
           "JOIN conversation_history_fts_map m ON m.rowid=f.rowid "
           "WHERE conversation_history_fts MATCH ? ORDER BY m.seq DESC LIMIT ?")
    cursor = execute_fulltext(conn, sql, ('"alpha"', 5))
    assert cursor is conn.native_cursor
    assert len(conn.statements) == 1
    assert "INSTR(" not in conn.statements[0][0]


def test_conversation_fallback_fails_closed_over_session_candidate_bound():
    conn = _ConversationFallbackConnection(session_rows=[(index,) for index in range(5001)])
    sql = ("SELECT f.rowid,m.seq FROM conversation_history_fts f "
           "JOIN conversation_history_fts_map m ON m.rowid=f.rowid "
           "WHERE conversation_history_fts MATCH ? AND m.session_id=? "
           "ORDER BY m.seq DESC LIMIT ? OFFSET ?")
    with pytest.raises(MySQLFullTextError, match="5000-row session bound"):
        execute_fulltext(conn, sql, ('"alpha"', "session-1", 8, 0))
    assert len(conn.statements) == 3
    assert "INSTR(" in conn.statements[1][0] and "AGAINST(" in conn.statements[1][0]
    assert conn.statements[2][0].startswith("SELECT seq FROM conversation_history_fts_map")


def test_conversation_empty_page_after_offset_does_not_trigger_fallback():
    conn = _ConversationFallbackConnection(probe_rowcount=1)
    sql = ("SELECT f.rowid,m.seq FROM conversation_history_fts f "
           "JOIN conversation_history_fts_map m ON m.rowid=f.rowid "
           "WHERE conversation_history_fts MATCH ? AND m.session_id=? "
           "ORDER BY m.seq DESC LIMIT ? OFFSET ?")
    cursor = execute_fulltext(conn, sql, ('"alpha"', "session-1", 8, 2))
    assert cursor is conn.native_cursor
    assert len(conn.statements) == 2
    assert "LIMIT 1 OFFSET 0" in conn.statements[1][0]


def test_conversation_rare_offset_page_uses_candidate_and_keeps_requested_page():
    conn = _ConversationFallbackConnection(candidate_rowcount=1, probe_rowcount=0)
    sql = ("SELECT f.rowid,m.seq FROM conversation_history_fts f "
           "JOIN conversation_history_fts_map m ON m.rowid=f.rowid "
           "WHERE conversation_history_fts MATCH ? AND m.session_id=? "
           "ORDER BY m.seq DESC LIMIT ? OFFSET ?")
    cursor = execute_fulltext(conn, sql, ('"RareFixedProbe"', "session-1", 1, 1))
    assert cursor is not conn.native_cursor
    assert len(conn.statements) == 3
    candidate = conn.statements[2][0]
    assert "LIMIT %(lka_param_2)s OFFSET %(lka_param_3)s" in candidate
    assert "m.session_id = %(lka_param_1)s" in candidate


def test_memory_match_and_non_fts_queries():
    conn = _Connection()
    assert execute_fulltext(conn, "SELECT id FROM normal_table WHERE id=?", (1,)) is None
    execute_fulltext(
        conn,
        "SELECT memory_id FROM memory_entries_fts WHERE memory_entries_fts MATCH ? LIMIT 200",
        ('"memory" "context"',),
    )
    assert "MATCH(memory_entries_fts.content, memory_entries_fts.memory_type)" in conn.statements[-1][0]
    assert "OR ((INSTR(memory_entries_fts.content, %(lka_backstop_1_0)s) > 0" in conn.statements[-1][0]
    assert "AND (INSTR(memory_entries_fts.content, %(lka_backstop_2_1)s) > 0" in conn.statements[-1][0]
    assert conn.statements[-1][1] == {
        "lka_param_0": '+"memory" +"context"',
        "lka_backstop_1_0": "memory",
        "lka_backstop_2_1": "context",
    }


def test_memory_literal_backstop_preserves_outer_scope_predicates_and_or_semantics():
    conn = _Connection()
    execute_fulltext(
        conn,
        "SELECT e.memory_id FROM memory_entries_fts f JOIN memory_entries e "
        "ON e.memory_id=f.memory_id WHERE memory_entries_fts MATCH ? "
        "AND e.scope=? AND e.status='active' ORDER BY rank LIMIT ?",
        ('"memory" OR "context"', "global", 10),
    )
    sql, params = conn.statements[-1]
    assert "e.scope = %(lka_param_1)s" in sql
    assert "e.status = 'active'" in sql
    assert "OR ((INSTR(f.content, %(lka_backstop_3_0)s) > 0" in sql
    assert "OR (INSTR(f.content, %(lka_backstop_4_1)s) > 0" in sql
    assert params["lka_backstop_3_0"] == "memory"
    assert params["lka_backstop_4_1"] == "context"
    assert params["lka_param_1"] == "global"


def test_memory_malformed_or_empty_query_does_not_enable_literal_backstop():
    conn = _Connection()
    execute_fulltext(
        conn,
        "SELECT memory_id FROM memory_entries_fts WHERE memory_entries_fts MATCH ?",
        ('""',),
    )
    sql, params = conn.statements[-1]
    assert "INSTR(" not in sql
    assert params["lka_param_0"] == '+""'


def test_knowledge_and_matters_search_queries_resolve_snippet_columns():
    conn = _Connection()
    execute_fulltext(
        conn,
        """SELECT c.chunk_id,snippet(knowledge_chunks_fts,3,'[',']','...',18) AS snippet
        FROM knowledge_chunks_fts JOIN knowledge_chunks c ON c.chunk_id=knowledge_chunks_fts.chunk_id
        JOIN knowledge_documents d ON d.document_id=c.document_id
        WHERE knowledge_chunks_fts MATCH ? AND d.status='active' AND d.source_type=?
        ORDER BY rank,c.chunk_id LIMIT ?""",
        ('"token budget"', "mail", 32),
    )
    knowledge_sql, knowledge_params = conn.statements[-1]
    assert "LEFT(knowledge_chunks_fts.text, 1440)" in knowledge_sql
    assert "MATCH(knowledge_chunks_fts.title, knowledge_chunks_fts.text, knowledge_chunks_fts.source_type)" in knowledge_sql
    assert knowledge_params == {
        "lka_param_0": '+"token budget"', "lka_param_1": "mail", "lka_param_2": 32,
        "lka_snippet_3": "token budget",
    }

    execute_fulltext(
        conn,
        """SELECT m.matter_id,snippet(matters_fts,2,'[',']','...',12) AS snippet
        FROM matters_fts JOIN matters m ON m.matter_id=matters_fts.matter_id
        WHERE matters_fts MATCH ? AND m.deleted_at IS NULL
        ORDER BY rank,m.updated_at DESC,m.matter_id LIMIT ?""",
        ('"release"', 10),
    )
    matters_sql, matters_params = conn.statements[-1]
    assert "LEFT(matters_fts.summary, 960)" in matters_sql
    assert "MATCH(matters_fts.title, matters_fts.summary, matters_fts.tags)" in matters_sql
    assert matters_params == {
        "lka_param_0": '+"release"', "lka_param_1": 10,
        "lka_snippet_2": "release",
    }


def test_unsupported_match_source_or_snippet_shape_fails_closed():
    conn = _Connection()
    with pytest.raises(MySQLFullTextError, match="unsupported FTS"):
        execute_fulltext(conn, "SELECT * FROM unknown_fts WHERE unknown_fts MATCH ?", ("word",))
    with pytest.raises(MySQLFullTextError, match="column index"):
        execute_fulltext(
            conn,
            "SELECT snippet(matters_fts,99,'[',']','...',12) FROM matters_fts WHERE matters_fts MATCH ?",
            ('"title"',),
        )


def test_bootstrap_creates_native_tables_indexes_triggers_and_backfill_calls():
    conn = _Connection()
    bootstrap_fulltext(conn)
    sql = "\n".join(statement for statement, _ in conn.statements)
    assert "CREATE TABLE IF NOT EXISTS conversation_history_fts" in sql
    assert sql.count("CREATE FULLTEXT INDEX") == 5
    assert "WITH PARSER ngram" in sql
    assert "CREATE TRIGGER memory_fts_insert" in sql
    assert "CREATE TRIGGER conversation_history_source_delete" in sql
    assert "SELECT matter_id,title,summary,tags FROM matters" not in sql


def test_bootstrap_records_source_metadata_for_external_fts_triggers():
    class _Schema:
        def __init__(self):
            self.rowids = []
            self.objects = []

        def remember_rowid(self, _conn, table, column, *, proxy_kind):
            self.rowids.append((table, column, proxy_kind))

        def remember_object(self, _conn, **kwargs):
            self.objects.append(kwargs)

        def rowid_column(self, _conn, table):
            return "rowid" if table.endswith("_fts") else None

    conn = _Connection()
    conn._rowid_column = None
    conn.set_rowid_resolver = lambda resolver: setattr(conn, "_rowid_column", resolver)
    conn._schema = _Schema()
    bootstrap_fulltext(conn)
    assert len(conn._schema.rowids) == 5
    assert {entry[0] for entry in conn._schema.rowids} == {
        "mail_messages_fts", "matters_fts", "knowledge_chunks_fts",
        "memory_entries_fts", "conversation_history_fts",
    }
    triggers = [item for item in conn._schema.objects if item["object_type"] == "external_trigger"]
    assert len(triggers) == 6
    assert {item["object_name"] for item in triggers} == {
        "memory_fts_insert", "memory_fts_update", "memory_fts_delete",
        "conversation_history_source_insert", "conversation_history_source_update",
        "conversation_history_source_delete",
    }


def test_verify_mode_is_read_only_and_fails_closed_for_incomplete_indexes():
    class _Schema:
        def table_exists(self, _conn, _table):
            return True

        def rowid_column(self, _conn, _table):
            return "rowid"

        def master_objects(self, _conn, *, object_type, names):
            source = {
                name: sql for name, _, sql in _SOURCE_TRIGGER_SQL
            }
            return [(name, source[name]) for name in names]

        @staticmethod
        def _normalize_probe_sql(statement):
            from app.storage.mysql_schema import MySQLSchema

            return MySQLSchema._normalize_probe_sql(statement)

    class _VerifyConn(_Connection):
        def execute_native(self, statement, parameters=()):
            self.statements.append((statement, parameters))
            if "INDEX_TYPE" in statement:
                return _Cursor(row=("FULLTEXT",))
            if "INFORMATION_SCHEMA.TRIGGERS" in statement:
                return _Cursor(row=(1,))
            return _Cursor()

    conn = _VerifyConn()
    conn._schema = _Schema()
    bootstrap_fulltext(conn, install=False)
    assert not any(statement.lstrip().upper().startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE"))
                   for statement, _ in conn.statements)

    conn = _VerifyConn()
    conn._schema = _Schema()
    conn.execute_native = lambda statement, parameters=(): (
        conn.statements.append((statement, parameters)) or
        _Cursor(row=("BTREE",) if "INDEX_TYPE" in statement else (1,))
    )
    with pytest.raises(MySQLFullTextError, match="migration is incomplete"):
        bootstrap_fulltext(conn, install=False)


def test_explicit_backfill_reads_existing_base_rows_only_when_requested():
    conn = _Connection()
    backfill_fulltext_indexes(conn)
    sql = "\n".join(statement for statement, _ in conn.statements)
    assert "INSERT INTO knowledge_chunks_fts" in sql
    assert "SELECT matter_id,title,summary,tags FROM matters" in sql
