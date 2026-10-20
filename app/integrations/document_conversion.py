"""Local, snapshot-only document conversion with no optional service clients."""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from urllib.parse import unquote, urlsplit

from defusedxml import ElementTree

MAX_ARCHIVE_MEMBERS = 2000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def _meaningful_text(text: str, extension: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
    if extension in {".xlsx", ".xls"}:
        text = "\n".join(line for line in text.splitlines() if not line.startswith("## "))
        text = text.strip(" \t\r\n|-:")
    return text.strip()


def _epub_chapters(archive: zipfile.ZipFile) -> list[bytes]:
    """Validate the entire spine before a converter can skip missing members."""
    names = set(archive.namelist())
    container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
    rootfiles = container.findall(".//{*}rootfile")
    if len(rootfiles) != 1:
        raise ValueError("EPUB must declare one package document")

    def member_path(base: str, href: str) -> str:
        uri = urlsplit(href)
        if uri.scheme or uri.netloc or uri.query or uri.fragment:
            raise ValueError("EPUB content references must be local archive paths")
        decoded = unquote(uri.path)
        resolved = posixpath.normpath(posixpath.join(base, decoded))
        if decoded.startswith("/") or resolved == ".." or resolved.startswith("../") or resolved not in names:
            raise ValueError("EPUB references missing or invalid content")
        return resolved

    opf_path = member_path("", rootfiles[0].get("full-path", ""))
    package = ElementTree.fromstring(archive.read(opf_path))
    manifest = {}
    for item in package.findall("./{*}manifest/{*}item"):
        identifier = item.get("id")
        if not identifier or identifier in manifest:
            raise ValueError("EPUB has invalid manifest identifiers")
        manifest[identifier] = item
    spine = package.findall("./{*}spine/{*}itemref")
    if not spine:
        raise ValueError("EPUB has no content spine")
    chapters = []
    for reference in spine:
        item = manifest.get(reference.get("idref"))
        if item is None:
            raise ValueError("EPUB spine references an unknown manifest item")
        if item.get("media-type") not in {"application/xhtml+xml", "text/html"}:
            raise ValueError("EPUB spine content is not supported HTML")
        path = member_path(posixpath.dirname(opf_path), item.get("href", ""))
        raw = archive.read(path)
        if item.get("media-type") == "application/xhtml+xml":
            ElementTree.fromstring(raw)
        from bs4 import BeautifulSoup

        body = BeautifulSoup(raw, "html.parser").find("body")
        if body is None:
            raise ValueError("EPUB chapter has no HTML body")
        chapters.append(str(body).encode("utf-8"))
    return chapters


def convert_document_snapshot(raw: bytes, extension: str) -> str:
    """Convert only the supplied type, with no generic fallback or remote clients."""
    chapters = []
    if extension in {".docx", ".pptx", ".xlsx", ".epub"}:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("Document archive exceeds member limit")
            if sum(member.file_size for member in members) > MAX_ARCHIVE_BYTES:
                raise ValueError("Document archive exceeds expanded byte limit")
            if len({member.filename for member in members}) != len(members):
                raise ValueError("Document archive has duplicate members")
            if any(member.flag_bits & 1 for member in members):
                raise ValueError("Encrypted document archives are unsupported")
            if extension == ".epub":
                chapters = _epub_chapters(archive)
    elif extension in {".html", ".htm", ".csv", ".json", ".xml"}:
        binary_signatures = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\xd0\xcf\x11\xe0", b"%PDF-",
                             b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"\x7fELF", b"\x1f\x8b")
        if raw.startswith(binary_signatures) or b"\x00" in raw:
            raise ValueError("Binary source does not match declared text type")
    from markitdown import MarkItDown, StreamInfo
    from markitdown.converters import (
        DocxConverter,
        EpubConverter,
        HtmlConverter,
        OutlookMsgConverter,
        PlainTextConverter,
        PptxConverter,
        XlsConverter,
        XlsxConverter,
    )

    converters = {
        ".docx": DocxConverter, ".pptx": PptxConverter, ".xlsx": XlsxConverter,
        ".xls": XlsConverter, ".msg": OutlookMsgConverter, ".epub": EpubConverter,
        ".html": HtmlConverter, ".htm": HtmlConverter, ".csv": PlainTextConverter,
        ".json": PlainTextConverter, ".xml": PlainTextConverter,
    }
    converter = MarkItDown(enable_builtins=False, enable_plugins=False)
    converter.register_converter(converters[extension]())
    if extension == ".epub":
        # Metadata and image placeholders cannot qualify an otherwise empty book.
        html_converter = HtmlConverter()
        if not any(_meaningful_text(html_converter.convert(
            io.BytesIO(chapter), StreamInfo(extension=".html"), keep_data_uris=False,
        ).text_content, ".html") for chapter in chapters):
            raise ValueError("EPUB has no extractable chapter content")
    result = converter.convert_stream(
        io.BytesIO(raw), stream_info=StreamInfo(extension=extension), keep_data_uris=False,
    )
    text = result.text_content
    if not _meaningful_text(text, extension):
        raise ValueError("Document has no extractable content")
    return text
