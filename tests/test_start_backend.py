from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "start_backend.py"
SPEC = importlib.util.spec_from_file_location("start_backend", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
start_backend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(start_backend)


def test_personal_profile_keeps_explicit_local_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(start_backend, "REPO_ROOT", tmp_path)
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


def test_personal_profile_loads_persistent_credentials_without_overriding_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(start_backend, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("LKA_MESSAGES_IMPORT_TOKEN", raising=False)
    monkeypatch.setenv("LKA_MESSAGES_API_TOKEN", "explicit-control-token")
    (tmp_path / ".env").write_text(
        "LKA_MESSAGES_IMPORT_TOKEN=stored-import-token\n"
        "LKA_MESSAGES_API_TOKEN=stored-control-token\n", encoding="utf-8"
    )
    try:
        start_backend.configure_profile("personal")
        assert os.environ["LKA_MESSAGES_IMPORT_TOKEN"] == "stored-import-token"
        assert os.environ["LKA_MESSAGES_API_TOKEN"] == "explicit-control-token"
    finally:
        os.environ.pop("LKA_MESSAGES_IMPORT_TOKEN", None)


def test_test_profile_does_not_load_private_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(start_backend, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("LKA_MESSAGES_IMPORT_TOKEN", raising=False)
    (tmp_path / ".env").write_text("LKA_MESSAGES_IMPORT_TOKEN=private-import-token\n", encoding="utf-8")
    start_backend.configure_profile("test", test_data_dir=tmp_path / "isolated")
    assert "LKA_MESSAGES_IMPORT_TOKEN" not in os.environ


def test_personal_release_identity_and_test_profile_isolation(monkeypatch, tmp_path):
    monkeypatch.setattr(start_backend, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "personal"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "local.toml"))
    monkeypatch.delenv("LKA_DEPLOYMENT_ID", raising=False)
    data = tmp_path / "personal"
    data.mkdir()
    (data / "production_release.json").write_text(json.dumps({"release_id": "a" * 64}), encoding="utf-8")
    start_backend.configure_profile("personal")
    assert os.environ["LKA_DEPLOYMENT_ID"] == "a" * 64
    start_backend.configure_profile("test", test_data_dir=tmp_path / "isolated")
    assert "LKA_DEPLOYMENT_ID" not in os.environ


@pytest.mark.parametrize("config_text", [None, "", "[auth]\nenabled=true\nrequire_https=false\n",
                                         "[auth]\nenabled=true\nsecure_cookie=false\n"])
def test_cloud_startup_rejects_missing_or_insecure_auth_config(monkeypatch, tmp_path, config_text):
    monkeypatch.setattr(start_backend, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    config = tmp_path / "local.toml"
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    if config_text is not None:
        config.write_text(config_text, encoding="utf-8")
    with pytest.raises(SystemExit, match="--require-auth.*requires"):
        start_backend.main(["personal", "--require-auth"])
    assert not (tmp_path / "data").exists()


def test_cloud_startup_keeps_proxy_trust_local(monkeypatch, tmp_path):
    monkeypatch.setattr(start_backend, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    config = tmp_path / "local.toml"
    config.write_text("[auth]\nenabled=true\n", encoding="utf-8")
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    assert start_backend.main(["personal", "--require-auth"]) == 0
    assert calls[0]["forwarded_allow_ips"] == "127.0.0.1"
    for args in (["personal", "--require-auth", "--reload"], ["test", "--require-auth"]):
        with pytest.raises(SystemExit, match="--require-auth requires"):
            start_backend.main(args)


def test_node_startup_rejects_default_fallback_and_pins_proxy_trust(monkeypatch, tmp_path):
    monkeypatch.setattr(start_backend, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    config = tmp_path / "node.toml"
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    with pytest.raises(SystemExit, match="existing configuration"):
        start_backend.main(["personal", "--require-node"])
    config.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="authenticated replica"):
        start_backend.main(["personal", "--require-node"])
    config.write_text('[sync]\nenabled=true\nrole="replica"\nhub_id="hub"\n'
                      'hub_url="https://hub.test"\n[connections]\nenabled=true\nnode_id="pc"\n'
                      'allowed_origins=["https://pc.test"]\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    assert start_backend.main(["personal", "--require-node"]) == 0
    assert calls[0]["forwarded_allow_ips"] == "127.0.0.1"
    for args in (["test", "--require-node"], ["personal", "--require-node", "--reload"]):
        with pytest.raises(SystemExit, match="--require-node requires"):
            start_backend.main(args)
