from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.schemas import KnowledgeChunkLoadRequest, KnowledgeImportRequest
from app.core.config import get_settings
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolContext


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    from app.api.main import create_app

    return create_app()


def _active_child_run_id(runtime) -> str:
    manager = runtime.agent_run_manager
    parent = manager.create_run(session_id="parent_session", user_input="parent")
    manager.mark_running(parent.run_id)
    child = manager.create_child_run(
        parent_run_id=parent.run_id, plan_id="scope_plan", step_id="scope_step",
        attempt=1, user_input="child",
    )
    manager.mark_child_running(child.run_id)
    return child.run_id


def _wiki_payload() -> KnowledgeImportRequest:
    return KnowledgeImportRequest(
        source={
            "source_type": "web_page",
            "display_name": "PRTS Wiki",
            "uri": "https://prts.wiki",
            "sensitivity": "public",
            "remote_policy": "allow",
            "metadata": {"seed_site": "prts"},
        },
        title="PRTS Wiki",
        uri="https://prts.wiki",
        text="PRTS 是明日方舟资料站。干员、关卡、材料和活动页面可导入本地知识库。",
        mime_type="text/markdown",
        sensitivity="public",
        remote_policy="allow",
        metadata={"crawler_reserved": True},
    )


def test_knowledge_package_is_agent_visible_and_read_only(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    package_names = {package.name for package in app.state.runtime.tool_registry.list_packages()}
    tool_specs = app.state.runtime.tool_registry.list_tools(package="knowledge")

    assert "knowledge" in package_names
    assert {tool.name for tool in tool_specs} == {
        "knowledge.list_sources",
        "knowledge.search",
        "knowledge.load_chunks",
        "knowledge.load_document",
    }
    assert all(tool.read_only is True for tool in tool_specs)
    capability = next(
        item for item in app.state.runtime.list_capabilities() if item.name == "knowledge"
    )
    assert capability.read_only is True
    assert capability.requires_confirmation is False


def test_default_knowledge_search_reranks_before_top_k_and_falls_back(tmp_path, monkeypatch):
    from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput

    app = _app(tmp_path, monkeypatch)
    runtime = app.state.runtime
    reranker = runtime.knowledge_service._reranker
    assert reranker is not None
    assert reranker.local_files_only is True
    for title in ("ordinary.md", "preferred.md"):
        runtime.knowledge_service.import_text_document(
            KnowledgeDocumentInput(
                source=KnowledgeSourceInput(
                    display_name=title, uri=f"local://{title}",
                    sensitivity="public", remote_policy="allow",
                ),
                title=title, uri=f"local://{title}",
                text="shared sentinel evidence", sensitivity="public", remote_policy="allow",
            )
        )
    monkeypatch.setattr(
        reranker, "score",
        lambda query, candidates: [1.0 if "preferred.md" in item else 0.0 for item in candidates],
    )
    context = ToolContext(session_id="default_rerank")
    result = runtime.tool_executor.execute(
        invocation_id="rerank_default", tool_name="knowledge.search",
        tool_input={"query": "sentinel", "limit": 1}, context=context,
    )
    assert result.status == "completed"
    assert result.output["rerank_applied"] is True
    assert [item["title"] for item in result.output["results"]] == ["preferred.md"]

    def unavailable(query, candidates):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(reranker, "score", unavailable)
    fallback = runtime.tool_executor.execute(
        invocation_id="rerank_fallback", tool_name="knowledge.search",
        tool_input={"query": "sentinel", "limit": 1}, context=context,
    )
    assert fallback.status == "completed"
    assert fallback.output["rerank_applied"] is False
    assert "local reranker unavailable" in fallback.output["retrieval_warning"]
    assert fallback.output["results"]


def test_knowledge_tools_search_load_and_validate_output(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    from app.api.routes.knowledge import import_knowledge_document

    request = SimpleNamespace(app=app)
    imported = import_knowledge_document(_wiki_payload(), request)
    context = ToolContext(session_id="session_knowledge", trace_id="trace_knowledge")

    sources = app.state.runtime.tool_executor.execute(
        invocation_id="knowledge_sources_001",
        tool_name="knowledge.list_sources",
        tool_input={},
        context=context,
    )
    assert sources.status == "completed"
    assert any(item["source_id"] == imported.source_id for item in sources.output["sources"])

    search = app.state.runtime.tool_executor.execute(
        invocation_id="knowledge_search_001",
        tool_name="knowledge.search",
        tool_input={"query": "干员 材料", "limit": 5, "source_ids": [imported.source_id]},
        context=context,
    )

    assert search.status == "completed"


    assert search.output["results"][0]["document_id"] == imported.document_id
    assert search.output["results"][0]["untrusted_data"] is True
    assert app.state.runtime.tool_executor.validate_output(
        tool_name="knowledge.search",
        result=search,
    ) == []

    chunk_id = search.output["results"][0]["chunk_id"]
    loaded = app.state.runtime.tool_executor.execute(
        invocation_id="knowledge_load_chunks_001",
        tool_name="knowledge.load_chunks",
        tool_input={"chunk_ids": [chunk_id], "max_chars_per_chunk": 120},
        context=context,
    )

    assert loaded.status == "completed"
    assert loaded.output["chunks"][0]["chunk_id"] == chunk_id
    assert "明日方舟" in loaded.output["chunks"][0]["text"]
    assert app.state.runtime.tool_executor.validate_output(
        tool_name="knowledge.load_chunks",
        result=loaded,
    ) == []

    document = app.state.runtime.tool_executor.execute(
        invocation_id="knowledge_load_document_001",
        tool_name="knowledge.load_document",
        tool_input={"document_id": imported.document_id},
        context=context,
    )

    assert document.status == "completed"
    assert document.output["text"] is None
    assert document.output["chunk_ids"] == [chunk_id]


def test_workspace_source_scope_is_enforced_for_parent_agent_tools(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput

    roots = [tmp_path / "workspace_a", tmp_path / "workspace_b"]
    for root in roots:
        root.mkdir()
    imported = []
    for root in roots:
        imported.append(app.state.runtime.knowledge_service.import_text_document(
            KnowledgeDocumentInput(
                source=KnowledgeSourceInput(
                    source_type="workspace_file", display_name=root.name,
                    uri=(root / "note.md").as_posix(),
                    access_scope={"workspace_paths": [root.as_posix()]},
                    sensitivity="public", remote_policy="allow",
                ),
                title="note.md", text="shared sentinel workspace note",
                uri=(root / "note.md").as_posix(),
                sensitivity="public", remote_policy="allow",
            )
        ))
    context = ToolContext(
        session_id="scope_session", trace_id="scope_trace",
        workspace_root=roots[0].as_posix(),
    )
    result = app.state.runtime.tool_executor.execute(
        invocation_id="scope_search", tool_name="knowledge.search",
        tool_input={"query": "shared sentinel workspace note", "limit": 10}, context=context,
    )
    assert result.status == "completed"
    assert {item["source_id"] for item in result.output["results"]} == {imported[0].source_id}
    sources = app.state.runtime.tool_executor.execute(
        invocation_id="scope_sources", tool_name="knowledge.list_sources", tool_input={}, context=context,
    )
    assert {item["source_id"] for item in sources.output["sources"]} == {imported[0].source_id}
    hidden_chunk = app.state.runtime.knowledge_service.search(
        query="shared sentinel", source_ids=[imported[1].source_id], mode="keyword"
    ).results[0].chunk_id
    loaded = app.state.runtime.tool_executor.execute(
        invocation_id="scope_load", tool_name="knowledge.load_chunks",
        tool_input={"chunk_ids": [hidden_chunk]}, context=context,
    )
    assert loaded.status == "completed"
    assert loaded.output["chunks"] == []


def test_child_knowledge_search_and_known_ids_are_filtered_by_source_and_account_scope(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput

    first = app.state.runtime.knowledge_service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                source_type="local_document", display_name="S1", uri="local://s1",
                metadata={"account_id": "account_1"},
            ),
            title="Authorized source", uri="local://s1/doc",
            text="shared scope sentinel authorized text",
        )
    )
    second = app.state.runtime.knowledge_service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                source_type="local_document", display_name="S2", uri="local://s2",
                metadata={"account_id": "account_2"},
            ),
            title="Unauthorized source", uri="local://s2/doc",
            text="shared scope sentinel unauthorized secret",
        )
    )
    root_context = ToolContext(session_id="root", trace_id="root")
    root_search = app.state.runtime.tool_executor.execute(
        invocation_id="root-search", tool_name="knowledge.search",
        tool_input={"query": "shared scope sentinel", "limit": 10}, context=root_context,
    )
    assert root_search.status == "completed"
    chunk_ids = {item["document_id"]: item["chunk_id"] for item in root_search.output["results"]}
    child_view = ToolView(
        snapshot_id="child_snapshot", child_run_id=_active_child_run_id(app.state.runtime),
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search", "knowledge.load_chunks", "knowledge.load_document"),
        allowed_source_ids=(first.source_id,), allowed_account_ids=("account_1",),
        side_effect_level=SideEffectLevel.READ,
    )
    child_context = ToolContext(session_id="child", tool_view=child_view)
    child_search = app.state.runtime.tool_executor.execute(
        invocation_id="child-search", tool_name="knowledge.search",
        tool_input={"query": "shared scope sentinel", "limit": 10}, context=child_context,
    )
    assert child_search.status == "completed", child_search.error
    assert {item["document_id"] for item in child_search.output["results"]} == {first.document_id}

    known_s2_chunk = chunk_ids[second.document_id]
    child_load = app.state.runtime.tool_executor.execute(
        invocation_id="child-load-known-s2", tool_name="knowledge.load_chunks",
        tool_input={"chunk_ids": [known_s2_chunk]}, context=child_context,
    )
    assert child_load.status == "completed"
    assert child_load.output["chunks"] == []
    child_document = app.state.runtime.tool_executor.execute(
        invocation_id="child-document-known-s2", tool_name="knowledge.load_document",
        tool_input={"document_id": second.document_id, "include_text": True}, context=child_context,
    )
    assert child_document.status == "failed"


def test_knowledge_http_import_search_and_load(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    request = SimpleNamespace(app=app)
    from app.api.routes.knowledge import (
        import_knowledge_document,
        load_knowledge_chunks,
        load_knowledge_document,
        search_knowledge,
    )

    assert "/knowledge/import" in app.openapi()["paths"]
    assert "/knowledge/search" in app.openapi()["paths"]
    assert "/knowledge/chunks/load" in app.openapi()["paths"]
    assert "/knowledge/semantic-index/sync" in app.openapi()["paths"]
    assert "/knowledge/mail-mirror/sync" in app.openapi()["paths"]

    imported = import_knowledge_document(_wiki_payload(), request)
    document_id = imported.document_id

    search = search_knowledge(request, q="关卡 材料", limit=5, source_type=None)
    assert len(search.results) == 1
    assert search.results[0].document_id == document_id

    chunk = load_knowledge_chunks(
        KnowledgeChunkLoadRequest(
            chunk_ids=[search.results[0].chunk_id],
            max_chars_per_chunk=120,
        ),
        request,
    )
    assert chunk.chunks[0].source_ref.startswith("web_page:")

    document = load_knowledge_document(
        document_id,
        request,
        include_text=False,
        max_chars=12000,
    )
    assert document.text is None


def test_knowledge_route_rejects_missing_document(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    request = SimpleNamespace(app=app)
    from app.api.routes.knowledge import load_knowledge_document

    with pytest.raises(Exception) as exc_info:
        load_knowledge_document("missing", request)
    assert getattr(exc_info.value, "status_code", None) == 404
