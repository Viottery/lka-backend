"""Offline migration backups preserve data and reject corruption/unsafe layouts."""
import json
import os
import sqlite3

import pytest

from scripts import data_snapshot


def symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows user lacks permission to create test symlinks")
        raise


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    with sqlite3.connect(root / "lka.sqlite3") as conn:
        conn.executescript("CREATE TABLE agent_runs(status TEXT); INSERT INTO agent_runs VALUES('completed'); "
                           "CREATE TABLE agent_sessions(session_id TEXT); INSERT INTO agent_sessions VALUES('old-session');")
    (root / "session_attachments").mkdir()
    (root / "session_attachments" / "document.txt").write_text("existing document", encoding="utf-8")
    (root / "empty-directory").mkdir()
    return root


def test_snapshot_preserves_wal_history_checkpoints_and_private_config(dataset, tmp_path):
    conn = sqlite3.connect(dataset / "lka.sqlite3")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO agent_sessions VALUES('latest-wal-session')")
        conn.commit()
        with sqlite3.connect(dataset / "agent_checkpoints.sqlite3") as checkpoints:
            checkpoints.executescript("CREATE TABLE checkpoints(id TEXT); INSERT INTO checkpoints VALUES('checkpoint');")
        env = tmp_path / "private.env"
        env.write_text("SYNTHETIC_KEY=only-in-private-backup\n", encoding="utf-8")
        output = tmp_path / "snapshot"
        result = data_snapshot.create(dataset, output, offline=True, env_file=env)
        assert result["verified"] and result["databases"] == 2
        assert data_snapshot.verify(output)["verified"]
        with sqlite3.connect(output / "data/lka.sqlite3") as copied:
            assert copied.execute("SELECT session_id FROM agent_sessions ORDER BY session_id").fetchall() == [
                ("latest-wal-session",), ("old-session",)]
        assert not (output / "data/lka.sqlite3-wal").exists()
        assert (output / "private/backend.env").read_bytes() == env.read_bytes()
        assert (output / "data/empty-directory").is_dir()
        assert conn.execute("SELECT count(*) FROM agent_sessions").fetchone()[0] == 2
    finally:
        conn.close()


def test_requires_stopped_writer_assertion_and_never_overwrites(dataset, tmp_path):
    output = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="--offline"):
        data_snapshot.create(dataset, output)
    data_snapshot.create(dataset, output, offline=True)
    with pytest.raises(FileExistsError):
        data_snapshot.create(dataset, output, offline=True)
    with pytest.raises(ValueError, match="contain"):
        data_snapshot.create(dataset, dataset / "recursive", offline=True)


def test_active_runs_block_snapshot(dataset, tmp_path):
    with sqlite3.connect(dataset / "lka.sqlite3") as conn:
        conn.execute("INSERT INTO agent_runs VALUES('running')")
    with pytest.raises(ValueError, match="active runs/jobs"):
        data_snapshot.create(dataset, tmp_path / "snapshot", offline=True)


def test_modified_or_unexpected_files_fail_verification(dataset, tmp_path):
    output = tmp_path / "snapshot"
    data_snapshot.create(dataset, output, offline=True)
    document = output / "data/session_attachments/document.txt"
    document.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch"):
        data_snapshot.verify(output)
    document.write_text("existing document", encoding="utf-8")
    (output / "data/injected.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        data_snapshot.verify(output)


def test_source_change_never_produces_complete_snapshot(dataset, tmp_path, monkeypatch):
    original = data_snapshot.copy_database

    def copy_and_mutate(source, destination):
        original(source, destination)
        (dataset / "late-write.txt").write_text("late", encoding="utf-8")

    monkeypatch.setattr(data_snapshot, "copy_database", copy_and_mutate)
    output = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="Source changed"):
        data_snapshot.create(dataset, output, offline=True)
    assert not (output / "manifest.json").exists()


@pytest.mark.parametrize("wal_bytes,allowed", [(b"", True), (b"new committed frames", False)])
def test_empty_read_sidecar_is_harmless_but_nonempty_wal_change_is_rejected(dataset, tmp_path, monkeypatch, wal_bytes, allowed):
    original = data_snapshot.copy_database

    def copy_with_sidecar(source, destination):
        original(source, destination)
        source.with_name(source.name + "-wal").write_bytes(wal_bytes)
        source.with_name(source.name + "-shm").write_bytes(b"reader locks")

    monkeypatch.setattr(data_snapshot, "copy_database", copy_with_sidecar)
    if allowed:
        assert data_snapshot.create(dataset, tmp_path / "snapshot", offline=True)["verified"]
    else:
        with pytest.raises(ValueError, match="Source changed"):
            data_snapshot.create(dataset, tmp_path / "snapshot", offline=True)


def test_external_links_are_not_followed_and_manifest_traversal_is_rejected(dataset, tmp_path):
    external = tmp_path / "outside-secret.txt"
    external.write_text("outside content", encoding="utf-8")
    symlink(dataset / "external", external)
    output = tmp_path / "snapshot"
    data_snapshot.create(dataset, output, offline=True)
    assert (output / "data/external").is_symlink()
    assert data_snapshot.verify(output)["verified"]
    path = output / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["records"]["data/../escape"] = {"kind": "file"}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Invalid snapshot-relative path"):
        data_snapshot.verify(output)


def test_verifier_never_follows_an_ancestor_symlink(dataset, tmp_path):
    output = tmp_path / "snapshot"
    data_snapshot.create(dataset, output, offline=True)
    linked = output / "data/session_attachments"
    (linked / "document.txt").unlink()
    linked.rmdir()
    symlink(linked, tmp_path, directory=True)
    with pytest.raises(ValueError):
        data_snapshot.verify(output)
