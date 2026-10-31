"""Application settings for the backend service."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.local_config import LocalAppConfig, load_local_config


class Settings(BaseSettings):
    """Runtime configuration loaded from `.env` and environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True,
    )

    app_name: str = Field(default="local-knowledge-agent-os", alias="LKA_APP_NAME")
    version: str = Field(default="0.2.0", alias="LKA_VERSION")
    deployment_id: str | None = Field(default=None, alias="LKA_DEPLOYMENT_ID", pattern=r"^[a-f0-9]{64}$")
    host: str = Field(default="127.0.0.1", alias="LKA_HOST")
    port: int = Field(default=8765, alias="LKA_PORT")
    data_dir: Path = Field(default=Path("./data/runtime"), alias="LKA_DATA_DIR")
    storage_backend: Literal["sqlite", "mysql"] = Field(default="sqlite", alias="LKA_STORAGE_BACKEND")
    mysql_host: str = Field(default="127.0.0.1", alias="LKA_MYSQL_HOST")
    mysql_port: int = Field(default=3306, alias="LKA_MYSQL_PORT", ge=1, le=65535)
    mysql_database: str = Field(default="lka", alias="LKA_MYSQL_DATABASE", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    mysql_user: str = Field(default="lka", alias="LKA_MYSQL_USER")
    mysql_password: SecretStr | None = Field(default=None, alias="LKA_MYSQL_PASSWORD", repr=False)
    mysql_pool_size: int = Field(default=8, alias="LKA_MYSQL_POOL_SIZE", ge=1, le=64)
    mysql_pool_queue_capacity: int = Field(default=128, alias="LKA_MYSQL_POOL_QUEUE_CAPACITY", ge=1, le=10000)
    mysql_pool_wait_seconds: float = Field(default=5, alias="LKA_MYSQL_POOL_WAIT_SECONDS", gt=0, le=60,
                                          allow_inf_nan=False)
    mysql_lock_wait_seconds: int = Field(default=5, alias="LKA_MYSQL_LOCK_WAIT_SECONDS", ge=1, le=60)

    @model_validator(mode="after")
    def validate_storage(self):
        if self.storage_backend == "mysql" and (not self.mysql_password or not self.mysql_password.get_secret_value()):
            raise ValueError("MySQL storage requires LKA_MYSQL_PASSWORD")
        return self

    def mysql_config(self):
        if self.storage_backend != "mysql":
            return None
        from app.storage.mysql import MySQLConfig

        return MySQLConfig(
            host=self.mysql_host, port=self.mysql_port, user=self.mysql_user,
            password=self.mysql_password.get_secret_value(), database=self.mysql_database,
            pool_size=self.mysql_pool_size, pool_wait_seconds=self.mysql_pool_wait_seconds,
            pool_queue_capacity=self.mysql_pool_queue_capacity,
            lock_wait_seconds=self.mysql_lock_wait_seconds,
            transaction_seconds=self.db_write_transaction_seconds,
        )
    db_timing_enabled: bool = Field(default=True, alias="LKA_DB_TIMING_ENABLED")
    db_timing_sample_rate: float = Field(
        default=0.01, alias="LKA_DB_TIMING_SAMPLE_RATE", ge=0, le=1, allow_inf_nan=False,
    )
    db_timing_slow_ms: float = Field(
        default=50, alias="LKA_DB_TIMING_SLOW_MS", ge=0, allow_inf_nan=False,
    )
    db_write_queue_capacity: int = Field(default=128, alias="LKA_DB_WRITE_QUEUE_CAPACITY", ge=1, le=10000)
    db_write_queue_wait_seconds: float = Field(
        default=5, alias="LKA_DB_WRITE_QUEUE_WAIT_SECONDS", gt=0, le=300, allow_inf_nan=False,
    )
    db_write_transaction_seconds: float = Field(
        default=30, alias="LKA_DB_WRITE_TRANSACTION_SECONDS", gt=0, le=3600, allow_inf_nan=False,
    )
    agent_execution_concurrency: int = Field(default=4, alias="LKA_AGENT_EXECUTION_CONCURRENCY", ge=1, le=64)
    agent_execution_interactive_reserved: int = Field(default=2, alias="LKA_AGENT_EXECUTION_INTERACTIVE_RESERVED", ge=0, le=63)
    agent_child_execution_concurrency: int = Field(default=2, alias="LKA_AGENT_CHILD_EXECUTION_CONCURRENCY", ge=1, le=32)

    @model_validator(mode="after")
    def validate_execution_capacity(self):
        if self.agent_execution_interactive_reserved >= self.agent_execution_concurrency:
            raise ValueError("Interactive reserved execution capacity must be less than total capacity")
        return self

    scheduling_mode: Literal["poll", "shadow", "event"] = Field(default="event", alias="LKA_SCHEDULING_MODE")
    scheduling_reconcile_seconds: float = Field(
        default=30, alias="LKA_SCHEDULING_RECONCILE_SECONDS", ge=1, le=300, allow_inf_nan=False,
    )
    scheduling_capacity: int = Field(default=1024, alias="LKA_SCHEDULING_CAPACITY", ge=8, le=100000)
    scheduling_workers: int = Field(default=2, alias="LKA_SCHEDULING_WORKERS", ge=1, le=8)
    scheduling_recovery_page_size: int = Field(default=128, alias="LKA_SCHEDULING_RECOVERY_PAGE_SIZE", ge=1, le=1024)
    scheduling_stop_seconds: float = Field(
        default=45, alias="LKA_SCHEDULING_STOP_SECONDS", ge=1, le=120, allow_inf_nan=False,
    )
    messages_media_cache_dir: Path | None = Field(
        default=None, alias="LKA_MESSAGES_MEDIA_CACHE_DIR"
    )
    platform: str = Field(default="auto", alias="LKA_PLATFORM")
    default_shell: str = Field(default="auto", alias="LKA_DEFAULT_SHELL")
    workspace_roots: str = Field(default="", alias="LKA_WORKSPACE_ROOTS")
    wsl_windows_mount_root: Path = Field(
        default=Path("/mnt"), alias="LKA_WSL_WINDOWS_MOUNT_ROOT"
    )
    cors_origins: str = Field(
        default=(
            "http://127.0.0.1:8780;"
            "http://localhost:8780;"
            "http://127.0.0.1:8000;"
            "http://localhost:8000"
        ),
        alias="LKA_CORS_ORIGINS",
    )
    allow_symlinks: bool = Field(default=False, alias="LKA_ALLOW_SYMLINKS")
    skip_hidden: bool = Field(default=True, alias="LKA_SKIP_HIDDEN")
    max_scan_files: int = Field(default=50_000, alias="LKA_MAX_SCAN_FILES")
    local_config_path: Path = Field(default=Path("./config/local.toml"), alias="LKA_LOCAL_CONFIG")
    qdrant_url: str = Field(default="http://127.0.0.1:6333", alias="QDRANT_URL")

    def parsed_workspace_roots(self) -> list[Path]:
        """Return configured workspace roots split with the platform path separator."""

        if not self.workspace_roots.strip():
            return []
        return [
            Path(root.strip()).expanduser()
            for root in self.workspace_roots.split(";")
            if root.strip()
        ]

    def parsed_cors_origins(self) -> list[str]:
        """Return configured local frontend origins."""

        return [
            origin.strip().rstrip("/")
            for origin in self.cors_origins.split(";")
            if origin.strip()
        ]

    def load_local_config(self) -> LocalAppConfig:
        """Load provider, mail, and embedding config from the local TOML file."""

        return load_local_config(self.local_config_path)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
