from __future__ import annotations

import httpx
import pytest

from app.core.config import get_settings
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry
from app.integrations.web_search import (
    BraveSearchAdapter,
    BraveSearchQuota,
    PublicPageFetcher,
    WebSearchError,
    _PinnedHTTPSConnection,
    _public_address,
    _safe_public_https_url,
)
from app.storage.db import connect, get_db_path
from app.tool_packages.web import WEB_PACKAGE, WebOpenTool, WebSearchTool


def test_brave_search_normalizes_web_results_and_provenance():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/web/search")
        assert request.headers["X-Subscription-Token"] == "test-key"
        assert request.url.params["count"] == "2"
        return httpx.Response(200, json={"web": {"results": [
            {"title": "  A result ", "url": "https://8.8.8.8/story", "description": "A  snippet", "page_age": "2025-01"},
            {"title": "B", "url": "https://1.1.1.1/b"},
            {"title": "C", "url": "https://9.9.9.9/c"},
        ]}})

    adapter = BraveSearchAdapter("test-key", transport=httpx.MockTransport(respond))
    result = adapter.search(" test query ", limit=2)
    assert result["query"] == "test query"
    assert result["result_count"] == 2
    assert result["results"][0] == {
        "title": "A result", "url": "https://8.8.8.8/story", "snippet": "A snippet",
        "published_at": "2025-01", "provider_fetched_at": None,
        "source": {"provider": "Brave Search", "mode": "web"},
    }


def test_brave_news_mode_and_missing_key_error():
    called = []

    def respond(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        return httpx.Response(200, json={"results": [
            {"title": "News", "url": "https://8.8.8.8/news", "description": "brief", "page_age": "2026-09-30"}
        ]})

    result = BraveSearchAdapter("key", transport=httpx.MockTransport(respond)).search("event", mode="news")
    assert called == ["/res/v1/news/search"]
    assert result["results"][0]["published_at"] == "2026-09-30"
    assert result["results"][0]["source"]["mode"] == "news"
    with pytest.raises(WebSearchError, match="no Brave Search API key"):
        BraveSearchAdapter(None).search("event")


@pytest.mark.parametrize("url", [
    "http://8.8.8.8/", "https://localhost/", "https://127.0.0.1/",
    "https://10.0.0.1/", "https://169.254.169.254/latest/meta-data/",
    "https://192.0.2.1/", "https://[::1]/", "https://[fc00::1]/",
    "https://user:pass@8.8.8.8/", "https://8.8.8.8:8443/",
])
def test_page_fetch_rejects_unsafe_or_ambiguous_hosts(url):
    fetcher = PublicPageFetcher(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    with pytest.raises(WebSearchError):
        fetcher.open(url)


def test_page_fetch_revalidates_redirect_and_strips_active_markup():
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})
        return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"},
                              text="<h1>Hi</h1><script>secret()</script><p>Readable</p>")

    fetcher = PublicPageFetcher(transport=httpx.MockTransport(respond))
    with pytest.raises(WebSearchError):
        fetcher.open("https://8.8.8.8/start")

    page = PublicPageFetcher(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"Content-Type": "text/html"},
            text="<h1>Hi</h1><script>secret()</script><style>hidden</style><p>Readable</p>",
        )),
    ).open("https://8.8.8.8/page")
    assert page["url"] == "https://8.8.8.8/page"
    assert page["fetched_at"]
    assert "Hi" in page["text"] and "Readable" in page["text"]
    assert "secret" not in page["text"] and "hidden" not in page["text"]


def test_page_fetch_enforces_byte_limit():
    fetcher = PublicPageFetcher(
        max_bytes=5,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"Content-Type": "text/plain"}, content=b"0123456789",
        )),
    )
    with pytest.raises(WebSearchError, match="byte limit"):
        fetcher.open("https://8.8.8.8/page")


def test_public_hostname_resolution_rejects_mixed_private_answers(monkeypatch):
    def addresses(host, port, *, type):
        assert host == "example.org" and port == 443
        return [
            (2, type, 6, "", ("8.8.8.8", 443)),
            (2, type, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr("app.integrations.web_search.socket.getaddrinfo", addresses)
    assert _safe_public_https_url("https://Example.Org/path") == "https://example.org/path"
    with pytest.raises(WebSearchError, match="non-public"):
        _public_address("example.org")


def test_page_connection_pins_address_and_verifies_original_hostname(monkeypatch):
    calls = []
    raw_socket = object()

    def connect(address, *, timeout):
        calls.append(("connect", address, timeout))
        return raw_socket

    class TLSContext:
        def wrap_socket(self, raw, *, server_hostname):
            calls.append(("tls", raw, server_hostname))
            return object()

    monkeypatch.setattr("app.integrations.web_search.socket.create_connection", connect)
    connection = _PinnedHTTPSConnection("example.org", "8.8.8.8", 4.0)
    connection._context = TLSContext()
    connection.connect()
    assert calls == [
        ("connect", ("8.8.8.8", 443), 4.0),
        ("tls", raw_socket, "example.org"),
    ]


def test_web_tools_follow_tool_executor_contracts_and_report_provider_unavailable():
    registry = ToolRegistry()
    registry.register_package(WEB_PACKAGE)
    registry.register_tool(WebSearchTool(BraveSearchAdapter(None)))
    registry.register_tool(WebOpenTool(PublicPageFetcher()))
    executor = ToolExecutor(registry)
    context = ToolContext(session_id="web-test")
    assert all(tool.read_only is True for tool in registry.list_tools(package="web"))

    unavailable = executor.execute(
        invocation_id="missing-key", tool_name="web.search", tool_input={"query": "news"}, context=context,
    )
    assert unavailable.status == "failed"
    assert "no Brave Search API key" in unavailable.error
    invalid = executor.execute(
        invocation_id="bad-mode", tool_name="web.search",
        tool_input={"query": "news", "mode": "all"}, context=context,
    )
    assert invalid.status == "rejected"
    assert "validation_errors" in invalid.output


def test_runtime_registers_web_tools_without_search_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    get_settings.cache_clear()
    from app.api.main import create_app

    runtime = create_app().state.runtime
    assert {tool.name for tool in runtime.tool_registry.list_tools(package="web")} == {
        "web.search", "web.open",
    }
    assert all(tool.read_only for tool in runtime.tool_registry.list_tools(package="web"))


def test_search_freshness_and_bad_provider_payload():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["freshness"] == "pw"
        assert request.url.params["country"] == "DE"
        assert request.url.params["search_lang"] == "de"
        return httpx.Response(200, json={"unexpected": []})

    adapter = BraveSearchAdapter("key", transport=httpx.MockTransport(respond))
    with pytest.raises(WebSearchError, match="invalid results payload"):
        adapter.search("tickets", mode="news", freshness="pw", country="DE", search_lang="de")


def test_monthly_search_quota_persists_and_blocks_before_network(tmp_path):
    db_path = get_db_path(tmp_path / "data")
    quota = BraveSearchQuota(lambda: connect(db_path), monthly_limit=1)
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"web": {"results": []}})

    adapter = BraveSearchAdapter("test-key", transport=httpx.MockTransport(respond), quota=quota)
    assert adapter.search("first")["result_count"] == 0
    with pytest.raises(WebSearchError, match="Monthly web search request limit"):
        adapter.search("second")
    assert calls == ["/res/v1/web/search"]
    with pytest.raises(WebSearchError, match="Monthly web search request limit"):
        BraveSearchQuota(lambda: connect(db_path), monthly_limit=1).reserve()


def test_search_results_drop_unsafe_provider_urls():
    adapter = BraveSearchAdapter(
        "key", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "web": {"results": [
                {"title": "private", "url": "https://127.0.0.1/private"},
                {"title": "http", "url": "http://example.org/article"},
                {"title": "public", "url": "https://example.org/article"},
            ]},
        })),
    )
    result = adapter.search("article", limit=3)
    assert [item["url"] for item in result["results"]] == ["https://example.org/article"]
