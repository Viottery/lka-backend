"""Global ASGI authentication boundary, including streaming revalidation."""
from __future__ import annotations

from http.cookies import CookieError, SimpleCookie

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class AccountAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        headers = Headers(scope=scope)
        path = scope.get("path", "")
        # Only the outer node middleware can set these after local verification.
        # Device connection sessions authenticate locally while the hub is offline.
        if state.get("device_connection") or state.get("device_connection_login"):
            return await self.app(scope, receive, send)
        service = getattr(scope["app"].state, "account_service", None)
        enabled = service is not None and service.config.enabled
        if (scope["type"] == "http" and scope["method"] in {"GET", "HEAD"}
                and path in {"/client", "/client/info", "/client/client.html",
                             "/client/client.js", "/client/client.css"}):
            return await self.app(scope, receive, send)

        async def reject(status, detail):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            else:
                await JSONResponse({"detail": detail}, status_code=status,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)

        node_path = path == "/node" or path.startswith("/node/")
        if enabled and path == "/health" and scope["type"] == "http" and scope["method"] in {"GET", "HEAD"}:
            return await JSONResponse({"status": "ok", "service": "local-knowledge-agent-os"})(scope, receive, send)
        if enabled and service.config.require_https and scope.get("scheme") not in {"https", "wss"}:
            return await reject(403, "HTTPS required")
        authorization = headers.get("authorization", "")
        token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        if node_path:
            if scope["type"] == "websocket":
                return await reject(403, "Node WebSocket transport unavailable")
            devices = getattr(scope["app"].state, "device_service", None)
            if devices is None or not getattr(devices.config, "enabled", False) or not token:
                return await reject(401, "Device authentication required")
            try:
                device = await run_in_threadpool(devices.authenticate, token)
            except (ValueError, PermissionError):
                device = None
            if not device:
                return await reject(401, "Device authentication required")
            state["device"] = device
            return await self.app(scope, receive, send)
        if not enabled:
            if path == "/auth" or path.startswith("/auth/"):
                return await reject(404, "Account authentication disabled")
            return await self.app(scope, receive, send)

        origin = headers.get("origin", "")
        # Host is never an authority for cookie mutations. The operator explicitly
        # lists public origins; forwarding headers cannot silently expand that list.
        origin_ok = origin in service.config.allowed_origins
        native = (service.config.allow_bearer_sessions and not origin
                  and headers.get("x-lka-client") == "native")
        if path == "/auth/login" and scope["type"] == "http" and scope["method"] == "POST":
            if not origin_ok and not native:
                return await reject(403, "Allowed Origin required")
            state["native_login"] = native
            return await self.app(scope, receive, send)
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
            item = cookie.get(service.config.cookie_name)
            cookie_token = item.value if item else ""
        except CookieError:
            cookie_token = ""
        # Keep account and domain credentials independent. Native clients can send
        # the account session explicitly while Authorization remains available to
        # message/memory control. Browser cookies always retain their CSRF gate.
        explicit_account = "x-lka-account-token" in headers
        using_bearer = explicit_account or not cookie_token and bool(authorization)
        if explicit_account:
            token = headers.get("x-lka-account-token", "")
        elif cookie_token:
            token = cookie_token
        if using_bearer and (not service.config.allow_bearer_sessions or not token or origin):
            return await reject(401, "Account authentication required")
        account = await run_in_threadpool(service.authenticate, token)
        if account is None:
            return await reject(401, "Account authentication required")
        if scope["type"] == "websocket":
            if not using_bearer and (not origin_ok or not service.check_csrf(account, headers.get("x-csrf-token", ""))):
                return await reject(403, "Allowed Origin and CSRF token required")
        elif (scope["method"] not in {"GET", "HEAD", "OPTIONS"} and not using_bearer
              and (not origin_ok or not service.check_csrf(account, headers.get("x-csrf-token", "")))):
            return await reject(403, "Allowed Origin and CSRF token required")
        state["account"] = {key: value for key, value in account.items() if key != "csrf_hash"}
        state["account_bearer"] = using_bearer
        streaming = False
        closed = False

        async def checked_send(message):
            nonlocal streaming, closed
            if closed:
                return
            kind = message["type"]
            if kind == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": response_headers}
                streaming = any(key.lower() == b"content-type" and b"text/event-stream" in value
                                for key, value in response_headers)
            if (kind == "http.response.body" and streaming) or kind == "websocket.send":
                current = await run_in_threadpool(service.authenticate, token)
                if current is None:
                    closed = True
                    if kind == "websocket.send":
                        await send({"type": "websocket.close", "code": 4401})
                    else:
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                    # Abort the producer after terminating the stream so no further
                    # Agent events are read or generated for this connection.
                    raise SessionRevoked()
            await send(message)

        async def checked_receive():
            nonlocal closed
            message = await receive()
            if message["type"] == "websocket.receive":
                current = await run_in_threadpool(service.authenticate, token)
                if current is None:
                    closed = True
                    await send({"type": "websocket.close", "code": 4401})
                    raise SessionRevoked()
            return message

        try:
            await self.app(scope, checked_receive, checked_send)
        except BaseExceptionGroup as exc:
            remaining = exc.subgroup(lambda error: not isinstance(error, (SessionRevoked, BaseExceptionGroup)))
            if remaining is not None:
                raise remaining
        except SessionRevoked:
            pass


class SessionRevoked(Exception):
    """Internal control signal used to terminate revoked streaming responses."""
