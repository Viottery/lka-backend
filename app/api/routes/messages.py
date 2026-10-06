"""Local message history controls, reads, and credential-separated imports."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from app.domains.message_attachments import SAFE_MEDIA_TYPES
from app.domains.message_history import PolicyRevisionConflict
from app.platform.safe_files import open_file_no_follow


def _cache_control(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(tags=["messages"], dependencies=[Depends(_cache_control)])


def _runtime(request: Request) -> Any:
    runtime = request.app.state.runtime
    local_config = getattr(runtime, "local_app_config", None)
    message_config = getattr(local_config, "message_history", None)
    if message_config is not None and not getattr(message_config, "enabled", True):
        raise HTTPException(status_code=503, detail="Message history is disabled.")
    return runtime


def _host_is_loopback(request: Request) -> bool:
    client = request.client
    try:
        peer = ipaddress.ip_address(client.host if client else "")
        host = urlsplit("//" + request.headers.get("host", "")).hostname or ""
        host = host.lower()
        host_is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
    return peer.is_loopback and host_is_loopback


def _valid_token(request: Request, env_name: str) -> bool:
    expected = os.getenv(env_name, "")
    if not expected:
        return False
    supplied = request.headers.get("x-lka-messages-token", "")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _require_ui(request: Request) -> None:
    if not _host_is_loopback(request):
        raise HTTPException(status_code=403, detail="Local message API unavailable.")
    if os.getenv("LKA_MESSAGES_API_TOKEN") and not (
        _valid_token(request, "LKA_MESSAGES_API_TOKEN") or _valid_token(request, "LKA_MESSAGES_CONTROL_TOKEN")
    ):
        raise HTTPException(status_code=401, detail="Message API authentication required.")
    origin = request.headers.get("origin")
    configured = set(request.app.state.runtime.settings.parsed_cors_origins())
    if origin:
        request_origin = f"{request.url.scheme}://{request.headers.get('host', '')}".rstrip("/")
        if origin.rstrip("/") not in configured and origin.rstrip("/") != request_origin:
            raise HTTPException(status_code=403, detail="Local message API unavailable.")


def _require_import(request: Request) -> None:
    if not _host_is_loopback(request) or request.headers.get("origin"):
        raise HTTPException(status_code=403, detail="Message import unavailable.")
    if not _valid_token(request, "LKA_MESSAGES_IMPORT_TOKEN"):
        raise HTTPException(status_code=401, detail="Message import authentication required.")


def _failure() -> HTTPException:
    return HTTPException(status_code=400, detail="Invalid message request.")


def _read_error() -> HTTPException:
    return HTTPException(status_code=404, detail="Message conversation unavailable.")


async def _read_bounded_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is None:
        raise HTTPException(status_code=411, detail="Content-Length is required for message requests.")
    declared_length: int | None = None
    try:
        declared_length = int(content_length)
        if declared_length < 0:
            raise _failure()
        if declared_length > maximum:
            raise HTTPException(status_code=413, detail="Message request too large.")
    except ValueError:
        raise _failure() from None
    if declared_length == 0:
        return b""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(status_code=413, detail="Message request too large.")
        chunks.append(chunk)
        if declared_length is not None and size >= declared_length:
            break
    return b"".join(chunks)


@router.get("/messages/policies")
def get_policies(request: Request) -> dict[str, Any]:
    _require_ui(request)
    return {"policies": _runtime(request).message_history.list_policies()}


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        raw = await _read_bounded_body(request, 65536)
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _failure() from None
    if not isinstance(payload, dict):
        raise _failure()
    return payload


@router.put("/messages/policies")
async def put_policy(request: Request) -> dict[str, Any]:
    _require_ui(request)
    try:
        payload = await _json_object(request)
        result = await asyncio.to_thread(_runtime(request).message_history.set_policy, payload)
        return {"policy": result}
    except PolicyRevisionConflict:
        raise HTTPException(status_code=409, detail="Message policy revision conflict.") from None
    except HTTPException:
        raise
    except (KeyError, TypeError, ValueError):
        raise _failure() from None


@router.get("/messages/conversations")
def conversations(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    _require_ui(request)
    return _runtime(request).message_history.list_conversations(limit=limit, offset=offset)


@router.get("/messages/recent")
def recent(request: Request, conversation_key: str | None = None, since: str | None = None,
           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    _require_ui(request)
    try:
        return _runtime(request).message_history.recent(conversation_key, since=int(since) if since else None,
                                                         limit=limit, offset=offset)
    except PermissionError:
        raise _read_error() from None
    except (ValueError, TypeError):
        raise _failure() from None


@router.get("/messages/search")
def search(request: Request, query: str = Query(..., min_length=1, max_length=256),
           conversation_key: str | None = None, sender_id: str | None = None,
           sender: str | None = Query(None, min_length=1, max_length=256),
           since: int | None = Query(None, ge=0), until: int | None = Query(None, ge=0),
           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    _require_ui(request)
    try:
        return _runtime(request).message_history.search(query, conversation_key=conversation_key,
                                                         sender_id=sender_id, sender=sender, since=since, until=until,
                                                         limit=limit, offset=offset)
    except PermissionError:
        raise _read_error() from None
    except (ValueError, TypeError):
        raise _failure() from None


@router.get("/messages/conversations/{conversation_key}/history")
def history(request: Request, conversation_key: str, before_seq: int | None = Query(None, ge=1),
            limit: int = Query(100, ge=1, le=200)) -> dict[str, Any]:
    _require_ui(request)
    try:
        return _runtime(request).message_history.history(conversation_key, before_seq=before_seq, limit=limit)
    except PermissionError:
        raise _read_error() from None
    except ValueError:
        raise _failure() from None


@router.get("/messages/conversations/{conversation_key}/coverage")
def coverage(request: Request, conversation_key: str) -> dict[str, Any]:
    _require_ui(request)
    try:
        return {"coverage": _runtime(request).message_history.coverage(conversation_key)}
    except PermissionError:
        raise _read_error() from None


@router.get("/messages/conversations/{conversation_key}/summary")
def summary(request: Request, conversation_key: str) -> dict[str, Any]:
    _require_ui(request)
    try:
        value = _runtime(request).message_history.summary(conversation_key)
        if value is None:
            raise _read_error()
        return {"summary": value}
    except PermissionError:
        raise _read_error() from None


@router.get("/messages/conversations/{conversation_key}/facts")
def facts(request: Request, conversation_key: str, limit: int = Query(50, ge=1, le=200),
          offset: int = Query(0, ge=0)) -> dict[str, Any]:
    _require_ui(request)
    try:
        return _runtime(request).message_history.facts(conversation_key=conversation_key, limit=limit, offset=offset)
    except PermissionError:
        raise _read_error() from None
    except ValueError:
        raise _failure() from None


@router.post("/messages/conversations/{conversation_key}/analyze")
def analyze(request: Request, conversation_key: str) -> dict[str, Any]:
    _require_ui(request)
    runtime = _runtime(request)
    policies = runtime.message_history.list_policies()
    policy = next((item for item in policies if item["conversation_key"] == conversation_key), None)
    if policy is None or not policy["record_enabled"]:
        raise _read_error()
    if not policy["analysis_enabled"]:
        raise HTTPException(status_code=409, detail="Message analysis is disabled for this conversation.")
    message_config = getattr(getattr(runtime, "local_app_config", None), "message_history", None)
    if message_config is not None and not getattr(message_config, "background_enabled", True):
        raise HTTPException(status_code=503, detail="Message analysis is disabled globally.")
    if runtime.message_history.reading_status()["paused"]:
        return {"jobs": [], "status": "paused"}
    try:
        jobs = runtime.message_history.schedule_pending(conversation_key, force=True)
    except (AttributeError, ValueError, KeyError):
        raise _failure() from None
    if not jobs:
        return {"jobs": [], "status": "nothing_pending"}
    pending = any(item.get("status") in {"queued", "running", "retry_wait"} for item in jobs)
    return {"jobs": jobs, "status": "queued" if pending else "blocked"}


@router.post("/messages/conversations/{conversation_key}/retry")
async def retry(request: Request, conversation_key: str) -> dict[str, Any]:
    _require_ui(request)
    runtime = _runtime(request)
    policies = await asyncio.to_thread(runtime.message_history.list_policies)
    policy = next((item for item in policies if item["conversation_key"] == conversation_key), None)
    if policy is None or not policy["record_enabled"]:
        raise _read_error()
    if not policy["analysis_enabled"]:
        raise HTTPException(status_code=409, detail="Message analysis is disabled for this conversation.")
    message_config = getattr(getattr(runtime, "local_app_config", None), "message_history", None)
    if message_config is not None and not getattr(message_config, "background_enabled", True):
        raise HTTPException(status_code=503, detail="Message analysis is disabled globally.")
    payload = await _json_object(request)
    expected = payload.get("expected_updated_at")
    allow_restart = payload.get("allow_checkpoint_restart", False)
    if (not isinstance(expected, str) or type(allow_restart) is not bool
            or set(payload) - {"expected_updated_at", "allow_checkpoint_restart"}):
        raise _failure()
    if allow_restart:
        from app.api.routes.message_reading import _human
        _human(request)
    try:
        result = await asyncio.to_thread(
            runtime.message_history.retry_analysis,
            conversation_key, expected_updated_at=expected, allow_checkpoint_restart=allow_restart,
        )
    except (AttributeError, ValueError, KeyError):
        raise _failure() from None
    status = result.get("status")
    if status == "retried":
        return {"status": "retried", "job": result.get("job")}
    if status == "not_allowed":
        raise _read_error()
    if status in {"missing", "conflict", "expired", "unsupported"}:
        raise HTTPException(status_code=409, detail="Message analysis retry is no longer available.")
    raise HTTPException(status_code=409, detail="Message analysis retry could not be applied.")


@router.post("/messages/conversations/{conversation_key}/replay")
async def replay(request: Request, conversation_key: str) -> dict[str, Any]:
    _require_ui(request)
    runtime = _runtime(request)
    config = runtime.message_analysis.config
    if not config.enabled or not config.background_enabled:
        raise HTTPException(status_code=503, detail="Message analysis is disabled globally.")
    payload = await _json_object(request)
    if set(payload) != {"expected_revision"} or type(payload["expected_revision"]) is not int or payload["expected_revision"] < 1:
        raise HTTPException(status_code=422, detail="Invalid replay request.")
    try:
        return await asyncio.to_thread(runtime.message_history.request_reading_replay,
                                      conversation_key, payload["expected_revision"])
    except PermissionError:
        raise _read_error() from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Message replay conflicts with current policy.") from None


@router.get("/background/services/message-reading")
def reading_service_status(request: Request) -> dict[str, Any]:
    _require_ui(request)
    runtime = _runtime(request)
    state = runtime.message_history.reading_status()
    coordinator = runtime.message_analysis
    state["enabled"] = coordinator.config.enabled
    state["background_enabled"] = coordinator.config.background_enabled
    state["budget"] = coordinator.controller.quota_usage("message_reading")
    state["pricing_known"] = coordinator._pricing() is not None
    state["limits"] = {key: value for key, value in coordinator.config.model_dump(mode="json").items()
                       if key.endswith("_limit") or key in {"max_job_tokens", "max_job_calls", "max_input_tokens", "max_recovery_restarts", "fragment_recovery_enabled"}}
    for schedule in state["schedules"]:
        if schedule["active_work_id"]:
            schedule["budget"] = coordinator.controller.quota_usage(schedule["active_work_id"])
    with runtime.message_history._connection() as conn:
        for schedule in state["schedules"]:
            policy = conn.execute("SELECT * FROM message_history_policies WHERE conversation_key=?",
                                  (schedule["conversation_key"],)).fetchone()
            schedule["analysis_job"] = runtime.message_history._latest_analysis_job(conn, schedule["conversation_key"], policy)
            family = conn.execute("SELECT * FROM message_reading_families WHERE family_id=?",
                                  (schedule["active_work_id"],)).fetchone()
            schedule["work"] = dict(family) if family else None
            schedule["blocked"] = bool(schedule["analysis_job"] and schedule["analysis_job"]["status"] in {"failed", "cancelled"}
                                       and schedule["pending_messages"])
            schedule["reason"] = ("globally_disabled" if not state["enabled"] or not state["background_enabled"] else
                                  "paused" if state["paused"] else
                                  "analysis_disabled" if not policy["analysis_enabled"] or not policy["record_enabled"] else
                                  "manual_only" if not policy["auto_analyze"] and family is None else
                                  schedule["analysis_job"]["error_class"] or schedule["analysis_job"]["status"]
                                  if schedule["analysis_job"] else
                                  "awaiting_due" if schedule["pending_since"] else "idle")
    return state


async def _control_reading(request: Request, paused: bool) -> dict[str, Any]:
    _require_ui(request)
    payload = await _json_object(request)
    if set(payload) != {"expected_revision"} or type(payload["expected_revision"]) is not int or payload["expected_revision"] < 1:
        raise HTTPException(status_code=422, detail="Invalid service control.")
    try:
        return await asyncio.to_thread(_runtime(request).message_history.set_reading_paused,
                                       paused, payload["expected_revision"])
    except ValueError:
        raise HTTPException(status_code=409, detail="Service revision conflict.") from None


@router.post("/background/services/message-reading/pause")
async def pause_reading(request: Request) -> dict[str, Any]:
    return await _control_reading(request, True)


@router.post("/background/services/message-reading/resume")
async def resume_reading(request: Request) -> dict[str, Any]:
    return await _control_reading(request, False)


@router.post("/messages/analysis-work/{family_id}/limits")
async def increase_reading_work_limits(request: Request, family_id: str) -> dict[str, Any]:
    _require_ui(request)
    service = _runtime(request).message_history
    def permitted():
        with service._connection() as conn:
            return conn.execute("SELECT f.family_id FROM message_reading_families f JOIN message_history_policies p "
                                "ON p.conversation_key=f.conversation_key WHERE f.family_id=? AND p.record_enabled=1",
                                (family_id,)).fetchone() is not None
    if not await asyncio.to_thread(permitted):
        raise _read_error()
    payload = await _json_object(request)
    if (set(payload) != {"expected_revision", "max_tokens", "max_calls"}
            or any(type(value) is not int for value in payload.values())
            or not 1 <= payload["max_tokens"] <= 500_000 or not 1 <= payload["max_calls"] <= 1000
            or payload["expected_revision"] < 1):
        raise HTTPException(status_code=422, detail="Invalid work limits.")
    try:
        return {"work": await asyncio.to_thread(service.raise_work_limits, family_id, **payload)}
    except PermissionError:
        raise _read_error() from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Work limits revision conflict or limits decreased.") from None


@router.get("/integrations/messages/policies")
def integration_policies(request: Request) -> dict[str, Any]:
    _require_import(request)
    # Collector needs capture grants, not analysis preferences or future profiles.
    fields = ("platform", "account_id", "conversation_type", "conversation_id", "record_enabled",
              "media_enabled", "revision", "capture_epoch", "minimum_import_version")
    return {"policies": [{key: policy[key] for key in fields if key in policy}
                         for policy in _runtime(request).message_history.list_policies()]}


MAX_IMPORT_BYTES = 8_000_000
MAX_IMPORT_MESSAGES = 100


class _MediaFileResponse(FileResponse):
    """Stream a safely opened descriptor, never reopen an unchecked file path."""

    def __init__(self, *args, service: Any, attachment_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.service = service
        self.attachment_id = attachment_id

    async def __call__(self, scope, receive, send):
        descriptor = None
        try:
            item = await asyncio.to_thread(self.service.attachment, self.attachment_id)
            if item["state"] != "cached":
                raise PermissionError("uncached")
            descriptor = await asyncio.to_thread(open_file_no_follow, Path(self.path))
            metadata = os.fstat(descriptor)
            if metadata.st_size != item["size_bytes"]:
                raise PermissionError("size_mismatch")
        except (PermissionError, OSError):
            if descriptor is not None:
                os.close(descriptor)
            await Response(status_code=404, headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            if scope["method"] == "HEAD":
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                while True:
                    chunk = await asyncio.to_thread(os.read, descriptor, self.chunk_size)
                    await send({"type": "http.response.body", "body": chunk, "more_body": bool(chunk)})
                    if not chunk:
                        break
        finally:
            os.close(descriptor)
        if self.background is not None:
            await self.background()


@router.get("/messages/attachments")
def attachments(request: Request, conversation_key: str | None = None, kind: str | None = None,
                query: str | None = Query(None, min_length=1, max_length=256),
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    _require_ui(request)
    try:
        return _runtime(request).message_history.attachments(conversation_key, kind=kind, query=query,
                                                            limit=limit, offset=offset)
    except PermissionError:
        raise _read_error() from None
    except ValueError:
        raise _failure() from None


@router.get("/messages/attachments/{attachment_id}")
def attachment(request: Request, attachment_id: str) -> dict[str, Any]:
    _require_ui(request)
    try:
        return {"attachment": _runtime(request).message_history.attachment(attachment_id)}
    except PermissionError:
        raise _read_error() from None


@router.get("/messages/attachments/{attachment_id}/content")
def attachment_content(request: Request, attachment_id: str) -> FileResponse:
    _require_ui(request)
    runtime = _runtime(request)
    try:
        item = runtime.message_history.attachment(attachment_id)
        if item["state"] != "cached" or item["mime_type"] not in SAFE_MEDIA_TYPES[item["kind"]]:
            raise PermissionError("uncached")
        configured = getattr(runtime.settings, "messages_media_cache_dir", "")
        if not configured:
            raise PermissionError("unconfigured")
        # IDs are generated from a SHA256 cache key; never accept importer paths.
        key = item["attachment_id"].removeprefix("message_attachment_")
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise PermissionError("invalid_key")
        path = Path(configured).absolute() / (key + ".bin")
        descriptor = open_file_no_follow(path)
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size != item["size_bytes"]:
                raise PermissionError("size_mismatch")
        finally:
            os.close(descriptor)
        return _MediaFileResponse(path, service=runtime.message_history, attachment_id=attachment_id,
                            media_type=item["mime_type"], filename=key + ".bin",
                            stat_result=metadata, content_disposition_type="attachment",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except (PermissionError, OSError):
        raise _read_error() from None


@router.post("/integrations/messages/media")
async def import_media(request: Request) -> dict[str, Any]:
    _require_import(request)
    payload = await _import_payload(request)
    if (set(payload) != {"schema_version", "media"} or type(payload.get("schema_version")) is not int
            or payload["schema_version"] not in (1, 2) or not isinstance(payload.get("media"), list)
            or len(payload["media"]) > MAX_IMPORT_MESSAGES):
        raise _failure()
    try:
        return await asyncio.to_thread(
            _runtime(request).message_history.update_media, payload["media"],
            **({"schema_version": 2} if payload["schema_version"] == 2 else {}),
        )
    except (TypeError, ValueError):
        raise _failure() from None


async def _import_payload(request: Request) -> dict[str, Any]:
    try:
        raw = await _read_bounded_body(request, MAX_IMPORT_BYTES)
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _failure() from None
    if not isinstance(payload, dict):
        raise _failure()
    return payload


@router.post("/integrations/messages/import")
async def import_messages(request: Request) -> dict[str, Any]:
    _require_import(request)
    payload = await _import_payload(request)
    messages = payload.get("messages")
    if (set(payload) != {"schema_version", "messages"} or type(payload.get("schema_version")) is not int
            or payload["schema_version"] not in (1, 2) or not isinstance(messages, list)
            or len(messages) > MAX_IMPORT_MESSAGES):
        raise _failure()
    try:
        return await asyncio.to_thread(
            _runtime(request).message_history.import_messages, messages,
            **({"schema_version": 2} if payload["schema_version"] == 2 else {}),
        )
    except (TypeError, ValueError):
        raise _failure() from None


@router.post("/integrations/qq/messages/import")
async def import_qq_messages(request: Request) -> dict[str, Any]:
    _require_import(request)
    payload = await _import_payload(request)
    if (set(payload) != {"schema_version", "messages"}
            or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or not isinstance(payload.get("messages"), list)
            or len(payload["messages"]) > MAX_IMPORT_MESSAGES):
        raise _failure()
    required = {"self_id", "message_id", "conversation_type", "conversation_id", "sender_id",
                "display_name", "text", "sent_at", "received_at"}
    mapped: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for row in payload["messages"]:
        row_valid = isinstance(row, dict) and required.issubset(row) and not (
            set(row) - required - {"content_kind", "schema_version", "attachments"}
        )
        if row_valid and "schema_version" in row:
            row_valid = type(row["schema_version"]) is int and row["schema_version"] == 1
        if row_valid:
            row_valid = all(
                isinstance(row[key], str | int) and not isinstance(row[key], bool)
                for key in ("self_id", "message_id")
            ) and all(str(row[key]).strip() for key in ("self_id", "message_id"))
        if not row_valid:
            identity = row if isinstance(row, dict) else {}
            invalid.append({
                "identity": {"self_id": identity.get("self_id"), "message_id": identity.get("message_id")},
                "reason": "invalid_message", "permanent": True,
            })
            continue
        mapped.append({
            "platform": "qq", "account_id": str(row["self_id"]),
            "message_id": str(row["message_id"]), "conversation_type": row["conversation_type"],
            "conversation_id": str(row["conversation_id"]), "sender_id": str(row["sender_id"]),
            "sender_name": row["display_name"], "text": row["text"],
            "sent_at": row["sent_at"], "received_at": row["received_at"],
            "content_kind": row.get("content_kind", "text"),
            **({"attachments": row["attachments"]} if "attachments" in row else {}),
        })
    try:
        result = await asyncio.to_thread(_runtime(request).message_history.import_messages, mapped)
    except (TypeError, ValueError):
        raise _failure() from None
    ack = result.get("acknowledged", [])
    reject = result.get("rejected", [])
    # Compatibility collector consumes self_id/message_id in acknowledgement rows.
    rejected = [{
        "self_id": (item.get("identity") or {}).get("account_id", item.get("account_id")),
        "message_id": (item.get("identity") or {}).get("message_id", item.get("message_id")),
        "reason": item.get("reason"), "permanent": item.get("permanent", True),
    } for item in reject]
    rejected.extend({
        "self_id": (item.get("identity") or {}).get("self_id"),
        "message_id": (item.get("identity") or {}).get("message_id"),
        "reason": item["reason"], "permanent": item["permanent"],
    } for item in invalid)
    return {
        "acknowledged": [{"self_id": item.get("account_id"), "message_id": item.get("message_id")}
                         for item in ack],
        "rejected": rejected,
    }
