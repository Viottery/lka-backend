"""Literal misses expose bounded source excerpts, never semantic matches or verification."""

import hashlib
from types import SimpleNamespace

import httpx
import pytest

from app.core.tools import ToolContext, ToolExecutor, ToolRegistry
from app.integrations.web_search import PublicPageFetcher, WebSearchError
from app.tool_packages.web import WEB_PACKAGE, WebFindTool

URL = "https://8.8.8.8/source"


def client_for(text):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/plain; charset=utf-8"}, text=text)

    return PublicPageFetcher(transport=httpx.MockTransport(respond)), calls


def assert_preview(result, text):
    assert result["recovery_preview_scope"] == "non_phrase_nonsemantic_excerpt"
    assert result["query_tokenization"] == "unicode_word_runs_no_cjk_segmentation"
    assert len(result["recovery_preview"]) <= 3
    assert sum(len(row["snippet"]) for row in result["recovery_preview"]) <= 1200
    for row in result["recovery_preview"]:
        assert 0 <= row["snippet_start"] <= row["snippet_end"] <= len(text)
        assert row["snippet"] == text[row["snippet_start"]:row["snippet_end"]]
        assert row["kind"] in {"query_token_context", "page_prefix"}
    assert result["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert result["snapshot_stable"] is False
    assert "verified" not in result and "source_read" not in result


def test_zero_phrase_match_returns_actual_writer_excerpt_without_another_fetch():
    text = "Only one writer may operate at a time; several readers may coexist."
    client, calls = client_for(text)

    result = client.find(URL, "single writer")

    assert result["matches"] == [] and result["total_matches"] == 0
    assert result["complete"] is True and result["next_offset"] is None
    assert any("one writer" in row["snippet"] for row in result.get("recovery_preview", []))
    assert result["match_status"] == "no_literal_match"
    assert all(row["kind"] == "query_token_context" for row in result["recovery_preview"])
    assert_preview(result, text)
    assert len(calls) == 1 and str(calls[0].url) == URL
    assert "single writer" not in str(calls[0].headers) and not calls[0].content


def test_unknown_literal_still_returns_bounded_prefix_not_invented_matches():
    text = ("Ordinary page introduction. " + "unrelated text " * 200).rstrip()
    client, calls = client_for(text)
    result = client.find(URL, "CONCURRENT")

    assert result["matches"] == [] and result["total_matches"] == 0
    assert result["match_status"] == "no_literal_match"
    assert result["recovery_preview"] == [{
        "kind": "page_prefix", "snippet_start": 0, "snippet_end": 1200,
        "snippet": text[:1200], "query_tokens": [],
    }]
    assert_preview(result, text)
    assert len(calls) == 1


def test_token_hint_beyond_open_preview_preserves_unicode_original_offsets_and_negation():
    text = "前缀🙂 " * 6000 + "The Straße option is NOT supported."
    client, calls = client_for(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    result = client.find(URL, "multiple STRASSE", expected_text_sha256=digest)

    assert result["matches"] == [] and result["total_matches"] == 0
    assert result["recovery_preview"][0]["snippet_start"] > 20_000
    assert "Straße option is NOT supported" in result["recovery_preview"][0]["snippet"]
    assert result["recovery_preview"][0]["query_tokens"] == ["strasse"]
    assert_preview(result, text)
    assert len(calls) == 1


def test_ranked_token_contexts_are_bounded_distinct_and_not_full_phrase_matches():
    text = ("alpha " + "x" * 500 + "alpha, then beta. " + "z" * 500 + "gamma " * 100).rstrip()
    client, _ = client_for(text)
    result = client.find(URL, "alpha beta gamma")

    assert result["matches"] == []
    assert set(result["recovery_preview"][0]["query_tokens"]) == {"alpha", "beta"}
    rows = result["recovery_preview"]
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            assert left["snippet_end"] <= right["snippet_start"] or right["snippet_end"] <= left["snippet_start"]
    assert_preview(result, text)


def test_cjk_runs_are_not_segmented_or_semantically_interpreted():
    text = "写者同时写入未获支持。"
    client, _ = client_for(text)
    result = client.find(URL, "并发写者")

    assert result["matches"] == []
    assert result["recovery_preview"][0]["kind"] == "page_prefix"
    assert result["recovery_preview"][0]["query_tokens"] == []
    assert_preview(result, text)


def test_offset_exhaustion_is_not_absence_and_does_not_repeat_recovery_preview():
    text = "ordinary body"
    client, _ = client_for(text)
    result = client.find(URL, "body", offset=100)

    assert result["matches"] == [] and result["total_matches"] == 1
    assert result["match_status"] == "offset_exhausted"
    assert result["recovery_preview"] == [] and result["next_offset"] is None
    assert_preview(result, text)


@pytest.mark.parametrize("offset", [1, 100])
def test_global_absence_at_positive_offset_is_not_offset_exhaustion(offset):
    text = "ordinary body"
    client, calls = client_for(text)
    result = client.find(URL, "missing", offset=offset)
    assert result["match_status"] == "no_literal_match"
    assert result["matches"] == [] and result["total_matches"] == 0
    assert result["recovery_preview"] == []
    assert result["complete"] is True and result["has_more"] is False
    assert result["next_offset"] is None
    assert_preview(result, text)
    assert len(calls) == 1


@pytest.mark.parametrize("offset", [0, 7])
def test_empty_extraction_never_invents_preview_or_semantic_evidence(offset):
    client, calls = client_for("")
    result = client.find(URL, "missing", offset=offset)
    assert result["match_status"] == "no_literal_match"
    assert result["total_chars"] == result["total_matches"] == 0
    assert result["matches"] == result["recovery_preview"] == []
    assert_preview(result, "")
    assert len(calls) == 1


def test_recovery_considers_only_four_bounded_query_terms_without_claiming_full_token_coverage(monkeypatch):
    from app.integrations import web_search

    query = "alpha bravo charlie delta echo foxtrot"
    text = ("alpha " * 100).rstrip()
    original = web_search._page_literal_matches
    patterns = []

    def record_patterns(source, folded_query):
        patterns.append(folded_query)
        yield from original(source, folded_query)

    monkeypatch.setattr(web_search, "_page_literal_matches", record_patterns)
    client, calls = client_for(text)
    result = client.find(URL, query)
    assert patterns == [query, "charlie", "foxtrot", "alpha", "bravo"]
    assert result["matches"] == [] and result["total_matches"] == 0
    assert_preview(result, text)
    assert len(calls) == 1


def test_real_phrase_matches_keep_original_match_pagination_and_no_recovery():
    text = "🙂 body " * 8
    client, _ = client_for(text)
    first = client.find(URL, "BODY", limit=5)
    second = client.find(URL, "BODY", offset=first["next_offset"], expected_text_sha256=first["text_sha256"])
    assert first["match_status"] == second["match_status"] == "phrase_matches"
    assert first["recovery_preview"] == second["recovery_preview"] == []
    assert len(first["matches"]) == 5 and len(second["matches"]) == 3
    assert first["total_matches"] is None and second["total_matches"] == 8
    assert first["next_offset"] == 5 and second["next_offset"] is None


def test_changed_expected_hash_refuses_recovery_after_only_one_fetch():
    client, calls = client_for("changed body")
    with pytest.raises(WebSearchError, match="changed"):
        client.find(URL, "missing", expected_text_sha256="0" * 64)
    assert len(calls) == 1


@pytest.mark.parametrize("url", ["https://127.0.0.1/private", "http://8.8.8.8/page"])
def test_recovery_never_bypasses_initial_ssrf_gate(url):
    client, calls = client_for("body")
    with pytest.raises(WebSearchError):
        client.find(url, "missing")
    assert calls == []


def test_redirect_and_byte_gates_still_refuse_before_recovery():
    redirect = PublicPageFetcher(transport=httpx.MockTransport(lambda _: httpx.Response(
        302, headers={"Location": "https://127.0.0.1/private"},
    )))
    with pytest.raises(WebSearchError):
        redirect.find(URL, "missing")
    bounded = PublicPageFetcher(max_bytes=5, transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"Content-Type": "text/plain"}, content=b"123456",
    )))
    with pytest.raises(WebSearchError, match="byte limit"):
        bounded.find(URL, "missing")


def test_real_executor_and_package_metadata_expose_recovery_without_verification_upgrade():
    text = "Only one writer may operate at a time."
    client, calls = client_for(text)
    tool = WebFindTool(client)
    registry = ToolRegistry()
    registry.register_package(WEB_PACKAGE)
    registry.register_tool(tool)

    executor = ToolExecutor(registry)
    result = executor.execute(
        invocation_id="find-recovery", tool_name=tool.spec.name,
        tool_input={"url": URL, "query": "single writer"}, context=ToolContext(session_id="isolated"),
    )

    assert result.status == "completed"
    assert executor.validate_output(tool_name=tool.spec.name, result=result) == []
    assert result.output["matches"] == []
    assert_preview(result.output, text)
    assert len(calls) == 1
    assert "no_literal_match" in tool.spec.output_schema["match_status"]["allowed_values"]
    assert any("nonsemantic" in hint and "no_literal_match" in hint for hint in WEB_PACKAGE.decision_hints)


@pytest.mark.parametrize("field,value", [
    ("match_status", "verified"),
    ("recovery_preview_scope", "semantic_match"),
    ("query_tokenization", "cjk_segmented"),
    ("kind", "phrase_match"),
    ("snippet_start", True),
    ("query_tokens", "writer"),
])
def test_executor_output_contract_rejects_invalid_recovery_labels_and_types(field, value):
    client, _ = client_for("Only one writer may operate at a time.")
    tool = WebFindTool(client)
    registry = ToolRegistry()
    registry.register_tool(tool)
    executor = ToolExecutor(registry)
    result = executor.execute(
        invocation_id="typed-recovery", tool_name=tool.spec.name,
        tool_input={"url": URL, "query": "single writer"}, context=ToolContext(session_id="isolated"),
    )
    assert executor.validate_output(tool_name=tool.spec.name, result=result) == []
    if field in {"kind", "snippet_start", "query_tokens"}:
        result.output["recovery_preview"][0][field] = value
    else:
        result.output[field] = value
    assert executor.validate_output(tool_name=tool.spec.name, result=result)


def test_actual_capability_api_keeps_readonly_catalog_and_runtime_find_recovery(tmp_path, monkeypatch):
    from app.api.main import create_app
    from app.api.routes.capabilities import list_capabilities
    from app.core.config import get_settings

    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    try:
        app = create_app()
        response = list_capabilities(SimpleNamespace(app=app))
        web = next(capability for capability in response.capabilities if capability.name == "web")
        assert web.read_only is True and web.requires_confirmation is False
        spec = app.state.runtime.tool_registry.get_tool("web.find").spec
        assert "no_literal_match" in spec.output_schema["match_status"]["allowed_values"]
        assert "nonsemantic" in spec.description
    finally:
        get_settings.cache_clear()
