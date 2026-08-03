"""Local TOML configuration for providers and local model settings."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class LLMProviderConfig(BaseModel):
    provider: str = "mock"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-4.1-mini"
    timeout_seconds: int = 60

    def resolved_api_key(self) -> str | None:
        return os.getenv(self.api_key_env) if self.api_key_env else None


class OutlookMailConfig(BaseModel):
    enabled: bool = False
    auth_method: str = "device_code"
    client_id_env: str = "MS_GRAPH_CLIENT_ID"
    tenant_id: str = "consumers"
    scopes: list[str] = Field(
        default_factory=lambda: ["User.Read", "Mail.Read", "offline_access"]
    )
    token_store_path: Path = Path("./data/secrets/outlook_token.json")
    sync_folder: str = "Inbox"
    download_attachment_content: bool = False

    def resolved_client_id(self) -> str | None:
        return os.getenv(self.client_id_env) if self.client_id_env else None


class IMAPMailConfig(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 993
    username: str = ""
    password_env: str = "MAIL_IMAP_PASSWORD"
    use_ssl: bool = True

    def resolved_password(self) -> str | None:
        return os.getenv(self.password_env) if self.password_env else None


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
