from __future__ import annotations

import hashlib

import pytest

from app.domains.web_views import build_page_view, build_search_view


def page(text: str) -> dict:
    return {"url": "https://example.test/a", "fetched_at": "now", "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "extra": {"keep": True}}


def test_short_page_preserves_metadata_and_returns_bounded_original_text():
    source = page("# Heading\n\nA readable paragraph.")
    result = build_page_view(source)
    assert result["url"] == source["url"] and result["text_sha256"] == source["text_sha256"]
    assert result["extra"] == source["extra"] and "text" in result
    assert result["text"] == source["text"]
    assert result["coverage_scope"] == "selected_readable_excerpts_not_full_source"
    assert result["outline"][0]["heuristic"] is True
    assert source["text"] == "# Heading\n\nA readable paragraph."


def test_page_mode_and_auto_offset_are_contiguous_and_unicode_character_addressed():
    body = "甲🙂e\u0301乙" * 1000
    data = page(body)
    first = build_page_view(data, view="page", max_chars=300)
    second = build_page_view(data, offset=first["next_offset"], max_chars=300)
    assert first["text"] == body[:300]
    assert second["text"] == body[300:600]
    assert first["returned_chars"] == 300 and first["has_more"]
    assert first["next_offset"] == 300 and second["offset"] == 300
    assert len(first["text"]) == first["returned_chars"]
    assert "text" not in {k for k in first if k != "text"}


def test_query_finds_tail_after_twenty_thousand_chars_without_full_text_return():
    body = "introductory material\n\n" + ("unrelated filler.\n\n" * 1800)
    body += "A rare target appears here.\n\nIts exception applies only when enabled.\n\nLater material."
    result = build_page_view(page(body), query="rare target", max_chars=2000)
    assert "rare target" in result["text"] and "exception" in result["text"]
    assert result["offset"] > 20_000
    assert result["text"] == body[result["offset"]:result["offset"] + result["returned_chars"]]
    assert result["has_more"] and result["next_offset"] == result["offset"] + result["returned_chars"]


def test_query_selects_adjacent_qualification_and_no_match_falls_back_honestly():
    body = "General intro.\n\nOnly when the flag is enabled.\n\nThe feature supports export.\n\nEnd."
    found = build_page_view(page(body), query="feature supports export")
    assert "Only when the flag is enabled" in found["text"]
    fallback = build_page_view(page(body), query="absent term")
    assert fallback["text"].startswith("General intro.")
    assert fallback["coverage_scope"] == "selected_readable_excerpts_not_full_source"


def test_overview_budgets_text_and_excerpt_leaves_and_marks_bounded_context():
    body = "prefix\n\n" + ("qualification " + "x" * 1800 + "\n\n") + "target " + "y" * 5000 + "\n\nend"
    result = build_page_view(page(body), query="target", max_chars=2400)
    assert len(result["text"]) + sum(len(x["snippet"]) for x in result["excerpts"]) <= 2400
    assert all(len(x["snippet"]) <= 1200 for x in result["excerpts"])
    assert all(body[x["snippet_start"]:x["snippet_end"]] == x["snippet"] for x in result["excerpts"])
    assert result["omitted_context"]
    assert all(x["start"] < x["end"] for x in result["omitted_context"])
    with pytest.raises(ValueError):
        build_page_view(page(body), max_chars=20_001)


def test_long_paragraph_hit_is_anchored_at_tail_and_supports_cjk_literal():
    body = ("普通内容。" * 800) + "稀有关键字在此。" + ("后续内容。" * 500)
    result = build_page_view(page(body), query="稀有关键字", max_chars=2400)
    assert "稀有关键字" in result["text"]
    assert result["offset"] > 1000
    assert body[result["offset"]:result["offset"] + result["returned_chars"]] == result["text"]
    assert len(result["text"]) <= 1200


def test_short_adjacent_condition_survives_long_match_paragraph_trimming():
    body = "仅当管理员开启时。\n\n" + ("普通段落。" * 500) + "目标关键词位于这里。" + ("补充说明。" * 500)
    result = build_page_view(page(body), query="目标关键词", max_chars=2400)
    assert "目标关键词" in result["text"]
    assert len(result["text"]) + sum(len(item["snippet"]) for item in result["excerpts"]) <= 2400
    assert any("仅当管理员开启时" in item["snippet"] for item in result["excerpts"])
    assert result["context_start"] == 0
    assert result["context_end"] > len(result["text"])
    assert result["context_complete"] is False


def test_second_distinct_hit_is_returned_as_nonduplicated_original_excerpt():
    body = "Target alpha is described here.\n\n" + ("background detail.\n\n" * 20)
    body += "Target beta has a separate result.\n\nConclusion."
    result = build_page_view(page(body), query="target", max_chars=2400)
    assert len(result["excerpts"]) == 1
    excerpt = result["excerpts"][0]
    assert "Target beta" in excerpt["snippet"]
    assert not (result["offset"] < excerpt["snippet_end"] and excerpt["snippet_start"] < result["offset"] + result["returned_chars"])
    assert body[excerpt["snippet_start"]:excerpt["snippet_end"]] == excerpt["snippet"]
    assert len(result["text"]) <= 1200 and len(excerpt["snippet"]) <= 1200
    assert len(result["text"]) + len(excerpt["snippet"]) <= 2400


def test_supplied_outline_is_preserved_and_bounded_with_omitted_count():
    outline = [{"text": f"heading {i}", "start": i, "end": i + 1} for i in range(15)]
    data = page("Readable text")
    data["outline"] = outline
    result = build_page_view(data)
    assert result["outline"] == outline[:12]
    assert result["outline_omitted_count"] == 3


@pytest.mark.parametrize(("prefix", "query"), [("ß", "target"), ("İ", "needle")])
def test_casefold_expansion_does_not_shift_source_offsets(prefix, query):
    body = prefix + ("ordinary text " * 400) + query + " appears here."
    result = build_page_view(page(body), query=query, max_chars=2400)
    assert query in result["text"]
    assert body[result["offset"]:result["offset"] + result["returned_chars"]] == result["text"]
    assert result["offset"] <= body.index(query) < result["offset"] + result["returned_chars"]


def test_no_lexical_match_reports_fallback_and_omitted_tail():
    body = "Prefix fallback.\n\n" + ("source material.\n\n" * 100)
    result = build_page_view(page(body), query="missing phrase", max_chars=80)
    assert result["query_status"] == "no_lexical_match"
    assert result["text"] == body[:40]
    assert result["context_start"] == 0 and result["context_end"] == len(body)
    assert result["context_complete"] is False
    assert result["omitted_context"] == [{"start": 40, "end": len(body), "reason": "bounded_context"}]


def test_long_match_keeps_short_following_condition_as_separate_excerpt():
    body = ("Ordinary material. " * 500) + "target appears in this long paragraph.\n\n"
    body += "Only when the account is active."
    result = build_page_view(page(body), query="target", max_chars=2400)
    assert "target" in result["text"]
    assert any("Only when the account is active" in item["snippet"] for item in result["excerpts"])
    assert all(body[item["snippet_start"]:item["snippet_end"]] == item["snippet"]
               for item in result["excerpts"])
    assert len(result["text"]) + sum(len(item["snippet"]) for item in result["excerpts"]) <= 2400


def test_page_mode_also_bounds_supplied_outline():
    headings = [{"text": f"heading {i}", "start": i, "end": i + 1} for i in range(15)]
    data = page("x" * 100)
    data["outline"] = headings
    result = build_page_view(data, view="page", max_chars=10)
    assert result["outline"] == headings[:12]
    assert result["outline_omitted_count"] == 3


def test_search_view_is_fair_preserves_rows_and_does_not_mutate_payload():
    payload = {"query": "q", "results": [
        {"title": "A", "url": "https://a.test", "published_at": "t1", "ref_id": "r1", "snippet": "甲🙂" * 300},
        {"title": "B", "url": "https://b.test", "published_at": "t2", "ref_id": "r2", "snippet": "short"},
    ], "result_count": 2, "possible_more": True}
    result = build_search_view(payload, snippet_chars=10)
    assert len(result["results"]) == 2 and result["result_count"] == 2
    assert result["results"][0]["snippet"] == "甲🙂" * 5
    assert result["results"][0]["snippet_truncated"] is True
    assert result["results"][0]["snippet_total_chars"] == 600
    assert result["results"][1]["snippet_truncated"] is False
    assert result["results"][0]["url"] == payload["results"][0]["url"]
    assert len(payload["results"][0]["snippet"]) == 600


def test_search_titles_are_bounded_with_original_ref_ids_preserved():
    row = {"title": "長" * 250, "url": "https://example.test", "ref_id": "stable-ref", "snippet": "s"}
    result = build_search_view({"results": [row]}, snippet_chars=5)
    viewed = result["results"][0]
    assert len(viewed["title"]) == 200 and viewed["title_truncated"]
    assert viewed["ref_id"] == "stable-ref"
