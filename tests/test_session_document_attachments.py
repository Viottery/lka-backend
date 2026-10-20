from __future__ import annotations

import hashlib
import io
import json
import zipfile
from urllib.parse import quote

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from test_session_attachments import Sessions, image_bytes, make_client

from app.domains.session_attachments import (
    SessionAttachmentError,
    SessionAttachmentService,
)


@pytest.fixture
def service(tmp_path):
    return SessionAttachmentService(tmp_path, Sessions())


@pytest.fixture
def anyio_backend():
    return "asyncio"


def upload(service, raw=b"document sentinel", filename="note.txt", media_type="", session_id="one"):
    return service.upload(session_id=session_id, filename=filename, media_type=media_type, raw=raw)


def _pdf(text="PDF sentinel"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if text:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def docx_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DOCX sentinel</w:t></w:r></w:p></w:body></w:document>')
    return stream.getvalue()


@pytest.mark.parametrize("filename,media_type,raw,processor,sentinel", [
    ("note.txt", "text/plain", b"text sentinel", "native_text", "text sentinel"),
    ("note.md", "application/octet-stream", b"# markdown sentinel", "native_text", "markdown sentinel"),
    ("report.pdf", "application/pdf", _pdf(), "native_pdf", "PDF sentinel"),
    ("report.docx", "", docx_bytes(), "markitdown", "DOCX sentinel"),
])
def test_round_trip_and_cached_extraction(service, monkeypatch, filename, media_type, raw, processor, sentinel):
    metadata = upload(service, raw, filename, media_type)
    identifier = metadata["attachment_id"]
    assert metadata["kind"] == "document" and metadata["processor"] == processor
    assert metadata["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "text" not in metadata
    folder = service.root / hashlib.sha256(b"one").hexdigest()
    assert (folder / (identifier + ".document")).read_bytes() == raw
    assert (folder / (identifier + ".text")).is_file()
    reopened = SessionAttachmentService(service.root.parent, service.session_service)
    monkeypatch.setattr(reopened.file_processor, "process_bytes", lambda *args: pytest.fail("reparsed cache"))
    assert reopened.get(session_id="one", attachment_id=identifier) == metadata
    assert reopened.read_file(session_id="one", attachment_id=identifier) == (metadata, raw)
    result = reopened.read_document(session_id="one", attachment_id=identifier)
    assert sentinel in result["text"]
    assert result["total_chars"] == len(result["text"]) == metadata["total_chars"]
    if filename.endswith(".pdf"):
        assert result["extraction"]["text_layer_only"] is True
        assert result["extraction"]["visual_fidelity_verified"] is False
    with pytest.raises(KeyError):
        reopened.read_image(session_id="one", attachment_id=identifier)


@pytest.mark.parametrize("filename,media_type,raw,status", [
    ("note.txt", "application/pdf", b"text", 415),
    ("note.pdf", "text/plain", _pdf(), 415),
    ("note.zip", "application/octet-stream", b"text", 415),
    ("note.doc", "application/octet-stream", b"text", 415),
    ("note.pdf", "application/pdf", b"broken", 422),
    ("note.docx", "", b"broken", 422),
    ("note.txt", "", b"\x00binary", 422),
    ("note.md", "", b"%PDF-pretending", 422),
    ("note.txt", "", b"\xef\xbb\xbf%PDF-pretending", 422),
    ("note.text", "", b"PK\x03\x04pretending", 422),
    ("note.txt", "", b"\xff\xfe", 422),
    ("note.txt", "", b"  \n\t", 422),
    ("note.pdf", "", _pdf(""), 422),
])
def test_bad_documents_rejected_before_storage(service, filename, media_type, raw, status):
    with pytest.raises(SessionAttachmentError) as error:
        upload(service, raw, filename, media_type)
    assert error.value.status_code == status
    if filename == "note.pdf" and raw == _pdf(""):
        assert "OCR" in str(error.value)
    assert not service.root.exists()


def test_encrypted_pdf_and_source_size(service, monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=10, height=10)
    writer.encrypt("secret")
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(SessionAttachmentError) as error:
        upload(service, stream.getvalue(), "encrypted.pdf")
    assert error.value.status_code == 422
    monkeypatch.setattr("app.domains.session_attachments.MAX_DOCUMENT_BYTES", 16)
    with pytest.raises(SessionAttachmentError) as error:
        upload(service, b"x" * 17)
    assert error.value.status_code == 413
    assert not service.root.exists()


def test_bounded_continuation_and_search(service):
    metadata = upload(service, "你好 alpha\n".encode() * 10)
    args = {"session_id": "one", "attachment_id": metadata["attachment_id"]}
    pieces = []
    offset = 0
    while True:
        result = service.read_document(**args, start_char=offset, max_chars=7)
        pieces.append(result["text"])
        assert result["end_char"] - result["start_char"] <= 7
        if result["next_start_char"] is None:
            break
        offset = result["next_start_char"]
    assert "".join(pieces) == ("你好 alpha\n" * 10).strip()
    result = service.read_document(**args, start_char=metadata["total_chars"])
    assert result["text"] == "" and result["next_start_char"] is None
    found = service.find_document(**args, query="alpha", max_matches=3)
    assert len(found["matches"]) == 3 and found["truncated"]
    continuation = service.find_document(**args, query="alpha", start_char=found["next_start_char"])
    assert len(continuation["matches"]) == 7 and continuation["next_start_char"] is None
    for bounds in ({"start_char": -1}, {"start_char": metadata["total_chars"] + 1},
                   {"max_chars": 0}, {"max_chars": 64001}, {"start_char": True}):
        with pytest.raises(SessionAttachmentError):
            service.read_document(**args, **bounds)
    for bounds in ({"query": ""}, {"query": "a" * 257}, {"query": "a", "max_matches": 101}):
        with pytest.raises(SessionAttachmentError):
            service.find_document(**args, **bounds)


@pytest.mark.parametrize("tamper", ["original", "text", "text_length", "text_hash", "symlink", "parent_symlink"])
def test_tampered_cache_rejected_on_every_access(service, tmp_path, tamper):
    metadata = upload(service)
    identifier = metadata["attachment_id"]
    folder = service.root / hashlib.sha256(b"one").hexdigest()
    original = folder / (identifier + ".document")
    text = original.with_suffix(".text")
    if tamper == "original":
        original.write_bytes(b"changed sentinel!")
    elif tamper == "text":
        text.write_bytes(b"changed sentinel!")
    elif tamper in {"text_hash", "text_length"}:
        metadata["text_sha256" if tamper == "text_hash" else "total_chars"] = "bad"
        original.with_suffix(".json").write_text(json.dumps(metadata))
    elif tamper == "symlink":
        target = tmp_path / "outside"
        target.write_bytes(text.read_bytes())
        text.unlink()
        text.symlink_to(target)
    else:
        target = tmp_path / "outside"
        folder.rename(target)
        folder.symlink_to(target, target_is_directory=True)
    for operation in (service.get, service.read_file, service.read_document, service.read_input):
        with pytest.raises(KeyError):
            operation(session_id="one", attachment_id=identifier)


def test_session_scope_and_legacy_images(service):
    metadata = upload(service, filename="../../note.txt")
    assert metadata["filename"] == "note.txt"
    for session in ("two", "missing"):
        with pytest.raises(KeyError):
            service.read_document(session_id=session, attachment_id=metadata["attachment_id"])
    service.session_service.active.remove("one")
    with pytest.raises(KeyError):
        service.get(session_id="one", attachment_id=metadata["attachment_id"])
    old = service.upload(session_id="two", filename="old.png", media_type="image/png", raw=image_bytes())
    old.pop("kind")
    folder = service.root / hashlib.sha256(b"two").hexdigest()
    (folder / (old["attachment_id"] + ".json")).write_text(json.dumps(old))
    assert service.read_image(session_id="two", attachment_id=old["attachment_id"])[0] == old


@pytest.mark.anyio
async def test_api_document_metadata_raw_and_restrictions(service):
    async with make_client(service) as client:
        response = await client.post("/sessions/one/attachments", content=b"<h1>sentinel</h1>",
                                     headers={"Content-Type": "text/html", "X-Filename": quote('资料".html')})
        assert response.status_code == 200
        metadata = response.json()
        assert metadata["kind"] == "document" and "text" not in metadata
        path = f"/sessions/one/attachments/{metadata['attachment_id']}"
        assert (await client.get(path)).json() == metadata
        raw = await client.get(path + "/raw")
        assert raw.content == b"<h1>sentinel</h1>"
        assert raw.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
        assert raw.headers["x-content-type-options"] == "nosniff"
        assert (await client.get(path.replace("/one/", "/two/"))).status_code == 404
        invalid = await client.post("/sessions/one/attachments", content=b"hello",
                                    headers={"Content-Type": "application/pdf", "X-Filename": "note.txt"})
        assert invalid.status_code == 415
    async with make_client(service, "192.0.2.1") as remote:
        assert (await remote.post("/sessions/one/attachments", content=b"hello",
                                 headers={"X-Filename": "note.txt"})).status_code == 403
        assert (await remote.get(path + "/raw")).status_code == 403


@pytest.mark.anyio
async def test_document_stream_has_256_mib_cap(service, monkeypatch):
    async def stream():
        for _ in range(9):
            yield b"x" * 2
    monkeypatch.setattr("app.domains.session_attachments.MAX_DOCUMENT_BYTES", 16)
    async with make_client(service) as client:
        result = await client.post("/sessions/one/attachments", content=stream(),
                                   headers={"X-Filename": "note.txt", "Content-Type": "application/octet-stream"})
        assert result.status_code == 413
        assert "Attachment exceeds" in result.json()["detail"]
    assert not service.root.exists()


def test_pdf_metadata_lists_are_bounded(service):
    writer = PdfWriter()
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(_pdf()))
    for _ in range(25):
        writer.add_page(reader.pages[0])
    stream = io.BytesIO()
    writer.write(stream)
    metadata = upload(service, stream.getvalue(), "many-pages.pdf")
    extraction = metadata["extraction"]
    assert extraction["page_count"] == 25
    assert len(extraction["page_spans"]) == 20
    assert extraction["page_spans_count"] == 25
    assert extraction["page_spans_truncated"] is True
    assert metadata["total_chars"] > extraction["page_spans"][-1]["end_char"]


def test_input_snapshot_reads_each_file_once(service, monkeypatch):
    raw = b"single validated document snapshot"
    metadata = upload(service, raw)
    reads = []
    original_read = service._read

    def counted_read(path, limit):
        reads.append(path.suffix)
        return original_read(path, limit)

    monkeypatch.setattr(service, "_read", counted_read)
    result_metadata, result_raw, preview = service.read_input(
        session_id="one", attachment_id=metadata["attachment_id"], max_chars=6,
    )
    assert reads == [".json", ".document", ".text"]
    assert (result_metadata, result_raw) == (metadata, raw)
    assert preview["text"] == "single" and preview["next_start_char"] == 6
    assert preview == service.read_document(session_id="one", attachment_id=metadata["attachment_id"], max_chars=6)
    with pytest.raises(KeyError):
        service.read_input(session_id="two", attachment_id=metadata["attachment_id"])
    for max_chars in (0, 64001, True):
        with pytest.raises(SessionAttachmentError):
            service.read_input(session_id="one", attachment_id=metadata["attachment_id"], max_chars=max_chars)


def test_image_input_snapshot_has_no_document_preview(service):
    raw = image_bytes()
    metadata = service.upload(session_id="one", filename="image.png", media_type="image/png", raw=raw)
    assert service.read_input(session_id="one", attachment_id=metadata["attachment_id"]) == (metadata, raw, None)
