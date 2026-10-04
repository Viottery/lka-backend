"""Loading evidence must not silently reuse the search-snippet truncator."""

from types import SimpleNamespace

import pytest

from app.api.routes.knowledge import load_knowledge_chunks
from app.api.schemas import KnowledgeChunkLoadRequest
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry
from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput
from app.tool_packages.knowledge import KNOWLEDGE_PACKAGE, LoadKnowledgeChunksTool
from tests.test_knowledge_service import _service


def imported_chunk(service, text, *, chunk_chars=1800):
    imported = service.import_text_document(KnowledgeDocumentInput(
        source=KnowledgeSourceInput(display_name="evidence", uri="local://evidence",
                                    sensitivity="public", remote_policy="allow"),
        title="Evidence", text=text, uri="local://evidence", chunk_chars=chunk_chars,
    ))
    return service.load_document(document_id=imported.document_id).chunk_ids[0]


def test_explicit_chunk_load_reads_tail_beyond_search_snippet_limit(tmp_path):
    service = _service(tmp_path)
    text = "背景" * 700 + "\n最终依据：BRIDGE_823。"
    chunk_id = imported_chunk(service, text)
    loaded = service.load_chunks(chunk_ids=[chunk_id], max_chars_per_chunk=1800).chunks[0]
    assert loaded.text == text and "BRIDGE_823" in loaded.text
    assert loaded.char_count == len(loaded.text) == len(text)
    assert loaded.total_chars == len(text) and loaded.truncated is False


def test_partial_chunk_is_exact_visible_prefix_with_truthful_coverage(tmp_path):
    service = _service(tmp_path)
    text = "本地证据🙂" * 200
    chunk_id = imported_chunk(service, text)
    loaded = service.load_chunks(chunk_ids=[chunk_id], max_chars_per_chunk=200).chunks[0]
    assert loaded.text == text[:200]
    assert loaded.char_count == 200 and loaded.total_chars == len(text)
    assert loaded.truncated is True


def test_api_and_registered_tool_can_request_one_full_maximum_chunk(tmp_path):
    service = _service(tmp_path)
    text = "x" * 5700 + "FINAL_EVIDENCE_417"
    chunk_id = imported_chunk(service, text, chunk_chars=6000)
    request = KnowledgeChunkLoadRequest(chunk_ids=[chunk_id], max_chars_per_chunk=6000)
    registry = ToolRegistry()
    registry.register_package(KNOWLEDGE_PACKAGE)
    registry.register_tool(LoadKnowledgeChunksTool(service))
    result = ToolExecutor(registry).execute(invocation_id="load", tool_name="knowledge.load_chunks",
        tool_input=request.model_dump(), context=ToolContext(session_id="quality"))
    assert result.status == "completed"
    chunk = result.output["chunks"][0]
    assert chunk["text"] == text and chunk["truncated"] is False
    assert chunk["char_count"] == len(chunk["text"])
    with pytest.raises(ValueError):
        KnowledgeChunkLoadRequest(chunk_ids=[chunk_id], max_chars_per_chunk=6001)


def test_redaction_expansion_can_be_read_to_the_end_without_revealing_original(tmp_path):
    service = _service(tmp_path)
    original = "a@b.co " * 850 + "END"
    imported = service.import_text_document(KnowledgeDocumentInput(
        source=KnowledgeSourceInput(display_name="private-view", uri="local://private-view",
                                    sensitivity="personal", remote_policy="redact"),
        title="Private view", text=original, uri="local://private-view", chunk_chars=6000,
    ))
    chunk_id = service.load_document(document_id=imported.document_id).chunk_ids[0]
    expected = service.privacy_gateway.redact(original)
    offset, text = 0, ""
    while True:
        page = service.load_chunks(chunk_ids=[chunk_id], max_chars_per_chunk=6000, offset=offset).chunks[0]
        assert "a@b.co" not in page.text and len(page.text) <= 6000
        assert page.offset == offset and page.total_chars == len(expected)
        assert page.char_count == len(page.text) and page.truncated is True
        text += page.text
        if page.next_offset is None:
            break
        assert page.next_offset > offset
        offset = page.next_offset
    assert text == expected and text.endswith("END")


def test_route_and_runtime_forward_unicode_offset_and_recheck_source_scope(tmp_path):
    service = _service(tmp_path)
    text = "知识🙂" * 200
    chunk_id = imported_chunk(service, text)
    runtime = LocalKnowledgeAgentRuntime.__new__(LocalKnowledgeAgentRuntime)
    runtime.knowledge_service = service
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    result = load_knowledge_chunks(KnowledgeChunkLoadRequest(
        chunk_ids=[chunk_id], max_chars_per_chunk=200, offset=200), request)
    assert result.chunks[0].text == text[200:400]
    assert result.chunks[0].next_offset == 400
    assert service.load_chunks(chunk_ids=[chunk_id], source_ids=[], offset=200).chunks == []


@pytest.mark.parametrize("offset", [-1, True, 1.5])
def test_bad_offset_is_rejected(tmp_path, offset):
    with pytest.raises(ValueError, match="offset"):
        _service(tmp_path).load_chunks(chunk_ids=[], offset=offset)
