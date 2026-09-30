from __future__ import annotations

from app.core.config import get_settings
from app.core.tools import ToolContext
from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput


def _runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    from app.api.main import create_app

    return create_app().state.runtime


def _import(runtime, title: str, text: str):
    return runtime.knowledge_service.import_text_document(
        KnowledgeDocumentInput(
            source=KnowledgeSourceInput(
                display_name=title, uri=f"local://{title}",
                sensitivity="public", remote_policy="allow",
            ),
            title=title, uri=f"local://{title}", text=text,
            sensitivity="public", remote_policy="allow",
        )
    )


def _invoke(runtime, payload: dict, invocation_id: str = "rewrite"):
    return runtime.tool_executor.execute(
        invocation_id=invocation_id, tool_name="knowledge.search",
        tool_input=payload, context=ToolContext(session_id="rewrite_test"),
    )


def test_explicit_rewrite_recovers_evidence_and_records_trace(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    runtime.knowledge_service._reranker = None
    doc = _import(runtime, "synonym.md", "The ancient star catalog lists the red giant.")

    direct = _invoke(runtime, {"query": "stellar register", "mode": "keyword"}, "direct")
    assert direct.status == "completed"
    assert direct.output["results"] == []
    assert "rewrite_trace" not in direct.output

    rewritten = _invoke(runtime, {
        "query": "  stellar   register ", "mode": "keyword", "limit": 10,
        "rewrite": {
            "evidence_gap": "The first search did not find the catalog entry.",
            "expected_gain": "Find the entry using its alternate terminology.",
            "queries": [{"query": "star catalog", "purpose": "Search for the catalog name."}],
        },
    })
    assert rewritten.status == "completed", rewritten.error
    assert doc.document_id in {item["document_id"] for item in rewritten.output["results"]}
    trace = rewritten.output["rewrite_trace"]
    assert trace["original_query"] == "  stellar   register "
    assert trace["normalized_query"] == "stellar register"
    assert [item["query"] for item in trace["retrievals"]] == ["stellar register", "star catalog"]
    assert trace["retrievals"][1]["hit_count"] > 0
    assert trace["retrievals"][1]["new_unique_hits"] > 0


def test_rewrite_rejects_duplicate_and_permission_fields(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    base = {"query": "star catalog", "rewrite": {
        "evidence_gap": "Missing evidence", "expected_gain": "Find alternate text",
        "queries": [{"query": "STAR  CATALOG", "purpose": "Retry"}],
    }}
    duplicate = _invoke(runtime, base, "duplicate")
    assert duplicate.status == "rejected"
    assert "duplicates" in duplicate.error

    base["rewrite"]["queries"] = [{"query": "astronomy list", "purpose": "Try synonym"}]
    base["rewrite"]["source_ids"] = ["unauthorized"]
    permission = _invoke(runtime, base, "permission")
    assert permission.status == "rejected"
    assert "Invalid query rewrite" in permission.error


def test_rewrite_cannot_expand_explicit_source_scope(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    runtime.knowledge_service._reranker = None
    allowed = _import(runtime, "allowed.md", "astronomy public entry")
    _import(runtime, "blocked.md", "private catalog details")

    result = _invoke(runtime, {
        "query": "astronomy entry", "source_ids": [allowed.source_id],
        "mode": "keyword", "rewrite": {
            "evidence_gap": "Need catalog details.",
            "expected_gain": "Find the catalog entry within authorized sources.",
            "queries": [{"query": "private catalog details", "purpose": "Find alternate wording."}],
        },
    }, "scoped_rewrite")
    assert result.status == "completed", result.error
    assert all(item["source_id"] == allowed.source_id for item in result.output["results"])
    assert result.output["rewrite_trace"]["retrievals"][1]["hit_count"] == 0


def test_tool_accepts_more_than_two_rewrites(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    runtime.knowledge_service._reranker = None
    result = _invoke(runtime, {
        "query": "original topic", "mode": "keyword",
        "rewrite": {
            "evidence_gap": "Several facts need separate evidence.",
            "expected_gain": "Search each fact independently.",
            "queries": [
                {"query": f"topic aspect {index}", "purpose": f"Find aspect {index}."}
                for index in range(4)
            ],
        },
    }, "four_rewrites")
    assert result.status == "completed", result.error
    assert len(result.output["rewrite_trace"]["queries"]) == 4
    assert len(result.output["rewrite_trace"]["retrievals"]) == 5
