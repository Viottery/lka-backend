"""Private GitHub handoff downloader contracts with mocked HTTP."""
import hashlib
import json
import urllib.error
from email.message import Message

import pytest

from scripts.deploy import download_handoff as dl


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status, self.headers, self.offset = body, status, headers or {}, 0
    def read(self, size=-1):
        if size < 0: size = len(self.body)
        data = self.body[self.offset:self.offset + size]; self.offset += len(data); return data
    def getcode(self): return self.status
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class FakeOpener:
    def __init__(self, manifest, payload, *, private=True, corrupt=False):
        self.manifest, self.payload, self.private, self.corrupt = manifest, payload, private, corrupt
        self.requests = []
    def open(self, request, timeout=60):
        self.requests.append(request)
        url = request.full_url
        if url.endswith("/repos/acme/private"):
            return Response(json.dumps({"private": self.private}).encode())
        if url.endswith("/releases/tags/v1"):
            assets = [{"id": 1, "name": dl.MANIFEST, "size": len(self.manifest)}]
            assets += [{"id": i + 2, "name": p["name"], "size": p["size"]} for i, p in enumerate(json.loads(self.manifest)["parts"])]
            return Response(json.dumps({"tag_name": "v1", "assets": assets}).encode())
        if "/releases/assets/" in url and "api.github.com" in url:
            ident = url.rsplit("/", 1)[-1]
            error = urllib.error.HTTPError(url, 302, "Found", Message(), None)
            error.headers["Location"] = f"https://release-assets.githubusercontent.com/signed/{ident}"
            raise error
        if "release-assets.githubusercontent.com" in url:
            if request.has_header("Authorization"):
                raise AssertionError("Authorization leaked to asset host")
            if request.has_header("Range"):
                start = int(request.get_header("Range").split("=")[1].split("-")[0])
                return Response(self.payload[start:], 206, {"Content-Range": f"bytes {start}-{len(self.payload)-1}/{len(self.payload)}"})
            body = self.payload[:-1] + bytes([self.payload[-1] ^ 1]) if self.corrupt else self.payload
            if url.endswith("/1"):
                return Response(self.manifest)
            return Response(body)
        raise AssertionError(f"Unexpected URL {url}")


def fixture_payload():
    payload = b"ciphertext-part-data"
    manifest_obj = {"format": 1, "cipher": "AES256", "encrypted_size": len(payload),
                    "encrypted_sha256": hashlib.sha256(payload).hexdigest(),
                    "snapshot_manifest_sha256": "a" * 64,
                    "parts": [{"name": "part-0001.gpg", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}]}
    return json.dumps(manifest_obj).encode(), payload


def test_private_release_download_and_verified_resume_skip(tmp_path, monkeypatch):
    manifest, payload = fixture_payload()
    opener = FakeOpener(manifest, payload)
    monkeypatch.setattr(dl, "_opener", lambda: opener)
    out = tmp_path / "handoff"
    assert dl.download("acme/private", "v1", out, token="secret-token") == str(out)
    assert (out / dl.MANIFEST).read_bytes() == manifest
    assert (out / "part-0001.gpg").read_bytes() == payload
    assert all(not r.has_header("Authorization") for r in opener.requests if "release-assets.githubusercontent.com" in r.full_url)
    before = len(opener.requests)
    dl.download("acme/private", "v1", out, token="secret-token")
    # Metadata calls still occur; verified part is not fetched again.
    assert len([r for r in opener.requests[before:] if "release-assets.githubusercontent.com" in r.full_url]) == 1  # manifest only


def test_partial_resumes_and_invalid_final_is_never_overwritten(tmp_path, monkeypatch):
    manifest, payload = fixture_payload()
    opener = FakeOpener(manifest, payload)
    monkeypatch.setattr(dl, "_opener", lambda: opener)
    out = tmp_path / "resume"; out.mkdir()
    (out / dl.MANIFEST).write_bytes(manifest)
    (out / "part-0001.gpg.partial").write_bytes(payload[:5])
    dl.download("acme/private", "v1", out, token="tok")
    assert (out / "part-0001.gpg").read_bytes() == payload
    assert any(r.has_header("Range") for r in opener.requests)
    (out / "part-0001.gpg").write_bytes(b"bad")
    with pytest.raises(dl.DownloadError, match="refusing to overwrite"):
        dl.download("acme/private", "v1", out, token="tok")
    assert (out / "part-0001.gpg").read_bytes() == b"bad"


@pytest.mark.parametrize("edit,match", [
    (lambda m: m["parts"][0].update(name="../escape"), "path or size"),
    (lambda m: m["parts"][0].update(sha256="nope"), "checksum"),
])
def test_invalid_manifest_rejected_before_writing(tmp_path, monkeypatch, edit, match):
    raw, _ = fixture_payload(); obj = json.loads(raw); edit(obj)
    manifest = json.dumps(obj).encode()
    opener = FakeOpener(manifest, b"payload")
    monkeypatch.setattr(dl, "_opener", lambda: opener)
    out = tmp_path / "bad"
    with pytest.raises(dl.DownloadError, match=match):
        dl.download("acme/private", "v1", out, token="tok")
    assert not out.exists()


def test_nonprivate_repo_and_corrupt_download_fail(tmp_path, monkeypatch):
    manifest, payload = fixture_payload()
    opener = FakeOpener(manifest, payload, private=False)
    monkeypatch.setattr(dl, "_opener", lambda: opener)
    with pytest.raises(dl.DownloadError, match="not private"):
        dl.download("acme/private", "v1", tmp_path / "private", token="tok")
    opener = FakeOpener(manifest, payload, corrupt=True)
    monkeypatch.setattr(dl, "_opener", lambda: opener)
    with pytest.raises(dl.DownloadError, match="checksum mismatch"):
        dl.download("acme/private", "v1", tmp_path / "corrupt", token="tok")
    assert not (tmp_path / "corrupt/part-0001.gpg").exists()
