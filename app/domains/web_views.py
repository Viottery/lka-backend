"""Deterministic, bounded views over normalized web search and page results."""

from __future__ import annotations

import re
from bisect import bisect_left
from copy import deepcopy
from math import log, log1p

_MAX_VIEW_CHARS = 20_000
_MAX_LEAF_CHARS = 1_200
_MAX_EXCERPTS = 3
_OVERVIEW_PRIMARY_CHARS = 1_200


def _paragraphs(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", text, re.DOTALL)]


def _tokens(query: str) -> list[str]:
    # Retain CJK runs as literal tokens as well as ordinary word runs.
    return list(dict.fromkeys(re.findall(r"[\w\u3400-\u9fff]+", query.casefold())))


def _page_excerpt(text: str, start: int, end: int, *, context_start: int, context_end: int) -> dict:
    return {
        "snippet": text[start:end], "snippet_start": start, "snippet_end": end,
        "context_start": context_start, "context_end": context_end,
        "context_complete": start <= context_start and end >= context_end,
    }


def _query_hits(source: str, paragraphs: list[tuple[int, int]], terms: list[str]) -> list[dict]:
    folded_parts = []
    folded_offsets = []
    for source_index, char in enumerate(source):
        folded_char = char.casefold()
        folded_parts.append(folded_char)
        folded_offsets.extend([source_index] * len(folded_char))
    folded = "".join(folded_parts)
    paragraph_texts = [
        folded[bisect_left(folded_offsets, start):bisect_left(folded_offsets, end)]
        for start, end in paragraphs
    ]
    document_frequency = {
        term: sum(term in paragraph for paragraph in paragraph_texts)
        for term in terms
    }
    term_weight = {
        term: 1.0 + log((len(paragraphs) + 1) / (frequency + 1))
        for term, frequency in document_frequency.items()
    }
    hits = []
    for index, ((start, end), paragraph) in enumerate(zip(paragraphs, paragraph_texts)):
        score = 0.0
        positions = []
        for term in terms:
            frequency = paragraph.count(term)
            if not frequency:
                continue
            score += term_weight[term] * (1.0 + min(0.35, log1p(frequency - 1) * 0.12))
            folded_position = folded.find(term, bisect_left(folded_offsets, start),
                                          bisect_left(folded_offsets, end))
            if folded_position >= 0:
                positions.append((term_weight[term], folded_offsets[folded_position]))
        if score:
            # Prefer anchoring on the most discriminative matched query term.
            anchor = max(positions, default=(0.0, start))[1]
            hits.append({"index": index, "start": start, "end": end, "score": score,
                         "hit": anchor, "exact_label": any(term == paragraph.strip() for term in terms)})
    return sorted(hits, key=lambda hit: (-hit["score"], hit["index"]))


def _context_window(source: str, paragraphs: list[tuple[int, int]], hit: dict,
                    limit: int, headings: list[dict] | None = None) -> tuple[int, int, int, int]:
    index = hit["index"]
    paragraph_start, paragraph_end = paragraphs[index]
    label = source[paragraph_start:paragraph_end].strip()
    first_line = label.splitlines()[0]
    # Heading identity comes from extraction, not task/domain keywords. A short
    # exact block can also be a section label; longer prose retains neighbors.
    is_heading = any(isinstance(item, dict) and isinstance(item.get("text"), str)
                     and item["text"].strip() == first_line
                     and type(item.get("start")) is int
                     and paragraph_start <= item["start"] < paragraph_end
                     for item in headings or [])
    is_heading = is_heading or (label == first_line and len(label) <= 100
                                and not re.search(r"[。！？.!?:：]", label)
                                and hit.get("exact_label") is True)
    if is_heading and index + 1 < len(paragraphs):
        context_start = paragraph_start
        context_end = paragraphs[min(len(paragraphs) - 1, index + 3)][1]
        following = [item["start"] for item in headings or []
                     if isinstance(item, dict) and type(item.get("start")) is int
                     and paragraph_end < item["start"] < context_end]
        if following:
            context_end = min(following)
        return context_start, min(context_end, context_start + limit), context_start, context_end
    context_start = paragraphs[max(0, index - 1)][0]
    context_end = paragraphs[min(len(paragraphs) - 1, index + 1)][1]
    if context_end - context_start <= limit:
        return context_start, context_end, context_start, context_end
    # The requested adjacent context remains explicit, while returned text stays
    # a bounded contiguous window centered on the source occurrence.
    hit_end = min(paragraph_end, hit["hit"] + limit)
    start = max(paragraph_start, hit_end - limit)
    return start, hit_end, context_start, context_end


def build_page_view(
    page: dict, *, view: str = "auto", query: str | None = None,
    offset: int = 0, max_chars: int = 2400,
) -> dict:
    """Return bounded source text plus navigation metadata, never the full page."""
    if view not in {"auto", "overview", "page"}:
        raise ValueError("view must be auto, overview, or page")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= _MAX_VIEW_CHARS:
        raise ValueError("max_chars must be between 1 and 20000")
    source = page.get("text", "")
    if not isinstance(source, str):
        raise TypeError("page text must be a string")
    result = {key: deepcopy(value) for key, value in page.items() if key != "text"}
    total = len(source)
    chosen_view = "page" if view == "page" or (view == "auto" and offset > 0) else "overview"
    result["view_mode"] = chosen_view
    excerpts: list[dict] = []
    outline: list[dict] = []

    if chosen_view == "page":
        start = min(offset, total)
        end = min(start + max_chars, total)
        selected = source[start:end]
        supplied_outline = page.get("outline")
        if isinstance(supplied_outline, list):
            result["outline"] = deepcopy(supplied_outline[:12])
            result["outline_omitted_count"] = max(0, len(supplied_outline) - 12)
        result.update(text=selected, offset=start, returned_chars=len(selected),
                      has_more=end < total, next_offset=end if end < total else None,
                      query_status="not_applied_page" if query else "not_requested")
        excerpts = []
    else:
        paras = _paragraphs(source)
        start = end = 0
        primary_context_start = primary_context_end = 0
        selected = ""
        excerpt_items: list[dict] = []
        hits = []
        if query and query.strip() and paras:
            hits = _query_hits(source, paras, _tokens(query))
            primary_limit = min(_OVERVIEW_PRIMARY_CHARS, max(1, max_chars // 2))
            selections = []
            for hit in hits:
                if len(selections) >= 2:
                    break
                win = _context_window(source, paras, hit, primary_limit, page.get("outline"))
                if any(win[0] < old[0][1] and old[0][0] < win[1] for old in selections):
                    continue
                selections.append((win, hit))
            if selections:
                (start, end, context_start, context_end), _primary_hit = selections[0]
                primary_context_start, primary_context_end = context_start, context_end
                selected = source[start:end]
                remaining = max_chars - len(selected)
                primary_index = _primary_hit["index"]
                if primary_context_start < start:
                    neighbor_start = primary_context_start
                    neighbor_end = paras[primary_index][0]
                    cap = min(_MAX_LEAF_CHARS, remaining)
                    if neighbor_end - neighbor_start > cap:
                        neighbor_start = max(neighbor_start, neighbor_end - cap)
                    if neighbor_end > neighbor_start:
                        excerpt_items.append(_page_excerpt(source, neighbor_start, neighbor_end,
                            context_start=primary_context_start, context_end=primary_context_end))
                        remaining -= neighbor_end - neighbor_start
                if primary_context_end > end and len(excerpt_items) < _MAX_EXCERPTS:
                    neighbor_start = paras[primary_index][1]
                    neighbor_end = primary_context_end
                    cap = min(_MAX_LEAF_CHARS, remaining)
                    if neighbor_end - neighbor_start > cap:
                        neighbor_end = neighbor_start + cap
                    if neighbor_end > neighbor_start:
                        excerpt_items.append(_page_excerpt(source, neighbor_start, neighbor_end,
                            context_start=primary_context_start, context_end=primary_context_end))
                        remaining -= neighbor_end - neighbor_start
                for (ex_start, ex_end, ex_context_start, ex_context_end), hit in selections[1:]:
                    cap = min(_MAX_LEAF_CHARS, primary_limit, remaining)
                    if cap <= 0 or len(excerpt_items) >= _MAX_EXCERPTS:
                        break
                    if ex_end - ex_start > cap:
                        ex_start, ex_end, ex_context_start, ex_context_end = _context_window(source, paras, hit, cap, page.get("outline"))
                    snippet = source[ex_start:ex_end]
                    overlaps_excerpt = any(
                        ex_start < item["snippet_end"] and item["snippet_start"] < ex_end
                        for item in excerpt_items
                    )
                    if snippet and not overlaps_excerpt and not (ex_start < end and start < ex_end):
                        excerpt_items.append(_page_excerpt(source, ex_start, ex_end,
                            context_start=ex_context_start, context_end=ex_context_end))
                        remaining -= len(snippet)
            excerpts = excerpt_items[:_MAX_EXCERPTS]
        else:
            limit = min(_OVERVIEW_PRIMARY_CHARS, max(1, max_chars // 2))
            end = min(total, limit)
            start = 0
            selected = source[start:end]
        if not selected:
            end = min(total, min(_OVERVIEW_PRIMARY_CHARS, max(1, max_chars // 2)))
            start = 0
            selected = source[start:end]
        no_match = bool(query and query.strip() and not hits)
        if no_match:
            primary_context_start, primary_context_end = 0, total
        supplied_outline = page.get("outline")
        if isinstance(supplied_outline, list):
            outline = [deepcopy(item) for item in supplied_outline[:12]]
            outline_omitted = max(0, len(supplied_outline) - len(outline))
        else:
            inferred = []
            for a, b in paras:
                line = source[a:b].splitlines()[0].strip()
                if line.startswith("#") or (len(line) < 100 and line.isupper()):
                    inferred.append({"text": line[:120], "start": a, "end": b, "heuristic": True})
            outline_omitted = max(0, len(inferred) - 12)
            outline = inferred[:12]
        omitted = []
        if query and not no_match:
            covered = sorted([(start, end)] + [
                (item["snippet_start"], item["snippet_end"]) for item in excerpts
                if item["snippet_end"] > primary_context_start and item["snippet_start"] < primary_context_end
            ])
            cursor = primary_context_start
            for covered_start, covered_end in covered:
                covered_start = max(covered_start, primary_context_start)
                covered_end = min(covered_end, primary_context_end)
                if covered_start > cursor:
                    omitted.append({"start": cursor, "end": covered_start, "reason": "bounded_context"})
                cursor = max(cursor, covered_end)
            if cursor < primary_context_end:
                omitted.append({"start": cursor, "end": primary_context_end, "reason": "bounded_context"})
        elif no_match and end < total:
            omitted.append({"start": end, "end": total, "reason": "bounded_context"})
        if not query and end < total:
            omitted.append({"start": end, "end": total, "reason": "overview_prefix"})
        result.update(text=selected, offset=start, returned_chars=len(selected),
                      has_more=end < total, next_offset=end if end < total else None,
                      excerpts=excerpts, outline=outline, outline_omitted_count=outline_omitted,
                      context_start=primary_context_start if query else start,
                      context_end=primary_context_end if query else end,
                      context_complete=(not omitted) if query and not no_match
                      else (end == total if no_match else True),
                      query_status="no_lexical_match" if no_match else
                      ("lexical_match" if query and query.strip() else "not_requested"),
                      omitted_context=omitted,
                      coverage_scope="selected_readable_excerpts_not_full_source")
    return result


def build_search_view(payload: dict, *, snippet_chars: int = 360) -> dict:
    """Copy a search response and bound each row's literal provider snippet."""
    if isinstance(snippet_chars, bool) or not isinstance(snippet_chars, int) or snippet_chars < 1:
        raise ValueError("snippet_chars must be a positive integer")
    result = deepcopy(payload)
    rows = payload.get("results", [])
    if not isinstance(rows, list):
        raise TypeError("results must be a list")
    compact = []
    for row in rows:
        item = deepcopy(row)
        title = item.get("title", "")
        title = title if isinstance(title, str) else str(title)
        item["title"] = title[:200]
        item["title_truncated"] = len(title) > 200
        snippet = item.get("snippet", "")
        snippet = snippet if isinstance(snippet, str) else str(snippet)
        item["snippet"] = snippet[:snippet_chars]
        item["snippet_truncated"] = len(snippet) > snippet_chars
        item["snippet_total_chars"] = len(snippet)
        compact.append(item)
    result["results"] = compact
    result["result_count"] = len(rows)
    return result
