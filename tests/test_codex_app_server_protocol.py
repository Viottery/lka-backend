"""Focused contract tests for the injectable Codex app-server protocol client."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.core.codex_app_server import (
    ApprovalRequest,
    CodexAppServerClient,
    CodexAppServerProtocolError,
    ServerNotification,
    ServerRequest,
)


class FakeLineTransport:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.outgoing: list[str] = []
        self.before_response: dict[str, list[dict[str, object]]] = {}

    async def write_line(self, line: str) -> None:
        self.outgoing.append(line)
        message = json.loads(line)
        method = message.get("method")
        if not isinstance(method, str) or "id" not in message:
            return
        for event in self.before_response.get(method, []):
            await self.incoming.put(json.dumps(event))
        if method == "initialize":
            result: dict[str, object] = {"userAgent": "codex-test"}
        elif method == "thread/start":
            result = {"thread": {"id": "thr-1"}}
            if "permissions" in message["params"]:
                result["activePermissionProfile"] = {
                    "id": message["params"]["permissions"], "extends": ":workspace"
                }
                result["runtimeWorkspaceRoots"] = message["params"]["runtimeWorkspaceRoots"]
            await self.incoming.put(json.dumps({
                "method": "thread/started", "params": {"thread": {"id": "thr-1"}}
            }))
        elif method == "turn/start":
            result = {"turn": {"id": "turn-1", "status": "inProgress"}}
        else:
            result = {}
        await self.incoming.put(json.dumps({"id": message["id"], "result": result}))

    async def read_line(self) -> str | None:
        return await self.incoming.get()

    async def push(self, message: dict[str, object]) -> None:
        await self.incoming.put(json.dumps(message))


class QueueLineTransport:
    """Blocking fake peer used to exercise concurrent request/event dispatch."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.outgoing: asyncio.Queue[str] = asyncio.Queue()

    async def write_line(self, line: str) -> None:
        await self.outgoing.put(line)

    async def read_line(self) -> str | None:
        line = await self.incoming.get()
        return None if line == "<EOF>" else line


def _run(coro):
    return asyncio.run(coro)


def test_initialization_and_thread_turn_use_newline_json_rpc() -> None:
    transport = FakeLineTransport()
    client = CodexAppServerClient(transport)

    async def exercise() -> None:
        initialized = await client.initialize(name="lka", title="LKA", version="0.1")
        assert initialized["userAgent"] == "codex-test"
        thread = await client.start_thread(cwd="/workspace", model="model-x")
        turn = await client.start_turn(thread_id=thread["id"], text="Inspect this repo")
        assert turn["id"] == "turn-1"
        event = await client.next_event()
        assert isinstance(event, ServerNotification)
        assert event.method == "thread/started"

    _run(exercise())
    sent = [json.loads(line) for line in transport.outgoing]
    assert [message["method"] for message in sent] == [
        "initialize", "initialized", "thread/start", "turn/start"
    ]
    assert all(line.endswith("\n") for line in transport.outgoing)
    assert sent[2]["params"]["approvalPolicy"] == "on-request"
    assert sent[2]["params"]["sandbox"] == "workspace-write"
    assert sent[3]["params"]["approvalPolicy"] == "on-request"
    assert sent[3]["params"]["sandboxPolicy"]["writableRoots"] == ["/workspace"]
    assert sent[3]["params"]["sandboxPolicy"]["networkAccess"] is False


def test_unsupported_restricted_read_fails_closed() -> None:
    class IncompatibleTransport(FakeLineTransport):
        async def write_line(self, line: str) -> None:
            message = json.loads(line)
            if message.get("method") == "turn/start":
                self.outgoing.append(line)
                await self.incoming.put(json.dumps({
                    "id": message["id"],
                    "error": {"code": -32602, "message": (
                        "workspaceWrite.readOnlyAccess is no longer supported; "
                        "use permissionProfile for restricted reads"
                    )},
                }))
                return
            await super().write_line(line)

    async def exercise() -> None:
        client = CodexAppServerClient(IncompatibleTransport())
        await client.initialize(name="lka", title="LKA", version="0.1")
        await client.start_thread(cwd="/workspace")
        with pytest.raises(CodexAppServerProtocolError, match="will not silently grant full host read"):
            await client.start_turn(thread_id="thr-1", text="Inspect workspace")

    _run(exercise())


def test_named_permission_profile_uses_experimental_profile_fields() -> None:
    transport = FakeLineTransport()
    client = CodexAppServerClient(
        transport, permission_profile_id="lka_codex_test123"
    )

    async def exercise() -> None:
        await client.initialize(name="lka", title="LKA", version="0.1")
        await client.start_thread(cwd="/workspace", model="model-x")
        await client.start_turn(thread_id="thr-1", text="Inspect this repo")

    _run(exercise())
    sent = [json.loads(line) for line in transport.outgoing]
    assert sent[0]["params"]["capabilities"] == {"experimentalApi": True}
    thread_params = sent[2]["params"]
    turn_params = sent[3]["params"]
    assert thread_params["permissions"] == "lka_codex_test123"
    assert turn_params["permissions"] == "lka_codex_test123"
    assert thread_params["runtimeWorkspaceRoots"] == ["/workspace"]
    assert turn_params["runtimeWorkspaceRoots"] == ["/workspace"]
    assert "sandbox" not in thread_params
    assert "sandboxPolicy" not in turn_params


@pytest.mark.parametrize("reported_profile", [None, {"id": ":workspace"}])
def test_named_profile_requires_server_activation_evidence(reported_profile) -> None:
    class WrongProfileTransport(FakeLineTransport):
        async def write_line(self, line: str) -> None:
            message = json.loads(line)
            if message.get("method") == "thread/start":
                self.outgoing.append(line)
                await self.incoming.put(json.dumps({
                    "id": message["id"],
                    "result": {
                        "thread": {"id": "thr-1"},
                        "activePermissionProfile": reported_profile,
                        "runtimeWorkspaceRoots": ["/workspace"],
                    },
                }))
                return
            await super().write_line(line)

    async def exercise() -> None:
        client = CodexAppServerClient(
            WrongProfileTransport(), permission_profile_id="lka_codex_expected"
        )
        await client.initialize(name="lka", title="LKA", version="0.1")
        with pytest.raises(CodexAppServerProtocolError, match="did not activate"):
            await client.start_thread(cwd="/workspace")

    _run(exercise())


def test_named_profile_requires_exact_isolated_workspace_root() -> None:
    class WrongRootsTransport(FakeLineTransport):
        async def write_line(self, line: str) -> None:
            message = json.loads(line)
            if message.get("method") == "thread/start":
                self.outgoing.append(line)
                await self.incoming.put(json.dumps({
                    "id": message["id"],
                    "result": {
                        "thread": {"id": "thr-1"},
                        "activePermissionProfile": {"id": "lka_codex_expected"},
                        "runtimeWorkspaceRoots": ["/workspace", "/unapproved"],
                    },
                }))
                return
            await super().write_line(line)

    async def exercise() -> None:
        client = CodexAppServerClient(
            WrongRootsTransport(), permission_profile_id="lka_codex_expected"
        )
        await client.initialize(name="lka", title="LKA", version="0.1")
        with pytest.raises(CodexAppServerProtocolError, match="isolated workspace root"):
            await client.start_thread(cwd="/workspace")

    _run(exercise())


@pytest.mark.parametrize("profile_id", [":workspace", "bad.name", "with space", "x=y"])
def test_permission_profile_id_rejects_non_custom_names(profile_id: str) -> None:
    with pytest.raises(ValueError, match="custom profile name"):
        CodexAppServerClient(FakeLineTransport(), permission_profile_id=profile_id)


def test_approval_is_captured_and_requires_explicit_single_use_reply() -> None:
    transport = FakeLineTransport()
    transport.before_response["turn/start"] = [{
            "id": 88,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "itemId": "item-1",
                "threadId": "thr-1",
                "turnId": "turn-1",
                "command": ["echo", "hello"],
            },
        }]
    client = CodexAppServerClient(transport)

    async def exercise() -> None:
        await client.initialize(name="lka", title="LKA", version="0.1")
        await client.start_thread(cwd="/workspace")
        await client.start_turn(thread_id="thr-1", text="Run the command")
        thread_event = await client.next_event()
        assert isinstance(thread_event, ServerNotification)
        approval = await client.next_event()
        assert isinstance(approval, ApprovalRequest)
        assert approval.request_id == 88
        assert approval.params["command"] == ["echo", "hello"]
        with pytest.raises(ValueError):
            await client.respond_to_approval(88, "acceptForSession")  # type: ignore[arg-type]
        await client.respond_to_approval(88, "accept")
        with pytest.raises(CodexAppServerProtocolError, match="unknown or already answered"):
            await client.respond_to_approval(88, "accept")

    _run(exercise())
    assert json.loads(transport.outgoing[-1]) == {
        "id": 88,
        "result": {"decision": "accept"},
    }


def test_interrupt_frames_thread_and_turn_ids() -> None:
    transport = FakeLineTransport()
    client = CodexAppServerClient(transport)

    async def exercise() -> None:
        await client.initialize(name="lka", title="LKA", version="0.1")
        await client.start_thread(cwd="/workspace")
        await client.start_turn(thread_id="thr-1", text="Work")
        await client.interrupt_turn(thread_id="thr-1", turn_id="turn-1")

    _run(exercise())
    assert json.loads(transport.outgoing[-1]) == {
        "method": "turn/interrupt",
        "id": 3,
        "params": {"threadId": "thr-1", "turnId": "turn-1"},
    }


def test_interrupt_rejects_thread_not_started_by_connection() -> None:
    transport = FakeLineTransport()
    client = CodexAppServerClient(transport)

    async def exercise() -> None:
        await client.initialize(name="lka", title="LKA", version="0.1")
        with pytest.raises(CodexAppServerProtocolError, match="not owned"):
            await client.interrupt_turn(thread_id="foreign-thread", turn_id="turn-1")

    _run(exercise())
    assert [json.loads(line)["method"] for line in transport.outgoing] == [
        "initialize", "initialized"
    ]


def test_unknown_server_request_fails_closed() -> None:
    transport = FakeLineTransport()
    client = CodexAppServerClient(transport)

    async def exercise() -> None:
        await client.initialize(name="lka", title="LKA", version="0.1")
        await transport.push({"id": 7, "method": "initialize/attestation", "params": {}})
        with pytest.raises(CodexAppServerProtocolError, match="Unsupported server request"):
            await client.next_event()
        assert json.loads(transport.outgoing[-1]) == {
            "id": 7,
            "error": {"code": -32601, "message": "Unsupported server request method: initialize/attestation"},
        }

    _run(exercise())


def test_unknown_notification_is_surfaced_for_trace_recording() -> None:
    transport = FakeLineTransport()
    client = CodexAppServerClient(transport)

    async def exercise() -> None:
        await client.initialize(name="lka", title="LKA", version="0.1")
        await transport.push({"method": "unknown/event", "params": {}})
        event = await client.next_event()
        assert isinstance(event, ServerNotification)
        assert event.method == "unknown/event"

    _run(exercise())


def test_user_input_server_request_is_captured_and_schema_checked() -> None:
    transport = FakeLineTransport()
    client = CodexAppServerClient(transport)

    async def exercise() -> None:
        await client.initialize(name="lka", title="LKA", version="0.1")
        await transport.push({
            "id": "input-1",
            "method": "item/tool/requestUserInput",
            "params": {
                "itemId": "item-1", "threadId": "thread-1", "turnId": "turn-1",
                "questions": [{"id": "q1", "question": "Which option?"}],
            },
        })
        request = await client.next_event()
        assert isinstance(request, ServerRequest)
        assert request.method == "item/tool/requestUserInput"
        with pytest.raises(ValueError, match="question ids"):
            await client.respond_to_server_request("input-1", {"answers": {"other": "x"}})
        await client.respond_to_server_request("input-1", {"answers": {"q1": "Option A"}})

    _run(exercise())
    assert json.loads(transport.outgoing[-1]) == {
        "id": "input-1", "result": {"answers": {"q1": "Option A"}}
    }


def test_permission_request_can_only_grant_requested_turn_scoped_subset() -> None:
    transport = FakeLineTransport()
    client = CodexAppServerClient(transport)

    async def exercise() -> None:
        await client.initialize(name="lka", title="LKA", version="0.1")
        await transport.push({
            "id": 21,
            "method": "item/permissions/requestApproval",
            "params": {
                "itemId": "item-1", "threadId": "thread-1", "turnId": "turn-1",
                "permissions": {"network": {"hosts": ["example.test"]}, "write": ["/tmp/x"]},
            },
        })
        request = await client.next_event()
        assert isinstance(request, ServerRequest)
        with pytest.raises(ValueError, match="only requested permissions"):
            await client.respond_to_server_request(21, {"permissions": {"write": ["/"]}})
        with pytest.raises(ValueError, match="current turn"):
            await client.respond_to_server_request(
                21, {"permissions": {"network": {"hosts": ["example.test"]}}, "scope": "session"}
            )
        await client.respond_to_server_request(
            21, {"permissions": {"network": {"hosts": ["example.test"]}}}
        )

    _run(exercise())
    assert json.loads(transport.outgoing[-1]) == {
        "id": 21,
        "result": {"permissions": {"network": {"hosts": ["example.test"]}}, "scope": "turn"},
    }


def test_event_waiter_does_not_block_interrupt_and_concurrent_responses_route_by_id() -> None:
    transport = QueueLineTransport()
    client = CodexAppServerClient(transport)

    async def exercise() -> None:
        initialize_task = asyncio.create_task(
            client.initialize(name="lka", title="LKA", version="0.1")
        )
        init_request = json.loads(await transport.outgoing.get())
        assert init_request["method"] == "initialize"
        await transport.incoming.put(json.dumps({"id": init_request["id"], "result": {}}))
        initialized_notification = json.loads(await transport.outgoing.get())
        assert initialized_notification["method"] == "initialized"
        await initialize_task

        thread_task = asyncio.create_task(client.start_thread(cwd="/workspace"))
        thread_request = json.loads(await transport.outgoing.get())
        assert thread_request["method"] == "thread/start"
        await transport.incoming.put(json.dumps({
            "id": thread_request["id"], "result": {"thread": {"id": "thread-a"}}
        }))
        await thread_task

        event_waiter = asyncio.create_task(client.next_event())
        await asyncio.sleep(0)
        first_interrupt = asyncio.create_task(
            client.interrupt_turn(thread_id="thread-a", turn_id="turn-a")
        )
        second_interrupt = asyncio.create_task(
            client.interrupt_turn(thread_id="thread-a", turn_id="turn-b")
        )
        first_request = json.loads(await transport.outgoing.get())
        second_request = json.loads(await transport.outgoing.get())
        assert first_request["method"] == second_request["method"] == "turn/interrupt"

        await transport.incoming.put(json.dumps({
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-a", "status": "interrupted"}},
        }))
        await transport.incoming.put(json.dumps({"id": second_request["id"], "result": {}}))
        await transport.incoming.put(json.dumps({"id": first_request["id"], "result": {}}))

        await asyncio.wait_for(asyncio.gather(first_interrupt, second_interrupt), timeout=1)
        event = await asyncio.wait_for(event_waiter, timeout=1)
        assert isinstance(event, ServerNotification)
        assert event.method == "turn/completed"

    _run(exercise())


def test_reader_delivers_queued_event_before_transport_error() -> None:
    transport = QueueLineTransport()
    client = CodexAppServerClient(transport)

    async def exercise() -> None:
        initialized = asyncio.create_task(client.initialize(name="lka", title="LKA", version="0.1"))
        request = json.loads(await transport.outgoing.get())
        await transport.incoming.put(json.dumps({"id": request["id"], "result": {}}))
        await transport.outgoing.get()  # initialized notification
        await initialized
        await transport.incoming.put(json.dumps({"method": "turn/completed", "params": {}}))
        await transport.incoming.put("<EOF>")

        event = await asyncio.wait_for(client.next_event(), timeout=1)
        assert isinstance(event, ServerNotification)
        assert event.method == "turn/completed"
        with pytest.raises(CodexAppServerProtocolError, match="transport closed"):
            await asyncio.wait_for(client.next_event(), timeout=1)

    _run(exercise())
