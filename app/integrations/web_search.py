"""Brave web search and tightly bounded public page text fetching."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import sqlite3
import ssl
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

BRAVE_SEARCH_BASE = "https://api.search.brave.com/res/v1"
MAX_QUERY_CHARS = 400
MAX_SEARCH_RESULTS = 10
MAX_PAGE_BYTES = 1_000_000
MAX_PAGE_TEXT_CHARS = 20_000
MAX_REDIRECTS = 3
MAX_FIND_CONTEXT_CHARS = 1_200
MIN_VISIBLE_HTML_CHARS = 1_200


class WebSearchError(RuntimeError):
    """Expected provider or page fetching failure suitable for tool feedback."""


class BraveSearchQuota:
    """Persistent, process-safe upper bound on paid search requests per UTC month."""

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection], monthly_limit: int) -> None:
        if monthly_limit < 0:
            raise ValueError("monthly_limit must be non-negative")
        self.conn_factory = conn_factory
        self.monthly_limit = monthly_limit

    def reserve(self) -> None:
        month = datetime.now(UTC).strftime("%Y-%m")
        conn = self.conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS web_search_quota (month TEXT PRIMARY KEY, requests INTEGER NOT NULL)")
            row = conn.execute("SELECT requests FROM web_search_quota WHERE month=?", (month,)).fetchone()
            count = int(row[0]) if row else 0
            if count >= self.monthly_limit:
                raise WebSearchError("Monthly web search request limit reached.")
            conn.execute(
                "INSERT INTO web_search_quota(month,requests) VALUES(?,1) "
                "ON CONFLICT(month) DO UPDATE SET requests=requests+1",
                (month,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class BraveSearchAdapter:
    """Small synchronous Brave Search API adapter; transport is injectable for tests."""

    def __init__(
        self,
        api_key: str | None,
        *,
        timeout_seconds: float = 8.0,
        transport: httpx.BaseTransport | None = None,
        quota: BraveSearchQuota | None = None,
    ) -> None:
        self.api_key = api_key.strip() if api_key else ""
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.quota = quota

    def search(self, query: str, *, mode: str = "web", limit: int = 5,
               freshness: str | None = None, country: str | None = None,
               search_lang: str | None = None, deadline: float | None = None) -> dict[str, Any]:
        query = query.strip()
        if not query or len(query) > MAX_QUERY_CHARS or len(query.split()) > 50:
            raise WebSearchError(f"query must contain 1 to {MAX_QUERY_CHARS} characters and at most 50 words")
        if mode not in {"web", "news"}:
            raise WebSearchError("mode must be 'web' or 'news'")
        if not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise WebSearchError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
        if freshness not in {None, "pd", "pw", "pm", "py"}:
            raise WebSearchError("freshness must be pd, pw, pm, or py")
        if country is not None and not re.fullmatch(r"[A-Z]{2}", country):
            raise WebSearchError("country must be a two-letter uppercase country code")
        if search_lang is not None:
            search_lang = search_lang.strip().lower().replace("_", "-")
            # Brave accepts script-specific Chinese codes, not generic ISO `zh`.
            if search_lang == "zh":
                search_lang = "zh-hant" if country in {"TW", "HK", "MO"} else "zh-hans"
            else:
                search_lang = {"zh-cn": "zh-hans", "zh-sg": "zh-hans",
                               "zh-tw": "zh-hant", "zh-hk": "zh-hant", "zh-mo": "zh-hant"}.get(
                                   search_lang, search_lang)
        if search_lang is not None and not re.fullmatch(r"[a-z]{2,8}(?:-[a-z]{2,8})?", search_lang):
            raise WebSearchError("search_lang must be a language code")
        if not self.api_key:
            raise WebSearchError("Web search is unavailable: no Brave Search API key is configured.")
        timeout = self.timeout_seconds
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise WebSearchError("Search acquisition deadline exceeded.")
        if self.quota is not None:
            self.quota.reserve()

        endpoint = "web/search" if mode == "web" else "news/search"
        try:
            with httpx.Client(
                timeout=timeout, transport=self.transport, trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.get(
                    f"{BRAVE_SEARCH_BASE}/{endpoint}",
                    params={"q": query, "count": limit, "safesearch": "strict",
                            "text_decorations": "false",
                            **({"freshness": freshness} if freshness else {}),
                            **({"country": country} if country else {}),
                            **({"search_lang": search_lang} if search_lang else {})},
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                )
                response.raise_for_status()
                if response.status_code != 200:
                    raise WebSearchError(f"Brave Search returned HTTP {response.status_code}")
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WebSearchError(f"Brave Search request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise WebSearchError("Brave Search returned an invalid JSON payload")
        container = payload.get("web") if mode == "web" else None
        rows = container.get("results") if isinstance(container, dict) else payload.get("results") if mode == "news" else None
        if not isinstance(rows, list):
            raise WebSearchError("Brave Search returned an invalid results payload")
        results = []
        for row in rows:
            if len(results) >= limit:
                break
            if not isinstance(row, dict):
                continue
            try:
                safe_url = _safe_public_https_url(str(row.get("url") or ""))
            except WebSearchError:
                continue
            results.append({
                "title": _clean_text(row.get("title")),
                "url": safe_url,
                "snippet": _clean_text(row.get("description"))[:1000],
                "published_at": _published_at(row, mode),
                "provider_fetched_at": row.get("page_fetched"),
                "source": {"provider": "Brave Search", "mode": mode},
            })
        return {"query": query, "mode": mode, "freshness": freshness,
                "country": country, "search_lang": search_lang, "results": results,
                "result_count": len(results), "possible_more": len(rows) >= limit}


def _published_at(row: dict[str, Any], mode: str) -> str | None:
    if mode == "news":
        value = row.get("page_age")
    else:
        value = row.get("page_age") or row.get("published_at")
    return str(value) if value else None


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


class _PlainTextParser(HTMLParser):
    _SKIP: ClassVar[set[str]] = {
        "head", "script", "style", "noscript", "template", "svg", "iframe", "object",
        "nav", "form",
    }
    _BLOCK: ClassVar[set[str]] = {
        "address", "article", "br", "dd", "div", "dt", "h1", "h2", "h3", "h4",
        "h5", "h6", "li", "main", "p", "pre", "section",
    }
    _VOID: ClassVar[set[str]] = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self, *, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.article_parts: list[str] = []
        self.title_parts: list[str] = []
        self.stack: list[tuple[str, bool, str | None]] = []
        self.title_depth = 0
        self.headings: list[str] = []
        self.heading_parts: list[str] | None = None
        self.table_rows: list[int | None] = []
        self._last_char = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        hidden_style = re.search(
            r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b",
            attr_map.get("style", "").lower(),
        )
        in_article = any(parent in {"main", "article"} for parent, _, _ in self.stack)
        hidden = (
            tag in self._SKIP
            or (tag in {"header", "footer"} and not in_article)
            or "hidden" in attr_map
            or attr_map.get("aria-hidden", "").lower() == "true"
            or hidden_style is not None
            or any(parent_hidden for _, parent_hidden, _ in self.stack)
        )
        href = attr_map.get("href") if tag == "a" else None
        if tag in self._VOID:
            if not hidden and tag in self._BLOCK:
                self._append_text("\n")
            return
        self.stack.append((tag, hidden, href))
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and not hidden:
            self.heading_parts = []
        if tag == "title":
            self.title_depth += 1
        if tag == "tr":
            self.table_rows.append(None if hidden else 0)
            if not hidden:
                self._append_row_break()
        elif tag in {"td", "th"}:
            if not hidden and self.table_rows and self.table_rows[-1] is not None:
                if self.table_rows[-1]:
                    self._append_text(" | ")
                self.table_rows[-1] += 1
        elif not hidden and tag in self._BLOCK:
            self._append_text("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID:
            return
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading_parts is not None:
            self.headings.append(_clean_text("".join(self.heading_parts)))
            self.heading_parts = None
        link_href = None
        link_hidden = True
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                _, link_hidden, link_href = self.stack[index]
                del self.stack[index:]
                break
        if not any(parent_hidden for _, parent_hidden, _ in self.stack) and tag in self._BLOCK:
            self._append_text("\n")
        if tag == "tr":
            visible_row = self.table_rows.pop() if self.table_rows else None
            if visible_row is not None:
                self._append_row_break()
        if tag == "a" and link_href and not link_hidden:
            self._append_text(f" ({urljoin(self.base_url, link_href)})")

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_parts.append(data)
        if not any(parent_hidden for _, parent_hidden, _ in self.stack):
            self._append_text(data)
            if self.heading_parts is not None:
                self.heading_parts.append(data)

    def text(self) -> str:
        article_value = "".join(self.article_parts)
        value = article_value if article_value.strip() else "".join(self.parts)
        title = " ".join("".join(self.title_parts).split())
        if title:
            value = f"{title}\n\n{value}" if value else title
        value = re.sub(r"[ \t\xa0]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()

    def _append_text(self, value: str) -> None:
        self.parts.append(value)
        if value:
            self._last_char = value[-1]
        if any(tag in {"main", "article"} and not hidden for tag, hidden, _ in self.stack):
            self.article_parts.append(value)

    def _append_row_break(self) -> None:
        if self._last_char != "\n":
            self._append_text("\n")


class _HiddenStructuredTextParser(HTMLParser):
    """Collect text only inside explicitly titled hidden content records."""

    _NEVER: ClassVar[set[str]] = {"script", "style", "noscript", "template", "form", "input", "textarea", "select", "button"}
    _VOID: ClassVar[set[str]] = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.stack: list[tuple[str, bool, bool, bool, int | None]] = []
        self.records: list[tuple[str, list[str]]] = []
        self.body_length = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        inherited_block = any(blocked for _, _, _, blocked, _ in self.stack)
        forbidden = tag in self._NEVER or inherited_block
        hidden = (
            "hidden" in attr_map
            or attr_map.get("aria-hidden", "").lower() == "true"
            or re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b",
                         attr_map.get("style", "").lower()) is not None
            or any(hidden for _, _, hidden, _, _ in self.stack)
        )
        root = bool(attr_map.get("data-title")) and not forbidden
        parent_record = next((record for _, _, _, _, record in reversed(self.stack) if record is not None), None)
        record = len(self.records) if root else parent_record
        if root:
            self.records.append((attr_map["data-title"], []))
        if tag not in self._VOID:
            self.stack.append((tag, record is not None and not forbidden, hidden, forbidden, record))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if (not self.stack or any(blocked for _, _, _, blocked, _ in self.stack)
                or not any(hidden for _, _, hidden, _, _ in self.stack)):
            return
        record = next((record for _, selected, _, _, record in reversed(self.stack)
                       if selected and record is not None), None)
        if record is None:
            return
        clean = _clean_text(data)
        if not clean or self.body_length >= self.limit:
            return
        body = self.records[record][1]
        separator = 1 if body else 0
        available = self.limit - self.body_length - separator
        if available <= 0:
            return
        clean = clean[:available]
        if separator:
            body.append(" ")
            self.body_length += 1
        body.append(clean)
        self.body_length += len(clean)

    def text(self) -> str:
        parts = []
        for label, body_parts in self.records:
            body = " ".join(body_parts).strip()
            if body:
                parts.append(f"{_clean_text(label)}\n{body}")
        return "\n".join(parts)[:self.limit].strip()


def _safe_public_https_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (len(url) > 2048 or any(ord(ch) < 32 or ord(ch) == 127 for ch in url)
                or parts.scheme.lower() != "https" or not parts.hostname
                or parts.username or parts.password):
            raise ValueError
        if parts.port not in (None, 443):
            raise ValueError
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            address = None
        if address is not None:
            if not address.is_global:
                raise ValueError
            host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        else:
            host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
            if "." not in host or host.endswith((".localhost", ".local", ".internal")):
                raise ValueError
        path = parts.path or "/"
        return urlunsplit(("https", host, path, parts.query, ""))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise WebSearchError("Only public HTTPS URLs without credentials or nonstandard ports are supported.") from exc


def _public_address(host: str) -> str:
    """Resolve once and require every DNS answer to be globally routable."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise WebSearchError("Page host is not a public IP address.")
        return literal.compressed
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebSearchError(f"Page DNS lookup failed: {type(exc).__name__}") from exc
    addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
    if not addresses or any(not address.is_global for address in addresses):
        raise WebSearchError("Page DNS resolved to a non-public address.")
    return addresses[0].compressed


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated IP while verifying TLS against the original host."""

    def __init__(self, host: str, pinned_ip: str, timeout: float) -> None:
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.pinned_ip = pinned_ip

    def connect(self) -> None:
        raw = socket.create_connection((self.pinned_ip, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _page_literal_matches(text: str, folded_query: str):
    """Return original Unicode offsets even when casefold expands a character."""
    folded_parts = []
    source_indexes = []
    for index, char in enumerate(text):
        folded = char.casefold()
        folded_parts.append(folded)
        source_indexes.extend([index] * len(folded))
    folded_text = "".join(folded_parts)
    cursor = 0
    previous = None
    while True:
        start = folded_text.find(folded_query, cursor)
        if start < 0:
            return
        end = start + len(folded_query)
        source_pair = (source_indexes[start], source_indexes[end - 1] + 1)
        cursor = end
        # A folded substring must cover whole original characters. For example,
        # "s" is only half of the folded "ß" and cannot cite that as a literal.
        if ((start > 0 and source_indexes[start - 1] == source_indexes[start])
                or (end < len(source_indexes) and source_indexes[end - 1] == source_indexes[end])):
            continue
        if source_pair != previous:
            yield source_pair
            previous = source_pair


def _find_context(text: str, start: int, end: int) -> dict[str, Any]:
    """Keep nearby extraction blocks intact when bounded, never infer support.

    Blank lines delimit readable blocks, not authenticated HTML paragraphs.
    The requested span includes one neighbor on either side; a large span is
    explicitly partial rather than silently dropping a distant qualification.
    """
    before = text.rfind("\n\n", 0, start)
    block_start = before + 2 if before >= 0 else 0
    after = text.find("\n\n", end)
    block_end = after if after >= 0 else len(text)
    previous = text.rfind("\n\n", 0, max(0, block_start - 2))
    context_start = previous + 2 if previous >= 0 else 0
    following = text.find("\n\n", min(len(text), block_end + 2))
    context_end = following if following >= 0 else len(text)
    if context_end - context_start <= MAX_FIND_CONTEXT_CHARS:
        snippet_start, snippet_end = context_start, context_end
        scope = "adjacent_readable_blocks"
    elif block_end - block_start <= MAX_FIND_CONTEXT_CHARS:
        snippet_start, snippet_end = block_start, block_end
        scope = "matched_readable_block"
    else:
        snippet_start = max(block_start, start - 100)
        snippet_end = max(end, min(block_end, snippet_start + 400))
        scope = "bounded_fragment"
    return {"snippet_start": snippet_start, "snippet_end": snippet_end,
            "snippet": text[snippet_start:snippet_end], "snippet_scope": scope,
            "context_start": context_start, "context_end": context_end,
            "context_complete": snippet_start == context_start and snippet_end == context_end}


def _find_recovery_preview(text: str, query: str) -> list[dict[str, Any]]:
    """Bounded lexical hints, not phrase/semantic matches or source verification.

    Consider four Unicode word runs (no CJK segmentation), at most 64 anchors
    each. Rank these bounded candidates by token overlap, then source offset.
    """
    tokens = sorted({token.casefold() for token in re.findall(r"\w+", query)
                     if len(token) >= 2}, key=lambda token: (-len(token), token))[:4]
    candidates = []
    for token in tokens:
        for index, (start, _) in enumerate(_page_literal_matches(text, token)):
            if index >= 64:
                break
            snippet_start = max(0, start - 100)
            snippet_end = min(len(text), snippet_start + 400)
            snippet = text[snippet_start:snippet_end]
            matched = [term for term in tokens if term in snippet.casefold()]
            candidates.append({"kind": "query_token_context", "snippet_start": snippet_start,
                               "snippet_end": snippet_end, "snippet": snippet, "query_tokens": matched})
    previews = []
    for candidate in sorted(candidates, key=lambda row: (-len(row["query_tokens"]), row["snippet_start"])):
        if any(candidate["snippet_start"] < row["snippet_end"]
               and row["snippet_start"] < candidate["snippet_end"] for row in previews):
            continue
        previews.append(candidate)
        if len(previews) == 3:
            break
    if not previews and text:
        previews.append({"kind": "page_prefix", "snippet_start": 0,
                         "snippet_end": min(len(text), 1200), "snippet": text[:1200], "query_tokens": []})
    return previews


class PublicPageFetcher:
    """Fetch bounded public HTTPS text with per-hop DNS validation and pinning."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        max_bytes: int = MAX_PAGE_BYTES,
        max_text_chars: int = MAX_PAGE_TEXT_CHARS,
        max_redirects: int = MAX_REDIRECTS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_text_chars = max_text_chars
        self.max_redirects = max_redirects
        self.transport = transport
        self._fetch_deadline = threading.local()

    def read(self, url: str, *, deadline: float | None = None,
             check: Callable[[], None] | None = None) -> dict[str, Any]:
        """Return one complete bounded extraction for snapshot acquisition."""
        return self._readable_page(url, deadline=deadline, check=check)

    def open(
        self, url: str, *, offset: int = 0, max_chars: int | None = None,
        expected_text_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Slice this fresh fetch's full readable extraction, not a stable cache.

        Offsets count Unicode characters in the normalized extraction. A caller
        continuing a page can require the previous extraction's fingerprint;
        changed text fails closed instead of silently mixing revisions.
        """
        cap = min(self.max_text_chars, MAX_PAGE_TEXT_CHARS)
        limit = cap if max_chars is None else max_chars
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise WebSearchError("offset must be a nonnegative character index.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= cap:
            raise WebSearchError(f"max_chars must be an integer from 1 through {cap}.")
        fetched = self._readable_page(url, expected_text_sha256=expected_text_sha256)
        text = fetched.pop("text")
        total = len(text)
        page = text[offset:offset + limit]
        end = min(total, offset + len(page))
        has_more = end < total
        return {**fetched, "text": page, "truncated": len(page) < total,
                "offset": offset, "returned_chars": len(page), "total_chars": total,
                "has_more": has_more, "next_offset": end if has_more else None}

    def find(
        self, url: str, query: str, *, offset: int = 0, limit: int = 5,
        expected_text_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Locate literals beyond a page preview without sending the query remotely."""
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise WebSearchError("query must contain 1 through 200 characters.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise WebSearchError("offset must be a nonnegative match index.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
            raise WebSearchError("limit must be an integer from 1 through 5.")
        fetched = self._readable_page(url, expected_text_sha256=expected_text_sha256)
        return self.find_in_page(fetched, query, offset=offset, limit=limit)

    @staticmethod
    def find_in_page(fetched: dict[str, Any], query: str, *, offset: int = 0,
                     limit: int = 5) -> dict[str, Any]:
        """Use the exact existing literal/context semantics without another HTTP call."""
        fetched = dict(fetched)
        text = fetched.pop("text")
        matches = []
        total = 0
        for start, end in _page_literal_matches(text, query.casefold()):
            if total >= offset:
                matches.append({"match_start": start, "match_end": end,
                                **_find_context(text, start, end)})
            total += 1
            if len(matches) > limit:
                break
        has_more = len(matches) > limit
        match_status = "phrase_matches" if matches else ("offset_exhausted" if total else "no_literal_match")
        recovery_preview = _find_recovery_preview(text, query) if total == 0 and offset == 0 else []
        return {**fetched, "query": query, "matches": matches[:limit], "offset": offset,
                "offset_unit": "matches", "total_chars": len(text), "has_more": has_more,
                "next_offset": offset + len(matches[:limit]) if has_more else None,
                "complete": not has_more, "total_matches": None if has_more else total,
                "match_status": match_status, "recovery_preview": recovery_preview,
                "coverage_scope": "literal_match_page_not_semantic_verification",
                "recovery_preview_scope": "non_phrase_nonsemantic_excerpt",
                "query_tokenization": "unicode_word_runs_no_cjk_segmentation"}

    def _readable_page(
        self, url: str, *, expected_text_sha256: str | None = None,
        deadline: float | None = None, check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Share the identical network, extraction and revision gate for read/find."""
        if expected_text_sha256 is not None and (
            not isinstance(expected_text_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_text_sha256) is None
        ):
            raise WebSearchError("expected_text_sha256 must be a lowercase SHA-256 hex digest.")
        target = _safe_public_https_url(url)
        requested_url = target
        redirects = []
        deadline = deadline if deadline is not None else time.monotonic() + self.timeout_seconds
        check = check or (lambda: None)
        try:
            for hop in range(self.max_redirects + 1):
                check()
                if time.monotonic() >= deadline:
                    raise WebSearchError("Page fetch timed out.")
                self._fetch_deadline.value = deadline
                status, headers, content = self._fetch(target)
                check()
                if time.monotonic() >= deadline:
                    raise WebSearchError("Page fetch timed out.")
                if status in {301, 302, 303, 307, 308}:
                    location = headers.get("location")
                    if not location or hop >= self.max_redirects:
                        raise WebSearchError("Page fetch exceeded the redirect limit or had no redirect target.")
                    next_target = _safe_public_https_url(urljoin(target, location))
                    redirects.append({"url": target, "status": status, "target": next_target})
                    target = next_target
                    continue
                if status != 200:
                    raise WebSearchError(f"Page fetch returned HTTP {status}.")
                content_type = headers.get("content-type", "")
                media_type = content_type.split(";", 1)[0].lower()
                if media_type not in {"text/html", "application/xhtml+xml", "text/plain", "text/x-wiki"}:
                    raise WebSearchError(f"Unsupported page content type: {media_type[:128] or '(missing)'}.")
                charset_match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
                charset = charset_match.group(1).strip('"\'') if charset_match else "utf-8"
                raw = content.decode(charset, errors="replace")
                parser = _PlainTextParser(base_url=target)
                alternative_extraction = False
                if media_type in {"text/html", "application/xhtml+xml"}:
                    parser.feed(raw)
                    parser.close()
                    text = parser.text()
                    if len(text) < MIN_VISIBLE_HTML_CHARS:
                        hidden_parser = _HiddenStructuredTextParser(self.max_text_chars)
                        hidden_parser.feed(raw)
                        hidden_parser.close()
                        alternative = hidden_parser.text()
                        if len(alternative) > len(text):
                            text = alternative
                            alternative_extraction = True
                else:
                    text = re.sub(r"[ \t\xa0]+", " ", raw.replace("\r\n", "\n")).strip()
                rendered_visibility = (
                    "alternative_hidden_structured_text" if alternative_extraction
                    else "not_applicable_plain_text" if media_type in {"text/plain", "text/x-wiki"}
                    else "visible_text"
                )
                extraction_method = (
                    "hidden_structured_data_title_fallback" if alternative_extraction
                    else "plain_text" if media_type in {"text/plain", "text/x-wiki"}
                    else "visible_html"
                )
                fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if expected_text_sha256 is not None and expected_text_sha256 != fingerprint:
                    raise WebSearchError("Page extraction changed; restart pagination from offset 0.")
                outline = []
                cursor = 0
                for heading in parser.headings[:256]:
                    start = text.find(heading, cursor) if heading else -1
                    if start >= 0:
                        outline.append({"text": heading[:120], "start": start,
                                        "end": start + len(heading), "heuristic": False})
                        cursor = start + len(heading)
                return {
                    "url": target,
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "text": text,
                    "text_sha256": fingerprint,
                    "snapshot_stable": False,
                    "text_scope": "readable_text_extraction",
                    "rendered_visibility": rendered_visibility,
                    "extraction_method": extraction_method,
                    "title": _clean_text("".join(parser.title_parts))[:200],
                    "outline": outline,
                    "extraction_version": "readable-html-v4" if alternative_extraction else "readable-html-v3",
                    "network_observations": {
                        "requested_url": requested_url, "redirects": redirects,
                        "redirect_count": len(redirects), "final_http_status": status,
                        "response_headers": {key: headers[key][:512] for key in (
                            "date", "age", "cache-control", "etag", "last-modified") if key in headers},
                        "cache_origin": "not_determined",
                        "response_headers_scope": "selected_headers_first_512_chars",
                        "scope": "this_fetch_only_not_search_provider_diagnostics",
                    },
                }
        except WebSearchError:
            raise
        except (httpx.HTTPError, http.client.HTTPException, OSError, ssl.SSLError, UnicodeError, LookupError) as exc:
            raise WebSearchError(f"Page fetch failed: {exc}") from exc
        finally:
            self._fetch_deadline.value = None
        raise WebSearchError("Page fetch did not produce a response.")

    def _fetch(self, target: str) -> tuple[int, dict[str, str], bytes]:
        deadline = getattr(self._fetch_deadline, "value", None) or time.monotonic() + self.timeout_seconds
        remaining = max(0.001, min(self.timeout_seconds, deadline - time.monotonic()))
        if self.transport is not None:
            # Injected transport is for deterministic tests only; production uses
            # a pinned socket so DNS cannot change between validation and connect.
            with (
                httpx.Client(timeout=remaining, transport=self.transport,
                             trust_env=False, follow_redirects=False) as client,
                client.stream("GET", target, headers={"Accept": "text/html,text/plain;q=0.9"}) as response,
            ):
                if response.status_code in {301, 302, 303, 307, 308}:
                    return response.status_code, dict(response.headers), b""
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise WebSearchError("Page fetch timed out.")
                    content.extend(chunk)
                    if len(content) > self.max_bytes:
                        raise WebSearchError("Page exceeded the configured byte limit.")
                return response.status_code, dict(response.headers), bytes(content)

        parts = urlsplit(target)
        host = parts.hostname or ""
        pinned_ip = _public_address(host)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebSearchError("Page fetch timed out.")
        conn = _PinnedHTTPSConnection(host, pinned_ip, min(self.timeout_seconds, remaining))
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request("GET", path, headers={
                "Host": parts.netloc, "User-Agent": "LocalKnowledgeAgent/0.1",
                "Accept": "text/html,text/plain;q=0.9", "Accept-Encoding": "identity",
            })
            response = conn.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                return response.status, headers, b""
            if headers.get("content-encoding", "identity").lower() != "identity":
                raise WebSearchError("Compressed page responses are not supported.")
            length = headers.get("content-length")
            if length:
                try:
                    declared_length = int(length)
                except ValueError as exc:
                    raise WebSearchError("Page returned an invalid content length.") from exc
                if declared_length > self.max_bytes:
                    raise WebSearchError("Page exceeded the configured byte limit.")
            content = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebSearchError("Page fetch timed out.")
                if conn.sock is not None:
                    conn.sock.settimeout(remaining)
                # read() may perform many receives to fill the requested size,
                # resetting the socket timeout for each trickled fragment.
                # read1() returns after one buffered read so we can recheck the
                # wall-clock deadline between fragments.
                chunk = response.read1(min(65_536, self.max_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > self.max_bytes:
                    raise WebSearchError("Page exceeded the configured byte limit.")
            return response.status, headers, bytes(content)
        finally:
            conn.close()
