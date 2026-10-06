from types import SimpleNamespace

from test_message_api_tools import _client, _request

from app.api.routes.message_reading import router


def client(tmp_path):
    app, history = _client(tmp_path)
    app.include_router(router)
    return app, history


def test_new_reading_routes_never_fallback_to_loopback_or_importer(tmp_path, monkeypatch):
    monkeypatch.delenv("LKA_MESSAGES_API_TOKEN", raising=False)
    monkeypatch.delenv("LKA_MESSAGES_CONTROL_TOKEN", raising=False)
    app, _ = client(tmp_path)
    for path in ("/messages/reading/overview", "/messages/reading/profile", "/messages/matter-proposals"):
        assert _request(app, "GET", path).status_code == 401
    monkeypatch.setenv("LKA_MESSAGES_CONTROL_TOKEN", "paired")
    monkeypatch.setenv("LKA_MESSAGES_IMPORT_TOKEN", "importer")
    assert _request(app, "GET", "/messages/reading/profile", headers={"X-LKA-Messages-Token": "importer"}).status_code == 401
    assert _request(app, "GET", "/messages/reading/profile", headers={"X-LKA-Messages-Token": "paired"}).status_code == 200


def test_human_decisions_need_dedicated_identity_not_api_or_body(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_MESSAGES_API_TOKEN", "reader")
    monkeypatch.setenv("LKA_MESSAGES_CONTROL_TOKEN", "paired")
    app, _ = client(tmp_path)
    decisions = []
    app.state.runtime.message_matter_proposals = SimpleNamespace(
        decide=lambda key, body, principal: decisions.append(principal) or {"state": "accepted"})
    path = "/messages/matter-proposals/test/decision"
    assert _request(app, "POST", path, payload={"approved": True}, headers={"X-LKA-Messages-Token": "reader"}).status_code == 401
    assert not decisions
    assert _request(app, "POST", path, payload={}, headers={"X-LKA-Messages-Token": "paired"}).status_code == 200
    assert decisions[0].kind == "human_control"
    assert decisions[0].principal_id.startswith("paired-ui:")
    assert decisions[0].principal_id.split(":", 1)[1] != "paired"


def test_explicit_checkpoint_restart_requires_paired_human_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_MESSAGES_API_TOKEN", "reader")
    monkeypatch.setenv("LKA_MESSAGES_CONTROL_TOKEN", "paired")
    app, history = client(tmp_path)
    policy = history.set_policy({"platform": "synthetic", "account_id": "self", "conversation_type": "group",
                                 "conversation_id": "test", "record_enabled": True, "analysis_enabled": True})
    calls = []
    history.retry_analysis = lambda key, **kwargs: calls.append(kwargs) or {"status": "retried", "job": {}}
    path = f"/messages/conversations/{policy['conversation_key']}/retry"
    payload = {"expected_updated_at": "synthetic timestamp", "allow_checkpoint_restart": True}
    assert _request(app, "POST", path, payload=payload, headers={"X-LKA-Messages-Token": "reader"}).status_code == 401
    assert not calls
    assert _request(app, "POST", path, payload=payload, headers={"X-LKA-Messages-Token": "paired"}).status_code == 200
    assert calls == [{"expected_updated_at": "synthetic timestamp", "allow_checkpoint_restart": True}]
    payload["allow_checkpoint_restart"] = 1
    assert _request(app, "POST", path, payload=payload, headers={"X-LKA-Messages-Token": "paired"}).status_code == 400


def test_profile_attention_strict_forms_and_cas(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_MESSAGES_CONTROL_TOKEN", "paired")
    app, _ = client(tmp_path)
    headers = {"X-LKA-Messages-Token": "paired"}
    path = "/messages/reading/profile"
    payload = {"profile": {"keywords": ["project"]}, "expected_revision": 0}
    response = _request(app, "PUT", path, payload=payload, headers=headers)
    assert response.status_code == 200
    assert _request(app, "PUT", path, payload=payload, headers=headers).status_code == 409
    assert _request(app, "PUT", path, payload={"keywords": ["wrong shape"], "expected_revision": 1}, headers=headers).status_code == 422
    assert _request(app, "POST", "/messages/reading/insights/test/attention", payload={"expected_revision": 0, "user_id": "other"}, headers=headers).status_code == 422


def test_scoped_agent_tools_are_only_reads(tmp_path):
    from app.tool_packages.messages import build_message_tools
    _, service = client(tmp_path)
    tools = {tool.spec.name: tool.spec for tool in build_message_tools(service)}
    assert all(spec.read_only is True for spec in tools.values())
    assert {"messages.overview", "messages.topics", "messages.insights", "messages.read_insight", "messages.topic_sources"} <= tools.keys()


def test_badcase_api_requires_preview_and_explicit_local_copy(tmp_path, monkeypatch):
    from app.domains.message_reading_evaluation import MessageReadingEvaluationService
    monkeypatch.setenv("LKA_MESSAGES_CONTROL_TOKEN", "paired")
    app, history = client(tmp_path)
    history.set_policy({"platform": "synthetic", "account_id": "self", "conversation_type": "group",
                        "conversation_id": "test", "record_enabled": True})
    history.import_messages([{"platform": "synthetic", "account_id": "self", "conversation_type": "group",
        "conversation_id": "test", "message_id": "one", "received_at": 100, "text": "synthetic deadline"}])
    app.state.runtime.message_reading_evaluation = MessageReadingEvaluationService(history)
    ids = [history.recent()["messages"][0]["message_id"]]
    headers = {"X-LKA-Messages-Token": "paired"}
    base = "/messages/reading/evaluation/badcases"
    assert _request(app, "GET", base).status_code == 401
    preview = _request(app, "POST", base + "/preview", payload={"evidence_message_ids": ids}, headers=headers)
    assert preview.status_code == 200
    payload = {"evidence_message_ids": ids, "expected_evidence_digest": preview.json()["evidence_digest"],
               "label": "missed_importance", "local_copy_consent": False}
    assert _request(app, "POST", base, payload=payload, headers=headers).status_code == 422
    payload["local_copy_consent"] = True
    assert _request(app, "POST", base, payload=payload, headers=headers).status_code == 200
    assert len(_request(app, "GET", base, headers=headers).json()["badcases"]) == 1
