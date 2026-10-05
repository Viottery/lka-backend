"""Recover bounded text beyond the first page without claiming a stable fetch."""

import hashlib

import httpx
import pytest

from app.core.tools import ToolContext, ToolExecutor, ToolInvocation, ToolRegistry, ToolResult
from app.integrations.web_search import PublicPageFetcher, WebSearchError
from app.tool_packages.web import WebOpenTool


def fetcher(html):
    return PublicPageFetcher(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"Content-Type": "text/html; charset=utf-8"}, text=html,
    )))


def test_html_tail_after_twenty_thousand_chars_is_recoverable_and_unicode_exact():
    body = "正文🙂é" * 5500 + "关键尾部：默认关闭，组件必须声明兼容。"
    client = fetcher(f"<html><main><p>{body}</p></main></html>")
    first = client.open("https://8.8.8.8/page")
    assert len(first["text"]) == 20_000 and first["truncated"] is True
    assert first["total_chars"] == len(body)
    second = client.open("https://8.8.8.8/page", offset=first["next_offset"],
                         max_chars=20_000, expected_text_sha256=first["text_sha256"])
    assert first["text"] + second["text"] == body
    assert "关键尾部" in second["text"]
    assert second["has_more"] is False and second["next_offset"] is None
    assert second["truncated"] is True  # This slice still omits the prefix.
    assert second["snapshot_stable"] is False
    assert second["text_sha256"] == hashlib.sha256(body.encode()).hexdigest()


def test_changed_extraction_is_refused_when_expected_fingerprint_is_supplied():
    original = fetcher("<main>original body</main>").open("https://8.8.8.8/page")
    changed = fetcher("<main>changed body</main>")
    with pytest.raises(WebSearchError, match="changed"):
        changed.open("https://8.8.8.8/page", offset=4,
                     expected_text_sha256=original["text_sha256"])
    fresh = changed.open("https://8.8.8.8/page", offset=4)
    assert fresh["text_sha256"] != original["text_sha256"]
    assert fresh["snapshot_stable"] is False


@pytest.mark.parametrize("kwargs", [
    {"offset": -1}, {"offset": True}, {"offset": 1.5},
    {"max_chars": 0}, {"max_chars": True}, {"max_chars": 20_001},
    {"expected_text_sha256": "bad"}, {"expected_text_sha256": 42},
])
def test_bad_paging_arguments_fail_before_http(kwargs):
    def unexpected(request):
        raise AssertionError("invalid input must not fetch")
    client = PublicPageFetcher(transport=httpx.MockTransport(unexpected))
    with pytest.raises(WebSearchError):
        client.open("https://8.8.8.8/page", **kwargs)


def test_offset_beyond_end_is_terminal_and_custom_output_cap_is_preserved():
    client = fetcher("<main>abc🙂def</main>")
    client.max_text_chars = 3
    first = client.open("https://8.8.8.8/page")
    assert first["text"] == "abc" and first["next_offset"] == 3
    final = client.open("https://8.8.8.8/page", offset=100)
    assert final["text"] == "" and final["returned_chars"] == 0
    assert final["has_more"] is False and final["next_offset"] is None
    with pytest.raises(WebSearchError):
        client.open("https://8.8.8.8/page", max_chars=4)


def test_tool_schema_and_invocation_forward_paging_without_coercion():
    tool = WebOpenTool(fetcher("<main>abcdef</main>"))
    properties = tool.spec.input_schema["properties"]
    assert properties["offset"]["minimum"] == 0
    assert properties["max_chars"]["maximum"] == 20_000
    invocation = ToolInvocation(invocation_id="page", tool=tool.spec, session_id="s",
                                context_id="c", input={"url": "https://8.8.8.8/page",
                                                       "offset": 3, "max_chars": 2})
    result = tool.invoke(invocation=invocation, context=ToolContext(session_id="s"))
    assert result.status == "completed"
    assert result.output["text"] == "de" and result.output["next_offset"] == 5
    invocation.input["offset"] = True
    assert tool.invoke(invocation=invocation, context=ToolContext(session_id="s")).status == "failed"
    invocation.input.update(offset=0, expected_text_sha256="0" * 64)
    refused = tool.invoke(invocation=invocation, context=ToolContext(session_id="s"))
    assert refused.status == "failed" and "changed" in refused.error


def test_paging_never_bypasses_private_redirect_or_full_response_byte_limit():
    private_redirect = PublicPageFetcher(transport=httpx.MockTransport(lambda request: httpx.Response(
        302, headers={"Location": "https://127.0.0.1/private"},
    )))
    with pytest.raises(WebSearchError):
        private_redirect.open("https://8.8.8.8/page", offset=20_000, max_chars=1)
    bounded = PublicPageFetcher(max_bytes=5, transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"Content-Type": "text/plain"}, content=b"123456",
    )))
    with pytest.raises(WebSearchError, match="byte limit"):
        bounded.open("https://8.8.8.8/page", offset=20_000, max_chars=1)


@pytest.mark.parametrize("field,bad_value", [
    ("next_offset", "20"), ("next_offset", True), ("next_offset", -1),
    ("text_sha256", 42), ("text_sha256", []),
    ("snapshot_stable", 1), ("snapshot_stable", "false"),
    ("text_scope", "whole_source"), ("text_scope", False),
])
def test_actual_executor_rejects_mutated_paging_output_fields(field, bad_value):
    tool = WebOpenTool(fetcher("<main>abc</main>"))
    registry = ToolRegistry()
    registry.register_tool(tool)
    executor = ToolExecutor(registry)
    output = tool.fetcher.open("https://8.8.8.8/page")
    result = ToolResult(invocation_id="validated", tool_name="web.open",
                        status="completed", output=output)
    assert executor.validate_output(tool_name="web.open", result=result) == []
    result.output = {**output, field: bad_value}
    errors = executor.validate_output(tool_name="web.open", result=result)
    assert errors and any(field in error for error in errors)
