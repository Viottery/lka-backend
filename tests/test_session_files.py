from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.main import create_app
from app.api.routes.session_files import (
    list_session_files,
    preview_session_file,
    preview_session_raw_file,
)
from app.core.config import get_settings


def _session_with_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    monkeypatch.delenv("LKA_WORKSPACE_ROOTS", raising=False)
    get_settings.cache_clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app()
    session = app.state.runtime.create_session(title="file browser test").session
    app.state.runtime.set_session_workspace(
        session_id=session.session_id,
        path=str(workspace),
        platform=app.state.runtime.platform.name,
    )
    return app, session.session_id, workspace


def _request(app, host="127.0.0.1"):
    return SimpleNamespace(app=app, client=SimpleNamespace(host=host))


def test_session_workspace_file_list_and_utf8_preview(tmp_path, monkeypatch):
    app, session_id, workspace = _session_with_workspace(tmp_path, monkeypatch)
    (workspace / "folder").mkdir()
    (workspace / "folder" / "note.txt").write_text("hello 世界\n", encoding="utf-8")
    (workspace / "image.bin").write_bytes(b"\xff\xfe")

    request = _request(app)
    payload = list_session_files(session_id, request, path="")
    assert payload["path"] == ""
    assert {entry["name"]: entry["type"] for entry in payload["entries"]} == {
        "folder": "directory",
        "image.bin": "file",
    }

    nested = list_session_files(session_id, request, path="folder")
    assert nested["entries"][0]["path"] == "folder/note.txt"

    preview = preview_session_file(session_id, request, path="folder/note.txt")
    assert preview["text"] == "hello 世界\n"
    assert preview["truncated"] is False

    with pytest.raises(HTTPException) as binary:
        preview_session_file(session_id, request, path="image.bin")
    assert binary.value.status_code == 415


def test_session_file_preview_is_bounded_and_reports_truncation(tmp_path, monkeypatch):
    app, session_id, workspace = _session_with_workspace(tmp_path, monkeypatch)
    (workspace / "large.txt").write_bytes(b"a" * (256 * 1024 + 20))

    payload = preview_session_file(session_id, _request(app), path="large.txt")
    assert len(payload["text"].encode("utf-8")) == 256 * 1024
    assert payload["truncated"] is True
    assert payload["size_bytes"] == 256 * 1024 + 20


def test_session_file_paths_reject_absolute_traversal_and_symlinks(tmp_path, monkeypatch):
    app, session_id, workspace = _session_with_workspace(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)

    request = _request(app)
    for unsafe in ("../outside.txt", str(outside), "C:/outside.txt"):
        with pytest.raises(HTTPException) as listing:
            list_session_files(session_id, request, path=unsafe)
        assert listing.value.status_code == 400
    with pytest.raises(HTTPException) as link:
        preview_session_file(session_id, request, path="link.txt")
    assert link.value.status_code == 403
    with pytest.raises(HTTPException) as missing:
        list_session_files("missing_session", request)
    assert missing.value.status_code == 404


def test_session_files_reject_unbound_and_deleted_sessions(tmp_path, monkeypatch):
    app, session_id, _workspace = _session_with_workspace(tmp_path, monkeypatch)
    unbound = app.state.runtime.create_session(title="unbound").session

    request = _request(app)
    with pytest.raises(HTTPException) as response:
        list_session_files(unbound.session_id, request)
    assert response.value.status_code == 409
    assert app.state.runtime.delete_session(session_id=session_id)
    with pytest.raises(HTTPException) as deleted:
        list_session_files(session_id, request)
    assert deleted.value.status_code == 404


def test_session_file_list_is_bounded(tmp_path, monkeypatch):
    app, session_id, workspace = _session_with_workspace(tmp_path, monkeypatch)
    for index in range(205):
        (workspace / f"{index:03}.txt").touch()

    response = list_session_files(session_id, _request(app), path="")
    assert len(response["entries"]) == 200
    assert response["truncated"] is True


def test_session_files_are_loopback_only(tmp_path, monkeypatch):
    app, session_id, _workspace = _session_with_workspace(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as remote:
        list_session_files(session_id, _request(app, "192.0.2.1"), path="")
    assert remote.value.status_code == 403


@pytest.mark.parametrize(
    ("filename", "content", "media_type", "has_pdf_csp"),
    [
        ("pixel.png", b"\x89PNG\r\n\x1a\n" + b"png-data", "image/png", False),
        ("photo.jpeg", b"\xff\xd8\xff" + b"jpeg-data", "image/jpeg", False),
        ("image.webp", b"RIFF\x04\x00\x00\x00WEBPpayload", "image/webp", False),
        ("paper.pdf", b"%PDF-1.7\npreview", "application/pdf", True),
    ],
)
def test_session_raw_preview_checks_signature_and_security_headers(
    tmp_path, monkeypatch, filename, content, media_type, has_pdf_csp
):
    app, session_id, workspace = _session_with_workspace(tmp_path, monkeypatch)
    (workspace / filename).write_bytes(content)

    response = preview_session_raw_file(session_id, _request(app), path=filename)

    assert response.body == content
    assert response.media_type == media_type
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert ("content-security-policy" in response.headers) is has_pdf_csp
    if has_pdf_csp:
        assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("looks-like-image.png", b"<script>alert(1)</script>"),
        ("document.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
        ("page.html", b"<html><script>alert(1)</script></html>"),
    ],
)
def test_session_raw_preview_rejects_scriptable_or_mismatched_content(
    tmp_path, monkeypatch, filename, content
):
    app, session_id, workspace = _session_with_workspace(tmp_path, monkeypatch)
    (workspace / filename).write_bytes(content)

    with pytest.raises(HTTPException) as rejected:
        preview_session_raw_file(session_id, _request(app), path=filename)
    assert rejected.value.status_code == 415


def test_session_raw_preview_rejects_large_files(tmp_path, monkeypatch):
    app, session_id, workspace = _session_with_workspace(tmp_path, monkeypatch)
    (workspace / "large.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (8 * 1024 * 1024))

    with pytest.raises(HTTPException) as oversized:
        preview_session_raw_file(session_id, _request(app), path="large.png")
    assert oversized.value.status_code == 413


def test_session_raw_preview_rejects_symlinks(tmp_path, monkeypatch):
    app, session_id, workspace = _session_with_workspace(tmp_path, monkeypatch)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nvalid")
    (workspace / "link.png").symlink_to(outside)

    with pytest.raises(HTTPException) as rejected:
        preview_session_raw_file(session_id, _request(app), path="link.png")
    assert rejected.value.status_code == 403
