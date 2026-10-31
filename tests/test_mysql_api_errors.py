"""Stable MySQL API/background error contracts without a database connection."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.database_errors import register_mysql_storage_error_handler
from app.core.background_jobs import _classify_worker_error
from app.storage.mysql import (
    MySQLCommitOutcomeUnknown,
    MySQLDeadlockError,
    MySQLIntegrityError,
    MySQLPoolTimeout,
    MySQLStorageError,
    MySQLTransactionGuardError,
)


def _app_for(error):
    app = FastAPI()
    register_mysql_storage_error_handler(app)

    @app.get("/fail")
    async def fail():
        raise error

    return app


def test_mysql_integrity_error_has_stable_payload_without_driver_message():
    secret_detail = "duplicate value; sql=INSERT secret_bind=do-not-return"
    error = MySQLIntegrityError(secret_detail, errno=1062, sqlstate="23000")

    async def request():
        async with AsyncClient(transport=ASGITransport(app=_app_for(error)), base_url="http://test") as client:
            return await client.get("/fail")

    response = asyncio.run(request())
    assert response.status_code == 409
    assert response.json() == {
        "detail": "database_operation_failed",
        "category": "mysql_integrity",
        "mysql_errno": 1062,
        "mysql_sqlstate": "23000",
        "retry_safe": False,
    }
    assert secret_detail not in response.text


def test_mysql_operational_owner_and_unknown_commit_errors_are_not_retried():
    cases = [
        (MySQLDeadlockError("private SQL", errno=1213, sqlstate="40001"), "mysql_deadlock", 1213),
        (MySQLTransactionGuardError("private token"), "mysql_transaction_guard_rejected", None),
        (MySQLPoolTimeout("private host"), "mysql_pool_timeout", None),
        (MySQLCommitOutcomeUnknown("secret params", errno=2013, sqlstate="HY000"),
         "mysql_commit_outcome_unknown", 2013),
    ]

    async def request(error):
        async with AsyncClient(transport=ASGITransport(app=_app_for(error)), base_url="http://test") as client:
            return await client.get("/fail")

    async def run_all():
        return [await request(error) for error, _, _ in cases]

    responses = asyncio.run(run_all())
    for response, (error, category, errno) in zip(responses, cases, strict=True):
        assert response.status_code == 503
        assert response.headers.get("retry-after") is None
        assert response.json() == {
            "detail": "database_operation_failed",
            "category": category,
            "mysql_errno": errno,
            "mysql_sqlstate": error.mysql_sqlstate,
            "retry_safe": False,
        }
        assert str(error) not in response.text


def test_unclassified_storage_error_is_internal_500_and_background_errors_never_retry():
    error = MySQLStorageError("must stay private", errno=1105, sqlstate="HY000")

    async def request():
        async with AsyncClient(transport=ASGITransport(app=_app_for(error)), base_url="http://test") as client:
            return await client.get("/fail")

    response = asyncio.run(request())
    assert response.status_code == 500
    assert response.json()["category"] == "mysql_database"
    assert "must stay private" not in response.text
    assert _classify_worker_error(error) == ("mysql_database", False)
    assert _classify_worker_error(MySQLCommitOutcomeUnknown("unknown")) == (
        "mysql_commit_outcome_unknown", False,
    )
