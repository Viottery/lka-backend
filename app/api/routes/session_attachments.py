"""Loopback-only upload and retrieval of validated session attachments."""

from functools import partial
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.routes.session_files import _require_loopback
from app.domains.session_attachments import SessionAttachmentError, attachment_type

router = APIRouter(prefix="/sessions/{session_id}/attachments", tags=["session attachments"])


async def _call(operation, **kwargs):
    try:
        return await run_in_threadpool(partial(operation, **kwargs))
    except KeyError as exc:
        raise HTTPException(404, "Active session or attachment not found.") from exc
    except SessionAttachmentError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(422, "Attachment storage is unavailable.") from exc


@router.post("")
async def upload_session_attachment(session_id: str, request: Request):
    _require_loopback(request)
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        filename = unquote(request.headers.get("x-filename", "image"), errors="strict")
    except UnicodeError as exc:
        raise HTTPException(422, "Invalid encoded attachment filename.") from exc
    try:
        _kind, _canonical_mime, limit = attachment_type(filename, media_type)
    except SessionAttachmentError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise HTTPException(413, f"Attachment exceeds {limit // (1024 * 1024)} MiB.")
        raw.extend(chunk)
    service = request.app.state.runtime.session_attachment_service
    return await _call(
        service.upload,
        session_id=session_id,
        filename=filename,
        media_type=media_type,
        raw=bytes(raw),
    )


@router.get("/{attachment_id}/raw")
async def read_session_attachment(session_id: str, attachment_id: str, request: Request):
    _require_loopback(request)
    metadata, raw = await _call(
        request.app.state.runtime.session_attachment_service.read_file,
        session_id=session_id,
        attachment_id=attachment_id,
    )
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if metadata.get("kind") == "document":
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(metadata["filename"], safe="")
    return Response(
        raw,
        media_type=metadata["media_type"],
        headers=headers,
    )


@router.get("/{attachment_id}")
async def get_session_attachment(session_id: str, attachment_id: str, request: Request):
    _require_loopback(request)
    return await _call(
        request.app.state.runtime.session_attachment_service.get,
        session_id=session_id,
        attachment_id=attachment_id,
    )
