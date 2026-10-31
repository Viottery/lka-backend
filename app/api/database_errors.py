"""Stable, payload-free HTTP mapping for MySQL storage failures."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.storage.mysql import (
    MySQLDependencyError,
    MySQLIntegrityError,
    MySQLOperationalError,
    MySQLStorageError,
)


def database_error_status(exc: MySQLStorageError) -> int:
    if isinstance(exc, MySQLIntegrityError):
        return 409
    if isinstance(exc, (MySQLOperationalError, MySQLDependencyError)):
        return 503
    return 500


def database_error_payload(exc: MySQLStorageError) -> dict:
    return {
        "detail": "database_operation_failed",
        "category": exc.error_category,
        "mysql_errno": exc.mysql_errno,
        "mysql_sqlstate": exc.mysql_sqlstate,
        "retry_safe": False,
    }


async def mysql_storage_error_handler(_request, exc: MySQLStorageError) -> JSONResponse:
    """Do not expose driver messages, SQL text, or bind values to API clients."""
    return JSONResponse(
        status_code=database_error_status(exc),
        content=database_error_payload(exc),
    )


async def run_asgi_with_mysql_storage_errors(app, scope, receive, send) -> None:
    """Map storage failures raised by HTTP middleware before response start.

    Once headers have been sent (including for streaming responses), the failure
    cannot be replaced with a second response. WebSocket behavior is unchanged.
    """
    if scope.get("type") != "http":
        await app(scope, receive, send)
        return
    response_started = False

    async def tracked_send(message):
        nonlocal response_started
        if message.get("type") == "http.response.start":
            response_started = True
        await send(message)

    try:
        await app(scope, receive, tracked_send)
    except MySQLStorageError as exc:
        if response_started:
            raise
        response = await mysql_storage_error_handler(None, exc)
        await response(scope, receive, send)


def register_mysql_storage_error_handler(app: FastAPI) -> None:
    app.add_exception_handler(MySQLStorageError, mysql_storage_error_handler)
