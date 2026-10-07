"""Opt-in configuration. Secret values never enter MCP inputs or results."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def local_backend_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_backend_url") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("backend_must_be_local_http")
    return value.rstrip("/")


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name.lower()}")
    return value


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str = field(repr=False)
    session_path: Path
    expected_account_id: str = ""
    proxy: dict | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, data_dir: Path) -> TelegramConfig:
        proxy_url = os.getenv("LKA_TG_PROXY_URL", "").strip()
        proxy = None
        if proxy_url:
            parsed = urlsplit(proxy_url)
            if (
                parsed.scheme not in {"socks5", "socks4", "http"}
                or not parsed.hostname
                or not parsed.port
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("invalid_telegram_proxy")
            from urllib.parse import unquote

            proxy = {
                "proxy_type": parsed.scheme,
                "addr": parsed.hostname,
                "port": parsed.port,
                "rdns": True,
                "username": unquote(parsed.username) if parsed.username else None,
                "password": unquote(parsed.password) if parsed.password else None,
            }
        api_hash = os.getenv("LKA_TG_API_HASH", "").strip()
        if len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash):
            raise ValueError("telegram_api_hash_required")
        return cls(
            api_id=_integer("LKA_TG_API_ID", 0, 1, 2**31 - 1),
            api_hash=api_hash,
            session_path=data_dir / "telegram.session",
            expected_account_id=os.getenv("LKA_TG_ACCOUNT_ID", "").strip(),
            proxy=proxy,
        )


@dataclass(frozen=True)
class ConnectorConfig:
    data_dir: Path
    backend_url: str
    import_token: str = field(repr=False)
    api_token: str = field(default="", repr=False)
    mcp_token: str = field(default="", repr=False)
    policy_interval: int = 10
    sync_interval: int = 2
    max_pending: int = 10000
    media_enabled: bool = False
    media_ttl_seconds: int = 72 * 3600
    media_max_bytes: int = 2 * 1024**3
    image_max_bytes: int = 20 * 1024**2
    video_max_bytes: int = 200 * 1024**2

    @classmethod
    def from_env(cls) -> ConnectorConfig:
        token = os.getenv("LKA_MESSAGES_IMPORT_TOKEN", "").strip()
        if not token:
            raise ValueError("message_import_token_required")
        return cls(
            data_dir=Path(os.getenv("LKA_CONNECTOR_DATA_DIR", "data/message-connector")).absolute(),
            backend_url=local_backend_url(
                os.getenv("LKA_CONNECTOR_BACKEND_URL", "http://127.0.0.1:8765")
            ),
            import_token=token,
            api_token=os.getenv("LKA_MESSAGES_API_TOKEN", ""),
            mcp_token=os.getenv("LKA_CONNECTOR_MCP_TOKEN", ""),
            policy_interval=_integer("LKA_CONNECTOR_POLICY_INTERVAL", 10, 1, 3600),
            sync_interval=_integer("LKA_CONNECTOR_SYNC_INTERVAL", 2, 1, 3600),
            max_pending=_integer("LKA_CONNECTOR_MAX_PENDING", 10000, 1, 100000),
            media_enabled=os.getenv("LKA_CONNECTOR_MEDIA_ENABLED", "false").lower() == "true",
            media_ttl_seconds=_integer("LKA_CONNECTOR_MEDIA_TTL_SECONDS", 259200, 1, 604800),
            media_max_bytes=_integer("LKA_CONNECTOR_MEDIA_MAX_BYTES", 2 * 1024**3, 1, 2**40),
            image_max_bytes=_integer("LKA_CONNECTOR_IMAGE_MAX_BYTES", 20 * 1024**2, 1, 2 * 10**9),
            video_max_bytes=_integer("LKA_CONNECTOR_VIDEO_MAX_BYTES", 200 * 1024**2, 1, 2 * 10**9),
        )
