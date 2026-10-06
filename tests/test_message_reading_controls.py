"""Local-only reading controls with no provider calls or real messages."""
from test_message_api_tools import _client, _request

from app.core.local_config import MessageHistoryConfig
from app.core.message_analysis import MessageAnalysisCoordinator

AUTH = {"Authorization": "Bearer control-test"}
IDENTITY = {"platform": "mock", "account_id": "a", "conversation_type": "group", "conversation_id": "c"}


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_MESSAGES_API_TOKEN", "control-test")
    monkeypatch.setenv("LKA_MESSAGES_IMPORT_TOKEN", "import-test")
    app, service = _client(tmp_path)
    app.state.runtime.message_analysis = MessageAnalysisCoordinator(service=service, store=service.jobs,
                                                                   config=MessageHistoryConfig())
    policy = service.set_policy({**IDENTITY, "record_enabled": True, "analysis_enabled": True, "batch_size": 50})
    service.import_messages([{**IDENTITY, "message_id": "m", "text": "private-body", "received_at": 1}])
    return app, service, policy


def test_pause_resume_cas_and_pending_explanation(tmp_path, monkeypatch):
    app, service, policy = setup(tmp_path, monkeypatch)
    base = "/background/services/message-reading"
    state = _request(app, "GET", base, headers=AUTH)
    assert state.status_code == 200 and "private-body" not in str(state.json())
    assert state.json()["budget"]["total_calls"] == 0
    paused = _request(app, "POST", base + "/pause", payload={"expected_revision": 1}, headers=AUTH)
    assert paused.status_code == 200 and paused.json()["paused"]
    assert _request(app, "POST", base + "/resume", payload={"expected_revision": 1}, headers=AUTH).status_code == 409
    analyze = _request(app, "POST", f"/messages/conversations/{policy['conversation_key']}/analyze", headers=AUTH)
    assert analyze.json()["status"] == "paused"
    assert service.recent()["messages"]
    resumed = _request(app, "POST", base + "/resume", payload={"expected_revision": paused.json()["revision"]}, headers=AUTH)
    assert resumed.status_code == 200 and not resumed.json()["paused"]
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_reading_control_events").fetchone()[0] == 2


def test_import_credential_and_invalid_types_cannot_control_service(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch)
    path = "/background/services/message-reading/pause"
    assert _request(app, "POST", path, payload={"expected_revision": 1},
                    headers={"Authorization": "Bearer import-test"}).status_code == 401
    assert _request(app, "POST", path, payload={"expected_revision": True}, headers=AUTH).status_code == 422
    assert _request(app, "POST", path, payload={"expected_revision": 1, "paused": True}, headers=AUTH).status_code == 422


def test_work_limit_raise_is_cas_and_permission_fenced(tmp_path, monkeypatch):
    app, service, policy = setup(tmp_path, monkeypatch)
    job = service.schedule_pending(policy["conversation_key"], force=True)[0]
    family = job["payload"]["work_family_id"]
    path = f"/messages/analysis-work/{family}/limits"
    data = {"expected_revision": 1, "max_tokens": 40000, "max_calls": 5}
    response = _request(app, "POST", path, payload=data, headers=AUTH)
    assert response.status_code == 200 and response.json()["work"]["revision"] == 2
    assert _request(app, "POST", path, payload=data, headers=AUTH).status_code == 409
    service.set_policy({**IDENTITY, "expected_revision": policy["revision"], "record_enabled": False})
    assert _request(app, "POST", path, payload={**data, "expected_revision": 2}, headers=AUTH).status_code == 404


def test_replay_control_auth_types_revision_and_pause(tmp_path, monkeypatch):
    app, service, policy = setup(tmp_path, monkeypatch)
    path = f"/messages/conversations/{policy['conversation_key']}/replay"
    data = {"expected_revision": policy["revision"]}
    assert _request(app, "POST", path, payload=data, headers={"Authorization": "Bearer import-test"}).status_code == 401
    assert _request(app, "POST", path, payload={"expected_revision": True}, headers=AUTH).status_code == 422
    assert _request(app, "POST", path, payload={"expected_revision": 99}, headers=AUTH).status_code == 409
    service.set_reading_paused(True, 1)
    response = _request(app, "POST", path, payload=data, headers=AUTH)
    assert response.status_code == 200 and response.json()["status"] == "paused"
    assert response.json()["through_seq"] == 1
    state = _request(app, "GET", "/background/services/message-reading", headers=AUTH).json()
    assert state["schedules"][0]["replay_pending"] == 1
    assert "private-body" not in str(state)
    app.state.runtime.message_analysis.config.background_enabled = False
    assert _request(app, "POST", path, payload=data, headers=AUTH).status_code == 503
    app.state.runtime.message_analysis.config.background_enabled = True
    service.set_policy({**IDENTITY, "expected_revision": policy["revision"], "record_enabled": False})
    assert _request(app, "POST", path, payload=data, headers=AUTH).status_code == 404
