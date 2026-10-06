"""Focused intelligence interface checks, no live servers or model requests."""

from datetime import UTC, datetime

from test_message_api_tools import _client, _context, _request

from app.api.routes.message_reading import router
from app.core.tools import ToolInvocation
from app.tool_packages.messages import MESSAGE_ORIGIN_CONSTRAINT, build_message_tools


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_MESSAGES_CONTROL_TOKEN", "control")
    monkeypatch.setenv("LKA_MESSAGES_API_TOKEN", "reader")
    app, service = _client(tmp_path)
    app.include_router(router)
    now = int(datetime.now(UTC).timestamp())
    policy = service.set_policy(
        {
            "platform": "mock",
            "account_id": "self",
            "conversation_type": "group",
            "conversation_id": "g",
            "record_enabled": True,
        }
    )
    service.import_messages(
        [
            {
                "platform": "mock",
                "account_id": "self",
                "conversation_type": "group",
                "conversation_id": "g",
                "message_id": str(i),
                "sender_id": "alice",
                "text": "hello",
                "sent_at": now - 7 * 3600 + i * 3 * 3600,
                "received_at": now,
            }
            for i in range(3)
        ]
    )
    return app, service, policy


def test_profile_api_requires_human_control_and_rechecks_revocation(tmp_path, monkeypatch):
    app, service, policy = setup(tmp_path, monkeypatch)
    key = policy["conversation_key"]
    path = f"/messages/reading/participants/{key}/alice"
    reader, control = {"X-LKA-Messages-Token": "reader"}, {"X-LKA-Messages-Token": "control"}
    listed = _request(app, "GET", "/messages/reading/participants", headers=reader)
    assert listed.status_code == 200 and len(listed.json()["participants"]) == 1
    original = _request(app, "GET", path, headers=reader).json()
    payload = {
        "expected_revision": original["revision"],
        "action": "correct",
        "summary": "人工纠正",
    }
    assert (
        _request(app, "POST", path + "/control", payload=payload, headers=reader).status_code == 401
    )
    assert (
        _request(app, "POST", path + "/control", payload=payload, headers=control).status_code
        == 200
    )
    assert (
        _request(app, "POST", path + "/control", payload=payload, headers=control).status_code
        == 409
    )
    focus = f"/messages/reading/focus/{key}"
    version = _request(app, "GET", focus, headers=reader).json()["revision"]
    form = {"expected_revision": version, "mode": "manual", "labels": ["social"]}
    assert _request(app, "PUT", focus, payload=form, headers=reader).status_code == 401
    assert _request(app, "PUT", focus, payload=form, headers=control).status_code == 200
    service.set_policy(
        {
            "platform": "mock",
            "account_id": "self",
            "conversation_type": "group",
            "conversation_id": "g",
            "record_enabled": False,
            "expected_revision": policy["revision"],
        }
    )
    assert _request(app, "GET", path, headers=reader).status_code == 404
    assert _request(app, "GET", focus, headers=reader).status_code == 404


def test_intelligence_tools_scope_and_origin_constraints(tmp_path, monkeypatch):
    _, service, policy = setup(tmp_path, monkeypatch)
    tools = {tool.spec.name: tool for tool in build_message_tools(service)}
    key = policy["conversation_key"]
    for name in ("messages.participants", "messages.participant", "messages.focus"):
        tool = tools[name]
        assert tool.spec.read_only and MESSAGE_ORIGIN_CONSTRAINT in tool.spec.origin_constraints
        invocation = ToolInvocation(
            invocation_id=name,
            tool=tool.spec,
            session_id="test",
            context_id="c",
            input={
                "conversation_key": key,
                **({"sender_id": "alice"} if name.endswith("participant") else {}),
            },
        )
        valid = tool.invoke(
            invocation=invocation,
            context=_context(
                sources=(policy["source_id"],), accounts=(policy["account_scope_id"],)
            ),
        )
        assert valid.status == "completed" and valid.output["untrusted_data"] is True
        denied = tool.invoke(invocation=invocation, context=_context(sources=()))
        assert denied.status == "rejected"
