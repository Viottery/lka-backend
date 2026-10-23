"""Remote shadow runs are viewed on the hub but controlled by their task protocol."""
from __future__ import annotations

import json
import re

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse


class RemoteSessionBoundaryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        runtime = scope["app"].state.runtime
        node = getattr(runtime, "remote_execution_worker", None) is not None
        if not node and (getattr(runtime, "remote_execution_service", None) is None
                         or scope.get("method") in {"GET", "HEAD", "OPTIONS"}):
            return await self.app(scope, receive, send)
        path = scope.get("path", "").rstrip("/")
        session = re.fullmatch(r"/sessions/([^/]+)(?:/.*)?", path)
        run = re.fullmatch(r"/agent/runs/([^/]+)(?:/.*)?", path)
        turn = path in {"/agent/turn", "/agent/turn/stream"}
        review = re.fullmatch(r"/agent/safety-reviews/([^/]+)(?:/decision)?", path)
        session_id = session[1] if session else None
        record = None
        if run:
            record = await run_in_threadpool(runtime.agent_run_manager.get_run, run[1])
            session_id = record.session_id if record else None
        if review:
            item = await run_in_threadpool(runtime.agent_run_manager.get_safety_review, review[1])
            if item:
                record = await run_in_threadpool(runtime.agent_run_manager.get_run, item.run_id)
                session_id = record.session_id if record else None
        if turn:
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 2 * 1024 * 1024:
                    return await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
                if not message.get("more_body"):
                    break
            try:
                payload = json.loads(body)
                session_id = payload.get("session_id") if isinstance(payload, dict) else None
            except (ValueError, UnicodeError):
                pass  # Preserve the route's normal validation response.
            original_receive, replayed = receive, False

            async def replay():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            receive = replay
        if session_id:
            detail = await run_in_threadpool(runtime.session_service.get_session_or_none,
                                            session_id=session_id)
            if ((record is not None and record.metadata.get("remote_execution"))
                    or detail is not None and detail.metadata.get("remote_execution")):
                return await JSONResponse(
                    {"detail": "remote_execution_requires_task_control"}, status_code=403 if node else 409,
                )(scope, receive, send)
        return await self.app(scope, receive, send)
