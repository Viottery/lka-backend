from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NullObject,
    NumberObject,
)

from app.core.context_driver import ToolView
from app.core.tools import ToolContext, ToolInvocation
from app.domains.documents import DocumentService
from app.tool_packages.documents import create_documents_tools


def make_pdf(path: Path, texts: list[str], *, encrypted: bool = False) -> Path:
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_outline_item("Start", 0)
    if encrypted:
        writer.encrypt("secret")
    writer.write(path)
    return path


def call(tools, operation, inputs, context=None):
    tool = tools[operation]
    context = context or ToolContext(session_id="s")
    return tool.invoke(invocation=ToolInvocation(invocation_id="i", session_id=context.session_id,
                                                context_id="c", tool=tool.spec, input=inputs), context=context)


def tool_map(root):
    return {tool.operation: tool for tool in create_documents_tools([root])}


def test_open_is_lazy_and_page_extraction_reuses_cache(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "test.pdf", ["page one", "page two", "page three"])
    service = DocumentService()
    from pypdf._page import PageObject
    original = PageObject.extract_text
    calls = []

    def tracked(page, *args, **kwargs):
        calls.append(page)
        return original(page, *args, **kwargs)
    monkeypatch.setattr(PageObject, "extract_text", tracked)
    opened = service.open(source)
    assert calls == []
    assert opened["page_count"] == 3
    assert opened["outline"][0] == {"title": "Start", "page": 1}
    first = service.read_pages(source, opened["sha256"], start_page=2)
    assert first["pages"][0]["text"] == "page two"
    assert first["next"] == {"start_page": 3, "start_char": 0}
    service.read_pages(source, opened["sha256"], start_page=2)
    assert len(calls) == 1


def test_text_continuation_preserves_all_characters(tmp_path):
    source = make_pdf(tmp_path / "test.pdf", ["abcdefghij", "klmnop"])
    service = DocumentService()
    sha = service.open(source)["sha256"]
    cursor = {"start_page": 1, "start_char": 0}
    slices = []
    while cursor:
        result = service.read_pages(source, sha, max_chars=3, **cursor)
        slices.extend(page["text"] for page in result["pages"])
        cursor = result["next"]
    assert "".join(slices) == "abcdefghijklmnop"
    assert all(not page["visual_fidelity_verified"] for page in result["pages"])


def test_search_bounded_and_continuable(tmp_path):
    source = make_pdf(tmp_path / "test.pdf", ["needle needle", "needle", "other"])
    service = DocumentService()
    sha = service.open(source)["sha256"]
    result = service.find(source, sha, query="needle", max_pages=1, max_matches=1)
    assert result["matches"][0]["page"] == 1
    assert result["pages_scanned"] == 1
    second = service.find(source, sha, query="needle", max_pages=1, max_matches=1, **result["next"])
    assert second["matches"][0]["start_char"] == 7
    third = service.find(source, sha, query="needle", max_matches=10, **second["next"])
    assert [item["page"] for item in third["matches"]] == [2]
    assert third["next"] is None


def test_empty_page_is_not_successful_complete_extraction(tmp_path):
    source = make_pdf(tmp_path / "test.pdf", [""])
    service = DocumentService()
    sha = service.open(source)["sha256"]
    page = service.read_pages(source, sha)["pages"][0]
    assert page["text_status"] == "no_extractable_text"
    assert page["needs_visual"] is True
    assert not page["visual_fidelity_verified"]
    assert service.find(source, sha, query="anything")["pages_without_text"] == [1]


def test_changed_corrupt_and_encrypted_files_fail(tmp_path):
    source = make_pdf(tmp_path / "test.pdf", ["before"])
    service = DocumentService()
    sha = service.open(source)["sha256"]
    make_pdf(source, ["after"])
    with pytest.raises(ValueError, match="changed"):
        service.read_pages(source, sha)
    source.write_bytes(b"not a pdf")
    with pytest.raises(ValueError, match="Invalid PDF"):
        service.open(source)
    make_pdf(source, ["secret"], encrypted=True)
    with pytest.raises(ValueError, match="Encrypted"):
        service.open(source)


def test_every_call_rechecks_exact_external_approval_and_child_scope(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    source = make_pdf(tmp_path / "outside.pdf", ["outside"])
    tools = tool_map(workspace)
    authorized = ToolContext(session_id="s", safety_review_approved=True, approved_paths=[str(source)])
    assert tools["open"].workspace_access_request({"path": str(source)}, authorized) == [str(source)]
    opened = call(tools, "open", {"path": str(source)}, authorized)
    assert opened.status == "completed"
    inputs = {"path": str(source), "expected_sha256": opened.output["sha256"]}
    assert call(tools, "read_pages", inputs).status == "failed"
    authorized.tool_view = ToolView(snapshot_id="snapshot", child_run_id="child", side_effect_level="read", allowed_paths=(str(workspace),))
    assert call(tools, "read_pages", inputs, authorized).status == "failed"


def test_workspace_symlink_retarget_cannot_reuse_cached_authority(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    source = make_pdf(workspace / "local.pdf", ["same"])
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(source.read_bytes())
    link = workspace / "alias.pdf"
    link.symlink_to(source)
    tools = tool_map(workspace)
    opened = call(tools, "open", {"path": "alias.pdf"})
    link.unlink()
    link.symlink_to(outside)
    result = call(tools, "read_pages", {"path": "alias.pdf", "expected_sha256": opened.output["sha256"]})
    assert result.status == "failed"


def test_render_preserves_source_and_existing_outputs(tmp_path):
    source = make_pdf(tmp_path / "test.pdf", ["visible"])
    tools = tool_map(tmp_path)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "page.png"
    inputs = {"path": str(source), "expected_sha256": sha, "page": 1, "output_path": str(output)}
    assert tools["render_page"].spec.read_only is False
    result = call(tools, "render_page", inputs)
    assert result.status == "completed", result.error
    assert output.read_bytes().startswith(b"\x89PNG")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == sha
    assert call(tools, "render_page", inputs).status == "failed"
    assert result.output["page"] == 1
    assert result.output["width"] == 500
    assert PdfReader(source).pages[0].extract_text() == "visible"


def test_render_output_requires_separate_exact_approval(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    source = make_pdf(workspace / "test.pdf", ["visible"])
    tools = tool_map(workspace)
    output = tmp_path / "outside.png"
    inputs = {"path": str(source), "expected_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "page": 1, "output_path": str(output)}
    assert call(tools, "render_page", inputs).status == "failed"
    assert not output.exists()
    assert tools["render_page"].workspace_access_request(inputs, ToolContext(session_id="s")) == [str(output)]
    authorized = ToolContext(session_id="s", safety_review_approved=True, approved_paths=[str(output)])
    assert call(tools, "render_page", inputs, authorized).status == "completed"


def test_image_only_page_exposes_visual_requirement(tmp_path):
    source = tmp_path / "scan.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    image = DecodedStreamObject()
    image.set_data(bytes([255, 0, 0]))
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(1),
                  NameObject("/Height"): NumberObject(1),
                  NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                  NameObject("/BitsPerComponent"): NumberObject(8)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): writer._add_object(image)})})
    stream = DecodedStreamObject()
    stream.set_data(b"q 200 0 0 200 10 10 cm /Im1 Do Q")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(source)
    service = DocumentService()
    sha = service.open(source)["sha256"]
    extracted = service.read_pages(source, sha)["pages"][0]
    assert extracted["direct_image_count"] == 1
    assert extracted["needs_visual"] is True
    assert extracted["text_status"] == "no_extractable_text"
    rendered = service.render_page(source, sha, page=1, output_path=tmp_path / "scan.png")
    assert rendered["bytes_written"] > 0


def test_layout_read_and_bounds_are_explicit(tmp_path):
    source = make_pdf(tmp_path / "test.pdf", ["layout"])
    service = DocumentService()
    sha = service.open(source)["sha256"]
    page = service.read_pages(source, sha, mode="layout")["pages"][0]
    assert "layout" in page["text"]
    assert page["extraction_mode"] == "layout"
    with pytest.raises(ValueError, match="start_char"):
        service.read_pages(source, sha, start_char=100)
    with pytest.raises(ValueError, match="page range"):
        service.read_pages(source, sha, start_page=2)


@pytest.mark.parametrize("malformed", ["resources_null", "xobjects_null", "image_null"])
def test_malformed_resource_dictionaries_fail_explicitly(tmp_path, malformed):
    source = tmp_path / "malformed.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    resources = {
        "resources_null": NullObject(),
        "xobjects_null": DictionaryObject({NameObject("/XObject"): NullObject()}),
        "image_null": DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): NullObject()})}),
    }
    page[NameObject("/Resources")] = resources[malformed]
    writer.write(source)
    tools = tool_map(tmp_path)
    opened = call(tools, "open", {"path": str(source)})
    assert opened.status == "completed"
    result = call(tools, "read_pages", {"path": str(source), "expected_sha256": opened.output["sha256"]})
    assert result.status == "failed"
    assert "not a dictionary" in result.error
    assert result.output == {}
    rendered = call(tools, "render_page", {"path": str(source), "expected_sha256": opened.output["sha256"],
                                          "page": 1, "output_path": str(tmp_path / "page.png")})
    assert rendered.status == "completed", rendered.error


def test_layout_preserves_rotated_text(tmp_path):
    source = make_pdf(tmp_path / "rotated.pdf", ["normal"])
    writer = PdfWriter(clone_from=source)
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 200 Td (normal) Tj ET\nBT /F1 12 Tf 0 1 -1 0 200 20 Tm (rotated) Tj ET")
    writer.pages[0][NameObject("/Contents")] = writer._add_object(stream)
    writer.write(source)
    service = DocumentService()
    sha = service.open(source)["sha256"]
    plain = service.read_pages(source, sha)["pages"][0]["text"]
    layout = service.read_pages(source, sha, mode="layout")["pages"][0]["text"]
    assert "normal" in plain and "rotated" in plain
    assert "normal" in layout and "rotated" in layout


def test_text_with_form_or_annotation_still_requires_visual_inspection(tmp_path):
    source = make_pdf(tmp_path / "form.pdf", ["caption"])
    writer = PdfWriter(clone_from=source)
    form = DecodedStreamObject()
    form.set_data(b"0 0 100 100 re f")
    from pypdf.generic import ArrayObject
    form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                 NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(100), NumberObject(100)])})
    page = writer.pages[0]
    page["/Resources"][NameObject("/XObject")] = DictionaryObject({NameObject("/Fm1"): writer._add_object(form)})
    writer.write(source)
    service = DocumentService()
    sha = service.open(source)["sha256"]
    result = service.read_pages(source, sha)["pages"][0]
    assert result["text_status"] == "text_layer"
    assert result["direct_image_count"] == 0
    assert result["has_form_xobjects"] and result["needs_visual"]
    assert result["has_annotations"] is False

    writer = PdfWriter(clone_from=source)
    writer.pages[0]["/Resources"].pop("/XObject")
    writer.add_annotation(0, {"/Type": "/Annot", "/Subtype": "/Text", "/Rect": [20, 20, 40, 40], "/Contents": "Important visual note"})
    writer.write(source)
    sha = service.open(source)["sha256"]
    result = service.read_pages(source, sha)["pages"][0]
    assert result["text_status"] == "text_layer"
    assert result["has_annotations"] and result["needs_visual"]
    assert result["has_form_xobjects"] is False


def test_render_pixel_limit_uses_rounded_bitmap_dimensions(tmp_path, monkeypatch):
    import pypdfium2 as pdfium
    source = make_pdf(tmp_path / "large.pdf", ["large"])
    writer = PdfWriter(clone_from=source)
    writer.pages[0].mediabox.upper_right = (3999.9, 4000.1)
    writer.write(source)
    service = DocumentService()
    sha = service.open(source)["sha256"]

    def unexpected_render(*args, **kwargs):
        pytest.fail("pixel bound must be checked before bitmap allocation")
    monkeypatch.setattr(pdfium.PdfPage, "render", unexpected_render)
    with pytest.raises(ValueError, match="16000000 pixels"):
        service.render_page(source, sha, page=1, dpi=72, output_path=tmp_path / "large.png")
    assert not (tmp_path / "large.png").exists()


def test_pdfium_operations_are_serialized_across_services(tmp_path, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import pypdfium2 as pdfium
    from PIL import Image

    source = make_pdf(tmp_path / "source.pdf", ["text"])
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    counter_lock = threading.Lock()
    active = peak = 0

    class Bitmap:
        def to_pil(self):
            return Image.new("RGB", (2, 2), "white")

        def close(self):
            pass

    class Page:
        def get_size(self):
            return 2, 2

        def render(self, **kwargs):
            return Bitmap()

        def close(self):
            pass

    class Document:
        def __init__(self, raw):
            nonlocal active, peak
            with counter_lock:
                active += 1
                peak = max(peak, active)
            # Release the GIL, so a per-service lock would allow overlap.
            time.sleep(0.02)

        def __enter__(self):
            return self

        def __getitem__(self, index):
            return Page()

        def __exit__(self, *args):
            nonlocal active
            with counter_lock:
                active -= 1

    monkeypatch.setattr(pdfium, "PdfDocument", Document)
    start = threading.Barrier(2)

    def render(index):
        service = DocumentService()
        start.wait(timeout=5)
        return service.render_page(source, sha, page=1, output_path=tmp_path / f"page{index}.png")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(render, range(2)))
    assert peak == 1
    assert active == 0
    assert all(result["bytes_written"] > 0 for result in results)


def test_search_match_at_page_end_skips_exhausted_page(tmp_path):
    source = make_pdf(tmp_path / "test.pdf", ["needle", "needle"])
    service = DocumentService()
    sha = service.open(source)["sha256"]
    first = service.find(source, sha, query="needle", max_matches=1)
    assert first["next"] == {"start_page": 2, "start_char": 0}
    second = service.find(source, sha, query="needle", max_matches=1, **first["next"])
    assert second["next"] is None
    assert second["truncated"] is False


def test_existing_output_fails_before_expensive_render(tmp_path, monkeypatch):
    import pypdfium2 as pdfium
    source = make_pdf(tmp_path / "test.pdf", ["text"])
    service = DocumentService()
    sha = service.open(source)["sha256"]
    output = tmp_path / "existing.png"
    output.write_bytes(b"user output")

    def unexpected_document(*args, **kwargs):
        pytest.fail("existing output must be rejected before PDFium rendering")
    monkeypatch.setattr(pdfium, "PdfDocument", unexpected_document)
    with pytest.raises(FileExistsError):
        service.render_page(source, sha, page=1, output_path=output)
    assert output.read_bytes() == b"user output"
