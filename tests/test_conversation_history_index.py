"""Local semantic cache scheduling and its source-delivery boundary."""

from types import SimpleNamespace

from app.core.background_jobs import BackgroundJobStore, BackgroundJobWorker
from app.core.conversation_history_index import ConversationHistoryIndexer
from app.core.sessions import SessionService
from app.core.tools import ToolContext
from app.domains.conversation_history import ConversationHistoryService
from app.domains.conversation_semantic import ConversationSemanticIndex
from app.storage.db import connect, init_db


class LocalProvider:
    model_info = SimpleNamespace(index_key="test:local:2", dimensions=2)

    def __init__(self):
        self.passages = 0

    def embed_passages(self, texts):
        self.passages += len(texts)
        return [[1.0, 0.0] if "automobile" in text else [0.0, 1.0] for text in texts]

    def embed_query(self, query):
        return [1.0, 0.0]


def setup_index(tmp_path):
    db = tmp_path / "history.sqlite3"
    init_db(db)
    factory = lambda: connect(db)
    sessions = SessionService(factory)
    sid = sessions.create_session(title="history").session.session_id
    sessions.record_context_exchange(session_id=sid, user_input="I bought an automobile.",
                                    agent_answer="Enjoy the trip.", trace_id="one")
    sessions.record_context_exchange(session_id=sid, user_input="I drank tea.",
                                    agent_answer="Have a nice day.", trace_id="two")
    store = BackgroundJobStore(db)
    store.ensure_schema()
    provider = LocalProvider()
    index = ConversationSemanticIndex(factory, provider, batch_size=1)
    indexer = ConversationHistoryIndexer(index=index, store=store, conn_factory=factory)
    history = ConversationHistoryService(factory, semantic_index=indexer)
    worker = BackgroundJobWorker(store=store, handlers={indexer.kind: indexer.handle})
    return history, indexer, worker, provider, ToolContext(session_id=sid), factory


def test_cold_search_schedules_bounded_jobs_then_serves_semantic_source(tmp_path):
    history, indexer, worker, provider, context, _ = setup_index(tmp_path)
    cold = history.search(context, query="vehicle", mode="hybrid")
    assert cold["messages"] == []
    assert cold["semantic"]["state"] == "warming"
    assert indexer.request_index(context.session_id) is False
    runs = 0
    while worker.run_one():
        runs += 1
        assert runs <= 8
    assert provider.passages == 4
    result = history.search(context, query="vehicle", mode="hybrid", limit=1)
    assert result["semantic"]["state"] == "ready"
    assert result["messages"][0]["seq"] == 1
    assert result["messages"][0]["match"] is False
    source = history.read(context, seq=result["messages"][0]["seq"])
    assert source["messages"][0]["content"] == "I bought an automobile."
    history.search(context, query="vehicle", mode="semantic")
    assert provider.passages == 4


def test_semantic_results_are_revalidated_before_source_delivery(tmp_path):
    history, _, _, _, context, factory = setup_index(tmp_path)
    history.search(context, query="automobile")
    hit = history.search(context, query="automobile")["messages"][0]

    class StaleIndex:
        def search(self, *args, **kwargs):
            with factory() as conn:
                conn.execute("UPDATE agent_session_context_messages SET content='removed' WHERE session_id=? AND seq=1",
                             (context.session_id,))
            return {"messages": [hit], "status": {"state": "ready"}}

    history.semantic_index = StaleIndex()
    assert history.search(context, query="vehicle", mode="semantic")["messages"] == []


def test_disabled_worker_and_failed_provider_keep_literal_search_available(tmp_path):
    history, indexer, worker, provider, context, _ = setup_index(tmp_path)
    indexer.enabled = False
    assert history.search(context, query="automobile", mode="hybrid")["messages"]
    assert not worker.run_one()
    indexer.enabled = True

    def fail(texts):
        raise RuntimeError("private model path")

    provider.embed_passages = fail
    history.search(context, query="automobile", mode="hybrid")
    assert worker.run_one()
    assert not indexer.request_index(context.session_id, force=True)
    fallback = history.search(context, query="automobile", mode="hybrid")
    assert fallback["messages"][0]["seq"] == 1
    assert "private model path" not in str(fallback)


def test_hybrid_cursor_survives_stale_candidates_and_reports_rank_window(tmp_path):
    history, _, _, _, context, factory = setup_index(tmp_path)
    hits = history.search(context)["messages"]

    class EditedIndex:
        def search(self, *args, **kwargs):
            with factory() as conn:
                conn.execute("UPDATE agent_session_context_messages SET content='edited' WHERE session_id=? AND seq=?",
                             (context.session_id, hits[0]["seq"]))
            return {"messages": hits, "status": {"state": "ready", "indexed_count": 101}}

    history.semantic_index = EditedIndex()
    result = history.search(context, query="unmatched", mode="hybrid", limit=1)
    assert result["messages"][0]["seq"] == hits[1]["seq"]
    assert result["next_offset"] == 2
    assert result["stale_candidates_filtered"]
    assert result["ranking_truncated"] and result["incomplete"]
