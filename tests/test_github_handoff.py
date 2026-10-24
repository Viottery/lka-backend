"""Encrypted private GitHub handoff package contract tests."""
import sqlite3
import tarfile

import pytest

from scripts import data_snapshot
from scripts.deploy import github_handoff as handoff


@pytest.fixture
def snapshot(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with sqlite3.connect(source / "lka.sqlite3") as db:
        db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE sample(v); INSERT INTO sample VALUES('base');")
        db.execute("INSERT INTO sample VALUES('wal')")
    external = tmp_path / "external"
    external.write_text("outside")
    (source / "link").symlink_to(external)
    result = tmp_path / "snapshot"
    data_snapshot.create(source, result, offline=True)
    return result


def test_roundtrip_preserves_wal_and_symlink(snapshot, tmp_path):
    out, key, unpack = tmp_path / "package", tmp_path / "secret.key", tmp_path / "unpacked"
    handoff.pack(snapshot, out, key)
    assert key.stat().st_mode & 0o777 == 0o600
    assert key not in out.parents and out not in key.parents
    result = handoff.decrypt(out, key, unpack)
    assert result == str(unpack.resolve())
    assert data_snapshot.verify(unpack / "snapshot")["verified"]
    assert (unpack / "snapshot/data/link").is_symlink()
    with sqlite3.connect(unpack / "snapshot/data/lka.sqlite3") as db:
        assert db.execute("select v from sample order by v").fetchall() == [("base",), ("wal",)]


def test_tampered_cipher_and_wrong_key_leave_no_extract(snapshot, tmp_path):
    out, key = tmp_path / "package", tmp_path / "key"
    handoff.pack(snapshot, out, key)
    part = out / "part-0001.gpg"
    part.write_bytes(part.read_bytes()[:-1] + bytes([part.read_bytes()[-1] ^ 1]))
    with pytest.raises(ValueError, match="checksum"):
        handoff.decrypt(out, key, tmp_path / "unpack1")
    assert not (tmp_path / "unpack1/snapshot").exists()
    # Restore ciphertext from a fresh package, then reject a wrong key via GPG integrity.
    out2, key2 = tmp_path / "package2", tmp_path / "key2"
    handoff.pack(snapshot, out2, key2)
    wrong = tmp_path / "wrong"; wrong.write_text("wrong", encoding="utf-8")
    with pytest.raises(ValueError, match="GPG operation failed"):
        handoff.decrypt(out2, wrong, tmp_path / "unpack2")
    assert not (tmp_path / "unpack2/snapshot").exists()


def test_fresh_paths_key_boundary_and_unsafe_archive(tmp_path, snapshot, monkeypatch):
    out, key = tmp_path / "package", tmp_path / "key"
    with pytest.raises(ValueError, match="outside"):
        handoff.pack(snapshot, tmp_path / "publish", tmp_path / "publish/key")
    handoff.pack(snapshot, out, key)
    occupied = tmp_path / "already"; occupied.mkdir()
    with pytest.raises(FileExistsError): handoff.decrypt(out, key, occupied)
    # Replace GPG output after authenticated decryption with a traversal archive.
    def unsafe(args, home, *, passphrase_file, stdin=None, stdout=None):
        with tarfile.open(fileobj=stdout, mode="w:gz") as tar:
            import io
            data = b"owned"
            info = tarfile.TarInfo("../escape")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(handoff, "_gpg", unsafe)
    with pytest.raises(ValueError, match="Unsafe archive"):
        handoff.decrypt(out, key, tmp_path / "unsafe")
    assert not (tmp_path / "escape").exists()

