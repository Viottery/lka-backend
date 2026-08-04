from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.core.local_config import load_local_config


def test_missing_local_config_uses_safe_defaults(tmp_path):
    config = load_local_config(tmp_path / "missing.toml")

    assert config.llm.provider == "mock"
    assert config.mail.outlook.auth_method == "device_code"
    assert config.mail.outlook.scopes == ["User.Read", "Mail.Read", "offline_access"]
    assert config.embedding.provider == "local_bge"
    assert config.embedding.model_name == "BAAI/bge-m3"


def test_local_config_loads_provider_mail_and_embedding_sections(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[llm]
provider = "openai"
api_key_env = "TEST_OPENAI_API_KEY"
model = "gpt-test"

[mail.outlook]
enabled = true
client_id_env = "TEST_MS_GRAPH_CLIENT_ID"
tenant_id = "consumers"
scopes = ["User.Read", "Mail.Read", "offline_access"]
download_attachment_content = false

[mail.imap]
enabled = true
host = "imap.example.com"
username = "user@example.com"
password_env = "TEST_MAIL_PASSWORD"

[embedding]
provider = "local_bge"
model_name = "BAAI/bge-m3"
device = "cpu"
cache_dir = "./data/test-models"
batch_size = 8
normalize_embeddings = true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_OPENAI_API_KEY", "llm-secret")
    monkeypatch.setenv("TEST_MS_GRAPH_CLIENT_ID", "client-id")
    monkeypatch.setenv("TEST_MAIL_PASSWORD", "mail-secret")

    config = load_local_config(config_path)

    assert config.llm.provider == "openai"
    assert config.llm.resolved_api_key() == "llm-secret"
    assert config.mail.outlook.enabled is True
    assert config.mail.outlook.resolved_client_id() == "client-id"
    assert config.mail.outlook.download_attachment_content is False
    assert config.mail.imap.enabled is True
    assert config.mail.imap.resolved_password() == "mail-secret"
    assert config.embedding.device == "cpu"
    assert config.embedding.cache_dir == Path("data/test-models")


def test_llm_api_key_can_be_loaded_from_dotenv(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[llm]
provider = "packyapi"
api_key_env = "PACKY_API_KEY"
model = "deepseek-v4-flash"
""",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text('PACKY_API_KEY="dotenv-secret"\n', encoding="utf-8")
    monkeypatch.delenv("PACKY_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    config = load_local_config(config_path)

    assert config.llm.resolved_api_key() == "dotenv-secret"


def test_outlook_client_id_can_be_loaded_directly_from_config(tmp_path):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[mail.outlook]
enabled = true
client_id = "dd5c654c-9e3c-41fc-9e14-15088af6c8bf"
""",
        encoding="utf-8",
    )

    config = load_local_config(config_path)

    assert config.mail.outlook.resolved_client_id() == "dd5c654c-9e3c-41fc-9e14-15088af6c8bf"


def test_outlook_client_id_env_accepts_literal_guid_for_local_config(tmp_path):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[mail.outlook]
enabled = true
client_id_env = "dd5c654c-9e3c-41fc-9e14-15088af6c8bf"
""",
        encoding="utf-8",
    )

    config = load_local_config(config_path)

    assert config.mail.outlook.resolved_client_id() == "dd5c654c-9e3c-41fc-9e14-15088af6c8bf"


def test_settings_loads_config_from_configured_path(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[embedding]
provider = "local_bge"
model_name = "BAAI/bge-m3"
device = "auto"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))

    settings = Settings()
    config = settings.load_local_config()

    assert config.embedding.provider == "local_bge"
    assert config.embedding.model_name == "BAAI/bge-m3"
