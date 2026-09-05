"""Local TOML configuration for providers and local model settings."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

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


class LLMClientConfig(BaseModel):
    name: str
    provider: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    default_model: str = "gpt-4.1-mini"
    available_models: list[str] = Field(default_factory=list)
    timeout_seconds: int = 180
    supports_stream: bool = True
    supports_json_mode: bool = False
    supports_function_calling: bool = False
    function_calling_strict: bool = False

    def resolved_api_key(self) -> str | None:
        return _resolved_env_value(self.api_key_env) if self.api_key_env else None


class LLMProviderConfig(BaseModel):
    provider: str = "mock"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-4.1-mini"
    timeout_seconds: int = 180
    default_client: str | None = None
    fallback_client: str | None = None
    default_response_mode: str = "json"
    max_attempts: int = 2
    clients: list[LLMClientConfig] = Field(default_factory=list)

    def resolved_api_key(self) -> str | None:
        return _resolved_env_value(self.api_key_env) if self.api_key_env else None

    def is_disabled(self) -> bool:
        if self.clients:
            return False
        return self.provider == "mock" or not self.resolved_api_key()

    def client_configs(self) -> list[LLMClientConfig]:
        if self.clients:
            return self.clients
        if self.provider == "mock":
            return []
        provider_type = (
            self.provider
            if self.provider in {"mock", "openai_compatible"}
            else "openai_compatible"
        )
        client_name = self.default_client or self.provider
        return [
            LLMClientConfig(
                name=client_name,
                provider=provider_type,
                base_url=self.base_url,
                api_key_env=self.api_key_env,
                default_model=self.model,
                available_models=[self.model],
                timeout_seconds=self.timeout_seconds,
            )
        ]


class SafetyReviewConfig(BaseModel):
    tool_review_mode: str = "skip"
    manual_wait_poll_seconds: float = 0.5


class AgentConfig(BaseModel):
    max_decision_steps: int = 10
    orchestrator: Literal["legacy", "langgraph"] = "legacy"


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
    startup_sync_enabled: bool = True
    background_sync_enabled: bool = True
    sync_interval_seconds: int = 300
    sync_limit: int = 25
    sync_max_pages: int = 1

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
    enabled: bool = True
    provider: str = "fastembed"
    index_provider: str = "sqlite_vec"
    model_name: str = "BAAI/bge-small-zh-v1.5"
    dimensions: int = 512
    device: str = "auto"
    cache_dir: Path = Path("./data/models")
    batch_size: int = 16
    normalize_embeddings: bool = True
    query_prefix: str = "为这个句子生成表示以用于检索相关文章："
    default_retrieval_mode: str = "hybrid"
    auto_index_on_import: bool = False
    local_files_only: bool = True


class LocalAppConfig(BaseModel):
    llm: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    safety: SafetyReviewConfig = Field(default_factory=SafetyReviewConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
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
