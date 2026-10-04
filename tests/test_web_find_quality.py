"""Full-page literal location without larger prompts or search-provider calls."""

import hashlib
import json

import httpx
import pytest

from app.core.tools import ToolContext, ToolExecutor, ToolRegistry
from app.integrations.web_search import PublicPageFetcher, WebSearchError
from app.tool_packages.web import WebFindTool, register_web_tools
from tests.test_web_page_paging_quality import fetcher


def test_literal_beyond_open_preview_is_found_then_read_with_same_revision():
    text = "正文🙂 " * 6000 + "Default option is disabled. End."
    client = fetcher(f"<main>{text}</main>")
    first = client.open("https://8.8.8.8/page")
    assert "Default option" not in first["text"]
    result = client.find("https://8.8.8.8/page", "default OPTION",
                         expected_text_sha256=first["text_sha256"])
    assert result["total_chars"] == len(text) and result["complete"] is True
    assert result["total_matches"] == 1
    hit = result["matches"][0]
    assert text[hit["match_start"]:hit["match_end"]] == "Default option"
    page = client.open("https://8.8.8.8/page", offset=hit["snippet_start"],
                       expected_text_sha256=result["text_sha256"])
    assert "Default option is disabled" in page["text"]


def test_unicode_casefold_and_paging_use_original_character_and_match_offsets():
    text = "前缀🙂 Straße " * 8
    client = fetcher(f"<main>{text}</main>")
    first = client.find("https://8.8.8.8/page", "STRASSE", limit=5)
    second = client.find("https://8.8.8.8/page", "STRASSE", offset=first["next_offset"],
                         expected_text_sha256=first["text_sha256"])
    assert first["has_more"] is True and first["complete"] is False
    assert first["total_matches"] is None and first["next_offset"] == 5
    assert second["has_more"] is False and second["total_matches"] == 8
    matches = first["matches"] + second["matches"]
    assert len({(h["match_start"], h["match_end"]) for h in matches}) == 8
    assert all(text[h["match_start"]:h["match_end"]] == "Straße" for h in matches)
    assert first["offset_unit"] == "matches" and second["offset"] == 5


def test_absent_query_and_past_end_do_not_invent_matches_or_current_state():
    client = fetcher("<main>ordinary body</main>")
    empty = client.find("https://8.8.8.8/page", "not found")
    assert empty["matches"] == [] and empty["total_matches"] == 0
    assert empty["complete"] is True and empty["next_offset"] is None
    beyond = client.find("https://8.8.8.8/page", "body", offset=100)
    assert beyond["matches"] == [] and beyond["total_matches"] == 1
    assert "unchanged" not in beyond and "verified" not in beyond


@pytest.mark.parametrize("kwargs", [
    {"query": None}, {"query": ""}, {"query": " "}, {"query": "x" * 201},
    {"offset": True}, {"offset": -1}, {"offset": 0.5},
    {"limit": True}, {"limit": 0}, {"limit": 6}, {"expected_text_sha256": "bad"},
])
def test_bad_find_input_rejected_before_network(kwargs):
    calls = []
    client = PublicPageFetcher(transport=httpx.MockTransport(lambda request: calls.append(request)))
    with pytest.raises(WebSearchError):
        client.find("https://8.8.8.8/page", **{"query": "ordinary", **kwargs})
    assert calls == []


def test_find_uses_identical_redirect_byte_and_revision_guards():
    changed = fetcher("<main>changed</main>")
    with pytest.raises(WebSearchError, match="changed"):
        changed.find("https://8.8.8.8/page", "changed",
                     expected_text_sha256=hashlib.sha256(b"original").hexdigest())
    redirect = PublicPageFetcher(transport=httpx.MockTransport(lambda request: httpx.Response(
        302, headers={"Location": "https://127.0.0.1/private"})))
    with pytest.raises(WebSearchError):
        redirect.find("https://8.8.8.8/page", "data")
    oversized = PublicPageFetcher(max_bytes=5, transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"Content-Type": "text/plain"}, content=b"123456")))
    with pytest.raises(WebSearchError, match="byte limit"):
        oversized.find("https://8.8.8.8/page", "data")


def test_registered_find_is_readonly_bounded_and_query_never_leaves_process():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/plain"},
                              text=("context " * 100 + "PRIVATE-LOCAL-QUERY ") * 10)

    registry = ToolRegistry()
    register_web_tools(registry, api_key=None)
    assert registry.get_tool("web.find").spec.read_only is True
    registry.register_tool(WebFindTool(PublicPageFetcher(transport=httpx.MockTransport(transport))))
    executor = ToolExecutor(registry)
    result = executor.execute(invocation_id="find", tool_name="web.find",
        tool_input={"url": "https://8.8.8.8/page", "query": "PRIVATE-LOCAL-QUERY"},
        context=ToolContext(session_id="s"))
    assert result.status == "completed" and executor.validate_output(tool_name="web.find", result=result) == []
    assert len(json.dumps(result.output, ensure_ascii=False)) < 5500
    assert len(requests) == 1 and str(requests[0].url) == "https://8.8.8.8/page"
    assert "PRIVATE-LOCAL-QUERY" not in str(requests[0].headers) and not requests[0].content


def test_find_obeys_existing_source_constraint_before_any_network_request():
    requests = []
    registry = ToolRegistry()
    registry.register_effect_constraint("restricted_source", blocked_domains=(), block_unrestricted=True)
    registry.register_tool(WebFindTool(PublicPageFetcher(transport=httpx.MockTransport(
        lambda request: requests.append(request)))))
    executor = ToolExecutor(registry)
    executor.constraint_store.add([("session", "restricted")], {"restricted_source"})
    result = executor.execute(invocation_id="find", tool_name="web.find",
        tool_input={"url": "https://8.8.8.8/page", "query": "body"},
        context=ToolContext(session_id="restricted", safety_review_approved=True))
    assert result.status == "rejected" and requests == []
