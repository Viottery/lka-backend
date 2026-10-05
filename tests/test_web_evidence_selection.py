from __future__ import annotations

import hashlib

import httpx

from app.domains.web_views import build_page_view
from app.integrations.web_search import PublicPageFetcher


def page(text: str) -> dict:
    return {"text": text, "url": "https://8.8.8.8/offline", "fetched_at": "offline"}


def test_discriminative_query_term_outranks_repeated_broad_word_sections():
    body = "\n\n".join(
        [f"Access policy guidance: access policy details for section {i}." for i in range(30)]
        + ["Access policy is suspended for audited accounts after review."]
    )
    result = build_page_view(page(body), query="access policy suspended", max_chars=1600)
    target = body.index("Access policy is suspended")
    assert "suspended for audited accounts" in result["text"]
    assert result["offset"] <= target < result["offset"] + result["returned_chars"]
    assert len(result["text"]) <= 800


def test_generic_word_frequency_does_not_outscore_a_rare_qualification():
    body = "\n\n".join(
        [f"Eligible applicants may proceed. Eligible status applies in section {i}." for i in range(24)]
        + ["Eligible only if the independent audit is complete."]
    )
    result = build_page_view(page(body), query="eligible independent audit", max_chars=1800)
    assert "independent audit is complete" in result["text"]
    assert result["offset"] > 0


def test_html_tables_keep_row_and_cell_boundaries_in_readable_text():
    source = """<main><table>
      <tr><th>Region</th><th>Limit</th></tr>
      <tr><td>North</td><td>12</td></tr>
      <tr><td>South</td><td>4</td></tr>
    </table></main>"""
    fetcher = PublicPageFetcher(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"Content-Type": "text/html; charset=utf-8"}, text=source,
    )))
    extracted = fetcher.read("https://8.8.8.8/table")["text"]
    rows = [line.strip() for line in extracted.splitlines() if line.strip()]
    assert rows == ["Region | Limit", "North | 12", "South | 4"]
    assert "RegionLimitNorth12" not in extracted
    assert fetcher.read("https://8.8.8.8/table")["extraction_version"] == "readable-html-v3"


def test_table_extraction_digest_and_offsets_address_canonical_rows():
    source = "<main><table><tr><td>Region</td><td>Limit</td></tr><tr><td>North</td><td>12</td></tr></table></main>"
    fetcher = PublicPageFetcher(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"Content-Type": "text/html"}, text=source,
    )))
    page_result = fetcher.read("https://8.8.8.8/digest")
    expected = "Region | Limit\nNorth | 12"
    assert page_result["text"] == expected
    assert page_result["text_sha256"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert page_result["text"][page_result["text"].index("12"):].startswith("12")


def test_unicode_literal_offsets_and_combined_view_budget_remain_exact():
    body = ("前置内容🙂。\n\n" * 400) + ("普通说明。" * 500) + "稀有条件：仅在e\u0301状态。"
    result = build_page_view(page(body), query="稀有条件", max_chars=900)
    assert "稀有条件" in result["text"]
    assert result["offset"] <= body.index("稀有条件") < result["offset"] + result["returned_chars"]
    assert result["text"] == body[result["offset"]:result["offset"] + result["returned_chars"]]
    leaves = result.get("excerpts", [])
    assert all(body[item["snippet_start"]:item["snippet_end"]] == item["snippet"] for item in leaves)
    assert len(result["text"]) + sum(len(item["snippet"]) for item in leaves) <= 900
    assert all(len(item["snippet"]) <= 1200 for item in leaves)


def test_regular_html_block_separation_remains_unchanged():
    source = "<main><h1>Title</h1><p>First paragraph.</p><p>Second paragraph.</p></main>"
    fetcher = PublicPageFetcher(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"Content-Type": "text/html"}, text=source,
    )))
    result = fetcher.read("https://8.8.8.8/ordinary")
    assert result["text"] == "Title\n\nFirst paragraph.\n\nSecond paragraph."
    assert result["extraction_version"] == "readable-html-v3"
