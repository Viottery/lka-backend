from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "start_backend.py"
SPEC = importlib.util.spec_from_file_location("start_backend", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
start_backend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(start_backend)


def test_personal_profile_keeps_explicit_local_paths(monkeypatch, tmp_path):
    personal_data = tmp_path / "personal"
    personal_config = tmp_path / "local.toml"
    monkeypatch.setenv("LKA_DATA_DIR", str(personal_data))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(personal_config))

    data_dir = start_backend.configure_profile("personal")

    assert data_dir == personal_data
    assert os.environ["LKA_LOCAL_CONFIG"] == str(personal_config)


def test_test_profile_overrides_personal_runtime_and_provider_config(monkeypatch, tmp_path):
    test_data = tmp_path / "isolated-test-data"
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "personal"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "real-local.toml"))

    data_dir = start_backend.configure_profile("test", test_data_dir=test_data)

    assert data_dir == test_data
    assert os.environ["LKA_DATA_DIR"] == str(test_data)
    assert os.environ["LKA_LOCAL_CONFIG"] == str(test_data / "missing-local.toml")


def test_test_profile_rejects_reload():
    with pytest.raises(SystemExit, match="reload"):
        start_backend.main(["test", "--reload"])
