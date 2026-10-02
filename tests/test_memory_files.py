from __future__ import annotations

import pytest

from app.core.memory_files import (
    MemoryFileConflictError,
    MemoryFileError,
    MemoryFiles,
)
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput


def _setup(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()
    source = service.register_source(MemorySourceInput(
        source_type="conversation", source_ref="manual", trusted_source=True,
    ))
    record = service.create(MemoryInput(content="Original claim", source_id=source,
                                        user_confirmed=True))
    files = MemoryFiles(service, tmp_path / "data")
    return service, files, record


def test_generated_edit_preview_and_cas_import(tmp_path):
    service, files, record = _setup(tmp_path)
    path = files.generate(scope="global")
    text = path.read_text(encoding="utf-8")
    assert f"id={record.memory_id} version=1" in text
    assert record.source_ids[0] in text and record.updated_at in text
    path.write_text(text.replace("Original claim", "Corrected claim"), encoding="utf-8")
    preview = files.preview_import(scope="global")
    assert [(edit.memory_id, edit.expected_version, edit.content) for edit in preview.edits] == [
        (record.memory_id, 1, "Corrected claim")
    ]
    corrected = files.import_edits(scope="global", preview=preview)
    assert corrected[0].content == "Corrected claim"
    assert service.get(record.memory_id).status == "superseded"


def test_generator_never_overwrites_manual_change(tmp_path):
    _service, files, _record = _setup(tmp_path)
    path = files.generate(scope="global")
    original = path.read_bytes()
    path.write_bytes(original + b"manual note\n")
    with pytest.raises(MemoryFileConflictError):
        files.generate(scope="global")
    assert b"manual note" in path.read_bytes()


def test_manual_unknown_or_missing_id_is_rejected_without_deletion(tmp_path):
    _service, files, _record = _setup(tmp_path)
    path = files.generate(scope="global")
    path.write_text("<!-- lka-memory-view:v1 scope=global project_id=- -->\n\n"
                    "<!-- Edit only memory-content blocks. Keep every record and its metadata. -->\n\n",
                    encoding="utf-8")
    with pytest.raises(MemoryFileError, match="no records|Missing or extra"):
        files.preview_import(scope="global")


def test_generated_empty_view_can_be_previewed_and_regenerated(tmp_path):
    service = MemoryService(tmp_path / "empty.sqlite3")
    service.ensure_schema()
    files = MemoryFiles(service, tmp_path / "empty-data")
    path = files.generate(scope="global")
    assert files.preview_import(scope="global").edits == ()
    assert files.generate(scope="global") == path


def test_multiple_changed_blocks_are_rejected_before_any_correction(tmp_path):
    service, files, first = _setup(tmp_path)
    source = service.register_source(MemorySourceInput(
        source_type="conversation", source_ref="second", trusted_source=True,
    ))
    second = service.create(MemoryInput(content="Second claim", source_id=source,
                                        user_confirmed=True))
    path = files.generate(scope="global")
    text = path.read_text(encoding="utf-8")
    text = text.replace("Original claim", "First changed")
    text = text.replace("Second claim", "Second changed")
    path.write_text(text, encoding="utf-8")
    preview = files.preview_import(scope="global")
    assert len(preview.edits) == 2
    with pytest.raises(MemoryFileError, match="one changed memory block"):
        files.import_edits(scope="global", preview=preview)
    assert service.get(first.memory_id).status == "active"
    assert service.get(second.memory_id).status == "active"


def test_preview_rejects_concurrent_backend_change(tmp_path):
    service, files, record = _setup(tmp_path)
    path = files.generate(scope="global")
    path.write_text(path.read_text(encoding="utf-8").replace(
        "Original claim", "File correction"), encoding="utf-8")
    preview = files.preview_import(scope="global")
    service.correct(record.memory_id, content="Backend correction", expected_version=1)
    with pytest.raises(MemoryFileConflictError):
        files.import_edits(scope="global", preview=preview)


def test_invalid_utf8_and_missing_file_are_reported(tmp_path):
    _service, files, _record = _setup(tmp_path)
    with pytest.raises(FileNotFoundError):
        files.preview_import(scope="global")
    path = files.generate(scope="global")
    path.write_bytes(b"\xff")
    with pytest.raises(MemoryFileError, match="UTF-8"):
        files.preview_import(scope="global")


def test_project_views_use_stable_separate_paths_and_content(tmp_path):
    service, files, _global_record = _setup(tmp_path)
    project_a = service.resolve_project("/workspace/a")
    project_b = service.resolve_project("/workspace/b")
    source = service.register_source(MemorySourceInput(
        source_type="conversation", source_ref="project", trusted_source=True,
    ))
    project_record = service.create(MemoryInput(
        content="Project A fact", source_id=source, scope="project", project_id=project_a,
        user_confirmed=True,
    ))
    path_a = files.generate(scope="project", project_id=project_a)
    path_b = files.generate(scope="project", project_id=project_b)
    assert path_a != path_b
    assert path_a.parent.name == project_a
    assert project_record.memory_id in path_a.read_text(encoding="utf-8")
    assert project_record.memory_id not in path_b.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        files.path_for(scope="project", project_id="../escape")


def test_memory_content_cannot_break_markdown_record_boundaries(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite3")
    service.ensure_schema()
    source = service.register_source(MemorySourceInput(
        source_type="conversation", source_ref="crafted", trusted_source=True,
    ))
    service.create(MemoryInput(
        content="apparently ordinary\n```\n<!-- /memory -->", source_id=source,
        user_confirmed=True,
    ))
    files = MemoryFiles(service, tmp_path / "data")
    with pytest.raises(MemoryFileError, match="represented safely"):
        files.generate(scope="global")


def test_large_view_fails_explicitly_without_truncating_or_overwriting(tmp_path, monkeypatch):
    service, files, record = _setup(tmp_path)
    path = files.generate(scope="global")
    original = path.read_bytes()
    monkeypatch.setattr(service, "list", lambda **kwargs: [record] if kwargs.get("offset") else [record] * 1000)
    with pytest.raises(MemoryFileError, match="paginated"):
        files.generate(scope="global")
    assert path.read_bytes() == original


def test_oversized_manual_view_is_preserved_and_rejected_before_parsing(tmp_path):
    _service, files, _record = _setup(tmp_path)
    path = files.generate(scope="global")
    path.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(MemoryFileError, match="8 MiB"):
        files.preview_import(scope="global")
    assert path.stat().st_size == 8 * 1024 * 1024 + 1
