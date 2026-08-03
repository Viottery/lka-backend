"""Application settings for the backend service."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from `.env` and environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="local-knowledge-agent-os", alias="LKA_APP_NAME")
    version: str = Field(default="0.1.0", alias="LKA_VERSION")
    host: str = Field(default="127.0.0.1", alias="LKA_HOST")
    port: int = Field(default=8765, alias="LKA_PORT")
    data_dir: Path = Field(default=Path("./data"), alias="LKA_DATA_DIR")
    platform: str = Field(default="auto", alias="LKA_PLATFORM")
    default_shell: str = Field(default="auto", alias="LKA_DEFAULT_SHELL")
    workspace_roots: str = Field(default="", alias="LKA_WORKSPACE_ROOTS")
    allow_symlinks: bool = Field(default=False, alias="LKA_ALLOW_SYMLINKS")
    skip_hidden: bool = Field(default=True, alias="LKA_SKIP_HIDDEN")
    max_scan_files: int = Field(default=50_000, alias="LKA_MAX_SCAN_FILES")
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
