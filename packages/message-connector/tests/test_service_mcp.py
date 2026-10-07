"""Synthetic backend and real MCP sessions; no Telegram, models or personal data."""

import asyncio
import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from test_store import ack, message, policy

from lka_message_connector.backend import BackendClient, BackendUnavailable
from lka_message_connector.config import ConnectorConfig, local_backend_url
from lka_message_connector.mcp_server import build_mcp, http_app
from lka_message_connector.service import AccessScope, Connector


class Backend:
    def __init__(self):
        self.policies = [policy()]
        self.requests = []
        self.fail = False

    def handle(self, request):
        self.requests.append(request)
        if self.fail:
            return httpx.Response(503, text="private upstream failure")
        if request.url.path == "/integrations/messages/policies":
            return httpx.Response(200, json={"policies": [p.model_dump() for p in self.policies]})
        if request.url.path == "/integrations/messages/import":
            payload = json.loads(request.content)
            assert payload["schema_version"] == 2
            return httpx.Response(
                200, json={"acknowledged": [ack(m) for m in payload["messages"]], "rejected": []}
            )
        if request.url.path.endswith("/summary"):
            return httpx.Response(200, json={"summary": {"text": "derived"}})
        raise AssertionError(request.url.path)


def connector(tmp_path, backend=None, *, scope=None, **config_changes):
    config = ConnectorConfig(tmp_path, "http://127.0.0.1:8765", "import-private", **config_changes)
    backend = backend or Backend()
    client = BackendClient(config, transport=httpx.MockTransport(backend.handle))
    return Connector(config, backend=client, scope=scope), backend


def test_sync_and_fresh_permissions(tmp_path):
    async def run():
        service, remote = connector(tmp_path)
        await service.refresh_policies()
        service.store.enqueue(message(text="资料🙂"))
        await service.sync_once()
        assert service.store.status()["acked"] == 1
        assert (await service.query("search", "资料"))["total"] == 1
        for request in remote.requests:
            assert request.headers["Authorization"] == "Bearer import-private"
        wire = next(r for r in remote.requests if r.method == "POST")
        assert "资料🙂".encode() in wire.content
        remote.fail = True
        with pytest.raises(BackendUnavailable):
            await service.query("read_message", message().internal_id)
        remote.fail = False
        remote.policies = [policy().model_copy(update={"record_enabled": False})]
        with pytest.raises(PermissionError):
            await service.query("read_message", message().internal_id)
        assert service.store.status()["quarantined"] == 1
        await service.backend.close()

    asyncio.run(run())


def test_analysis_credentials_separate_from_capture(tmp_path):
    async def run():
        service, remote = connector(tmp_path, api_token="read-private")
        await service.refresh_policies()
        service.store.enqueue(message())
        assert (await service.analysis(policy().key, "summary"))["summary"]["text"] == "derived"
        assert remote.requests[-1].headers["Authorization"] == "Bearer read-private"
        service2, _ = connector(tmp_path / "separate")
        await service2.refresh_policies()
        with pytest.raises(PermissionError, match="analysis_access_not_configured"):
            await service2.analysis(policy().key, "summary")
        await service.backend.close()
        await service2.backend.close()

    asyncio.run(run())


def test_reading_analysis_views_are_scoped_bounded_and_read_only(tmp_path):
    async def run():
        paths = []

        def handle(request):
            paths.append((request.url.path, dict(request.url.params)))
            assert request.method == "GET"
            if request.url.path == "/integrations/messages/policies":
                return httpx.Response(200, json={"policies": [policy().model_dump()]})
            assert request.headers["Authorization"] == "Bearer read-private"
            return httpx.Response(200, json={"items": []})

        service, _ = connector(tmp_path, api_token="read-private")
        await service.backend.close()
        service.backend = BackendClient(service.config, transport=httpx.MockTransport(handle))
        for view in ("topics", "insights", "participants", "dossiers", "overview"):
            await service.analysis(policy().key, view, limit=20, cursor="opaque")
            path, params = paths[-1]
            assert path == f"/messages/reading/{view}"
            assert params["conversation_key"] == policy().key
            if view != "overview":
                assert params["limit"] == "20" and params["cursor"] == "opaque"
        await service.analysis(policy().key, "participant", sender_id="42")
        assert paths[-1][0].endswith(f"/participants/{policy().key}/42")
        for arguments in (
            {"view": "pause"},
            {"view": "participant", "sender_id": "../profile"},
            {"view": "topics", "limit": 10000},
        ):
            with pytest.raises(ValueError):
                await service.analysis(policy().key, **arguments)
        service.scope = AccessScope(frozenset())
        with pytest.raises(PermissionError):
            await service.analysis(policy().key, "topics")
        await service.backend.close()

    asyncio.run(run())


def test_shared_policy_lease_serializes_fetch_and_apply_across_instances(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        first, _ = connector(tmp_path)
        second, remote = connector(tmp_path)

        async def delayed(request):
            snapshot = policy().model_dump()
            started.set()
            await release.wait()
            return httpx.Response(200, json={"policies": [snapshot]})

        await first.backend.close()
        first.backend = BackendClient(first.config, transport=httpx.MockTransport(delayed))
        remote.policies = [
            policy().model_copy(update={"record_enabled": False, "capture_epoch": 2, "revision": 2})
        ]
        old_request = asyncio.create_task(first.refresh_policies())
        await started.wait()
        new_request = asyncio.create_task(second.refresh_policies())
        await asyncio.sleep(0.1)
        assert not remote.requests
        release.set()
        await asyncio.gather(old_request, new_request)
        final = first.store.policy(policy().conversation)
        assert not final.record_enabled and final.capture_epoch == 2
        await first.backend.close()
        await second.backend.close()

    asyncio.run(run())


def test_backfill_drains_all_wire_batches_and_disconnects(tmp_path):
    async def run():
        service, remote = connector(tmp_path)

        class Adapter:
            platform = "telegram"
            account_id = "account"
            disconnected = False

            async def connect(self):
                pass

            async def disconnect(self):
                self.disconnected = True

            async def backfill(self, conversation, limit):
                from lka_message_connector.models import SourceEvent

                for number in range(limit):
                    yield SourceEvent(conversation, number, 100)

            def normalize(self, event, approved):
                return message(event.payload)

        adapter = Adapter()
        assert await service.backfill(adapter, policy().key, 205) == 205
        assert adapter.disconnected and service.store.status()["pending"] == 0
        posts = [r for r in remote.requests if r.method == "POST"]
        assert [len(json.loads(r.content)["messages"]) for r in posts] == [100, 100, 5]
        await service.backend.close()

    asyncio.run(run())


def test_cancelled_policy_apply_keeps_lease_until_worker_finishes(tmp_path, monkeypatch):
    async def run():
        import threading

        first, _ = connector(tmp_path)
        second, remote = connector(tmp_path)
        started, release = threading.Event(), threading.Event()
        original = first.store.replace_policies

        def blocked(policies):
            started.set()
            assert release.wait(5)
            original(policies)

        monkeypatch.setattr(first.store, "replace_policies", blocked)
        older = asyncio.create_task(first.refresh_policies())
        assert await asyncio.to_thread(started.wait, 5)
        older.cancel()
        remote.policies = [
            policy().model_copy(update={"record_enabled": False, "capture_epoch": 2, "revision": 2})
        ]
        newer = asyncio.create_task(second.refresh_policies())
        await asyncio.sleep(0.1)
        assert not remote.requests
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await older
        await newer
        assert not second.store.policy(policy().conversation).record_enabled
        await first.backend.close()
        await second.backend.close()

    asyncio.run(run())


def test_capture_media_insert_finishes_before_observed_revocation(tmp_path, monkeypatch):
    async def run():
        import threading

        from lka_message_connector.models import SourceEvent

        first, remote = connector(tmp_path, media_enabled=True)
        approved = policy(media_enabled=True)
        remote.policies = [approved]
        await first.refresh_policies()
        second, second_remote = connector(tmp_path, media_enabled=True)
        second_remote.policies = [
            approved.model_copy(update={"media_enabled": False, "revision": 2})
        ]
        started, release = threading.Event(), threading.Event()
        cache = await first.media_cache()
        original = cache.enqueue

        def blocked(message, policy):
            started.set()
            assert release.wait(5)
            original(message, policy)

        monkeypatch.setattr(cache, "enqueue", blocked)

        class Adapter:
            platform = "telegram"
            account_id = "account"

            def normalize(self, event, approved):
                return message(attachments=[{"ordinal": 0, "kind": "image"}])

        capture = asyncio.create_task(
            first.consume(Adapter(), SourceEvent(approved.conversation, None, 100))
        )
        assert await asyncio.to_thread(started.wait, 5)
        revoke = asyncio.create_task(second.refresh_policies())
        await asyncio.sleep(0.1)
        assert not second_remote.requests
        release.set()
        await asyncio.gather(capture, revoke)
        row = cache._rows()[0]
        assert row["revoked"] == 1
        second_remote.policies = [approved.model_copy(update={"revision": 3})]
        await second.refresh_policies()
        assert cache._rows()[0]["revoked"] == 1
        await first.backend.close()
        await second.backend.close()

    asyncio.run(run())


def test_native_stdio_mcp_starts_without_telegram_credentials(tmp_path):
    async def run():
        import sys

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lka_message_connector.cli", "mcp", "--transport", "stdio"],
            env={
                "LKA_CONNECTOR_DATA_DIR": str(tmp_path),
                "LKA_MESSAGES_IMPORT_TOKEN": "synthetic-import",
            },
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            catalog = await client.list_tools()
            assert any(t.name == "messages.search" for t in catalog.tools)
            status = await client.call_tool("messages.status", {})
            assert not status.isError and status.structuredContent["complete_for_platform"] is False

    asyncio.run(run())


def test_real_mcp_protocol_schema_scope_and_revocation(tmp_path):
    async def run():
        service, remote = connector(tmp_path)
        await service.refresh_policies()
        service.store.enqueue(message())
        server = build_mcp(service)
        async with create_connected_server_and_client_session(server) as client:
            catalog = await client.list_tools()
            names = {t.name for t in catalog.tools}
            assert {
                "messages.search",
                "messages.context",
                "messages.analysis",
                "messages.read_attachment_chunk",
            } <= names
            assert all(t.annotations.readOnlyHint and t.meta["read_only"] for t in catalog.tools)
            assert not any("login" in n or "send" in n or "backfill" in n for n in names)
            result = await client.call_tool("messages.search", {"query": "message"})
            assert not result.isError
            assert result.structuredContent["total"] == 1
            service.scope = AccessScope(frozenset())
            result = await client.call_tool("messages.search", {"query": "message"})
            assert not result.isError and result.structuredContent["total"] == 0
            result = await client.call_tool(
                "messages.read_message", {"message_id": message().internal_id}
            )
            assert result.isError
            service.scope = AccessScope()
            remote.policies = []
            result = await client.call_tool(
                "messages.read_message", {"message_id": message().internal_id}
            )
            assert result.isError
            remote.fail = True
            result = await client.call_tool("messages.search", {"query": "message"})
            assert result.isError
            assert "private" not in str(result.content)
        await service.backend.close()

    asyncio.run(run())


def test_mcp_http_auth_origin_host_and_initialization(tmp_path):
    async def run():
        service, _ = connector(tmp_path)
        server = build_mcp(service)
        token = "a" * 40
        await service.refresh_policies()
        service.store.enqueue(message())
        app = http_app(server, token, service)
        async with app.router.lifespan_context(app):  # noqa: SIM117 -- lifespan owns transport
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8791"
            ) as client:
                request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "synthetic", "version": "1"},
                    },
                }
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                }
                assert (await client.post("/mcp", json=request)).status_code == 401
                assert (
                    await client.post(
                        "/mcp", json=request, headers={**headers, "Host": "evil.example"}
                    )
                ).status_code == 421
                assert (
                    await client.post(
                        "/mcp", json=request, headers={**headers, "Origin": "http://evil.example"}
                    )
                ).status_code == 403
                response = await client.post("/mcp", json=request, headers=headers)
                assert response.status_code == 200
                assert response.json()["result"]["serverInfo"]["name"] == "LKA Message Connector"
                response = await client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                )
                assert response.status_code == 200
                assert all(
                    t["annotations"]["readOnlyHint"] for t in response.json()["result"]["tools"]
                )
                for _ in range(2):
                    response = await client.post(
                        "/mcp",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {
                                "name": "messages.search",
                                "arguments": {"query": "message"},
                            },
                        },
                    )
                    assert response.status_code == 200
                    result = response.json()["result"]
                    assert not result.get("isError") and result["structuredContent"]["total"] == 1
                    assert not service.backend._client.is_closed
        assert service.backend._client.is_closed
        await service.backend.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1:8765/path",
        "http://user:pass@localhost",
        "http://localhost?token=x",
    ],
)
def test_backend_url_rejects_nonlocal_or_credential_paths(url):
    with pytest.raises(ValueError):
        local_backend_url(url)


def test_session_lease_and_cli_require_explicit_commands(tmp_path, monkeypatch, capsys):
    from lka_message_connector.cli import main
    from lka_message_connector.lease import SessionLease

    with SessionLease(tmp_path / "session.lock"):  # noqa: SIM117 -- outer lock holds ownership
        with pytest.raises(RuntimeError, match="telegram_session_in_use"):
            SessionLease(tmp_path / "session.lock")
    monkeypatch.setenv("LKA_TG_API_HASH", "SENSITIVE_BAD_HASH")
    assert main(["login"]) == 1
    assert "SENSITIVE_BAD_HASH" not in capsys.readouterr().err
