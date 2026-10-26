import sqlite3

from app.domains.conversation_semantic import ConversationSemanticIndex


class Provider:
    model_info = type("Info", (), {"index_key": "test-local-v1"})()

    def __init__(self):
        self.passages = 0
        self.queries = 0

    @staticmethod
    def vector(text):
        value = text.lower()
        if any(word in value for word in ("automobile", "car", "vehicle")):
            return [1.0, 0.0, 0.0]
        if any(word in value for word in ("tea", "drink", "beverage")):
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    def embed_passages(self, passages):
        self.passages += len(passages)
        return [self.vector(text) for text in passages]

    def embed_query(self, query):
        self.queries += 1
        return self.vector(query)


def setup_db(tmp_path):
    path = tmp_path / "semantic.sqlite"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    with connect() as conn:
        conn.executescript("""
            CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY, kind TEXT, status TEXT);
            CREATE TABLE agent_session_context_messages(
              session_id TEXT, seq INTEGER, role TEXT, content TEXT,
              created_at TEXT, trace_id TEXT, PRIMARY KEY(session_id,seq));
            INSERT INTO agent_sessions VALUES('s','ordinary','active');
            INSERT INTO agent_session_context_messages VALUES
              ('s',1,'user','I drove an automobile yesterday','t1','turn1'),
              ('s',2,'agent','You drove a car yesterday','t2','turn1'),
              ('s',3,'user','I like tea','t3','turn2'),
              ('s',4,'agent','Tea is a beverage','t4','turn2'),
              ('s',5,'agent','tool unrelated output','t5','turn3'),
              ('s',6,'tool','tool payload','t6','turn3');
        """)
    return connect


def test_incremental_search_cache_and_eligibility(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    index = ConversationSemanticIndex(connect, provider)
    status = index.sync_batch("s")
    assert status["state"] == "ready"
    assert status["indexed_count"] == 4
    hits = index.search("s", "vehicle")
    assert hits["messages"][0]["seq"] in {1, 2}
    assert index.search("s", "vehicle")["messages"]
    assert provider.passages == 4
    assert provider.queries == 1

    with connect() as conn:
        conn.execute("UPDATE agent_session_context_messages SET content='I prefer a beverage' WHERE session_id='s' AND seq=3")
    index.sync_batch("s")
    assert provider.passages == 5
    with connect() as conn:
        conn.execute("DELETE FROM agent_session_context_messages WHERE session_id='s' AND seq=1")
    assert all(row["seq"] != 1 for row in index.search("s", "vehicle")["messages"])


def test_embedding_publication_revalidates_concurrent_edit(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    original = provider.embed_passages

    def edit_during_embedding(passages):
        vectors = original(passages)
        with connect() as conn:
            conn.execute("UPDATE agent_session_context_messages SET content='changed to tea' WHERE session_id='s' AND seq=1")
        return vectors

    provider.embed_passages = edit_during_embedding
    index = ConversationSemanticIndex(connect, provider)
    result = index.sync_batch("s")
    assert result["state"] == "warming"
    assert all(hit["seq"] != 1 for hit in index.search("s", "vehicle")["messages"])
    provider.embed_passages = original
    index.sync_batch("s")
    assert provider.passages == 5


def test_model_fingerprint_and_candidate_cap(tmp_path):
    connect = setup_db(tmp_path)
    first = Provider()
    index = ConversationSemanticIndex(connect, first, model_revision="a", max_candidates=3)
    status = index.sync_batch("s")
    assert status["coverage_capped"]
    assert status["complete"] is False
    assert status["indexed_count"] == 3
    second = Provider()
    other = ConversationSemanticIndex(connect, second, model_revision="b", max_candidates=3)
    assert other.search("s", "vehicle")["messages"] == []
    other.sync_batch("s")
    assert second.passages == 3


def test_eligibility_change_removes_related_agent_vector(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    index = ConversationSemanticIndex(connect, provider)
    index.sync_batch("s")
    with connect() as conn:
        conn.execute("INSERT INTO agent_session_context_messages VALUES('s',7,'tool','tool','now','turn1')")
    status = index.sync_batch("s")
    assert status["state"] == "ready"
    assert all(hit["seq"] != 2 for hit in index.search("s", "vehicle")["messages"])


def test_cold_scan_watermark_progresses_across_ineligible_rows(tmp_path):
    connect = setup_db(tmp_path)
    with connect() as conn:
        for seq in range(7, 90):
            conn.execute("INSERT INTO agent_session_context_messages VALUES(?,?,?,?,?,?)",
                         ("s", seq, "tool", "noise", "now", None))
    provider = Provider()
    index = ConversationSemanticIndex(connect, provider, batch_size=5)
    statuses = [index.sync_batch("s") for _ in range(20)]
    assert statuses[-1]["watermark"] >= 89
    assert not statuses[-1]["pending"]
    assert provider.passages == 4


def test_query_cache_is_exact_bounded_and_does_not_reembed_passages(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    index = ConversationSemanticIndex(connect, provider)
    index.sync_batch("s")
    index.search("s", "vehicle")
    index.search("s", "vehicle")
    index.search("s", "vehicle ")
    assert provider.queries == 2
    assert provider.passages == 4
    assert len(index._query_cache) == 2


def test_dimension_change_and_provider_failure_degrade_cleanly(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    index = ConversationSemanticIndex(connect, provider)
    index.sync_batch("s")
    provider.embed_query = lambda query: [1.0, 0.0]
    assert index.search("s", "vehicle")["status"]["unavailable"]

    failing = Provider()
    failing.embed_passages = lambda passages: (_ for _ in ()).throw(RuntimeError("private detail"))
    failed = ConversationSemanticIndex(connect, failing, model_revision="failed")
    status = failed.sync_batch("s")
    assert status["unavailable"]
    assert "private detail" not in repr(status)


def test_delete_clears_vector_and_invalidates_matrix(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    index = ConversationSemanticIndex(connect, provider)
    index.sync_batch("s")
    index.search("s", "vehicle")
    old_revision = index._matrix_cache["s"][1]
    with connect() as conn:
        conn.execute("DELETE FROM agent_session_context_messages WHERE session_id='s' AND seq=1")
    index.sync_batch("s")
    assert index._matrix_cache.get("s") is None
    assert all(hit["seq"] != 1 for hit in index.search("s", "vehicle")["messages"])
    assert old_revision >= 0


def test_matrix_lru_is_bounded_by_session_count(tmp_path):
    connect = setup_db(tmp_path)
    with connect() as conn:
        conn.execute("INSERT INTO agent_sessions VALUES('s2','ordinary','active')")
        conn.execute("INSERT INTO agent_session_context_messages VALUES('s2',1,'user','car is useful','now','x')")
    provider = Provider()
    index = ConversationSemanticIndex(connect, provider, matrix_sessions=1)
    index.sync_batch("s")
    index.search("s", "vehicle")
    index.sync_batch("s2")
    index.search("s2", "vehicle")
    assert list(index._matrix_cache) == ["s2"]


def test_model_fingerprint_dirty_queues_are_independent(tmp_path):
    connect = setup_db(tmp_path)
    one = Provider()
    first = ConversationSemanticIndex(connect, one, model_revision="one")
    first.sync_batch("s")
    two = Provider()
    second = ConversationSemanticIndex(connect, two, model_revision="two")
    second.sync_batch("s")
    with connect() as conn:
        conn.execute("UPDATE agent_session_context_messages SET content='a vehicle update' WHERE session_id='s' AND seq=1")
    first.sync_batch("s")
    before_sync = second.search("s", "vehicle")
    assert all(hit["seq"] != 1 for hit in before_sync["messages"])
    assert before_sync["status"]["state"] == "warming"
    second.sync_batch("s")
    assert two.passages == 5


def test_failed_cold_batch_stays_queued_and_recovers(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    original = provider.embed_passages
    calls = 0

    def fail_once(passages):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary local model failure")
        return original(passages)

    provider.embed_passages = fail_once
    index = ConversationSemanticIndex(connect, provider, batch_size=8)
    failed = index.sync_batch("s")
    assert failed["unavailable"]
    assert failed["pending"]
    provider.embed_passages = original
    recovered = index.sync_batch("s")
    assert recovered["indexed_count"] == 4
    assert not recovered["pending"]
    assert provider.passages == 4


def test_cold_rows_survive_dirty_backlog_and_drain(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    index = ConversationSemanticIndex(connect, provider, batch_size=2)
    index.sync_batch("s")
    with connect() as conn:
        conn.executemany(
            "UPDATE agent_session_context_messages SET content=? WHERE session_id='s' AND seq=?",
            [(f"edited passage {seq}", seq) for seq in (1, 2, 3, 4)],
        )
        for seq in range(7, 27):
            conn.execute("INSERT INTO agent_session_context_messages VALUES(?,?,?,?,?,?)",
                         ("s", seq, "user", f"new tea passage {seq}", "now", f"new-{seq}"))
    statuses = []
    for _ in range(40):
        status = index.sync_batch("s")
        statuses.append(status)
        if not status["pending"]:
            break
    assert statuses[-1]["watermark"] == 26
    assert not statuses[-1]["pending"]
    assert statuses[-1]["indexed_count"] == 24


def test_declared_model_dimensions_reject_mismatched_passages(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    provider.model_info = type("Info", (), {"index_key": "declared-4d", "dimensions": 4})()
    index = ConversationSemanticIndex(connect, provider)
    status = index.sync_batch("s")
    assert status["unavailable"]
    assert status["pending"]
    assert status["indexed_count"] == 0
    assert status["watermark"] == 6


def test_query_time_source_edit_is_fenced_after_embedding(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    index = ConversationSemanticIndex(connect, provider)
    index.sync_batch("s")
    original = provider.embed_query

    def edit_during_query(query):
        vector = original(query)
        with connect() as conn:
            conn.execute("UPDATE agent_session_context_messages SET content='changed to tea' WHERE session_id='s' AND seq=1")
        return vector

    provider.embed_query = edit_during_query
    result = index.search("s", "vehicle")
    assert result["messages"] == []
    assert result["status"]["state"] == "warming"
    assert result["status"]["pending"]


def test_matrix_storage_is_readonly_and_cache_respects_byte_budget(tmp_path):
    connect = setup_db(tmp_path)
    provider = Provider()
    index = ConversationSemanticIndex(connect, provider, max_matrix_cache_bytes=1)
    index.sync_batch("s")
    index.search("s", "vehicle")
    assert not index._matrix_cache

    cached = ConversationSemanticIndex(connect, provider)
    cached.search("s", "vehicle")
    matrix = cached._matrix_cache["s"][3]
    if hasattr(matrix, "flags"):
        assert not matrix.flags.writeable
        assert matrix.flags.c_contiguous
        assert matrix.nbytes <= cached._max_matrix_cache_bytes
