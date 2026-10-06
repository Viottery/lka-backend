"""Synthetic dossier/API checks; no production data or remote models."""

from pathlib import Path

import pytest
from test_message_api_tools import _context
from test_message_api_tools import _request as _raw_request
from test_message_profile_api_tools import setup

from app.api.routes.message_metadata import router as metadata_router
from app.core.tools import ToolInvocation
from app.domains.message_profile_documents import MessageProfileDocumentStore
from app.tool_packages.messages import build_message_tools


def _request(app, method, path, **kwargs):
    route, _, query = path.partition("?")

    async def with_query(scope, receive, send):
        await app({**scope, "query_string": query.encode()}, receive, send)

    return _raw_request(with_query, method, route, **kwargs)


def dossier_setup(tmp_path, monkeypatch):
    app, service, policy = setup(tmp_path, monkeypatch)
    app.include_router(metadata_router)
    key = policy["conversation_key"]
    now = service._intelligence_now()
    service.import_messages([{"platform": "mock", "account_id": "self", "conversation_type": "group",
        "conversation_id": "g", "message_id": "dossier", "sender_id": "alice",
        "text": "我喜欢工具", "sent_at": now, "received_at": now}])
    message = service.recent(key)["messages"][0]
    source = {"id": message["message_id"], "sender": "alice", "text": message["text"],
              "kind": "text", "sent_at": now, "received_at": now, "seq": message["seq"],
              "reply": None, "parts": []}
    store = MessageProfileDocumentStore(Path(service.db_path).parent / "message_profiles")
    claim = {"sender": "alice", "kind": "preference", "text": "我喜欢工具", "quote": "我喜欢工具",
             "source_ids": [source["id"]], "basis": "explicit", "valid_until": None}
    assert store.ingest(key, [claim], [source], now, capture_epoch=policy["capture_epoch"])["accepted"] == 1
    return app, service, policy, source


def test_dossier_read_auth_and_live_controls_override_stale_files(tmp_path, monkeypatch):
    app, service, policy, source = dossier_setup(tmp_path, monkeypatch)
    key = policy["conversation_key"]
    headers = {"X-LKA-Messages-Token": "reader"}
    url = f"/messages/reading/dossiers/{key}/alice"
    assert _request(app, "GET", url).status_code == 401
    result = _request(app, "GET", url, headers=headers)
    assert result.status_code == 200 and result.json()["claims"]
    assert result.headers["cache-control"] == "no-store"
    evidence = _request(app, "GET", url + "/sources", headers=headers)
    assert evidence.json()["sources"][0]["message_id"] == source["id"]
    profile = service.get_participant(key, "alice")
    service.update_participant(key, "alice", profile["revision"], "hide")
    assert _request(app, "GET", url, headers=headers).status_code == 404
    assert service.list_dossiers()["dossiers"] == []
    profile = service.get_participant(key, "alice")
    service.update_participant(key, "alice", profile["revision"], "unhide")
    profile = service.get_participant(key, "alice")
    service.update_participant(key, "alice", profile["revision"], "correct", summary="人工纠正")
    corrected = service.get_dossier(key, "alice")
    assert corrected["human_correction"] == "人工纠正" and corrected["claims"] == []


def test_dossier_current_epoch_scope_and_canonical_proof(tmp_path, monkeypatch):
    _, service, policy, source = dossier_setup(tmp_path, monkeypatch)
    key = policy["conversation_key"]
    with pytest.raises(PermissionError):
        service.get_dossier(key, "alice", allowed_accounts=[])
    with service._connection() as conn:
        conn.execute("UPDATE message_history_messages SET text=? WHERE internal_message_id=?", ("different", source["id"]))
        conn.commit()
    with pytest.raises(PermissionError):
        service.get_dossier(key, "alice")
    off = service.set_policy({"platform": "mock", "account_id": "self", "conversation_type": "group",
        "conversation_id": "g", "record_enabled": False, "expected_revision": policy["revision"]})
    service.set_policy({"platform": "mock", "account_id": "self", "conversation_type": "group",
        "conversation_id": "g", "record_enabled": True, "expected_revision": off["revision"]})
    assert service.list_dossiers()["dossiers"] == []


def test_new_tools_accept_ids_and_enforce_both_grants(tmp_path, monkeypatch):
    _, service, policy, source = dossier_setup(tmp_path, monkeypatch)
    tools = {tool.spec.name: tool for tool in build_message_tools(service)}
    inputs = {
        "messages.resolve_conversations": {"query": "g"},
        "messages.conversation_metadata": {"conversation_key": policy["conversation_key"]},
        "messages.read_message": {"message_id": source["id"]},
        "messages.context": {"message_id": source["id"], "before": 1, "after": 0},
        "messages.dossier": {"conversation_key": policy["conversation_key"], "sender_id": "alice"},
        "messages.dossier_sources": {"conversation_key": policy["conversation_key"], "sender_id": "alice"},
        "messages.dossiers": {},
    }
    for name, payload in inputs.items():
        tool = tools[name]
        invocation = ToolInvocation(invocation_id=name, tool=tool.spec, session_id="s", context_id="c", input=payload)
        result = tool.invoke(invocation=invocation, context=_context(sources=(policy["source_id"],), accounts=(policy["account_scope_id"],)))
        assert result.status == "completed", (name, result.error)
        assert tool.spec.read_only and result.output["untrusted_data"]
        denied = tool.invoke(invocation=invocation, context=_context(sources=(policy["source_id"],), accounts=()))
        assert denied.status == "rejected"


def test_metadata_routes_reader_importer_control_are_separate(tmp_path, monkeypatch):
    app, _, policy, source = dossier_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("LKA_MESSAGES_IMPORT_TOKEN", "importer")
    key = policy["conversation_key"]
    reader = {"X-LKA-Messages-Token": "reader"}
    importer = {"X-LKA-Messages-Token": "importer"}
    control = {"X-LKA-Messages-Token": "control"}
    assert _request(app, "GET", "/messages/conversations/resolve?query=g", headers=reader).status_code == 200
    url = f"/messages/conversations/{key}/metadata"
    assert _request(app, "GET", url, headers=importer).status_code == 401
    payload = {"expected_revision": 0, "user_alias": "项目组"}
    assert _request(app, "PATCH", url, payload=payload, headers=reader).status_code == 401
    assert _request(app, "PATCH", url, payload=payload, headers=control).status_code == 200
    assert _request(app, "PATCH", url, payload=payload, headers=control).status_code == 409
    assert _request(app, "GET", f"/messages/records/{source['id']}/context?before=26", headers=reader).status_code == 422
