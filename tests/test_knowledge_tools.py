from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.schemas import KnowledgeChunkLoadRequest, KnowledgeImportRequest
from app.core.config import get_settings
from app.core.tools import ToolContext


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    from app.api.main import create_app

    return create_app()


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


def test_knowledge_tools_search_load_and_validate_output(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    from app.api.routes.knowledge import import_knowledge_document

    request = SimpleNamespace(app=app)
    imported = import_knowledge_document(_wiki_payload(), request)
    context = ToolContext(session_id="session_knowledge", trace_id="trace_knowledge")

    search = app.state.runtime.tool_executor.execute(
        invocation_id="knowledge_search_001",
        tool_name="knowledge.search",
        tool_input={"query": "干员 材料", "limit": 5},
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
