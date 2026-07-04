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
    qdrant_url: str = Field(default="http://127.0.0.1:6333", alias="QDRANT_URL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
