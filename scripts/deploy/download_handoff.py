"""Download and verify encrypted handoff assets from a private GitHub release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
MANIFEST = "handoff-manifest.json"
MAX_MANIFEST = 1024 * 1024
BLOCK = 1024 * 1024
ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com",
               "objects.githubusercontent.com", "github-releases.githubusercontent.com"}


class DownloadError(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(_NoRedirect())


def _token():
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                                check=False, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                                capture_output=True, text=True, check=False, timeout=15, env=env)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("password=") and line[9:]:
                    return line[9:]
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise DownloadError("No GitHub credential found (set GH_TOKEN/GITHUB_TOKEN, or authenticate gh/git)")


def _host_allowed(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "api.github.com" or host in ASSET_HOSTS)


def _request(url, token, *, accept="application/vnd.github+json", byte_range=None, api_auth=True):
    headers = {"Accept": accept, "User-Agent": "lka-private-handoff/1"}
    if api_auth:
        headers["Authorization"] = "Bearer " + token
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if byte_range:
        headers["Range"] = byte_range
    req = urllib.request.Request(url, headers=headers)
    try:
        return _opener().open(req, timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise DownloadError(f"GitHub request failed with HTTP {exc.code}") from None
        location = exc.headers.get("Location")
        exc.close()
        if not location:
            raise DownloadError("GitHub asset redirect omitted its destination")
        destination = urllib.parse.urljoin(url, location)
        if not _host_allowed(destination) or urllib.parse.urlsplit(destination).hostname == "api.github.com":
            raise DownloadError("GitHub returned an untrusted asset host")
        # Signed asset URL is intentionally never included in errors or output.
        forwarded = {"User-Agent": "lka-private-handoff/1"}
        if byte_range:
            forwarded["Range"] = byte_range
        try:
            return _opener().open(urllib.request.Request(destination, headers=forwarded), timeout=60)
        except urllib.error.HTTPError as follow_error:
            raise DownloadError(f"GitHub asset request failed with HTTP {follow_error.code}") from None


def _json_request(url, token, limit):
    with _request(url, token) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise DownloadError("GitHub metadata exceeds the allowed size")
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DownloadError("GitHub returned invalid JSON metadata") from None


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_manifest(data):
    if not isinstance(data, dict) or data.get("format") != 1 or data.get("cipher") != "AES256":
        raise DownloadError("Unsupported handoff manifest")
    parts = data.get("parts")
    if not isinstance(parts, list) or not parts:
        raise DownloadError("Handoff manifest has no parts")
    total = 0
    for index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise DownloadError("Invalid handoff part record")
        expected_name = f"part-{index:04d}.gpg"
        size, digest = part.get("size"), part.get("sha256")
        if part.get("name") != expected_name or not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= 512 * 1024 * 1024:
            raise DownloadError("Invalid handoff part path or size")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DownloadError("Invalid handoff part checksum")
        total += size
    if data.get("encrypted_size") != total:
        raise DownloadError("Handoff package size mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", str(data.get("encrypted_sha256", ""))):
        raise DownloadError("Invalid package checksum")
    if not re.fullmatch(r"[0-9a-f]{64}", str(data.get("snapshot_manifest_sha256", ""))):
        raise DownloadError("Invalid snapshot manifest checksum")
    return parts


def _download_asset(url, target, *, token, expected_size, expected_hash):
    partial = target.with_name(target.name + ".partial")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > expected_size:
        partial.unlink(); offset = 0
    digest = hashlib.sha256()
    if offset:
        with partial.open("rb") as stream:
            for block in iter(lambda: stream.read(BLOCK), b""):
                digest.update(block)
        if offset == expected_size:
            if digest.hexdigest() == expected_hash:
                os.replace(partial, target)
                return
            partial.unlink(); offset = 0; digest = hashlib.sha256()
    response = _request(url, token, accept="application/octet-stream", byte_range=f"bytes={offset}-" if offset else None)
    status = getattr(response, "status", response.getcode())
    if offset and status == 200:
        response.close(); partial.unlink(); offset = 0; digest = hashlib.sha256()
        response = _request(url, token, accept="application/octet-stream")
        status = getattr(response, "status", response.getcode())
    if offset and status != 206:
        response.close()
        raise DownloadError("GitHub did not honor the resume range")
    if not offset and status not in (200, 206):
        response.close()
        raise DownloadError("GitHub returned an unexpected asset response")
    if offset:
        content_range = response.headers.get("Content-Range", "")
        if not content_range.startswith(f"bytes {offset}-"):
            response.close()
            raise DownloadError("GitHub returned an invalid resume range")
    mode = "ab" if offset else "wb"
    written = offset
    with response, partial.open(mode) as stream:
        while block := response.read(BLOCK):
            written += len(block)
            if written > expected_size:
                raise DownloadError("Downloaded asset exceeds its declared size")
            stream.write(block); digest.update(block)
    if written != expected_size or digest.hexdigest() != expected_hash:
        raise DownloadError("Downloaded asset size or checksum mismatch")
    os.replace(partial, target)


def download(repository, tag, output, *, token=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise DownloadError("Repository must be OWNER/REPO")
    if not tag or len(tag) > 200 or any(ord(char) < 32 for char in tag):
        raise DownloadError("Invalid release tag")
    token = token or _token()
    encoded_repo = urllib.parse.quote(repository, safe="/")
    privacy = _json_request(f"{API}/repos/{encoded_repo}", token, 256 * 1024)
    if not isinstance(privacy, dict) or privacy.get("private") is not True:
        raise DownloadError("Target GitHub repository is not private")
    release = _json_request(f"{API}/repos/{encoded_repo}/releases/tags/{urllib.parse.quote(tag, safe='')}", token, 1024 * 1024)
    assets = release.get("assets") if isinstance(release, dict) else None
    if not isinstance(assets, list):
        raise DownloadError("Release metadata has no assets")
    if release.get("tag_name") != tag:
        raise DownloadError("GitHub release tag does not match the requested tag")
    def asset_named(name):
        matches = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
        if len(matches) != 1:
            raise DownloadError(f"Release must contain exactly one {name}")
        asset = matches[0]
        if not isinstance(asset.get("id"), int) or asset["id"] <= 0 or not isinstance(asset.get("size"), int) or asset["size"] <= 0:
            raise DownloadError(f"Invalid release asset metadata for {name}")
        return asset
    manifest_asset = asset_named(MANIFEST)
    if manifest_asset["size"] > MAX_MANIFEST:
        raise DownloadError("Handoff manifest exceeds the allowed size")
    manifest_url = f"{API}/repos/{encoded_repo}/releases/assets/{manifest_asset['id']}"
    with _request(manifest_url, token, accept="application/octet-stream") as response:
        manifest_bytes = response.read(MAX_MANIFEST + 1)
    if len(manifest_bytes) != manifest_asset["size"] or len(manifest_bytes) > MAX_MANIFEST:
        raise DownloadError("Downloaded handoff manifest size mismatch")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DownloadError("Release handoff manifest is invalid JSON") from None
    parts = _validate_manifest(manifest)
    for part in parts:
        asset = asset_named(part["name"])
        if asset["size"] != part["size"]:
            raise DownloadError("Release asset size disagrees with handoff manifest")
    output_path = Path(output).absolute()
    if any(parent.is_symlink() for parent in (output_path, *output_path.parents)):
        raise DownloadError("Output path must not traverse a symlink")
    if output_path.is_symlink():
        raise DownloadError("Output directory must not be a symlink")
    output_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(path.is_symlink() for path in output_path.iterdir()):
        raise DownloadError("Output directory contains a symlink")
    manifest_path = output_path / MANIFEST
    manifest_raw = manifest_bytes
    if manifest_path.exists():
        if manifest_path.is_symlink() or manifest_path.read_bytes() != manifest_raw:
            raise DownloadError("Existing handoff manifest is invalid; refusing to overwrite")
    else:
        with manifest_path.open("xb") as stream:
            stream.write(manifest_raw)
    for part in parts:
        target = output_path / part["name"]
        if target.exists():
            if target.is_symlink() or target.stat().st_size != part["size"] or _hash_file(target) != part["sha256"]:
                raise DownloadError(f"Existing {part['name']} is invalid; refusing to overwrite")
            continue
        asset = asset_named(part["name"])
        url = f"{API}/repos/{encoded_repo}/releases/assets/{asset['id']}"
        _download_asset(url, target, token=token, expected_size=part["size"], expected_hash=part["sha256"])
    total_hash = hashlib.sha256()
    for part in parts:
        with (output_path / part["name"]).open("rb") as stream:
            for block in iter(lambda: stream.read(BLOCK), b""): total_hash.update(block)
    if total_hash.hexdigest() != manifest["encrypted_sha256"]:
        raise DownloadError("Complete handoff package checksum mismatch")
    return str(output_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        download(args.repository, args.tag, args.output)
    except (DownloadError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
