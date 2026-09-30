from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.codex_workspace import (
    CodexWorkspaceFactory,
    CodexWorkspaceUnsupportedError,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(root, "add", "tracked.txt", ".gitignore")
    return root


def _factory(tmp_path: Path, **limits: int) -> CodexWorkspaceFactory:
    return CodexWorkspaceFactory(tmp_path / "codex-leases", **limits)


def test_create_copies_current_git_files_and_excludes_secrets_ignored_and_symlinks(tmp_path):
    source = _repo(tmp_path)
    (source / "tracked.txt").write_text("dirty tracked content\n", encoding="utf-8")
    (source / "new.txt").write_text("nonignored untracked\n", encoding="utf-8")
    (source / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (source / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (source / ".codex").mkdir()
    (source / ".codex" / "auth.json").write_text("secret\n", encoding="utf-8")
    (source / "data" / "agent_logs").mkdir(parents=True)
    (source / "data" / "agent_logs" / "trace.txt").write_text("private\n", encoding="utf-8")
    (source / "config").mkdir()
    (source / "config" / "local.toml").write_text("private = true\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (source / "link.txt").symlink_to(outside)

    lease = _factory(tmp_path).create(source)

    assert (lease.cwd / "tracked.txt").read_text(encoding="utf-8") == "dirty tracked content\n"
    assert (lease.cwd / "new.txt").read_text(encoding="utf-8") == "nonignored untracked\n"
    assert not (lease.cwd / "ignored.txt").exists()
    assert not (lease.cwd / ".env").exists()
    assert not (lease.cwd / ".codex").exists()
    assert not (lease.cwd / "data" / "agent_logs").exists()
    assert not (lease.cwd / "config" / "local.toml").exists()
    assert not (lease.cwd / "link.txt").exists()
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert (source / "tracked.txt").read_text(encoding="utf-8") == "dirty tracked content\n"


def test_collect_changes_returns_staged_diff_and_detects_source_conflicts(tmp_path):
    source = _repo(tmp_path)
    lease = _factory(tmp_path).create(source)

    (lease.cwd / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (lease.cwd / "new.txt").write_text("added\n", encoding="utf-8")
    (lease.cwd / ".gitignore").unlink()
    changes = lease.collect_changes()
    by_path = {change.path: change for change in changes}

    assert {path: change.status for path, change in by_path.items()} == {
        ".gitignore": "deleted",
        "new.txt": "added",
        "tracked.txt": "modified",
    }
    assert "original" in by_path["tracked.txt"].diff
    assert "+changed" in by_path["tracked.txt"].diff
    assert by_path["new.txt"].original_sha256 is None
    assert by_path["new.txt"].result_sha256 is not None
    assert lease.source_conflicts(changes) == ()
    assert (source / "tracked.txt").read_text(encoding="utf-8") != "changed\n"

    (source / "tracked.txt").write_text("user concurrent edit\n", encoding="utf-8")
    refreshed = lease.collect_changes()
    refreshed_by_path = {change.path: change for change in refreshed}
    assert refreshed_by_path["tracked.txt"].source_conflict is True
    assert refreshed_by_path["tracked.txt"].diff is None
    assert lease.source_conflicts(refreshed) == ("tracked.txt",)


def test_snapshot_rejects_file_limits_and_workspace_base_inside_source(tmp_path):
    source = _repo(tmp_path)
    with pytest.raises(CodexWorkspaceUnsupportedError, match="size limit"):
        _factory(tmp_path, max_file_bytes=3).create(source)

    with pytest.raises(CodexWorkspaceUnsupportedError, match="outside and separate"):
        CodexWorkspaceFactory(source / ".leases").create(source)


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        (".env", "TOKEN=x"),
        (".codex/auth.json", "{}"),
    ],
)
def test_collect_changes_refuses_sensitive_paths_created_by_codex(tmp_path, relative_path, content):
    source = _repo(tmp_path)
    lease = _factory(tmp_path).create(source)
    generated = lease.cwd / relative_path
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(content, encoding="utf-8")

    with pytest.raises(CodexWorkspaceUnsupportedError, match="excluded from workspace snapshots"):
        lease.collect_changes()


def test_collect_changes_refuses_symlinks_created_by_codex(tmp_path):
    source = _repo(tmp_path)
    lease = _factory(tmp_path).create(source)
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    (lease.cwd / "escape.txt").symlink_to(outside)

    with pytest.raises(CodexWorkspaceUnsupportedError, match="created a symlink"):
        lease.collect_changes()


def test_collect_changes_ignores_only_empty_codex_runtime_directory(tmp_path):
    source = _repo(tmp_path)
    lease = _factory(tmp_path).create(source)
    (lease.cwd / ".codex").mkdir()
    assert lease.collect_changes() == ()

    (lease.cwd / ".codex" / "config.toml").write_text("untrusted\n", encoding="utf-8")
    with pytest.raises(CodexWorkspaceUnsupportedError, match="excluded from workspace snapshots"):
        lease.collect_changes()
