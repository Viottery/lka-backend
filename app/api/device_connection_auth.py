"""Local node authentication and the ordinary-session HTTP admission boundary."""
from __future__ import annotations

import json
import re
import sqlite3
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.api.routes.distributed_client import is_workbench_asset_request
from app.domains.device_connections import is_loopback


def ordinary_session_allowed(runtime, session_id):
    """Use durable provenance, including management links, even for local admins.

    Historical unclassified roots remain usable, but unknown descendants and any
    cloud execution envelope are denied. Metadata cannot grant admission.
    """
    conn = runtime.session_service._conn_factory()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT kind,creation_source,creator_session_id,metadata "
                           "FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None or row["kind"] != "ordinary" or row["creator_session_id"]:
            return False
        if row["creation_source"] not in {"user", "unknown"}:
            return False
        metadata = json.loads(row["metadata"])
        if "remote_execution" in metadata:
            return False
        tables = {item[0] for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return not ("managed_session_links" in tables and conn.execute(
                "SELECT 1 FROM managed_session_links WHERE task_session_id=? OR manager_session_id=? LIMIT 1",
                (session_id, session_id)).fetchone())
    finally:
        conn.close()


class DeviceConnectionAuthMiddleware:
    """Runs outside AccountAuthMiddleware; only verified flags bypass account login."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        service = getattr(scope["app"].state, "device_connection_service", None)
        if service is None or not service.config.enabled or not service.config.node_id:
            return await self.app(scope, receive, send)
        state, headers = scope.setdefault("state", {}), Headers(scope=scope)
        path = scope.get("path", "").rstrip("/") or "/"
        method, origin = scope.get("method", ""), headers.get("origin", "")
        origin_ok = origin in service.config.allowed_origins
        native = bool(service.config.allow_native and not origin
                      and headers.get("x-lka-client") == "native")

        async def reject(status, detail):
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 4403})
            return await JSONResponse({"detail": detail}, status_code=status,
                                      headers={"Cache-Control": "no-store"})(scope, receive, send)

        # Account login remains available as an independent, local credential.
        if (path == "/health" or path == "/auth" or path.startswith("/auth/")
                or method in {"GET", "HEAD"} and path in {
                    "/client", "/client/info", "/client/client.html", "/client/client.js", "/client/client.css"}
                or is_workbench_asset_request(scope)):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await reject(403, "direct node WebSocket transport unavailable")
        if path == "/connections/redeem" and method == "POST":
            if scope.get("scheme") != "https" or not (origin_ok or native):
                return await reject(403, "HTTPS and allowed Origin or native client required")
            state["device_connection_login"] = True
            state["device_connection_native"] = native
            return await self.app(scope, receive, send)

        cookies = SimpleCookie()
        try:
            cookies.load(headers.get("cookie", ""))
            item = cookies.get(service.config.cookie_name)
            cookie_token = item.value if item else ""
        except CookieError:
            cookie_token = ""
        explicit = headers.get("x-lka-connection-token", "")
        token = explicit or cookie_token
        principal = None
        if token:
            if scope.get("scheme") != "https" or explicit and not native:
                return await reject(403, "HTTPS and native token transport required")
            principal = await run_in_threadpool(service.authenticate, token)
            if principal is None:
                return await reject(401, "device connection authentication required")
            state["device_connection_csrf"] = service.csrf_for(token)
            if (not explicit and method not in {"GET", "HEAD", "OPTIONS"}
                    and (not origin_ok or not service.check_csrf(principal, headers.get("x-csrf-token", "")))):
                return await reject(403, "allowed Origin and CSRF token required")
        else:
            accounts = getattr(scope["app"].state, "account_service", None)
            local = is_loopback(scope.get("client")) and (not origin or origin_ok)
            if not local and not (accounts and accounts.config.enabled):
                return await reject(401, "device connection authentication required")
            if path.startswith("/connections"):
                return await reject(401, "device connection authentication required")

        runtime = scope["app"].state.runtime
        # All node HTTP users share this boundary, including loopback and local
        # account credentials. Assistant traffic always goes through the hub.
        match = re.fullmatch(r"/sessions/([^/]+)(?:/(.*))?", path)
        run_match = re.fullmatch(r"/agent/runs/([^/]+)(?:/(.*))?", path)
        review_match = re.fullmatch(r"/agent/safety-reviews/([^/]+)(/decision)?", path)
        collection = path in {"/sessions", "/sessions/deleted"} and method == "GET"
        create = path == "/sessions" and method == "POST"
        turn = path in {"/agent/turn", "/agent/turn/stream"} and method == "POST"
        workspace_index = path == "/workspaces/index" and method == "POST"
        local_workspaces = re.fullmatch(r"/local/workspaces(?:/([^/]+)/(entries|file|directories|status))?", path)
        local_filesystem = path == "/local/filesystem/entries" and method == "GET"
        workspace_selection = path == "/local/workspaces/selections" and method == "POST"
        connection = path == "/connections/me" and method == "GET" or (
            path == "/connections/logout" and method == "POST") or (
            path == "/connections/sessions" and method == "GET") or (
            re.fullmatch(r"/connections/sessions/[a-f0-9]{32}", path) and method == "DELETE")
        shared_memory = (path in {"/local/shared-memory/status", "/local/shared-memory/records",
                                  "/local/shared-memory/mutations"} and method == "GET" or
                         re.fullmatch(r"/local/shared-memory/mutations/[^/]+", path) and method == "GET" or
                         path == "/local/shared-memory/mutations" and method == "POST")
        if not (match or run_match or review_match or collection or create or turn or workspace_index or connection or shared_memory or local_workspaces or local_filesystem or workspace_selection
                or path in {"/capabilities", "/agent/models", "/agent/ui-defaults", "/local/cache"} and method == "GET"):
            return await reject(403, "direct node access is limited to ordinary sessions and workspaces")
        if local_workspaces and local_workspaces[1]:
            approved = (await run_in_threadpool(service.workspace_roots, principal)
                        if principal else service.config.workspace_roots)
            if local_workspaces[1] not in approved:
                return await reject(403, "workspace is not approved")
        if (local_filesystem or workspace_selection) and not principal:
            return await reject(401, "authenticated device connection required")
        if collection:
            state["ordinary_sessions_only"] = True
            query = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="replace"))
            if any(key in query for key in ("creator_session_id", "project_id")):
                return await reject(403, "assistant and project queries require the hub")
            if query.get("creation_source", ["user"])[0] not in {"user", "unknown"}:
                return await reject(403, "assistant sessions require the hub")

        payload = {}
        original_receive = receive
        needs_body = create or turn or workspace_index or workspace_selection or method in {"POST", "PUT", "PATCH"} and (
            path.endswith(("/workspace", "/inputs")) or run_match and run_match[2] in {"replace", "continue"})
        if needs_body:
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 2 * 1024 * 1024:
                    return await reject(413, "request body too large")
                if not message.get("more_body"):
                    break
            try:
                payload = json.loads(body) if body else {}
                if not isinstance(payload, dict):
                    return await reject(422, "JSON object required")
            except (ValueError, UnicodeError):
                return await reject(422, "JSON object required")
            replayed = False

            async def replay_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            receive = replay_receive

        workspace_principal = principal or {"workspace_ids": list(service.config.workspace_roots)}

        def allowed_session(identity):
            if not ordinary_session_allowed(runtime, identity):
                return False
            # The normal session lookup hides soft-deleted rows. Their durable
            # workspace still limits recycle-bin listing and restore responses.
            conn = runtime.session_service._conn_factory()
            try:
                row = conn.execute("SELECT metadata FROM agent_sessions WHERE session_id=?", (identity,)).fetchone()
                metadata = json.loads(row[0]) if row else None
            except (TypeError, ValueError):
                return False
            finally:
                conn.close()
            if not isinstance(metadata, dict):
                return False
            workspace = metadata.get("workspace")
            if workspace:
                return (isinstance(workspace, dict)
                        and service.path_allowed(workspace_principal, workspace.get("backend_path", "")))
            return True

        def session_exists(identity):
            conn = runtime.session_service._conn_factory()
            try:
                return conn.execute("SELECT 1 FROM agent_sessions WHERE session_id=?", (identity,)).fetchone() is not None
            finally:
                conn.close()

        def allowed_run(identity):
            seen, first = set(), None
            while identity:
                if identity in seen or len(seen) >= 64:
                    return None
                seen.add(identity)
                current = runtime.agent_run_manager.get_run(identity)
                if (current is None or not allowed_session(current.session_id)
                        or "remote_execution" in getattr(current, "metadata", {})):
                    return None
                if first is None:
                    first = current
                identity = getattr(current, "parent_run_id", None)
            return first

        def frozen_policy_allowed(run):
            frozen = getattr(run, "metadata", {}).get("session_policy", {})
            if frozen.get("allow_default_workspace") is not False or frozen.get("safety_review_mode") != "manual":
                return False
            paths = frozen.get("allowed_paths")
            current = service.session_policy(run.session_id)
            if not isinstance(paths, list) or current is None:
                return False
            try:
                return all(Path(target).is_absolute() and any(
                    Path(target).resolve().is_relative_to(Path(root).resolve())
                    for root in current["allowed_paths"]) for target in paths)
            except (OSError, RuntimeError, ValueError, TypeError):
                return False

        if not collection and match and not await run_in_threadpool(allowed_session, match[1]):
            exists = await run_in_threadpool(session_exists, match[1])
            return await reject(403 if exists else 404,
                                "ordinary local session required" if exists else "session not found")
        if turn:
            identity = payload.get("session_id")
            if not isinstance(identity, str) or not await run_in_threadpool(allowed_session, identity):
                return await reject(403, "existing ordinary local session required")
            if principal:
                await run_in_threadpool(service.bind_session_policy, identity, principal)
        if run_match:
            run = await run_in_threadpool(allowed_run, run_match[1])
            if run is None:
                return await reject(403, "ordinary local run required")
            if payload.get("session_id") not in (None, run.session_id):
                return await reject(403, "run session mismatch")
            if principal and method == "POST":
                await run_in_threadpool(service.bind_session_policy, run.session_id, principal)
                if run_match[2] not in {"cancel", "interrupt", "replace"} and not await run_in_threadpool(frozen_policy_allowed, run):
                    return await reject(403, "run predates or exceeds the direct connection policy; start a new turn")
            if run_match[2] and run_match[2].startswith("children/"):
                parts = run_match[2].split("/")
                child = await run_in_threadpool(allowed_run, parts[1])
                if child is None:
                    return await reject(403, "ordinary local child run required")
                if principal and method == "POST":
                    await run_in_threadpool(service.bind_session_policy, child.session_id, principal)
                    if not await run_in_threadpool(frozen_policy_allowed, child):
                        return await reject(403, "child run exceeds the direct connection policy")
        if review_match:
            review = await run_in_threadpool(runtime.agent_run_manager.get_safety_review, review_match[1])
            run = await run_in_threadpool(allowed_run, review.run_id) if review else None
            if run is None:
                return await reject(403, "ordinary local safety review required")
            if principal and method == "POST":
                await run_in_threadpool(service.bind_session_policy, run.session_id, principal)
                if not await run_in_threadpool(frozen_policy_allowed, run):
                    return await reject(403, "review run exceeds the direct connection policy")
        if principal and match and match[2] and match[2].split("/")[0] == "inputs" and method == "POST":
            await run_in_threadpool(service.bind_session_policy, match[1], principal)
        if create:
            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict) or "remote_execution" in metadata:
                return await reject(403, "cloud execution sessions require the hub")
            if payload.get("project_id") or metadata.get("project_id"):
                return await reject(403, "project association requires the hub")
            workspace = metadata.get("workspace")
            if workspace and (not isinstance(workspace, dict) or not await run_in_threadpool(
                    service.path_allowed, workspace_principal, workspace.get("backend_path", ""))):
                return await reject(403, "workspace is not approved")
        if workspace_index or path.endswith("/workspace") and method == "PUT":
            target = payload.get("workspace") if workspace_index else payload.get("path")
            if not await run_in_threadpool(service.path_allowed, workspace_principal, target):
                return await reject(403, "workspace is not approved")
        if principal and needs_body and (turn or path.endswith("/inputs")
                                        or run_match and run_match[2] == "replace"):
            payload["safety_review_mode"] = "manual"
            body = bytearray(json.dumps(payload, ensure_ascii=False).encode())
            scope["headers"] = [(key, value) for key, value in scope.get("headers", [])
                                if key.lower() != b"content-length"] + [
                                    (b"content-length", str(len(body)).encode())]
        if principal:
            state["device_connection"] = {key: value for key, value in principal.items() if key != "csrf_hash"}

        start, buffered = None, bytearray()

        async def checked_send(message):
            nonlocal start
            if message["type"] == "http.response.start":
                updated = dict(message)
                updated["headers"] = list(message.get("headers", [])) + [(b"cache-control", b"no-store")]
                if (collection or create) and 200 <= message["status"] < 300:
                    start = updated
                    return
                message = updated
            if (collection or create) and start is not None and message["type"] == "http.response.body":
                buffered.extend(message.get("body", b""))
                if message.get("more_body"):
                    return
                data = json.loads(buffered)
                if collection:
                    visible = []
                    for session in data.get("sessions", []):
                        if await run_in_threadpool(allowed_session, session.get("session_id", "")):
                            visible.append(session)
                    data["sessions"] = visible
                elif principal:
                    identity = data.get("session", {}).get("session_id")
                    if identity:
                        await run_in_threadpool(service.bind_session_policy, identity, principal)
                raw = json.dumps(data, ensure_ascii=False).encode()
                start["headers"] = [(key, value) for key, value in start["headers"] if key.lower() != b"content-length"]
                start["headers"].append((b"content-length", str(len(raw)).encode()))
                await send(start)
                return await send({"type": "http.response.body", "body": raw})
            # Authentication on every SSE frame bounds an expired/revoked local
            # connection without asking the hub to stay online.
            if (principal and message["type"] == "http.response.body" and message.get("more_body")
                    and not await run_in_threadpool(service.authenticate, token)):
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                raise ConnectionExpired()
            await send(message)

        try:
            await self.app(scope, receive, checked_send)
        except BaseExceptionGroup as exc:
            remaining = exc.subgroup(lambda error: not isinstance(error, (ConnectionExpired, BaseExceptionGroup)))
            if remaining is not None:
                raise remaining
        except ConnectionExpired:
            pass


class ConnectionExpired(Exception):
    """Stop production after an expired direct client stream is closed."""
