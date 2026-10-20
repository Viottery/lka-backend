from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from app.api.routes.session_attachments import router
from app.domains.session_attachments import (
    MAX_ATTACHMENT_BYTES,
    SessionAttachmentError,
    SessionAttachmentService,
)


class Sessions:
    def __init__(self):
        self.active = {"one", "two", "../outside"}

    def get_session_or_none(self, *, session_id):
        return SimpleNamespace(session_id=session_id) if session_id in self.active else None

    def get_session(self, *, session_id):
        if session_id not in self.active:
            raise KeyError(session_id)
        return SimpleNamespace(session=SimpleNamespace(session_id=session_id))


@pytest.fixture
def service(tmp_path):
    return SessionAttachmentService(tmp_path, Sessions())


def image_bytes(format="PNG", size=(8, 6), *, animated=False):
    output = io.BytesIO()
    image = Image.new("RGB", size, "red")
    kwargs = {}
    if animated:
        kwargs = {"save_all": True, "append_images": [Image.new("RGB", size, "blue")]}
    image.save(output, format=format, **kwargs)
    return output.getvalue()


def upload(service, raw=None, media_type="image/png", **kwargs):
    return service.upload(
        session_id=kwargs.pop("session_id", "one"),
        filename=kwargs.pop("filename", "picture.png"),
        media_type=media_type,
        raw=image_bytes() if raw is None else raw,
    )


@pytest.mark.parametrize(
    ("format", "media_type"), [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
def test_valid_images_round_trip(service, format, media_type):
    raw = image_bytes(format)
    metadata = upload(service, raw, media_type)
    assert metadata["width"] == 8 and metadata["height"] == 6
    assert metadata["size_bytes"] == len(raw)
    assert metadata["sha256"] == hashlib.sha256(raw).hexdigest()
    assert service.get(session_id="one", attachment_id=metadata["attachment_id"]) == metadata
    assert service.read_image(session_id="one", attachment_id=metadata["attachment_id"]) == (
        metadata,
        raw,
    )
    # Files and metadata remain usable after service reconstruction.
    reopened = SessionAttachmentService(service.root.parent, service.session_service)
    assert reopened.read_image(session_id="one", attachment_id=metadata["attachment_id"]) == (
        metadata,
        raw,
    )


@pytest.mark.parametrize(
    ("raw", "media_type", "status"),
    [
        (b"<svg/>", "image/svg+xml", 415),
        (b"\x89PNG\r\n\x1a\ncorrupt", "image/png", 422),
        (image_bytes(), "image/jpeg", 415),
        (image_bytes()[:-20], "image/png", 422),
        (image_bytes(size=(2001, 2000)), "image/png", 413),
        (image_bytes(animated=True), "image/png", 422),
        (image_bytes("WEBP", animated=True), "image/webp", 422),
    ],
)
def test_upload_rejects_invalid_images(service, raw, media_type, status):
    with pytest.raises(SessionAttachmentError) as error:
        upload(service, raw, media_type)
    assert error.value.status_code == status
    assert not service.root.exists()


def test_image_upload_limit_is_256_mib(service):
    assert MAX_ATTACHMENT_BYTES == 256 * 1024 * 1024
    # Keep this check allocation-free; service enforces the same limit after streaming.
    from app.domains.session_attachments import attachment_type
    assert attachment_type("picture.png", "image/png")[2] == MAX_ATTACHMENT_BYTES


def test_scope_deletion_and_untrusted_paths(service):
    metadata = upload(service, filename="../../private/picture.png", session_id="../outside")
    assert metadata["filename"] == "picture.png"
    assert service.root.is_dir()
    for session_id, attachment_id in [
        ("two", metadata["attachment_id"]),
        ("../outside", "../../outside"),
        ("missing", metadata["attachment_id"]),
    ]:
        with pytest.raises(KeyError):
            service.read_image(session_id=session_id, attachment_id=attachment_id)
    service.session_service.active.remove("../outside")
    with pytest.raises(KeyError):
        service.get(session_id="../outside", attachment_id=metadata["attachment_id"])


@pytest.mark.parametrize("filename", ["", ".", "..", "a\x00.png", "a" * 241, "界" * 81])
def test_filename_is_bounded(service, filename):
    with pytest.raises(SessionAttachmentError):
        upload(service, filename=filename)


@pytest.mark.parametrize(
    "replacement", ["changed", "corrupt", "metadata", "symlink", "parent_symlink"]
)
def test_reads_revalidate_integrity_and_reject_links(service, tmp_path, replacement):
    metadata = upload(service)
    folder = service.root / hashlib.sha256(b"one").hexdigest()
    path = folder / (metadata["attachment_id"] + ".image")
    if replacement == "changed":
        path.write_bytes(image_bytes(size=(4, 4)))
    elif replacement == "corrupt":
        path.write_bytes(b"corrupt")
    elif replacement == "metadata":
        path.with_suffix(".json").write_text("{}")
    elif replacement == "symlink":
        outside = tmp_path / "outside.png"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    else:
        outside = tmp_path / "outside"
        folder.rename(outside)
        folder.symlink_to(outside, target_is_directory=True)
    with pytest.raises(KeyError):
        service.get(session_id="one", attachment_id=metadata["attachment_id"])


def make_client(service, host="127.0.0.1"):
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(session_attachment_service=service)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 50000)), base_url="http://test"
    )


@pytest.mark.anyio
async def test_api_upload_raw_and_scoped_access(service):
    client = make_client(service)
    raw = image_bytes()
    response = await client.post(
        "/sessions/one/attachments",
        content=raw,
        headers={
            "Content-Type": "image/png",
            "X-Filename": quote("图片.png"),
        },
    )
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["filename"] == "图片.png"
    path = f"/sessions/one/attachments/{metadata['attachment_id']}"
    assert (await client.get(path)).json() == metadata
    result = await client.get(path + "/raw")
    assert result.status_code == 200 and result.content == raw
    assert result.headers["content-type"] == "image/png"
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert (await client.get(path.replace("/one/", "/two/"))).status_code == 404
    service.session_service.active.remove("one")
    assert (await client.get(path + "/raw")).status_code == 404
    assert (
        await client.post(
            "/sessions/one/attachments", content=raw, headers={"Content-Type": "image/png"}
        )
    ).status_code == 404


@pytest.mark.anyio
async def test_api_bounds_stream_and_status_codes(service, monkeypatch):
    client = make_client(service)
    path = "/sessions/one/attachments"
    assert (
        await client.post(path, content=b"invalid", headers={"Content-Type": "image/png"})
    ).status_code == 422
    assert (
        await client.post(path, content=b"invalid", headers={"Content-Type": "text/plain"})
    ).status_code == 415
    monkeypatch.setattr("app.domains.session_attachments.MAX_ATTACHMENT_BYTES", 16)
    response = await client.post(
        path, content=oversized_stream(), headers={"Content-Type": "image/png"}
    )
    assert response.status_code == 413
    assert not service.root.exists()
    remote = make_client(service, "192.0.2.1")
    assert (
        await remote.post(path, content=image_bytes(), headers={"Content-Type": "image/png"})
    ).status_code == 403
    assert (await remote.get(path + "/att_" + "0" * 32)).status_code == 403


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def oversized_stream():
    for _ in range(3):
        yield b"x" * 8
