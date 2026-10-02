import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.main import create_app
from app.api.routes.agent import run_agent_turn
from app.api.routes.mail import import_mail
from app.api.schemas import AgentTurnRequest, MailImportRequest
from app.core.agent_turn import AgentTurnLoop
from app.core.config import get_settings
from app.core.llm import LLMResponse
from app.core.tool_result_gate import cache_scope_compatible, historical_cache_status


def _cached_scope(**overrides):
    scope = {
        "allowed_tools": ["mail.load_messages"],
        "allowed_source_ids": ["source-a"],
        "allowed_account_ids": ["account-a"],
        "allowed_paths": [],
        "full_data_authority": False,
        "full_workspace_authority": False,
        "workspace_root": None,
        "workspace_sensitive": False,
    }
    scope.update(overrides)
    return {
        "permission_version": "grant-v1",
        "workspace_version": "workspace-v1",
        "scope": scope,
    }


def _current_scope(**overrides):
    scope = {
        "allowed_tools": ["mail.load_messages"],
        "allowed_source_ids": ["source-a"],
        "allowed_account_ids": ["account-a"],
        "allowed_paths": [],
        "full_data_authority": False,
        "full_workspace_authority": False,
        "workspace_root": None,
        "workspace_sensitive": False,
        "permission_version": "grant-v1",
        "workspace_version": "workspace-v1",
    }
    scope.update(overrides)
    return scope


def test_old_mail_is_history_after_same_query_source_changes():
    now = datetime(2026, 10, 2, tzinfo=UTC)
    cache = historical_cache_status(
        {
            "created_at": (now - timedelta(days=1)).isoformat(),
            "run_id": "run-old",
            "source_version": "mail-snapshot-1",
            "ttl_seconds": 3600,
        },
        now=now,
    )

    assert cache["age_status"] == "expired"
    assert cache["freshness"] == "source_version_not_revalidated"
    assert cache["as_of"] == (now - timedelta(days=1)).isoformat()
    assert cache["run_id"] == "run-old"
    assert cache["current_evidence"] is False
    assert cache["historical_only"] is True

    old = {
        "tool_name": "mail.search",
        "input": {"query": "invoice"},
        "result": {"status": "completed", "output": {"count": 1}},
        "_cache": cache,
    }
    # A historical same-query result must not satisfy this turn's current read.
    assert AgentTurnLoop._completed_tool_call_summaries([old]) == []


def test_cache_scope_rejects_workspace_switch_and_permission_tightening():
    old = _cached_scope(
        allowed_paths=["/workspace-a/docs"],
        workspace_root="/workspace-a",
        workspace_sensitive=True,
    )
    switched_workspace = _current_scope(
        allowed_paths=["/workspace-b/docs"],
        workspace_root="/workspace-b",
        workspace_version="workspace-v2",
    )
    assert not cache_scope_compatible(old, switched_workspace, tool_name="mail.load_messages")

    tightened = _current_scope(
        permission_version="grant-v2",
        allowed_source_ids=[],
        allowed_account_ids=[],
    )
    assert not cache_scope_compatible(_cached_scope(), tightened, tool_name="mail.load_messages")


def test_cache_is_rejected_when_tool_is_no_longer_authorized_or_scope_is_unknown():
    assert not cache_scope_compatible(
        _cached_scope(), _current_scope(allowed_tools=[]), tool_name="mail.load_messages"
    )
    assert not cache_scope_compatible(
        {"permission_version": "grant-v1"}, _current_scope(), tool_name="mail.load_messages"
    )


def test_unknown_freshness_never_claims_current_even_when_ttl_is_known():
    status = historical_cache_status(
        {"created_at": "2026-10-02T00:00:00+00:00", "ttl_seconds": 600},
        now=datetime(2026, 10, 2, tzinfo=UTC),
    )
    assert status["age_status"] == "within_ttl"
    assert status["freshness"] == "unknown_source_version"
    assert status["current_evidence"] is False


class _MailFreshnessLLM:
    def __init__(self):
        self.answer_observations = []

    def complete_text(self, *, system_prompt, user_prompt, prompt_summary, **_kwargs):
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps({"selected_package": "mail", "reason": "Read matching mail."})
        elif "Tool Result Checker" in system_prompt:
            content = json.dumps({"status": "accepted", "message": "Relevant mail found."})
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            current = [item for item in payload["observations"] if "_cache" not in item]
            if not current:
                operation = {
                    "type": "tool_call", "tool_name": "mail.search",
                    "tool_input": {"query": "daily report", "limit": 8},
                    "final_answer": None, "reason": "Refresh the same query.", "confidence": "high",
                }
            elif current[-1].get("tool_name") == "mail.search":
                message = max(
                    current[-1]["result"]["output"]["messages"],
                    key=lambda item: item.get("received_at", ""),
                )
                operation = {
                    "type": "tool_call", "tool_name": "mail.load_messages",
                    "tool_input": {"message_ids": [message["message_id"]]},
                    "final_answer": None, "reason": "Load newest match.", "confidence": "high",
                }
            else:
                operation = {
                    "type": "final_answer", "final_answer": None,
                    "reason": "Use newly loaded evidence.", "confidence": "high",
                }
            content = json.dumps({"operation": operation, "assistant_message": "Checking mail."})
        elif "Final Answer Writer" in system_prompt:
            self.answer_observations = json.loads(user_prompt)["observations"]
            loaded = [
                item for item in self.answer_observations
                if item.get("tool_name") == "mail.load_messages" and "_cache" not in item
            ]
            body = loaded[-1]["result"]["output"]["messages"][0]["body_text"]
            content = f"Latest email says: {body}"
        else:
            content = "Unexpected prompt."
        return LLMResponse(provider="fake_mail_freshness", status="completed", content=content,
                           prompt_summary=prompt_summary)


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_same_mail_query_reads_new_mail_through_full_turn_flow(tmp_path, monkeypatch, orchestrator):
    local_config = tmp_path / "local.toml"
    local_config.write_text(
        '[agent]\norchestrator = "langgraph"\n' if orchestrator == "langgraph" else "",
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(local_config))
    get_settings.cache_clear()
    app = create_app()
    request = SimpleNamespace(app=app)

    def add_mail(external_id: str, body: str):
        import_mail(
            MailImportRequest(
                account={"provider": "local_json", "email_address": "watch@example.com"},
                messages=[{
                    "external_id": external_id, "folder": "Inbox", "subject": "Daily report",
                    "sender": "reports@example.com", "to": ["watch@example.com"],
                    "received_at": (
                        "2026-10-01T08:00:00Z" if external_id == "report-old"
                        else "2026-10-02T08:00:00Z"
                    ),
                    "body_text": body,
                }],
            ),
            request,
        )

    add_mail("report-old", "OLD_REPORT_SENTINEL")
    fake_llm = _MailFreshnessLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm
    first = run_agent_turn(
        AgentTurnRequest(session_id=f"cache-real-{orchestrator}", user_input="Find the daily report."),
        request,
    )
    assert [event.tool_name for event in first.tool_events] == ["mail.search", "mail.load_messages"]
    assert first.tool_events[1].cache_metadata.get("run_id") == first.run_id

    add_mail("report-new", "NEW_REPORT_SENTINEL")
    second = run_agent_turn(
        AgentTurnRequest(session_id=f"cache-real-{orchestrator}", user_input="Find the daily report."),
        request,
    )

    assert [event.tool_name for event in second.tool_events] == ["mail.search", "mail.load_messages"]
    assert "NEW_REPORT_SENTINEL" in second.answer
    historical = [item for item in fake_llm.answer_observations if item.get("_cache", {}).get("historical_only")]
    assert historical
    assert all(item["_cache"]["current_evidence"] is False for item in historical)
