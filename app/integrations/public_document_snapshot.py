"""Fetch public pages into locally stored, plain-text document snapshots.

This module is intentionally an ingestion helper, not an Agent-visible tool.
The knowledge store receives the resulting files as ``local_document`` inputs.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from urllib.request import Request, urlopen


class _ArticleTextParser(HTMLParser):
    """Extract the main MediaWiki article body without a parser dependency."""

    _VOID_TAGS = {"br", "hr", "img", "input", "link", "meta", "source", "wbr"}
    _BLOCK_TAGS = {"br", "dd", "div", "dt", "h1", "h2", "h3", "h4", "li", "p", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capture_depth = 0
        self._skip_depth = 0
        self._parts: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self._in_title = True
        if self._capture_depth == 0 and tag in {"div", "main"}:
            classes = set((attributes.get("class") or "").split())
            if "mw-parser-output" in classes:
                self._capture_depth = 1
                return
        if self._capture_depth:
            if tag in {"script", "style", "noscript", "table"}:
                self._skip_depth += 1
            if tag in self._BLOCK_TAGS:
                self._append_newline()
            if tag not in self._VOID_TAGS:
                self._capture_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if not self._capture_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._append_newline()
        if tag in {"script", "style", "noscript", "table"} and self._skip_depth:
            self._skip_depth -= 1
        if tag not in self._VOID_TAGS:
            self._capture_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._capture_depth and not self._skip_depth:
            normalized = re.sub(r"\s+", " ", unescape(data))
            if normalized:
                self._parts.append(normalized)

    def _append_newline(self) -> None:
        if self._parts and self._parts[-1] != "\n":
            self._parts.append("\n")

    def text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def fetch_public_html(url: str, *, timeout_seconds: int = 30) -> str:
    """Fetch a public document with an explicit, non-browser user agent."""

    request = Request(url, headers={"User-Agent": "LKA-Knowledge-Sample/0.1"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def extract_article_snapshot(html: str) -> tuple[str, str]:
    """Return ``(title, text)`` from a public MediaWiki HTML document."""

    parser = _ArticleTextParser()
    parser.feed(html)
    parser.close()
    title = re.sub(r"\s+", " ", parser.title).strip()
    text = parser.text()
    if not text:
        raise ValueError("page did not contain a MediaWiki article body")
    return title or "Untitled document", text
