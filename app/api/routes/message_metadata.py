"""Authenticated local message metadata and bounded context endpoints."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.routes.message_reading import _reader
from app.api.routes.messages import _cache_control, _read_bounded_body, _require_import, _runtime
from app.domains.message_metadata import MessageMetadataRevisionConflict

router = APIRouter(tags=["message-metadata"], dependencies=[Depends(_cache_control)])


async def _object(request: Request, max_bytes: int = 65536) -> dict[str, Any]:
    try:
        value = json.loads(await _read_bounded_body(request, max_bytes))
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=422, detail="Invalid message metadata request.") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="Expected an object.")
    return value


async def _call(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Message conversation unavailable.") from None
    except MessageMetadataRevisionConflict:
        raise HTTPException(status_code=409, detail="Conversation metadata revision conflict.") from None
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid message metadata request.") from None


@router.get("/messages/conversations/resolve")
async def resolve(request: Request, query: str = Query(..., min_length=1, max_length=256)):
    _reader(request)
    return await _call(_runtime(request).message_history.resolve_conversations, query)


@router.get("/messages/conversations/{conversation_key}/metadata")
async def get_metadata(conversation_key: str, request: Request):
    _reader(request)
    return await _call(_runtime(request).message_history.get_conversation_metadata, conversation_key)


@router.patch("/messages/conversations/{conversation_key}/metadata")
async def update_metadata(conversation_key: str, request: Request):
    # Manual labels require the separate paired-human credential.
    from app.api.routes.message_reading import _human
    _human(request)
    payload = await _object(request)
    allowed = {"expected_revision", "user_alias", "display_name"}
    if set(payload) - allowed or "expected_revision" not in payload or type(payload["expected_revision"]) is not int:
        raise HTTPException(status_code=422, detail="Invalid conversation metadata fields.")
    return await _call(_runtime(request).message_history.update_conversation_metadata,
                       conversation_key, **payload)


@router.post("/integrations/messages/conversations/metadata")
async def import_metadata(request: Request):
    _require_import(request)
    payload = await _object(request, 131072)
    if set(payload) != {"metadata"} or not isinstance(payload["metadata"], list) or len(payload["metadata"]) > 100:
        raise HTTPException(status_code=422, detail="Invalid metadata import batch.")
    return await _call(_runtime(request).message_history.import_conversation_metadata, payload["metadata"])


@router.get("/messages/records/{message_id}")
async def get_message(message_id: str, request: Request):
    _reader(request)
    return await _call(_runtime(request).message_history.get_message, message_id)


@router.get("/messages/records/{message_id}/context")
async def get_context(message_id: str, request: Request, before: int = Query(10, ge=0, le=25),
                      after: int = Query(10, ge=0, le=25)):
    _reader(request)
    return await _call(_runtime(request).message_history.get_message_context,
                       message_id, before=before, after=after)
