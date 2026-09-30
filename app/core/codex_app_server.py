"""Small JSON-RPC client for the stable Codex app-server surface.

This module deliberately does not spawn Codex or execute commands. A caller
must provide a line-oriented transport and explicitly answer captured approval
requests. Protocol messages are newline-delimited JSON objects; the app-server
omits the JSON-RPC ``jsonrpc`` header on the wire.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol


class JsonLineTransport(Protocol):
    """Injectable transport boundary; implementations own process/socket I/O."""

    async def write_line(self, line: str) -> None: ...

    async def read_line(self) -> str | None: ...


class CodexAppServerProtocolError(RuntimeError):
    """Raised when the peer sends an invalid or unsupported protocol message."""


class CodexAppServerRemoteError(RuntimeError):
    """A JSON-RPC error returned for a client request."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class _UnsupportedServerRequest(CodexAppServerProtocolError):
    """An unrecognized server request; the reader returns a JSON-RPC error."""


@dataclass(frozen=True)
class ServerNotification:
    method: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ApprovalRequest:
    """Pending command or file-change approval captured from the app-server."""

    request_id: int | str
    method: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ServerRequest:
    """Known non-command server request that must be handled explicitly."""

    request_id: int | str
    method: str
    params: dict[str, Any]


ServerEvent = ServerNotification | ApprovalRequest | ServerRequest
ApprovalDecision = Literal["accept", "decline", "cancel"]


_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}
_SERVER_REQUEST_REQUIRED_PARAMS = {
    "item/permissions/requestApproval": ("itemId", "threadId", "turnId"),
    "item/tool/requestUserInput": ("itemId", "threadId", "turnId"),
    "mcpServer/elicitation/request": ("threadId",),
}
_NOTIFICATION_METHODS = {
    "thread/started",
    "thread/archived",
    "thread/unarchived",
    "thread/closed",
    "thread/status/changed",
    "turn/started",
    "turn/completed",
    "turn/plan/updated",
    "turn/diff/updated",
    "item/started",
    "item/completed",
    "item/agentMessage/delta",
    "item/plan/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/commandExecution/outputDelta",
    "item/reasoning/textDelta",
    "item/fileChange/outputDelta",
    "command/exec/outputDelta",
    "command/exec/exited",
    "process/outputDelta",
    "process/exited",
    "fs/changed",
    "configWarning",
    "warning",
    "windowsSandbox/setupCompleted",
    "hook/started",
    "hook/completed",
    "model/safetyBuffering/updated",
    "model/rerouted",
    "model/verification",
    "thread/tokenUsage/updated",
    "fuzzyFileSearch/sessionUpdated",
    "fuzzyFileSearch/sessionCompleted",
    "skills/changed",
    "app/list/updated",
    "error",
    "serverRequest/resolved",
}


class CodexAppServerClient:
    """Protocol-only app-server client with explicit, at-most-once approvals."""

    def __init__(
        self,
        transport: JsonLineTransport,
        *,
        permission_profile_id: str | None = None,
    ) -> None:
        if permission_profile_id is not None and (
            not re.fullmatch(r"[A-Za-z0-9_-]+", permission_profile_id)
            or permission_profile_id.startswith(":")
        ):
            raise ValueError("Codex permission profile id must be a custom profile name.")
        self._transport = transport
        self._permission_profile_id = permission_profile_id
        self._next_id = 0
        self._initialized = False
        self._events: asyncio.Queue[ServerEvent | BaseException] = asyncio.Queue()
        self._pending_approvals: dict[int | str, ApprovalRequest] = {}
        self._pending_server_requests: dict[int | str, ServerRequest] = {}
        self._used_approval_ids: set[int | str] = set()
        self._pending_requests: dict[int | str, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._reader_error: BaseException | None = None
        self._event_waiters = 0
        self._thread_cwds: dict[str, str] = {}
        self._closed = False

    async def initialize(
        self,
        *,
        name: str,
        title: str,
        version: str,
    ) -> dict[str, Any]:
        if self._initialized:
            raise CodexAppServerProtocolError("App-server connection is already initialized.")
        params: dict[str, Any] = {
            "clientInfo": {"name": name, "title": title, "version": version}
        }
        if self._permission_profile_id is not None:
            params["capabilities"] = {"experimentalApi": True}
        result = await self._request(
            "initialize",
            params,
            allow_uninitialized=True,
        )
        if not isinstance(result, dict):
            raise CodexAppServerProtocolError("initialize result must be an object.")
        self._initialized = True
        await self._write_message({"method": "initialized", "params": {}})
        return result

    async def start_thread(
        self,
        *,
        cwd: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        self._require_initialized()
        if not Path(cwd).is_absolute():
            raise ValueError("Codex thread cwd must be an absolute, validated workspace path.")
        cwd = str(Path(cwd).resolve(strict=False))
        params: dict[str, Any] = {
            "cwd": cwd,
            # Keep the protocol foundation safe by construction. A later
            # server-owned policy layer may choose stricter settings, but this
            # client cannot opt out of approvals or select full access.
            "approvalPolicy": "on-request",
        }
        if self._permission_profile_id is not None:
            params["permissions"] = self._permission_profile_id
            params["runtimeWorkspaceRoots"] = [cwd]
        else:
            params["sandbox"] = "workspace-write"
        if model is not None:
            params["model"] = model
        result = await self._request("thread/start", params)
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise CodexAppServerProtocolError("thread/start result is missing thread.")
        if self._permission_profile_id is not None:
            active_profile = result.get("activePermissionProfile")
            if (
                not isinstance(active_profile, dict)
                or active_profile.get("id") != self._permission_profile_id
            ):
                raise CodexAppServerProtocolError(
                    "Codex did not activate the requested restricted permission profile."
                )
            if result.get("runtimeWorkspaceRoots") != [cwd]:
                raise CodexAppServerProtocolError(
                    "Codex did not activate the requested isolated workspace root."
                )
        thread = result["thread"]
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexAppServerProtocolError("thread/start result is missing thread id.")
        self._thread_cwds[thread_id] = cwd
        return thread

    async def start_turn(
        self,
        *,
        thread_id: str,
        text: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        self._require_initialized()
        if not thread_id or not text:
            raise ValueError("thread_id and text must be non-empty.")
        cwd = self._thread_cwds.get(thread_id)
        if cwd is None:
            raise CodexAppServerProtocolError("Codex thread is not owned by this client connection.")
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            "cwd": cwd,
            "approvalPolicy": "on-request",
        }
        if self._permission_profile_id is not None:
            params["permissions"] = self._permission_profile_id
            params["runtimeWorkspaceRoots"] = [cwd]
        else:
            params["sandboxPolicy"] = {
                "type": "workspaceWrite",
                "writableRoots": [cwd],
                "readOnlyAccess": {
                    "type": "restricted",
                    "includePlatformDefaults": True,
                    "readableRoots": [cwd],
                },
                "networkAccess": False,
            }
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        try:
            result = await self._request("turn/start", params)
        except CodexAppServerRemoteError as exc:
            if "readOnlyAccess is no longer supported" in str(exc) or "use permissionProfile" in str(exc):
                raise CodexAppServerProtocolError(
                    "This Codex CLI does not support the restricted-read sandbox "
                    "required by the Codex expert. Use a compatible Codex CLI "
                    "or add a verified process-level filesystem sandbox; "
                    "the adapter will not silently grant full host read access."
                ) from exc
            raise
        if not isinstance(result, dict) or not isinstance(result.get("turn"), dict):
            raise CodexAppServerProtocolError("turn/start result is missing turn.")
        return result["turn"]

    async def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None:
        self._require_initialized()
        if not thread_id or not turn_id:
            raise ValueError("thread_id and turn_id must be non-empty.")
        if thread_id not in self._thread_cwds:
            raise CodexAppServerProtocolError("Codex thread is not owned by this client connection.")
        result = await self._request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )
        if result != {}:
            raise CodexAppServerProtocolError("turn/interrupt result must be an empty object.")

    async def next_event(self) -> ServerEvent:
        """Read one notification or captured approval request from the peer."""
        self._require_initialized()
        # Deliver already received events before reporting a later transport
        # failure; otherwise a fast EOF can hide a valid final notification.
        if self._events.empty() and self._reader_error is not None:
            raise self._reader_error
        self._event_waiters += 1
        try:
            event = await self._events.get()
        finally:
            self._event_waiters -= 1
        if isinstance(event, BaseException):
            raise event
        return event

    async def respond_to_approval(
        self,
        request_id: int | str,
        decision: ApprovalDecision,
    ) -> None:
        """Reply once to a previously captured approval; no implicit grants."""
        if decision not in {"accept", "decline", "cancel"}:
            raise ValueError("Approval decision must be accept, decline, or cancel.")
        pending = self._pending_approvals.get(request_id)
        if pending is None or request_id in self._used_approval_ids:
            raise CodexAppServerProtocolError("Approval request is unknown or already answered.")
        # Mark consumed before I/O so transport errors cannot make an approval
        # replayable and accidentally turn a retry into a second authorization.
        self._used_approval_ids.add(request_id)
        del self._pending_approvals[request_id]
        await self._write_message({"id": request_id, "result": {"decision": decision}})

    async def respond_to_server_request(
        self,
        request_id: int | str,
        response: dict[str, Any],
    ) -> None:
        """Answer a captured user-input or permission request with bounded data.

        Permission responses can only grant a recursive subset of what Codex
        requested and may not persist beyond the current turn. Elicitation
        requests can only be declined or cancelled here.
        """
        pending = self._pending_server_requests.get(request_id)
        if pending is None or request_id in self._used_approval_ids:
            raise CodexAppServerProtocolError("Server request is unknown or already answered.")
        if not isinstance(response, dict):
            raise TypeError("Server request response must be an object.")
        method = pending.method
        safe_response: dict[str, Any]
        if method == "item/tool/requestUserInput":
            answers = response.get("answers")
            if set(response) != {"answers"} or not isinstance(answers, dict):
                raise ValueError("User-input response must contain only an answers object.")
            questions = pending.params.get("questions")
            question_ids = {
                question.get("id")
                for question in questions
                if isinstance(question, dict) and isinstance(question.get("id"), str)
            } if isinstance(questions, list) else set()
            if not answers or not set(answers).issubset(question_ids):
                raise ValueError("Answers must reference requested question ids.")
            if any(not isinstance(answer, str) for answer in answers.values()):
                raise ValueError("User-input answers must be strings.")
            safe_response = {"answers": dict(answers)}
        elif method == "item/permissions/requestApproval":
            if set(response) - {"permissions", "scope"}:
                raise ValueError("Permission response contains unsupported fields.")
            granted = response.get("permissions", {})
            scope = response.get("scope", "turn")
            requested = pending.params.get("permissions")
            if requested is None:
                requested = pending.params.get("requestedPermissions")
            if not isinstance(granted, dict) or not isinstance(requested, dict):
                raise ValueError("Permission response must be an object subset of requested permissions.")
            if not _is_recursive_subset(granted, requested):
                raise ValueError("Permission response may grant only requested permissions.")
            if scope != "turn":
                raise ValueError("Permission grants are limited to the current turn.")
            safe_response = {"permissions": dict(granted), "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            if set(response) != {"action"} or response.get("action") not in {"decline", "cancel"}:
                raise ValueError("Elicitation can only be declined or cancelled by this client.")
            safe_response = {"action": response["action"], "content": None}
        else:
            raise CodexAppServerProtocolError("No response handler is configured for this server request.")

        self._used_approval_ids.add(request_id)
        del self._pending_server_requests[request_id]
        await self._write_message({"id": request_id, "result": safe_response})

    async def close(self) -> None:
        """Stop the reader and close an owned transport when it supports it."""
        if self._closed:
            return
        self._closed = True
        reader_task = self._reader_task
        if reader_task is not None and not reader_task.done():
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        for response_future in self._pending_requests.values():
            if not response_future.done():
                response_future.cancel()
        self._pending_requests.clear()
        self._pending_approvals.clear()
        self._pending_server_requests.clear()
        close = getattr(self._transport, "close", None)
        if close is not None:
            await close()

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        allow_uninitialized: bool = False,
    ) -> Any:
        if not allow_uninitialized:
            self._require_initialized()
        self._ensure_reader()
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[Any] = loop.create_future()
        self._pending_requests[request_id] = response_future
        try:
            await self._write_message({"method": method, "id": request_id, "params": params})
        except BaseException:
            self._pending_requests.pop(request_id, None)
            if not response_future.done():
                response_future.cancel()
            raise
        # Shield keeps the reader's correlation future alive if a caller task
        # is cancelled while the peer is still completing the JSON-RPC call.
        return await asyncio.shield(response_future)

    def _ensure_reader(self) -> None:
        if self._reader_error is not None:
            raise CodexAppServerProtocolError("App-server reader has failed.") from self._reader_error
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        try:
            while True:
                message = await self._read_message()
                if "method" in message:
                    try:
                        event = self._classify_server_message(message)
                    except _UnsupportedServerRequest as exc:
                        await self._write_message({
                            "id": _message_id(message["id"]),
                            "error": {"code": -32601, "message": str(exc)},
                        })
                        raise
                    if event is not None:
                        self._events.put_nowait(event)
                    continue
                response_id = _message_id(message.get("id"))
                response_future = self._pending_requests.pop(response_id, None)
                if response_future is None:
                    raise CodexAppServerProtocolError(
                        f"Unexpected JSON-RPC response id {response_id!r}."
                    )
                if response_future.done():
                    raise CodexAppServerProtocolError("JSON-RPC response future was already resolved.")
                if "error" in message:
                    error = message["error"]
                    if (
                        not isinstance(error, dict)
                        or isinstance(error.get("code"), bool)
                        or not isinstance(error.get("code"), int)
                        or not isinstance(error.get("message"), str)
                    ):
                        raise CodexAppServerProtocolError("Malformed JSON-RPC error response.")
                    response_future.set_exception(
                        CodexAppServerRemoteError(
                            error["code"], error["message"], error.get("data")
                        )
                    )
                else:
                    response_future.set_result(message["result"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail all pending RPCs on any reader failure.
            self._reader_error = exc
            for response_future in self._pending_requests.values():
                if not response_future.done():
                    response_future.set_exception(exc)
            self._pending_requests.clear()
            for _ in range(max(1, self._event_waiters)):
                self._events.put_nowait(exc)

    def _classify_server_message(self, message: dict[str, Any]) -> ServerEvent | None:
        method = message.get("method")
        if not isinstance(method, str):
            return None
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise CodexAppServerProtocolError("Server message params must be an object.")
        if "id" in message:
            request_id = _message_id(message["id"])
            if method not in _APPROVAL_METHODS and method not in _SERVER_REQUEST_REQUIRED_PARAMS:
                raise _UnsupportedServerRequest(f"Unsupported server request method: {method}")
            required = ("itemId", "threadId", "turnId") if method in _APPROVAL_METHODS else _SERVER_REQUEST_REQUIRED_PARAMS[method]
            for key in required:
                if not isinstance(params.get(key), str) or not params[key]:
                    raise CodexAppServerProtocolError(f"Server request is missing {key}.")
            if request_id in self._pending_approvals or request_id in self._pending_server_requests or request_id in self._used_approval_ids:
                raise CodexAppServerProtocolError("Duplicate server request id.")
            if method in _APPROVAL_METHODS:
                approval = ApprovalRequest(request_id=request_id, method=method, params=dict(params))
                self._pending_approvals[request_id] = approval
                return approval
            request = ServerRequest(request_id=request_id, method=method, params=dict(params))
            self._pending_server_requests[request_id] = request
            return request
        # Notifications are append-only observations. Protocol additions from
        # newer Codex versions must reach the trace consumer instead of killing
        # the reader or disappearing. The set above documents known families.
        return ServerNotification(method=method, params=dict(params))

    async def _write_message(self, message: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise CodexAppServerProtocolError("Message is not JSON serializable.") from exc
        async with self._write_lock:
            if self._reader_error is not None:
                raise CodexAppServerProtocolError("App-server reader has failed.") from self._reader_error
            await self._transport.write_line(encoded + "\n")

    async def _read_message(self) -> dict[str, Any]:
        line = await self._transport.read_line()
        if line is None:
            raise CodexAppServerProtocolError("App-server transport closed unexpectedly.")
        try:
            message = json.loads(line)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CodexAppServerProtocolError("App-server sent invalid JSON.") from exc
        if not isinstance(message, dict):
            raise CodexAppServerProtocolError("JSON-RPC message must be an object.")
        if message.get("jsonrpc", "2.0") != "2.0":
            raise CodexAppServerProtocolError("Unsupported JSON-RPC version.")
        if "method" in message:
            if not isinstance(message["method"], str) or not message["method"]:
                raise CodexAppServerProtocolError("JSON-RPC method must be a non-empty string.")
            if "result" in message or "error" in message:
                raise CodexAppServerProtocolError("Request/notification cannot also be a response.")
        else:
            if "id" not in message or ("result" in message) == ("error" in message):
                raise CodexAppServerProtocolError("Malformed JSON-RPC response.")
            _message_id(message["id"])
        return message

    def _require_initialized(self) -> None:
        if self._closed:
            raise CodexAppServerProtocolError("App-server connection is closed.")
        if not self._initialized:
            raise CodexAppServerProtocolError("Initialize the app-server connection first.")

def _message_id(value: Any) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CodexAppServerProtocolError("JSON-RPC id must be a string or integer.")
    return value


def _is_recursive_subset(granted: Any, requested: Any) -> bool:
    if isinstance(granted, dict):
        return isinstance(requested, dict) and all(
            key in requested and _is_recursive_subset(value, requested[key])
            for key, value in granted.items()
        )
    if isinstance(granted, list):
        return isinstance(requested, list) and all(item in requested for item in granted)
    return type(granted) is type(requested) and granted == requested
