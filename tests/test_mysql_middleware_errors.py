"""MySQL errors raised across ASGI authentication boundaries stay structured."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.account_auth import AccountAuthMiddleware
from app.api.device_connection_auth import DeviceConnectionAuthMiddleware
from app.storage.mysql import MySQLIntegrityError, MySQLPoolTimeout


def _middleware_app(middleware, error):
    app = FastAPI()
    app.state.account_service = None
    app.state.device_connection_service = None

    @app.get("/fail")
    async def fail():
        raise error

    app.add_middleware(middleware)
    return app


@pytest.mark.parametrize("middleware", [AccountAuthMiddleware, DeviceConnectionAuthMiddleware])
@pytest.mark.parametrize("error,status,category", [
    (MySQLPoolTimeout("private pool detail"), 503, "mysql_pool_timeout"),
    (MySQLIntegrityError("private SQL and bind", errno=1062, sqlstate="23000"), 409, "mysql_integrity"),
])
def test_auth_middleware_maps_mysql_storage_errors(middleware, error, status, category):
    async def request():
        async with AsyncClient(
            transport=ASGITransport(app=_middleware_app(middleware, error)),
            base_url="http://test",
        ) as client:
            return await client.get("/fail")

    response = asyncio.run(request())
    assert response.status_code == status
    assert response.json() == {
        "detail": "database_operation_failed",
        "category": category,
        "mysql_errno": error.mysql_errno,
        "mysql_sqlstate": error.mysql_sqlstate,
        "retry_safe": False,
    }
    assert str(error) not in response.text


@pytest.mark.parametrize("middleware", [AccountAuthMiddleware, DeviceConnectionAuthMiddleware])
def test_auth_middleware_does_not_swallow_non_storage_errors(middleware):
    async def request():
        async with AsyncClient(
            transport=ASGITransport(app=_middleware_app(middleware, ValueError("ordinary failure"))),
            base_url="http://test",
        ) as client:
            return await client.get("/fail")

    with pytest.raises(ValueError, match="ordinary failure"):
        asyncio.run(request())


@pytest.mark.parametrize("middleware", [AccountAuthMiddleware, DeviceConnectionAuthMiddleware])
def test_auth_middleware_does_not_send_second_response_after_headers(middleware):
    error = MySQLPoolTimeout("private pool detail")
    sent = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise error

    app = FastAPI()
    app.state.account_service = None
    app.state.device_connection_service = None
    wrapped = middleware(inner)
    scope = {
        "type": "http", "method": "GET", "path": "/", "headers": [],
        "state": {}, "app": app, "client": ("127.0.0.1", 1234), "scheme": "http",
    }

    async def run():
        with pytest.raises(MySQLPoolTimeout):
            async def send(message):
                sent.append(message)
            await wrapped(scope, lambda: None, send)

    asyncio.run(run())
    assert [item["type"] for item in sent] == ["http.response.start"]
