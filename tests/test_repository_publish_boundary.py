"""Keep personal installation state out of Git; never inspect secret contents."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        ".env.backup",
        "config/local.toml",
        "config/custom-provider.toml",
        "config/secrets/outlook_token.json",
        "config/cache/model.bin",
        "data/runtime/lka.sqlite3",
        "data/agent_logs/private.json",
        "scratch/private-portable.zip",
        ".venv/Lib/site-packages/private.py",
        ".uv-cache/private.bin",
        ":memory:.ses",
        "temporary.sqlite3-wal",
        "temporary.sqlite3-shm",
    ],
)
def test_personal_files_are_ignored(path):
    if not (ROOT / ".git").exists():
        pytest.skip("Git checkout required")
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--stdin"],
        cwd=ROOT,
        input="./" + path + "\n",
        text=True,
        check=False,
    )
    assert result.returncode == 0, path


@pytest.mark.parametrize("path", [".env.example", "config/local.example.toml"])
def test_credential_free_templates_remain_publishable(path):
    if not (ROOT / ".git").exists():
        pytest.skip("Git checkout required")
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--stdin"],
        cwd=ROOT,
        input="./" + path + "\n",
        text=True,
        check=False,
    )
    assert result.returncode == 1, path


def test_tracked_paths_do_not_include_personal_installation_state():
    if not (ROOT / ".git").exists():
        pytest.skip("Git checkout required")
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    for name in filter(None, result.stdout.split("\0")):
        path = Path(name)
        assert not name.startswith(("data/", "scratch/", ".venv/", ".bootstrap/", ".uv-cache/"))
        assert not (name.startswith(".env") and name != ".env.example")
        assert not (name.startswith("config/") and not name.endswith(".example.toml"))
        assert not path.name.startswith(":memory:")
        assert not name.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".dpapi"))
