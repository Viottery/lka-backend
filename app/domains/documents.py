"""Bounded, lazy PDF reading. Text extraction is never a claim of visual fidelity."""
from __future__ import annotations

import hashlib
import io
import math
import threading
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PAGE_TEXT_CHARS = 2_000_000
# PDFium is process-global and not thread-safe, even for separate documents.
# Service-local locks only protect each pypdf reader and its extraction cache.
_PDFIUM_LOCK = threading.Lock()


class DocumentService:
    """Small process-local cache; authorization remains the caller's responsibility."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._documents: OrderedDict[str, tuple[bytes, PdfReader, OrderedDict]] = OrderedDict()

    def load(self, path: Path, expected_sha256: str | None = None) -> tuple[str, bytes, PdfReader]:
        # Hash on every invocation, including cache hits: stat-only validation misses
        # replacements preserving size and mtime. Extraction/rendering remain lazy.
        with path.open("rb") as stream:
            raw = stream.read(MAX_PDF_BYTES + 1)
        return self._load_snapshot(raw, expected_sha256)

    def _load_snapshot(
        self, raw: bytes, expected_sha256: str | None = None, *,
        source_max_bytes: int | None = None,
    ) -> tuple[str, bytes, PdfReader]:
        """Parse an already captured source without reopening its filesystem path."""
        source_max_bytes = MAX_PDF_BYTES if source_max_bytes is None else source_max_bytes
        if len(raw) > source_max_bytes:
            raise ValueError("PDF exceeds the local reading size limit")
        sha = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and expected_sha256 != sha:
            raise ValueError("PDF changed since opening; reopen it and use the new sha256")
        with self._lock:
            if sha in self._documents:
                self._documents.move_to_end(sha)
                return sha, raw, self._documents[sha][1]
            try:
                reader = PdfReader(io.BytesIO(raw), strict=False)
                if reader.is_encrypted:
                    raise ValueError("Encrypted PDFs are unsupported; supply a decrypted copy")
                if len(reader.pages) > 20_000:
                    raise ValueError("PDF exceeds the 20000-page reading limit")
            except PdfReadError as exc:
                raise ValueError(f"Invalid PDF: {exc}") from exc
            self._documents[sha] = (raw, reader, OrderedDict())
            while len(self._documents) > 3:
                self._documents.popitem(last=False)
            return sha, raw, reader

    def extract_snapshot(self, raw: bytes, *, max_chars: int = 2_000_000,
                         source_max_bytes: int | None = None,
                         max_pages: int = 200) -> dict[str, Any]:
        """Extract a complete bounded text layer; never silently index partial PDFs."""
        if not 1 <= max_chars <= MAX_PAGE_TEXT_CHARS or not 1 <= max_pages <= 20_000:
            raise ValueError("invalid PDF extraction bounds")
        with self._lock:
            sha, _, reader = self._load_snapshot(raw, source_max_bytes=source_max_bytes)
            if len(reader.pages) > max_pages:
                raise ValueError("PDF exceeds the indexing page limit")
            parts: list[str] = []
            spans: list[dict[str, Any]] = []
            empty_pages: list[int] = []
            visual_pages: list[int] = []
            offset = 0
            for number in range(1, len(reader.pages) + 1):
                page = self._page(sha, reader, number, "plain")
                text = page["text"].strip()
                if page["needs_visual"]:
                    visual_pages.append(number)
                if not text:
                    empty_pages.append(number)
                    continue
                marker = f"\n\n<!-- PDF page {number} -->\n\n" if parts else f"<!-- PDF page {number} -->\n\n"
                marker_start = offset
                start = offset + len(marker)
                offset = start + len(text)
                if offset > max_chars:
                    raise ValueError("PDF text exceeds the indexing character limit")
                parts.append(marker + text)
                spans.append({"page": number, "marker_start_char": marker_start,
                              "start_char": start, "end_char": offset})
            if not parts:
                raise ValueError("PDF has no extractable text layer")
            return {"text": "".join(parts), "sha256": sha,
                    "page_count": len(reader.pages), "page_spans": spans,
                    "pages_without_text": empty_pages, "pages_needing_visual": visual_pages,
                    "text_layer_only": True, "visual_fidelity_verified": False}

    def open(self, path: Path) -> dict[str, Any]:
        with self._lock:
            sha, raw, reader = self.load(path)
            metadata = {str(k): str(v)[:2000] for k, v in (reader.metadata or {}).items()}
            outline: list[dict[str, Any]] = []
            omitted = False

            def visit(items: list, depth: int = 0) -> None:
                nonlocal omitted
                if depth > 20:
                    omitted = True
                    return
                for item in items:
                    if len(outline) >= 200:
                        omitted = True
                        return
                    if isinstance(item, list):
                        visit(item, depth + 1)
                    else:
                        index = reader.get_destination_page_number(item)
                        outline.append({"title": str(item.title)[:1000], "page": index + 1 if index >= 0 else None})
            try:
                visit(reader.outline)
                outline_error = None
            except (PdfReadError, KeyError, ValueError, TypeError) as exc:
                outline_error = str(exc)
            return {"path": str(path), "sha256": sha, "document_id": sha,
                    "file_size_bytes": len(raw), "page_count": len(reader.pages),
                    "metadata": metadata, "outline": outline, "outline_truncated": omitted,
                    "outline_error": outline_error, "extraction": "not_started",
                    "fidelity_notice": "Text omits visual layout, charts and possibly reading order. Original PDF is preserved; render relevant pages to inspect them."}

    def _page(self, sha: str, reader: PdfReader, number: int, mode: str) -> dict[str, Any]:
        if not 1 <= number <= len(reader.pages):
            raise ValueError("page is outside PDF page range")
        cache = self._documents[sha][2]
        key = (number, mode)
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        page = reader.pages[number - 1]
        resources = page.get("/Resources", {})
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        if not isinstance(resources, dict):
            raise TypeError(f"Invalid PDF page {number}: /Resources is not a dictionary; render this page")
        xobjects = resources.get("/XObject", {})
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        if not isinstance(xobjects, dict):
            raise TypeError(f"Invalid PDF page {number}: /XObject is not a dictionary; render this page")
        images = 0
        forms = 0
        for value in xobjects.values():
            value = value.get_object() if hasattr(value, "get_object") else value
            if not isinstance(value, dict):
                raise TypeError(f"Invalid PDF page {number}: image/form resource is not a dictionary; render this page")
            images += value.get("/Subtype") == "/Image"
            forms += value.get("/Subtype") == "/Form"
        # pypdf's layout default silently strips rotated text. Retain it even
        # though the approximate layout may degrade; the source remains available.
        text = page.extract_text(extraction_mode=mode, layout_mode_strip_rotated=False) or ""
        if len(text) > MAX_PAGE_TEXT_CHARS:
            raise ValueError("Extracted page text exceeds the 2000000-character limit; render this page")
        result = {"page": number, "text": text, "total_chars": len(text), "extraction_mode": mode,
                  "width_points": float(page.mediabox.width), "height_points": float(page.mediabox.height),
                  "rotation": page.rotation, "direct_image_count": images,
                  "has_form_xobjects": forms > 0, "has_annotations": bool(page.get("/Annots")),
                  "text_status": "text_layer" if text.strip() else "no_extractable_text",
                  "needs_visual": images > 0 or forms > 0 or bool(page.get("/Annots")) or not text.strip(), "visual_fidelity_verified": False,
                  "fidelity_notice": "Text extraction does not preserve diagrams, tables, annotations or exact layout; render relevant pages."}
        cache[key] = result
        # Bounded retained text, but full source bytes allow re-extraction.
        while len(cache) > 32 or sum(item["total_chars"] for item in cache.values()) > 4_000_000:
            cache.popitem(last=False)
        return result

    def read_pages(self, path: Path, expected_sha256: str, *, start_page: int = 1,
                   max_pages: int = 1, start_char: int = 0, max_chars: int = 16000,
                   mode: str = "plain") -> dict[str, Any]:
        if mode not in ("plain", "layout") or not 1 <= max_pages <= 10 or not 1 <= max_chars <= 64000 or start_char < 0:
            raise ValueError("invalid reading bounds or extraction mode")
        with self._lock:
            sha, _, reader = self.load(path, expected_sha256)
            if not 1 <= start_page <= len(reader.pages):
                raise ValueError("start_page is outside PDF page range")
            pages = []
            budget = max_chars
            cursor = None
            for number in range(start_page, min(start_page + max_pages, len(reader.pages) + 1)):
                page = self._page(sha, reader, number, mode)
                offset = start_char if number == start_page else 0
                if offset > page["total_chars"]:
                    raise ValueError("start_char is beyond the extracted page text")
                selected = page["text"][offset:offset + budget]
                end = offset + len(selected)
                pages.append({**page, "text": selected, "start_char": offset, "end_char": end,
                              "truncated": end < page["total_chars"]})
                budget -= len(selected)
                if end < page["total_chars"]:
                    cursor = {"start_page": number, "start_char": end}
                    break
                if number < len(reader.pages):
                    cursor = {"start_page": number + 1, "start_char": 0}
                else:
                    cursor = None
                if not budget:
                    break
            return {"path": str(path), "sha256": sha, "page_count": len(reader.pages), "pages": pages,
                    "next": cursor, "truncated": cursor is not None, "visual_fidelity_verified": False}

    def find(self, path: Path, expected_sha256: str, *, query: str, start_page: int = 1,
             start_char: int = 0, max_pages: int = 20, max_matches: int = 20) -> dict[str, Any]:
        if not query or len(query) > 1000 or not 1 <= max_pages <= 50 or not 1 <= max_matches <= 100 or start_char < 0:
            raise ValueError("invalid search query or bounds")
        with self._lock:
            sha, _, reader = self.load(path, expected_sha256)
            if not 1 <= start_page <= len(reader.pages):
                raise ValueError("start_page is outside PDF page range")
            matches, empty_pages = [], []
            scanned = 0
            cursor = None
            for number in range(start_page, min(start_page + max_pages, len(reader.pages) + 1)):
                page = self._page(sha, reader, number, "plain")
                text = page["text"]
                offset = start_char if number == start_page else 0
                if offset > len(text):
                    raise ValueError("start_char is beyond the extracted page text")
                scanned += 1
                if not text.strip():
                    empty_pages.append(number)
                while (position := text.find(query, offset)) >= 0:
                    matches.append({"page": number, "start_char": position, "end_char": position + len(query),
                                    "preview": text[max(0, position - 100):position + len(query) + 100]})
                    offset = position + len(query)
                    if len(matches) >= max_matches:
                        next_cursor = ({"start_page": number, "start_char": offset} if offset < len(text)
                                       else {"start_page": number + 1, "start_char": 0} if number < len(reader.pages)
                                       else None)
                        return {"path": str(path), "sha256": sha, "matches": matches, "pages_scanned": scanned,
                                "pages_without_text": empty_pages, "next": next_cursor,
                                "truncated": next_cursor is not None, "search_scope": "text_layer_only_case_sensitive"}
                cursor = {"start_page": number + 1, "start_char": 0} if number < len(reader.pages) else None
            return {"path": str(path), "sha256": sha, "matches": matches, "pages_scanned": scanned,
                    "pages_without_text": empty_pages, "next": cursor, "truncated": cursor is not None,
                    "search_scope": "text_layer_only_case_sensitive"}

    def render_page(self, path: Path, expected_sha256: str, *, page: int, output_path: Path, dpi: int = 120) -> dict[str, Any]:
        import pypdfium2 as pdfium
        if not 72 <= dpi <= 200:
            raise ValueError("dpi must be between 72 and 200")
        with self._lock:
            sha, raw, reader = self.load(path, expected_sha256)
            if not 1 <= page <= len(reader.pages):
                raise ValueError("page is outside PDF page range")
            if output_path.exists() or output_path.is_symlink():
                raise FileExistsError(f"Render output already exists: {output_path}")
            with _PDFIUM_LOCK, pdfium.PdfDocument(raw) as document, closing(document[page - 1]) as source:
                width, height = source.get_size()
                if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                    raise ValueError("PDF page has invalid render dimensions")
                # PDFium rounds each dimension up before allocating the bitmap.
                if math.ceil(width * dpi / 72) * math.ceil(height * dpi / 72) > 16_000_000:
                    raise ValueError("render exceeds 16000000 pixels; use lower dpi")
                bitmap = source.render(scale=dpi / 72)
                try:
                    image = bitmap.to_pil()
                    with image:
                        png = io.BytesIO()
                        image.save(png, format="PNG")
                        data = png.getvalue()
                        rendered_width, rendered_height = image.size
                finally:
                    bitmap.close()
            # Exclusive creation preserves existing output/user files. Parent must exist.
            with output_path.open("xb") as stream:
                stream.write(data)
            return {"path": str(path), "sha256": sha, "page": page, "output_path": str(output_path),
                    "mime_type": "image/png", "dpi": dpi, "width": rendered_width, "height": rendered_height,
                    "image_sha256": hashlib.sha256(data).hexdigest(), "bytes_written": len(data),
                    "visual_fidelity_verified": False}
