"""Encrypt and split an already verified LKA data snapshot for private transfer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

PART_SIZE = 512 * 1024 * 1024
MANIFEST = "handoff-manifest.json"


def _digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _fresh(path):
    path = Path(path).resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    return path


def _snapshot_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("lka_data_snapshot", Path(__file__).resolve().parents[1] / "data_snapshot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gpg(args, home, *, passphrase_file, stdin=None, stdout=None):
    cmd = ["gpg", "--batch", "--no-tty", "--homedir", str(home), "--pinentry-mode", "loopback",
           "--passphrase-file", str(passphrase_file), *args]
    try:
        result = subprocess.run(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise ValueError("GPG could not be started") from exc
    if result.returncode:
        raise ValueError("GPG operation failed (wrong key or damaged package)")


def _kill_agent(home):
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def pack(snapshot, output, key_file, includes=()):
    snapshot, output, key_file = Path(snapshot).resolve(), Path(output).resolve(), Path(key_file).resolve()
    if output == key_file or output in key_file.parents:
        raise ValueError("Key file must be outside the publish directory")
    if output == snapshot or output in snapshot.parents or snapshot in output.parents:
        raise ValueError("Snapshot and publish directory must not contain one another")
    if key_file == snapshot or snapshot in key_file.parents:
        raise ValueError("Key file must be outside the snapshot")
    _snapshot_module().verify(snapshot)
    initial_snapshot_hash = _digest(snapshot / "manifest.json")
    if key_file.exists():
        raise FileExistsError(key_file)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    key_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(secrets.token_urlsafe(48).encode("ascii"))
    metadata = []
    for item in includes:
        label, sep, raw = item.partition("=")
        source = Path(raw).resolve()
        if (not sep or not label or label in (".", "..") or "/" in label or "\\" in label
                or ":" in label or not source.is_file()):
            raise ValueError("Each include must be LABEL=existing-file")
        if source == key_file:
            raise ValueError("Key file cannot be included as metadata")
        metadata.append(("metadata/" + label, source))
    with tempfile.TemporaryDirectory(prefix="lka-gpg-") as temp:
        home = Path(temp) / "home"
        home.mkdir(mode=0o700)
        encrypted = Path(temp) / "package.gpg"
        with encrypted.open("wb") as out:
            proc = subprocess.Popen(["gpg", "--batch", "--no-tty", "--homedir", str(home), "--pinentry-mode", "loopback",
                                     "--passphrase-file", str(key_file), "--symmetric", "--cipher-algo", "AES256", "--compress-algo", "none", "--output", "-"],
                                    stdin=subprocess.PIPE, stdout=out, stderr=subprocess.PIPE)
            try:
                with tarfile.open(fileobj=proc.stdin, mode="w|gz", format=tarfile.PAX_FORMAT) as tar:
                    tar.add(snapshot, arcname="snapshot", recursive=True)
                    for name, src in metadata:
                        tar.add(src, arcname=name, recursive=False)
                proc.communicate()
            except BaseException:
                proc.kill(); proc.wait(); _kill_agent(home); raise
            if proc.returncode:
                raise ValueError("GPG encryption failed")
            _kill_agent(home)
        size = encrypted.stat().st_size
        parts = []
        whole = hashlib.sha256()
        with encrypted.open("rb") as src:
            index = 1
            while True:
                name = f"part-{index:04d}.gpg"
                target = output / name
                part_hash = hashlib.sha256()
                part_size = 0
                with target.open("xb") as dst:
                    while part_size < PART_SIZE:
                        block = src.read(min(1024 * 1024, PART_SIZE - part_size))
                        if not block: break
                        dst.write(block); whole.update(block); part_hash.update(block); part_size += len(block)
                if not part_size:
                    target.unlink()
                    break
                parts.append({"name": name, "size": part_size, "sha256": part_hash.hexdigest()})
                index += 1
        snapshot_hash = _digest(snapshot / "manifest.json")
    _snapshot_module().verify(snapshot)
    if snapshot_hash != initial_snapshot_hash:
        raise ValueError("Snapshot manifest changed while packaging")
    manifest = {"format": 1, "cipher": "AES256", "encrypted_size": size, "parts": parts,
                "encrypted_sha256": whole.hexdigest(),
                "snapshot_manifest_sha256": snapshot_hash}
    (output / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _safe_member(member):
    name = member.name
    p = PurePosixPath(name)
    if p.is_absolute() or not name or any(part in ("", ".", "..") or ":" in part for part in p.parts) or "\\" in name:
        raise ValueError("Unsafe archive path")
    if not (member.isdir() or member.isfile() or member.issym()):
        raise ValueError("Unsupported archive entry")
    if member.issym() and (not p.parts or p.parts[0] != "snapshot"):
        raise ValueError("Symlinks are allowed only in the snapshot")


def decrypt(package, key_file, output):
    package, key_file = Path(package).resolve(), Path(key_file).resolve()
    output = _fresh(output)
    manifest = json.loads((package / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not manifest.get("parts"):
        raise ValueError("Unsupported handoff manifest")
    names = [p.get("name") for p in manifest["parts"]]
    if names != [f"part-{i:04d}.gpg" for i in range(1, len(names) + 1)]:
        raise ValueError("Invalid part sequence")
    if set(os.listdir(package)) != {MANIFEST, *names}:
        raise ValueError("Package contains missing or unexpected files")
    if sum(part.get("size", -1) for part in manifest["parts"]) != manifest.get("encrypted_size"):
        raise ValueError("Ciphertext package size mismatch")
    whole = hashlib.sha256()
    for part in manifest["parts"]:
        path = package / part["name"]
        if part["size"] < 1 or part["size"] > PART_SIZE:
            raise ValueError("Invalid ciphertext part size")
        if path.stat().st_size != part["size"] or _digest(path) != part["sha256"]:
            raise ValueError("Ciphertext part checksum mismatch")
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""): whole.update(block)
    if whole.hexdigest() != manifest.get("encrypted_sha256"):
        raise ValueError("Ciphertext package checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="lka-gpg-") as temp:
        home = Path(temp) / "home"; home.mkdir(mode=0o700)
        cipher = Path(temp) / "package.gpg"
        with cipher.open("wb") as dst:
            for name in names:
                with (package / name).open("rb") as src: shutil.copyfileobj(src, dst)
        tarpath = output / ".verified-archive.tar.gz"
        try:
            with tarpath.open("wb") as plain:
                _gpg(["--decrypt", "--output", "-", str(cipher)], home, passphrase_file=key_file, stdout=plain)
        except BaseException:
            _kill_agent(home)
            raise
        try:
            with tarfile.open(tarpath, "r:gz") as tar:
                members = tar.getmembers(); seen = set()
                for m in members:
                    _safe_member(m)
                    if m.name in seen: raise ValueError("Duplicate archive path")
                    seen.add(m.name)
                # Reject any entry nested beneath an archived symlink.
                symlinks = {m.name for m in members if m.issym()}
                if any(any(str(PurePosixPath(m.name).parent) == link or str(PurePosixPath(m.name)).startswith(link + "/") for link in symlinks) for m in members):
                    raise ValueError("Archive path traverses symlink")
                root = output.resolve()
                for m in members:
                    target = (root / m.name).resolve(strict=False)
                    if root not in target.parents and target != root: raise ValueError("Archive path escapes output")
                    if m.isdir(): target.mkdir(parents=True, exist_ok=True)
                    elif m.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with tar.extractfile(m) as src, target.open("xb") as dst: shutil.copyfileobj(src, dst)
                for m in members:
                    if m.issym():
                        target = root / m.name; target.parent.mkdir(parents=True, exist_ok=True); target.symlink_to(m.linkname)
        finally:
            tarpath.unlink(missing_ok=True)
            _kill_agent(home)
    snap = output / "snapshot"
    if _digest(snap / "manifest.json") != manifest["snapshot_manifest_sha256"]:
        raise ValueError("Snapshot manifest hash mismatch")
    _snapshot_module().verify(snap)
    return str(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pack"); p.add_argument("--snapshot", required=True); p.add_argument("--output", required=True); p.add_argument("--key-file", required=True); p.add_argument("--include", action="append", default=[])
    d = sub.add_parser("decrypt"); d.add_argument("--input", required=True); d.add_argument("--key-file", required=True); d.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "pack": pack(args.snapshot, args.output, args.key_file, args.include)
        else: decrypt(args.input, args.key_file, args.output)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__": main()
