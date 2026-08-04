"""Local TOML configuration for providers and local model settings."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


def _is_env_var_name(value: str) -> bool:
    return bool(value) and (value[0].isalpha() or value[0] == "_") and all(
        character.isalnum() or character == "_" for character in value
    )


def _dotenv_value(name: str, path: Path = Path(".env")) -> str | None:
    if not name or not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        return value.strip().strip('"').strip("'")
    return None


def _resolved_env_value(name: str) -> str | None:
    return os.getenv(name) or _dotenv_value(name)


class LLMProviderConfig(BaseModel):
    provider: str = "mock"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-4.1-mini"
    timeout_seconds: int = 60

    def resolved_api_key(self) -> str | None:
        return _resolved_env_value(self.api_key_env) if self.api_key_env else None


class OutlookMailConfig(BaseModel):
    enabled: bool = False
    auth_method: str = "device_code"
    client_id: str = ""
    client_id_env: str = "MS_GRAPH_CLIENT_ID"
    tenant_id: str = "consumers"
    scopes: list[str] = Field(
        default_factory=lambda: ["User.Read", "Mail.Read", "offline_access"]
    )
    token_store_path: Path = Path("./data/secrets/outlook_token.json")
    sync_folder: str = "Inbox"
    download_attachment_content: bool = False

    def resolved_client_id(self) -> str | None:
        if self.client_id.strip():
            return self.client_id.strip()
        configured_value = self.client_id_env.strip()
        if not configured_value:
            return None
        env_value = _resolved_env_value(configured_value)
        if env_value:
            return env_value
        if not _is_env_var_name(configured_value):
            return configured_value
        return None


class IMAPMailConfig(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 993
    username: str = ""
    password_env: str = "MAIL_IMAP_PASSWORD"
    use_ssl: bool = True

    def resolved_password(self) -> str | None:
        return _resolved_env_value(self.password_env) if self.password_env else None


class MailProviderConfig(BaseModel):
    outlook: OutlookMailConfig = Field(default_factory=OutlookMailConfig)
    imap: IMAPMailConfig = Field(default_factory=IMAPMailConfig)


class EmbeddingConfig(BaseModel):
    provider: str = "local_bge"
    model_name: str = "BAAI/bge-m3"
    device: str = "auto"
    cache_dir: Path = Path("./data/models")
    batch_size: int = 16
    normalize_embeddings: bool = True


class LocalAppConfig(BaseModel):
    llm: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    mail: MailProviderConfig = Field(default_factory=MailProviderConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)


def load_local_config(path: Path) -> LocalAppConfig:
    """Load local TOML config. Missing files intentionally fall back to defaults."""

    expanded_path = path.expanduser()
    if not expanded_path.exists():
        return LocalAppConfig()
    with expanded_path.open("rb") as config_file:
        payload = tomllib.load(config_file)
    return LocalAppConfig.model_validate(payload)
