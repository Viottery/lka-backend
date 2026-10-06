from __future__ import annotations

import pytest

from app.core.background_jobs import BackgroundJobStore
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry, ToolResult, ToolSpec
from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeService, KnowledgeSourceInput
from app.domains.message_history import MessageHistoryService
from app.domains.message_knowledge import MessageKnowledgeAdapter
from app.storage.db import connect, init_db


def _message_service(tmp_path):
    path = tmp_path / "messages.sqlite3"
    jobs = BackgroundJobStore(path)
    service = MessageHistoryService(path, jobs)
    service.ensure_schema()
    return service


def _setup_conversation(service, *, conversation_id="group-1", text="release on Friday"):
    policy = service.set_policy({
        "platform": "mock", "account_id": "acct", "conversation_type": "group",
        "conversation_id": conversation_id, "display_name": "Project Group",
        "expected_revision": 0, "record_enabled": True,
    })
    service.import_messages([{
        "platform": "mock", "account_id": "acct", "message_id": "msg-1",
        "conversation_type": "group", "conversation_id": conversation_id,
        "sender_id": "sender-1", "sender_name": "Alice", "text": text,
        "sent_at": 100, "received_at": 100,
    }])
    return policy


def test_live_message_search_load_and_revocation_are_epoch_fenced(tmp_path):
    service = _message_service(tmp_path)
    policy = _setup_conversation(service)
    adapter = MessageKnowledgeAdapter(service)
    source_id = policy["source_id"]
    account_scope = policy["account_scope_id"]

    found = adapter.search(query="Friday", limit=10, source_ids=[source_id],
                           account_ids=[account_scope], max_snippet_chars=420)
    assert len(found) == 1
    item = found[0]
    assert item.source_type == "chat_message"
    assert item.chunk_id.startswith("chat_message_chunk_")
    assert item.metadata["group_name"] == "Project Group"
    assert item.metadata["capture_epoch"] == policy["capture_epoch"]
    assert item.metadata["source_policy"]["constraint_id"] == "message_evidence_human_matter"
    loaded = adapter.load_chunks(chunk_ids=[item.chunk_id], max_chars_per_chunk=420, offset=0,
                                 source_ids=[source_id], account_ids=[account_scope])
    assert loaded[0].text == "release on Friday"

    revoked = service.set_policy({
        "platform": "mock", "account_id": "acct", "conversation_type": "group",
        "conversation_id": "group-1", "expected_revision": policy["revision"],
        "record_enabled": False,
    })
    assert revoked["record_enabled"] is False
    assert adapter.search(query="Friday", limit=10, source_ids=[source_id],
                          account_ids=[account_scope], max_snippet_chars=420) == []
    assert adapter.load_chunks(chunk_ids=[item.chunk_id], max_chars_per_chunk=420, offset=0,
                               source_ids=[source_id], account_ids=[account_scope]) == []

    reenabled = service.set_policy({
        "platform": "mock", "account_id": "acct", "conversation_type": "group",
        "conversation_id": "group-1", "expected_revision": revoked["revision"],
        "record_enabled": True,
    })
    assert reenabled["capture_epoch"] > policy["capture_epoch"]
    assert adapter.search(query="Friday", limit=10, source_ids=[source_id],
                          account_ids=[account_scope], max_snippet_chars=420) == []
    assert adapter.load_document(document_id=item.document_id, include_text=True, max_chars=1000,
                                 source_ids=[source_id], account_ids=[account_scope]) is None


def test_unified_search_merges_local_and_live_message_sources_and_empty_grants_deny(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    init_db(path)
    knowledge = KnowledgeService(lambda: connect(path))
    # The knowledge schema is initialized by the normal runtime migration.
    message_service = _message_service(tmp_path)
    policy = _setup_conversation(message_service)
    knowledge.register_source_provider(MessageKnowledgeAdapter(message_service))
    knowledge.import_text_document(KnowledgeDocumentInput(
        source=KnowledgeSourceInput(source_type="local_document", display_name="notes.md",
                                   uri="local://notes.md", sensitivity="personal", remote_policy="redact"),
        title="notes.md", text="release on Friday", sensitivity="personal", remote_policy="redact",
    ))

    sources = knowledge.list_sources()
    message_source = next(item for item in sources if item.source_type == "chat_message")
    assert message_source.source_id == policy["source_id"]
    result = knowledge.search(query="Friday", mode="keyword", limit=10,
                              provider_account_ids=[policy["account_scope_id"]])
    assert {item.source_type for item in result.results} == {"local_document", "chat_message"}
    denied = knowledge.search(query="Friday", mode="keyword", limit=10,
                              provider_account_ids=[])
    assert all(item.source_type != "chat_message" for item in denied.results)
    denied_by_generic_scope = knowledge.search(query="Friday", mode="keyword", limit=10,
                                               account_ids=[])
    assert all(item.source_type != "chat_message" for item in denied_by_generic_scope.results)
    blank = knowledge.search(query="", mode="keyword", limit=10)
    assert all(item.source_type != "chat_message" for item in blank.results)
    assert knowledge.resolve_origin_constraints(
        invocation=type("Invocation", (), {"input": {"source_ids": [policy["source_id"]]}})(),
        context=None,
    ) == ["message_evidence_human_matter"]
    assert knowledge.allow_cached_observation(
        result={"results": [{"chunk_id": next(item.chunk_id for item in result.results
                                                  if item.source_type == "chat_message")}]},
    ) is False
    assert knowledge.allow_cached_observation(result={"results": [{"chunk_id": "knowledge_chunk_static"}]}) is True


def test_registered_search_persists_message_origin_constraint_but_local_only_does_not(tmp_path):
    from app.tool_packages.knowledge import SearchKnowledgeTool
    from app.tool_packages.messages import MESSAGE_ORIGIN_CONSTRAINT, register_message_constraints

    path = tmp_path / "knowledge.sqlite3"
    init_db(path)
    knowledge = KnowledgeService(lambda: connect(path))
    message_service = _message_service(tmp_path)
    _setup_conversation(message_service)
    knowledge.register_source_provider(MessageKnowledgeAdapter(message_service))
    knowledge.import_text_document(KnowledgeDocumentInput(
        source=KnowledgeSourceInput(source_type="local_document", display_name="notes.md",
                                   uri="local://notes.md", sensitivity="personal", remote_policy="redact"),
        title="notes.md", text="release Friday", sensitivity="personal", remote_policy="redact",
    ))

    class MatterWrite:
        spec = ToolSpec(name="matter.write", type="local_tool", description="write matter",
                        read_only=False, effect_domains=("matter",))

        def invoke(self, *, invocation, context):
            return ToolResult(invocation_id=invocation.invocation_id,
                              tool_name=self.spec.name, status="completed")

    registry = ToolRegistry()
    register_message_constraints(registry)
    registry.register_tool(SearchKnowledgeTool(knowledge))
    registry.register_tool(MatterWrite())
    executor = ToolExecutor(registry)
    context = ToolContext(session_id="origin-session", safety_review_approved=True)

    local = executor.execute(invocation_id="local-search", tool_name="knowledge.search",
                             tool_input={"query": "Friday", "source_types": ["local_document"]},
                             context=context)
    assert local.status == "completed"
    assert executor.execute(invocation_id="local-write", tool_name="matter.write",
                            tool_input={}, context=context).status == "completed"

    message = executor.execute(invocation_id="message-search", tool_name="knowledge.search",
                               tool_input={"query": "Friday", "source_types": ["chat_message"]},
                               context=context)
    assert message.status == "completed"
    assert any(item["source_type"] == "chat_message" for item in message.output["results"])
    blocked = executor.execute(invocation_id="message-write", tool_name="matter.write",
                              tool_input={}, context=context)
    assert blocked.status == "rejected"
    assert MESSAGE_ORIGIN_CONSTRAINT in executor.constraint_store.get([("session", "origin-session")])


def test_child_with_source_but_no_account_grant_cannot_search_message_provider(tmp_path):
    from app.core.context_driver import ToolView
    from app.core.multi_agent import SideEffectLevel
    from app.core.tools import ToolContext
    from app.tool_packages.knowledge import SearchKnowledgeTool

    path = tmp_path / "knowledge.sqlite3"
    init_db(path)
    knowledge = KnowledgeService(lambda: connect(path))
    message_service = _message_service(tmp_path)
    policy = _setup_conversation(message_service)
    knowledge.register_source_provider(MessageKnowledgeAdapter(message_service))
    tool = SearchKnowledgeTool(knowledge)
    context = ToolContext(session_id="child-session", tool_view=ToolView(
        snapshot_id="snapshot", child_run_id="child-run", allowed_packages=("knowledge",),
        allowed_source_ids=(policy["source_id"],), allowed_account_ids=(),
        side_effect_level=SideEffectLevel.READ,
    ))
    result = tool.invoke(
        invocation=type("Invocation", (), {"invocation_id": "i", "input": {"query": "Friday"}})(),
        context=context,
    )
    assert result.status == "completed"
    assert result.output["results"] == []


def test_message_knowledge_http_requires_reader_and_keeps_authenticated_responses_private(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.api.main import create_app

    app = create_app()
    runtime = app.state.runtime
    runtime.message_history.set_policy({
        "platform": "mock", "account_id": "acct", "conversation_type": "group",
        "conversation_id": "http-group", "display_name": "HTTP Group",
        "expected_revision": 0, "record_enabled": True,
    })
    runtime.message_history.import_messages([{
        "platform": "mock", "account_id": "acct", "message_id": "http-msg",
        "conversation_type": "group", "conversation_id": "http-group",
        "sender_id": "author", "sender_name": "Author", "text": "deploy Friday",
        "sent_at": 100, "received_at": 100,
    }])
    from fastapi import HTTPException
    from starlette.responses import Response

    from app.api.routes import knowledge as knowledge_routes
    from app.api.schemas import KnowledgeChunkLoadRequest

    def reader(request):
        if request.headers.get("x-lka-messages-token") != "reader-secret":
            raise HTTPException(status_code=401)

    monkeypatch.setattr("app.api.routes.message_reading._reader", reader)
    response = Response()
    request = type("Request", (), {"app": app, "headers": {}})()
    public = knowledge_routes.search_knowledge(request, response, q="Friday", limit=10,
                                                source_type=None, source_id=None, mode="keyword")
    assert all(item.source_type != "chat_message" for item in public.results)
    with pytest.raises(HTTPException) as exc:
        knowledge_routes.search_knowledge(request, response, q="Friday", limit=10,
                                           source_type=["chat_message"], source_id=None, mode="keyword")
    assert exc.value.status_code == 401
    importer_request = type("Request", (), {"app": app, "headers": {"x-lka-messages-token": "import-secret"}})()
    with pytest.raises(HTTPException) as exc:
        knowledge_routes.search_knowledge(importer_request, response, q="Friday", limit=10,
                                           source_type=["chat_message"], source_id=None, mode="keyword")
    assert exc.value.status_code == 401

    reader_request = type("Request", (), {"app": app, "headers": {"x-lka-messages-token": "reader-secret"}})()
    reader_response = Response()
    reader = knowledge_routes.search_knowledge(reader_request, reader_response, q="Friday", limit=10,
                                                source_type=None, source_id=None, mode="keyword")
    assert reader_response.headers["cache-control"] == "no-store"
    message = next(item for item in reader.results if item.source_type == "chat_message")
    with pytest.raises(HTTPException) as exc:
        knowledge_routes.load_knowledge_chunks(
            KnowledgeChunkLoadRequest(chunk_ids=[message.chunk_id]), request, Response()
        )
    assert exc.value.status_code == 401
    load_response = Response()
    loaded = knowledge_routes.load_knowledge_chunks(
        KnowledgeChunkLoadRequest(chunk_ids=[message.chunk_id]), reader_request, load_response
    )
    assert load_response.headers["cache-control"] == "no-store"
    assert loaded.chunks[0].text == "deploy Friday"
