"""Explicit operator commands; no credentials accepted in command-line arguments."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from .config import ConnectorConfig, TelegramConfig
from .lease import SessionLease


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Standalone Telegram account capture and MCP reads"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Interactive user-account login; does not enable capture")
    commands.add_parser("capture", help="Capture approved inbound messages and sync to LKA")
    backfill = commands.add_parser(
        "backfill", help="Explicitly import bounded approved chat history"
    )
    backfill.add_argument("conversation_key")
    backfill.add_argument("--limit", type=int, default=100)
    mcp = commands.add_parser("mcp", help="Read-only MCP server; capture runs separately")
    mcp.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8791)
    args = parser.parse_args(argv)
    # Telethon and transport debug messages can include provider objects. The
    # CLI exposes fixed error codes only and never logs events or SDK tracebacks.
    logging.getLogger("telethon").disabled = True
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    try:
        if args.command == "login":
            from pathlib import Path

            from .adapters.telegram import TelegramAdapter

            directory = Path(
                os.getenv("LKA_CONNECTOR_DATA_DIR", "data/message-connector")
            ).absolute()
            config = TelegramConfig.from_env(directory)
            with SessionLease(directory / "telegram.lock"):
                account_id = asyncio.run(TelegramAdapter(config).login())
            print(f"Set LKA_TG_ACCOUNT_ID={account_id} to pin this account before capture.")
            return 0
        config = ConnectorConfig.from_env()
        from .service import AccessScope, Connector

        scope_json = os.getenv("LKA_CONNECTOR_MCP_SCOPE", "")
        keys = json.loads(scope_json) if scope_json else None
        if keys is not None and (
            not isinstance(keys, list) or any(type(k) is not str for k in keys)
        ):
            raise ValueError("invalid_mcp_scope")
        connector = Connector(
            config, scope=AccessScope(frozenset(keys) if keys is not None else None)
        )
        if args.command == "mcp":
            from .mcp_server import build_mcp, http_app

            server = build_mcp(connector, host=args.host, port=args.port)
            if args.transport == "stdio":

                async def serve_stdio():
                    try:
                        await server.run_stdio_async()
                    finally:
                        await connector.backend.close()

                asyncio.run(serve_stdio())
            else:
                import uvicorn

                app = http_app(server, config.mcp_token, connector)
                uvicorn.run(
                    app, host=args.host, port=args.port, log_level="warning", access_log=False
                )
        else:
            from .adapters.telegram import TelegramAdapter

            adapter = TelegramAdapter(TelegramConfig.from_env(config.data_dir))
            with SessionLease(config.data_dir / "telegram.lock"):
                if args.command == "capture":
                    connector.register_adapter(adapter)
                    asyncio.run(connector.run())
                else:

                    async def import_history():
                        try:
                            return await connector.backfill(
                                adapter, args.conversation_key, args.limit
                            )
                        finally:
                            await connector.backend.close()

                    count = asyncio.run(import_history())
                    print(f"Captured up to {count} approved history messages.")
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 -- sanitize all third-party SDK startup failures
        # Never print raw exceptions: SDKs can contain phone, API hashes, proxy
        # credentials, session paths and provider message contents in exceptions.
        print(
            "Connector could not start or complete the command. Check account login, "
            "required environment, backend access and session ownership.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
