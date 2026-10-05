"""Real knowledge tools must agree with server full-data discovery semantics."""

import pytest

from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry, tool_scope_discovery_denial
from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput
from app.tool_packages.knowledge import (
    KNOWLEDGE_PACKAGE,
    ListKnowledgeSourcesTool,
    LoadKnowledgeChunksTool,
    LoadKnowledgeDocumentTool,
    SearchKnowledgeTool,
)
from tests.test_knowledge_service import _service

KINDS = ("list_sources", "search", "load_chunks", "load_document")


@pytest.fixture
def corpus(tmp_path):
    service = _service(tmp_path)
    current = (tmp_path / "project_a").as_posix()
    other = (tmp_path / "project_b").as_posix()
    scopes = {
        "global": {},
        "workspace": {"workspace_paths": [current]},
        "other_project": {"workspace_paths": [other]},
        "session": {"session_ids": ["child_session"]},
        "other_session": {"session_ids": ["foreign_session"]},
        "both_denied": {"workspace_paths": [current], "session_ids": ["foreign_session"]},
    }
    records = {}
    for name, access in scopes.items():
        imported = service.import_text_document(KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                display_name=name, uri=f"local://{name}", access_scope=access,
                sensitivity="public", remote_policy="allow",
            ),
            title=name, uri=f"local://{name}", text=f"sentinel evidence {name}",
            sensitivity="public", remote_policy="allow",
        ))
        document = service.load_document(document_id=imported.document_id)
        records[name] = (imported.source_id, imported.document_id, document.chunk_ids[0])
    return service, current, records


def _executor(service):
    registry = ToolRegistry()
    registry.register_package(KNOWLEDGE_PACKAGE)
    for cls in (ListKnowledgeSourcesTool, SearchKnowledgeTool,
                LoadKnowledgeChunksTool, LoadKnowledgeDocumentTool):
        registry.register_tool(cls(service))
    return ToolExecutor(registry)


def _context(workspace, *, full, grants=()):
    return ToolContext(session_id="child_session", workspace_root=workspace, tool_view=ToolView(
        snapshot_id="snapshot_full", child_run_id="child_full",
        allowed_packages=("knowledge",),
        allowed_tools=tuple(f"knowledge.{kind}" for kind in KINDS),
        allowed_source_ids=grants, full_data_authority=full,
        allowed_paths=(workspace,), side_effect_level=SideEffectLevel.READ,
    ))


def _input(kind, records):
    if kind == "list_sources":
        return {}
    if kind == "search":
        return {"query": "sentinel", "mode": "keyword", "limit": 30}
    if kind == "load_chunks":
        return {"chunk_ids": [r[2] for r in records.values()] or ["missing_chunk"]}
    return {"document_id": records.get("workspace", (None, "missing_document"))[1]}


def _sources(kind, result):
    if kind == "load_document":
        return {result.output["source_id"]}
    field = {"list_sources": "sources", "search": "results", "load_chunks": "chunks"}[kind]
    return {item["source_id"] for item in result.output[field]}


@pytest.mark.parametrize("kind", KINDS)
def test_full_empty_grant_uses_current_service_visibility_not_all_sources(corpus, kind):
    service, workspace, records = corpus
    executor = _executor(service)
    context = _context(workspace, full=True)
    expected = {records[key][0] for key in ("global", "workspace", "session")}
    assert set(service.list_authorized_source_ids(
        workspace_path=workspace, session_id="child_session"
    )) == expected
    spec = executor.registry.get_tool(f"knowledge.{kind}").spec
    assert tool_scope_discovery_denial(spec, context.tool_view) is None
    result = executor.execute(invocation_id=f"full-{kind}", tool_name=spec.name,
                              tool_input=_input(kind, records), context=context)
    assert result.status == "completed", result.error
    assert spec.read_only is True and executor.validate_output(tool_name=spec.name, result=result) == []
    assert _sources(kind, result) == ({records["workspace"][0]} if kind == "load_document" else expected)
    if kind == "load_document":
        for name in ("other_project", "other_session", "both_denied"):
            denied = executor.execute(invocation_id=f"deny-{name}", tool_name=spec.name,
                                      tool_input={"document_id": records[name][1]}, context=context)
            assert denied.status == "failed" and not denied.output


@pytest.mark.parametrize("kind", KINDS)
def test_full_empty_corpus_is_empty_or_not_found_not_permission_rejected(tmp_path, kind):
    executor = _executor(_service(tmp_path))
    result = executor.execute(invocation_id=f"empty-{kind}", tool_name=f"knowledge.{kind}",
                              tool_input=_input(kind, {}), context=_context(tmp_path.as_posix(), full=True))
    if kind == "load_document":
        assert result.status == "failed" and "not found" in result.error
    else:
        assert result.status == "completed", result.error
        assert _sources(kind, result) == set()


@pytest.mark.parametrize("kind", KINDS)
def test_restricted_empty_grant_still_rejects(corpus, kind):
    service, workspace, records = corpus
    executor = _executor(service)
    context = _context(workspace, full=False)
    spec = executor.registry.get_tool(f"knowledge.{kind}").spec
    assert tool_scope_discovery_denial(spec, context.tool_view) is not None
    result = executor.execute(invocation_id=f"restricted-{kind}", tool_name=spec.name,
                              tool_input=_input(kind, records), context=context)
    assert result.status == "rejected" and not result.output
    assert result.execution_started is False


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("kind", KINDS)
def test_nonempty_grants_always_intersect_visibility_even_with_full_flag(corpus, kind, full):
    service, workspace, records = corpus
    executor = _executor(service)
    context = _context(workspace, full=full,
                       grants=(records["workspace"][0], records["other_project"][0]))
    result = executor.execute(invocation_id=f"grant-{kind}-{full}", tool_name=f"knowledge.{kind}",
                              tool_input=_input(kind, records), context=context)
    assert result.status == "completed", result.error
    assert _sources(kind, result) == {records["workspace"][0]}
    if kind == "load_document":
        # A visible global source still lies outside the explicit child grant.
        denied = executor.execute(invocation_id=f"known-outside-grant-{full}",
                                  tool_name="knowledge.load_document",
                                  tool_input={"document_id": records["global"][1]}, context=context)
        assert denied.status == "failed" and not denied.output


@pytest.mark.parametrize("full", [False, True])
def test_requested_search_sources_cannot_expand_explicit_grants(corpus, full):
    service, workspace, records = corpus
    executor = _executor(service)
    context = _context(workspace, full=full, grants=(records["workspace"][0],))
    result = executor.execute(invocation_id=f"requested-{full}", tool_name="knowledge.search",
                              tool_input={"query": "sentinel", "mode": "keyword",
                                          "source_ids": [records["global"][0]]}, context=context)
    assert result.status == "completed" and result.output["results"] == []
