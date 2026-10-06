"""Local reading views and authenticated, explicit human decisions."""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.routes.messages import (
    _cache_control,
    _read_bounded_body,
    _require_ui,
    _runtime,
    _valid_token,
)

router = APIRouter(tags=["message-reading"], dependencies=[Depends(_cache_control)])


def _human(request: Request):
    _require_ui(request)
    # Never treat loopback, an importer credential, or safety-review approval
    # as a human identity. The paired UI alone holds this dedicated credential.
    if not _valid_token(request, "LKA_MESSAGES_CONTROL_TOKEN"):
        raise HTTPException(status_code=401, detail="Paired human control authentication required.")
    from app.domains.message_matter_proposals import HumanControlPrincipal
    principal_id = "paired-ui:" + hashlib.sha256(os.environ["LKA_MESSAGES_CONTROL_TOKEN"].encode()).hexdigest()[:24]
    return HumanControlPrincipal(principal_id=principal_id)


def _reader(request: Request) -> None:
    _require_ui(request)
    if not (_valid_token(request, "LKA_MESSAGES_API_TOKEN") or _valid_token(request, "LKA_MESSAGES_CONTROL_TOKEN")):
        raise HTTPException(status_code=401, detail="Paired message reading authentication required.")


async def _body(request: Request) -> dict[str, Any]:
    import json
    try:
        value = json.loads(await _read_bounded_body(request, 65536))
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=422, detail="Invalid control payload.") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="Expected an object.")
    return value


async def _call(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Reading source unavailable.") from None
    except (ValueError, TypeError) as exc:
        # Validation failures never echo untrusted text, reviewed fields or secrets.
        status = 409 if "conflict" in type(exc).__name__.lower() else 422
        raise HTTPException(status_code=status, detail="Reading request conflicts with current state." if status == 409 else "Invalid reading request.") from None


def _service(request):
    _reader(request)
    return _runtime(request).message_history


@router.get("/messages/reading/overview")
async def overview(request: Request, conversation_key: str | None = None):
    return await _call(_service(request).reading_overview, conversation_key=conversation_key)


@router.get("/messages/reading/topics")
async def topics(request: Request, conversation_key: str | None = None,
                 limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                 since: str | None = None, until: str | None = None):
    return await _call(_service(request).list_topics, conversation_key=conversation_key, limit=limit, cursor=cursor, since=since, until=until)


@router.get("/messages/reading/topics/{topic_id}")
async def topic(topic_id: str, request: Request):
    return await _call(_service(request).get_topic, topic_id)


@router.get("/messages/reading/topics/{topic_id}/sources")
async def topic_sources(topic_id: str, request: Request, limit: int = Query(50, ge=1, le=50), cursor: str | None = None):
    return await _call(_service(request).topic_sources, topic_id, limit=limit, cursor=cursor)


@router.get("/messages/reading/insights")
async def insights(request: Request, conversation_key: str | None = None, importance: str | None = None,
                   unseen: bool = False, limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                   kind: str | None = None, since: str | None = None, until: str | None = None):
    return await _call(_service(request).list_insights, conversation_key=conversation_key,
                       importance=importance, unseen=unseen, limit=limit, cursor=cursor, kind=kind, since=since, until=until)


@router.get("/messages/reading/insights/{insight_id}")
async def insight(insight_id: str, request: Request):
    return await _call(_service(request).get_insight, insight_id)


@router.get("/messages/reading/insights/{insight_id}/sources")
async def insight_sources(insight_id: str, request: Request, limit: int = Query(50, ge=1, le=50), cursor: str | None = None):
    return await _call(_service(request).derived_sources, "insight", insight_id, limit=limit, cursor=cursor)


@router.post("/messages/reading/insights/{insight_id}/attention")
async def attention(insight_id: str, request: Request):
    _human(request)
    payload = await _body(request)
    if "expected_revision" not in payload or set(payload) - {"expected_revision", "viewed_revision", "dismissed_revision", "snoozed_until"}:
        raise HTTPException(status_code=422, detail="Invalid attention fields.")
    return await _call(_service(request).set_attention, insight_id, **payload)


@router.get("/messages/reading/profile")
async def profile(request: Request, scope: str = "global"):
    return await _call(_service(request).get_reading_profile, scope=scope)


@router.get("/messages/reading/participants")
async def participants(request: Request, conversation_key: str | None = None,
                       limit: int = Query(30, ge=1, le=100), cursor: str | None = None):
    return await _call(_service(request).list_participants,
                       conversation_key=conversation_key, limit=limit, cursor=cursor)


@router.get("/messages/reading/participants/{conversation_key}/{sender_id}")
async def participant(conversation_key: str, sender_id: str, request: Request):
    return await _call(_service(request).get_participant, conversation_key, sender_id)


@router.get("/messages/reading/participants/{conversation_key}/{sender_id}/sources")
async def participant_sources(conversation_key: str, sender_id: str, request: Request,
                              limit: int = Query(30, ge=1, le=50), cursor: str | None = None):
    return await _call(_service(request).participant_sources, conversation_key, sender_id,
                       limit=limit, cursor=cursor)


@router.post("/messages/reading/participants/{conversation_key}/{sender_id}/control")
async def participant_control(conversation_key: str, sender_id: str, request: Request):
    _human(request)
    payload = await _body(request)
    if not {"expected_revision", "action"}.issubset(payload) or set(payload) - {
        "expected_revision", "action", "summary"
    }:
        raise HTTPException(status_code=422, detail="Invalid participant control fields.")
    return await _call(_service(request).update_participant, conversation_key, sender_id, **payload)


@router.get("/messages/reading/dossiers")
async def dossiers(request: Request, conversation_key: str | None = None,
                   limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await _call(_service(request).list_dossiers, conversation_key=conversation_key,
                       limit=limit, offset=offset)


@router.get("/messages/reading/dossiers/{conversation_key}/{sender_id}")
async def dossier(conversation_key: str, sender_id: str, request: Request,
                  limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await _call(_service(request).get_dossier, conversation_key, sender_id, limit=limit, offset=offset)


@router.get("/messages/reading/dossiers/{conversation_key}/{sender_id}/sources")
async def dossier_sources(conversation_key: str, sender_id: str, request: Request,
                          limit: int = Query(30, ge=1, le=50), offset: int = Query(0, ge=0)):
    return await _call(_service(request).dossier_sources, conversation_key, sender_id,
                       limit=limit, offset=offset)


@router.get("/messages/reading/focus/{conversation_key}")
async def conversation_focus(conversation_key: str, request: Request):
    return await _call(_service(request).get_conversation_focus, conversation_key)


@router.put("/messages/reading/focus/{conversation_key}")
async def save_conversation_focus(conversation_key: str, request: Request):
    _human(request)
    payload = await _body(request)
    if not {"expected_revision", "mode"}.issubset(payload) or set(payload) - {
        "expected_revision", "mode", "labels"
    }:
        raise HTTPException(status_code=422, detail="Invalid conversation focus fields.")
    return await _call(_service(request).set_conversation_focus, conversation_key, **payload)


@router.put("/messages/reading/profile")
async def save_profile(request: Request, scope: str = "global"):
    _human(request)
    payload = await _body(request)
    if set(payload) != {"profile", "expected_revision"}:
        raise HTTPException(status_code=422, detail="Expected profile and expected_revision.")
    return await _call(_service(request).set_reading_profile, payload["profile"], expected_revision=payload["expected_revision"], scope=scope)


@router.get("/messages/conversations/{conversation_key}/digest")
async def digest(conversation_key: str, request: Request):
    return await _call(_service(request).reading_digest, conversation_key)


def _proposals(request):
    _reader(request)
    return _runtime(request).message_matter_proposals


@router.get("/messages/matter-proposals")
async def proposals(request: Request, state: str | None = None, limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                    conversation_key: str | None = None, since: str | None = None, until: str | None = None):
    return await _call(_proposals(request).list_proposals, state=state, limit=limit, cursor=cursor,
                       conversation_key=conversation_key, since=since, until=until)


@router.get("/messages/matter-proposals/{proposal_id}")
async def proposal(proposal_id: str, request: Request):
    return await _call(_proposals(request).get_proposal, proposal_id)


@router.post("/messages/matter-proposals")
async def new_proposal(request: Request):
    _human(request)
    return await _call(_proposals(request).publish_proposal, await _body(request))


@router.post("/messages/matter-proposals/{proposal_id}/decision")
async def decide(proposal_id: str, request: Request):
    principal = _human(request)
    return await _call(_proposals(request).decide, proposal_id, await _body(request), principal=principal)


@router.post("/messages/matter-proposals/{proposal_id}/revalidate")
async def revalidate(proposal_id: str, request: Request):
    principal = _human(request)
    payload = await _body(request)
    if set(payload) != {"expected_revision"}:
        raise HTTPException(status_code=422, detail="Expected revision only.")
    return await _call(_proposals(request).revalidate, proposal_id, expected_revision=payload["expected_revision"], principal=principal)


def _evaluation(request):
    return _runtime(request).message_reading_evaluation


@router.post("/messages/reading/evaluation/badcases/preview")
async def badcase_preview(request: Request):
    principal = _human(request)
    payload = await _body(request)
    if set(payload) != {"evidence_message_ids"}:
        raise HTTPException(status_code=422, detail="Expected evidence_message_ids only.")
    return await _call(_evaluation(request).preview, payload["evidence_message_ids"], principal=principal)


@router.post("/messages/reading/evaluation/badcases")
async def save_badcase(request: Request):
    principal = _human(request)
    return await _call(_evaluation(request).save, await _body(request), principal=principal)


@router.get("/messages/reading/evaluation/badcases")
async def badcases(request: Request, limit: int = Query(20, ge=1, le=50), cursor: str | None = None):
    principal = _human(request)
    return await _call(_evaluation(request).list_badcases, principal=principal, limit=limit, cursor=cursor)


@router.get("/messages/reading/evaluation/badcases/{badcase_id}")
async def badcase(badcase_id: str, request: Request):
    principal = _human(request)
    return await _call(_evaluation(request).get_badcase, badcase_id, principal=principal)
