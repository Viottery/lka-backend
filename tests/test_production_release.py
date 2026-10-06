import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("production_release", Path(__file__).parents[1] / "scripts/production_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def install(root):
    for name, content in {"app/core/runtime.py": "# code\n", "scripts/start_backend.py": "# start\n",
                          "pyproject.toml": "# config\n", "uv.lock": "# lock\n", "config/local.toml": "# private\n"}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_release_content_identity_detects_dirty_and_missing_files(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    install(source)
    install(target)
    before = release.verify(source, target, record=True)
    assert before["identical"] and before["file_count"] == 4
    assert (target / "data/runtime/production_release.json").is_file()
    (source / "app/new.py").write_text("# untracked\n")
    with pytest.raises(ValueError, match="app/new.py"):
        release.verify(source, target)


def test_release_snapshot_preserves_old_install_and_private_config(tmp_path):
    install(tmp_path)
    result = release.snapshot(tmp_path)
    backup = Path(result["backup_path"])
    assert (backup / "app/core/runtime.py").read_text() == "# code\n"
    assert (backup / "config/local.toml").read_text() == "# private\n"
    assert (tmp_path / "config/local.toml").read_text() == "# private\n"
