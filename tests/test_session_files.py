from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.main import create_app
from app.api.routes import session_files
from app.api.routes.session_files import (
    list_session_files,
    preview_session_file,
    preview_session_raw_file,
)
from app.core.config import get_settings


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Named pipes require POSIX")
@pytest.mark.parametrize("preview", ["preview_session_file", "preview_session_raw_file"])
def test_session_file_preview_rejects_fifo_without_waiting(tmp_path, preview):
    fifo = tmp_path / "pipe.pdf"
    os.mkfifo(fifo)
    # Keep the regression bounded even if a future change restores blocking open.
    script = """
import sys
from pathlib import Path
from types import SimpleNamespace
from fastapi import HTTPException
from app.api.routes import session_files
root = Path(sys.argv[1])
session_files._get_workspace = lambda request, session_id: root
request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'))
try:
    getattr(session_files, sys.argv[2])('session', request, path='pipe.pdf')
except HTTPException as exc:
    assert exc.status_code == 400, exc
else:
    raise AssertionError('FIFO was accepted')
"""
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), preview],
        timeout=5,
        check=True,
        capture_output=True,
        text=True,
    )


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


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-relative opening")
@pytest.mark.parametrize("preview", [preview_session_file, preview_session_raw_file])
@pytest.mark.parametrize("replace_root", [False, True])
def test_session_file_preview_rejects_ancestor_replacement(
    tmp_path, monkeypatch, preview, replace_root,
):
    root = tmp_path / "workspace"
    folder = root / "nested"
    folder.mkdir(parents=True)
    (folder / "note.pdf").write_bytes(b"%PDF-safe")
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "nested" / "note.pdf").write_bytes(b"%PDF-secret")
    (outside / "note.pdf").write_bytes(b"%PDF-secret")
    monkeypatch.setattr(session_files, "_get_workspace", lambda request, session_id: root)
    original = session_files._file_descriptor_no_follow

    def replace_ancestor_then_open(path):
        ancestor = root if replace_root else folder
        ancestor.rename(tmp_path / "original")
        ancestor.symlink_to(outside, target_is_directory=True)
        return original(path)

    monkeypatch.setattr(session_files, "_file_descriptor_no_follow", replace_ancestor_then_open)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    with pytest.raises(HTTPException) as response:
        preview("session", request, path="nested/note.pdf")
    assert response.value.status_code == 403


@pytest.mark.skipif(os.name != "posix", reason="POSIX symbolic links")
@pytest.mark.parametrize("endpoint", [list_session_files, preview_session_file, preview_session_raw_file])
def test_session_files_reject_bound_root_replaced_before_request(tmp_path, endpoint):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.pdf").write_bytes(b"%PDF-secret")
    detail = SimpleNamespace(session=SimpleNamespace(workspace=SimpleNamespace(backend_path=str(root))))
    runtime = SimpleNamespace(get_session=lambda **kwargs: detail)
    request = _request(SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    root.rename(tmp_path / "original")
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException) as response:
        endpoint("session", request, path="" if endpoint is list_session_files else "note.pdf")
    assert response.value.status_code == 403


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-relative listing")
@pytest.mark.parametrize("replace_root", [False, True])
def test_session_file_listing_rejects_ancestor_replacement(tmp_path, monkeypatch, replace_root):
    root = tmp_path / "workspace"
    folder = root / "nested"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "secret.txt").write_text("secret")
    monkeypatch.setattr(session_files, "_get_workspace", lambda request, session_id: root)
    original = session_files._open_posix_path_no_follow

    def replace_ancestor_then_open(path, flags):
        ancestor = root if replace_root else folder
        ancestor.rename(tmp_path / "original")
        ancestor.symlink_to(outside, target_is_directory=True)
        return original(path, flags)

    monkeypatch.setattr(session_files, "_open_posix_path_no_follow", replace_ancestor_then_open)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    with pytest.raises(HTTPException) as response:
        list_session_files("session", request, path="nested")
    assert response.value.status_code == 403


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
