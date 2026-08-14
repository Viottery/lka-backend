from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.agent import run_agent_turn
from app.api.routes.mail import import_mail
from app.api.routes.sessions import get_session
from app.api.schemas import AgentTurnRequest, MailImportRequest
from app.core.config import get_settings
from app.core.llm import LLMRateLimitError, LLMResponse
from app.domains.matters import MatterCreateInput


def test_agent_turn_expands_mail_package_and_records_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    assert "/agent/turn" in app.openapi()["paths"]
    assert "/mail/process" not in app.openapi()["paths"]

    full_body_sentinel = "FULL_BODY_SENTINEL_NTUSO_REQUIREMENTS"
    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_ntuso_001",
                    "folder": "Inbox",
                    "subject": "NTUSO Audition requirements",
                    "sender": "ntuso@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": (
                        "NTUSO audition requires scales and a prepared piece. "
                        f"{full_body_sentinel}"
                    ),
                },
            ],
        ),
        request,
    )

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_agent_turn_ntuso",
            user_input="帮我查询 NTUSO 的乐团考试相关要求，并给我建议",
        ),
        request,
    )

    assert response.session_id == "session_agent_turn_ntuso"
    assert response.selected_package == "mail"
    assert [event.tool_name for event in response.tool_events] == [
        "mail.search",
        "mail.load_messages",
    ]
    assert [event.feedback["status"] for event in response.tool_events] == [
        "accepted",
        "accepted",
    ]
    assert [event.action for event in response.decision_events] == [
        "select_package",
        "call_tool",
        "call_tool",
        "answer",
    ]
    assert [event.type for event in response.progress_events] == [
        "package_selected",
        "tool_started",
        "tool_completed",
        "tool_started",
        "tool_completed",
        "final_answer",
    ]
    assert response.progress_events[0].package_name == "mail"
    assert response.progress_events[2].tool_name == "mail.search"
    assert response.verification_warnings == []
    assert full_body_sentinel in response.answer
    assert response.log_path is not None

    log_text = Path(response.log_path).read_text(encoding="utf-8")
    assert "## Package Catalog" in log_text
    assert "## Progress Events" in log_text
    assert "## Verification Warnings" in log_text
    assert "mail.search" in log_text
    assert "mail.load_messages" in log_text
    assert full_body_sentinel in log_text

    session = get_session("session_agent_turn_ntuso", request)
    assert [message.role for message in session.messages] == ["user", "agent"]
    assert session.messages[1].payload["trace_id"] == response.trace_id
    assert session.messages[1].payload["selected_package"] == "mail"


def test_agent_turn_retries_rate_limited_llm_and_logs_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)

    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_rate_limit_001",
                    "folder": "Inbox",
                    "subject": "NTUSO Audition",
                    "sender": "ntuso@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": "NTUSO audition details for retry test.",
                },
            ],
        ),
        request,
    )
    fake_llm = _RateLimitedThenWorkingLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm
    app.state.runtime.agent_turn_loop.default_rate_limit_wait_seconds = 0.0

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_agent_turn_rate_limit",
            user_input="帮我查询 NTUSO 的乐团考试相关要求",
        ),
        request,
    )

    assert fake_llm.calls == 7
    assert fake_llm.max_output_tokens_seen == [None, None, None, None, None, None, None]
    assert response.answer == "LLM final answer after retry."
    assert [event.status for event in response.llm_events] == [
        "rate_limited",
        "completed",
        "completed",
        "completed",
        "completed",
        "completed",
        "completed",
    ]
    assert [event.stage for event in response.llm_events] == [
        "route",
        "route",
        "decision",
        "tool_result_check",
        "decision",
        "tool_result_check",
        "decision",
    ]
    assert response.llm_events[0].status_code == 429
    assert response.llm_events[0].retry_after == "0"
    assert [event.action for event in response.decision_events] == [
        "select_package",
        "call_tool",
        "call_tool",
        "answer",
    ]
    assert [event.tool_name for event in response.tool_events] == [
        "mail.search",
        "mail.load_messages",
    ]
    assert [event.feedback["source"] for event in response.tool_events] == [
        "llm",
        "llm",
    ]

    log_text = Path(response.log_path or "").read_text(encoding="utf-8")
    assert "rate_limited" in log_text
    assert "Choose at most one tool package" in log_text
    assert "Choose the next single action" in log_text
    assert "## Decision Events" in log_text


def test_agent_turn_passes_request_llm_options_to_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    fake_llm = _LLMOptionsRecordingLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_llm_options",
            user_input="直接回答当前问题，不需要工具。",
            llm={
                "client_name": "mock-client",
                "model": "user-selected-model",
                "response_mode": "json",
            },
        ),
        request,
    )

    assert response.answer == "request llm options received"
    assert fake_llm.calls
    assert {call["client_name"] for call in fake_llm.calls} == {"mock-client"}
    assert {call["model"] for call in fake_llm.calls} == {"user-selected-model"}
    assert {call["response_mode"].value for call in fake_llm.calls} == {"json"}


def test_agent_turn_maintains_recent_session_context_window(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    fake_llm = _ContextAwareLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    first = run_agent_turn(
        AgentTurnRequest(
            session_id="session_context_window",
            user_input="第一轮问题：记住我的关注点是ICA学生签证。",
        ),
        request,
    )
    second = run_agent_turn(
        AgentTurnRequest(
            session_id="session_context_window",
            user_input="第二轮问题：继续刚才的话题。",
        ),
        request,
    )

    assert first.answer == "第一轮回答：已记录ICA学生签证关注点。"
    assert second.answer == "第二轮回答：我看到了上一轮关于ICA学生签证的近期问答。"
    assert fake_llm.route_contexts[0]["recent_messages"] == []
    second_context = fake_llm.route_contexts[1]
    assert second_context["token_budget"] == 65_536
    assert second_context["token_estimate"] > 0
    assert [message["role"] for message in second_context["recent_messages"]] == [
        "user",
        "agent",
    ]
    assert "第一轮问题" in second_context["recent_messages"][0]["content"]
    assert "第一轮回答" in second_context["recent_messages"][1]["content"]
    assert "tool_events" not in json.dumps(second_context, ensure_ascii=False)

    window = app.state.runtime.session_service.get_context_window(
        session_id="session_context_window"
    )
    assert len(window.recent_messages) == 4
    assert "第二轮回答" in window.recent_messages[-1].content

    log_text = Path(second.log_path or "").read_text(encoding="utf-8")
    assert "## Session Context Window" in log_text
    assert "第一轮问题" in log_text


def test_agent_turn_answers_follow_up_from_context_without_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    fake_llm = _FollowUpContextAnswerLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    first = run_agent_turn(
        AgentTurnRequest(
            session_id="session_follow_up_context",
            user_input="第一轮：帮我整理ICA学生签证进度。",
        ),
        request,
    )
    second = run_agent_turn(
        AgentTurnRequest(
            session_id="session_follow_up_context",
            user_input="继续刚才的话题，只列出已完成和待完成事项。",
        ),
        request,
    )

    assert first.answer == "第一轮回答：ICA进度已整理。"
    assert second.answer == "第二轮回答：已完成申请递交；待完成OSE办理。"
    assert [event.stage for event in second.llm_events] == [
        "route",
        "context_answer",
    ]
    assert [event.action for event in second.decision_events] == [
        "select_package",
        "answer",
    ]
    assert second.decision_events[1].source == "llm"
    assert second.tool_events == []
    assert fake_llm.context_answer_inputs
    assert "第一轮回答：ICA进度已整理。" in json.dumps(
        fake_llm.context_answer_inputs[-1]["session_context_window"],
        ensure_ascii=False,
    )

    log_text = Path(second.log_path or "").read_text(encoding="utf-8")
    assert "context_answer" in log_text


def test_agent_turn_reuses_cached_loaded_mail_for_follow_up(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    full_body_sentinel = "CACHE_BODY_SENTINEL_NTUSO_REQUIREMENTS"
    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_cached_mail_001",
                    "folder": "Inbox",
                    "subject": "NTUSO Audition requirements",
                    "sender": "ntuso@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": (
                        "NTUSO audition requires G major scale. "
                        f"{full_body_sentinel}"
                    ),
                },
            ],
        ),
        request,
    )
    fake_llm = _MailCacheReuseLLM(full_body_sentinel)
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    first = run_agent_turn(
        AgentTurnRequest(
            session_id="session_cached_mail_follow_up",
            user_input="帮我查询 NTUSO 的乐团考试相关要求。",
        ),
        request,
    )
    second = run_agent_turn(
        AgentTurnRequest(
            session_id="session_cached_mail_follow_up",
            user_input="继续刚才NTUSO话题，按今天到考试当天排清单。",
        ),
        request,
    )

    assert [event.tool_name for event in first.tool_events] == [
        "mail.search",
        "mail.load_messages",
    ]
    assert all(event.feedback["status"] == "accepted" for event in first.tool_events)
    assert second.selected_package == "mail"
    assert second.tool_events == []
    assert second.answer == "第二轮回答：我复用了缓存邮件正文。"
    assert fake_llm.second_route_context is not None
    assert "cached_mail_messages" in fake_llm.second_route_context
    assert full_body_sentinel in json.dumps(
        fake_llm.second_decision_observations,
        ensure_ascii=False,
    )
    assert second.decision_events[1].action == "answer"


def test_agent_turn_recovers_mail_route_from_malformed_llm_json(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    fake_llm = _MalformedRouteLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_malformed_route",
            user_input="现在是8月5号，还有什么NTU相关的日程没有完成",
        ),
        request,
    )

    assert response.selected_package == "mail"
    assert response.decision_events[0].source == "llm"
    assert response.decision_events[0].reason == (
        "Recovered mail package from malformed route output."
    )
    assert response.answer == "Recovered route answer."


def test_agent_turn_recovers_plain_text_decision_as_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_plain_text_answer_001",
                    "folder": "Inbox",
                    "subject": "ICA Student Pass checklist",
                    "sender": "ica@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": "Bring IPA letter, passport, SG Arrival Card, and photo.",
                },
            ],
        ),
        request,
    )
    fake_llm = _PlainTextDecisionAnswerLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_plain_text_answer",
            user_input="继续刚才ICA签证话题，列出材料清单。",
        ),
        request,
    )

    assert response.answer == "纯文本最终回答：带 IPA、护照、SGAC 和照片。"
    assert [event.action for event in response.decision_events] == [
        "select_package",
        "call_tool",
        "call_tool",
        "invalid_plain_text_decision",
    ]
    assert response.decision_events[-1].source == "llm"
    assert response.decision_events[-1].reason == (
        "Rejected non-JSON decision output."
    )
    assert "纯文本最终回答" in (response.decision_events[-1].raw_output or "")
    assert any(event.stage == "answer" for event in response.llm_events)
    assert "当前未配置可用 LLM" not in response.answer


def test_agent_turn_does_not_final_plain_text_progress_before_tool_execution(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    sentinel = "NTUSO_PLAIN_PROGRESS_SENTINEL"
    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_plain_progress_001",
                    "folder": "Inbox",
                    "subject": "NTUSO Audition requirements",
                    "sender": "ntuso@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": (
                        "NTUSO audition requires scales and one prepared piece. "
                        f"{sentinel}"
                    ),
                },
            ],
        ),
        request,
    )
    app.state.runtime.agent_turn_loop.llm_client = _PlainTextProgressBeforeToolLLM()

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_plain_text_progress",
            user_input="搜索一下NTUSO的audition要求，我要怎么做？",
        ),
        request,
    )

    assert response.answer != "搜索本地邮箱中与 NTUSO audition 相关的邮件。"
    assert sentinel in response.answer
    assert [event.tool_name for event in response.tool_events] == [
        "mail.search",
        "mail.load_messages",
    ]
    assert [event.action for event in response.decision_events] == [
        "select_package",
        "invalid_plain_text_decision",
        "call_tool",
        "call_tool",
        "answer",
    ]
    assert response.decision_events[1].source == "llm"
    assert response.decision_events[1].reason == (
        "Rejected non-JSON decision output."
    )


def test_agent_turn_does_not_final_plain_text_progress_after_mail_search(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    sentinel = "NTUSO_AFTER_SEARCH_PROGRESS_SENTINEL"
    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_after_search_progress_001",
                    "folder": "Inbox",
                    "subject": "Fw: NTUSO Audition",
                    "sender": "ntuso@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": (
                        "NTUSO audition requires scales and one prepared piece. "
                        f"{sentinel}"
                    ),
                },
            ],
        ),
        request,
    )
    app.state.runtime.agent_turn_loop.llm_client = _PlainTextProgressAfterSearchLLM()

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_plain_text_after_search",
            user_input="搜索一下NTUSO的audition要求，我要怎么做？",
        ),
        request,
    )

    assert response.answer != "找到一封相关的邮件“Fw: NTUSO Audition”，正在读取完整内容以获取 audition 要求。"
    assert sentinel in response.answer
    assert [event.tool_name for event in response.tool_events] == [
        "mail.search",
        "mail.search",
        "mail.load_messages",
    ]
    assert [event.action for event in response.decision_events] == [
        "select_package",
        "call_tool",
        "invalid_plain_text_decision",
        "call_tool",
        "call_tool",
        "answer",
    ]


def test_agent_turn_accepts_operation_envelope_decisions(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_envelope_001",
                    "folder": "Inbox",
                    "subject": "NTUSO Audition envelope",
                    "sender": "ntuso@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": "Envelope decision test body.",
                },
            ],
        ),
        request,
    )
    fake_llm = _EnvelopeDecisionLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_operation_envelope",
            user_input="帮我查一下NTUSO考试。",
        ),
        request,
    )

    assert response.answer == "Envelope final answer."
    assert [event.action for event in response.decision_events] == [
        "select_package",
        "call_tool",
        "answer",
    ]
    assert response.decision_events[1].assistant_message == "我先检索相关邮件。"
    assert response.decision_events[1].operation["type"] == "tool_call"
    assert response.tool_events[0].feedback["source"] == "llm"


def test_agent_turn_does_not_use_assistant_message_as_missing_final_answer(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    sentinel = "MISSING_FINAL_ANSWER_SENTINEL"
    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_missing_final_001",
                    "folder": "Inbox",
                    "subject": "NTUSO Audition missing final",
                    "sender": "ntuso@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": f"Full mail body for fallback answer. {sentinel}",
                },
            ],
        ),
        request,
    )
    app.state.runtime.agent_turn_loop.llm_client = _MissingFinalAnswerEnvelopeLLM()

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_missing_final_answer",
            user_input="帮我查一下NTUSO考试。",
        ),
        request,
    )

    assert response.answer != "这是 assistant_message，不应该成为最终答案。"
    assert sentinel in response.answer
    assert response.decision_events[2].action == "invalid_final_answer"
    assert response.decision_events[2].assistant_message == (
        "这是 assistant_message，不应该成为最终答案。"
    )


def test_agent_turn_does_not_recover_malformed_tool_call_as_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    fake_llm = _MalformedToolCallDecisionLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_malformed_tool_call",
            user_input="帮我创建一个NTU事项。",
        ),
        request,
    )

    assert response.tool_events == []
    assert response.decision_events[-1].action == "malformed_tool_call"
    assert '"tool_name":"matter.create"' in (response.decision_events[-1].raw_output or "")
    assert '"tool_name":"matter.create"' not in response.answer
    assert "损坏 JSON" in response.answer


def test_agent_turn_can_expand_matter_package_after_mail_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    import_mail(
        MailImportRequest(
            account={
                "provider": "local_json",
                "email_address": "user@example.com",
            },
            messages=[
                {
                    "external_id": "agent_turn_cross_package_001",
                    "folder": "Inbox",
                    "subject": "ICA Student Pass appointment",
                    "sender": "ica@example.com",
                    "to": ["user@example.com"],
                    "received_at": "2026-08-05T09:30:00Z",
                    "body_text": "Attend ICA formalities on 2026-08-14 13:00.",
                },
            ],
        ),
        request,
    )
    fake_llm = _CrossPackageMatterLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_cross_package_matter",
            user_input="从ICA邮件里提取确定日程并加入本地事务。",
        ),
        request,
    )

    assert [event.action for event in response.decision_events] == [
        "select_package",
        "call_tool",
        "call_tool",
        "expand_package",
        "call_tool",
        "answer",
    ]
    assert [event.tool_name for event in response.tool_events] == [
        "mail.search",
        "mail.load_messages",
        "matter.create_many",
    ]
    assert "matter.create_many" in [tool["name"] for tool in response.expanded_tools]
    assert response.tool_events[-1].feedback["source"] == "llm"
    assert response.tool_events[-1].result["output"]["matters_created"] == 1
    progress_types = [event.type for event in response.progress_events]
    assert "package_expanded" in progress_types
    assert "tool_feedback" in progress_types
    assert response.progress_events[-1].type == "final_answer"
    assert response.verification_warnings == []

    matters = app.state.runtime.list_matters(limit=10)
    assert len(matters.matters) == 1
    assert matters.matters[0].title == "ICA Student Pass appointment"
    assert matters.matters[0].source_links[0].source_id


def test_agent_turn_feeds_tool_input_validation_errors_back_to_llm(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    fake_llm = _InvalidMatterInputThenRepairLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_invalid_tool_input_repair",
            user_input="创建一个NTUSO audition准备事项。",
        ),
        request,
    )

    assert [event.tool_name for event in response.tool_events] == [
        "matter.create_many",
        "matter.create_many",
    ]
    assert response.tool_events[0].result["status"] == "rejected"
    assert "tool_input.matters[0].priority must be one of" in (
        response.tool_events[0].result["output"]["validation_errors"][0]
    )
    assert response.tool_events[0].feedback["status"] == "failed"
    assert response.tool_events[1].result["status"] == "completed"
    assert response.tool_events[1].result["output"]["matters_created"] == 1
    assert response.answer == "已创建 NTUSO audition 准备事项。"


def test_agent_turn_adds_matter_domain_summary_to_tool_feedback(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    app.state.runtime.create_matter(
        payload=MatterCreateInput(
            title="NTU incomplete task",
            summary="Open item.",
            status="open",
            priority="high",
        )
    )
    app.state.runtime.create_matter(
        payload=MatterCreateInput(
            title="NTU completed task",
            summary="Done item.",
            status="done",
            priority="normal",
        )
    )
    fake_llm = _MatterFeedbackSummaryLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_matter_feedback_summary",
            user_input="有哪些NTU事项？",
        ),
        request,
    )

    feedback = response.tool_events[0].feedback
    assert feedback["status"] == "accepted"
    assert feedback["message"] == "Tool ran; domain summary is authoritative."
    assert feedback["domain_summary"]["matter_count"] == 2
    assert feedback["domain_summary"]["matter_status_counts"] == {
        "done": 1,
        "open": 1,
    }
    assert feedback["domain_summary"]["open_count"] == 1
    assert feedback["domain_summary"]["done_count"] == 1
    assert fake_llm.checker_payloads[0]["tool_feedback"]["domain_summary"] == (
        feedback["domain_summary"]
    )


def test_agent_turn_warns_when_final_answer_claims_unsupported_calendar_action(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    app.state.runtime.agent_turn_loop.llm_client = _UnsupportedCalendarClaimLLM()

    response = run_agent_turn(
        AgentTurnRequest(
            session_id="session_unsupported_calendar_claim",
            user_input="把明天的讲座加入日历。",
        ),
        request,
    )

    assert response.tool_events == []
    assert response.answer == "我已把明天的讲座加入日历。"
    assert [warning.code for warning in response.verification_warnings] == [
        "unsupported_calendar_claim",
    ]
    assert response.progress_events[-1].type == "verification_warning"

    log_text = Path(response.log_path or "").read_text(encoding="utf-8")
    assert "unsupported_calendar_claim" in log_text


def test_agent_turn_summarizes_context_window_with_llm_when_full(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()

    app = create_app()
    request = SimpleNamespace(app=app)
    fake_llm = _ContextSummarizingLLM()
    app.state.runtime.agent_turn_loop.llm_client = fake_llm
    app.state.runtime.agent_turn_loop.session_context_token_budget = 60

    first = run_agent_turn(
        AgentTurnRequest(
            session_id="session_context_summarize",
            user_input="first historical topic " + ("alpha " * 15),
        ),
        request,
    )
    second = run_agent_turn(
        AgentTurnRequest(
            session_id="session_context_summarize",
            user_input="second current topic " + ("beta " * 15),
        ),
        request,
    )

    window = app.state.runtime.session_service.get_context_window(
        session_id="session_context_summarize"
    )
    assert fake_llm.summary_inputs
    assert fake_llm.summary_inputs[0]["existing_summary"] == ""
    assert len(fake_llm.summary_inputs[0]["messages_to_summarize"]) == 2
    assert window.summary.startswith("LLM SUMMARY:")
    assert len(window.recent_messages) == 2
    assert "second current topic" in window.recent_messages[0].content
    assert "first historical topic" not in json.dumps(
        [message.model_dump(mode="json") for message in window.recent_messages],
        ensure_ascii=False,
    )

    assert not any(event.stage == "context_summarize" for event in first.llm_events)
    assert any(event.stage == "context_summarize" for event in second.llm_events)
    log_text = Path(second.log_path or "").read_text(encoding="utf-8")
    assert "context_summarize" in log_text
    assert "Session Context Compressor" in log_text


class _RateLimitedThenWorkingLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.max_output_tokens_seen: list[int | None] = []

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls += 1
        self.max_output_tokens_seen.append(max_output_tokens)
        if self.calls == 1:
            raise LLMRateLimitError(
                status_code=429,
                message="LLM provider returned HTTP 429: retry later",
                retry_after="0",
            )
        if "Tool Result Checker" in system_prompt:
            content = _tool_check_content()
        elif "Choose at most one tool package" in system_prompt:
            content = (
                '{"selected_package":"mail","reason":"test route",'
                '"search_query":"NTUSO"}'
            )
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload["observations"]
            if not observations:
                content = json.dumps(
                    {
                        "action": "call_tool",
                        "tool_name": "mail.search",
                        "tool_input": {"query": "NTUSO", "limit": 8},
                        "reason": "Search first.",
                    }
                )
            elif observations[-1]["tool_name"] == "mail.search":
                messages = observations[-1]["result"]["output"]["messages"]
                message_ids = [message["message_id"] for message in messages[:1]]
                content = json.dumps(
                    {
                        "action": "call_tool",
                        "tool_name": "mail.load_messages",
                        "tool_input": {"message_ids": message_ids},
                        "reason": "Load the matching message.",
                    }
                )
            else:
                content = json.dumps(
                    {
                        "action": "answer",
                        "answer": "LLM final answer after retry.",
                        "reason": "Loaded message is enough.",
                    }
                )
        else:
            content = "LLM final answer after retry."
        return LLMResponse(
            provider="fake_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _LLMOptionsRecordingLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        client_name: str | None = None,
        model: str | None = None,
        response_mode=None,
        require_json: bool = False,
        metadata: dict | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "client_name": client_name,
                "model": model,
                "response_mode": response_mode,
                "require_json": require_json,
                "metadata": metadata,
            }
        )
        if "Choose at most one tool package" in system_prompt:
            content = '{"selected_package":null,"reason":"context is enough"}'
        else:
            content = '{"answer":"request llm options received"}'
        return LLMResponse(
            provider="fake_llm_options",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _UnsupportedCalendarClaimLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": None,
                    "reason": "Claiming a calendar write without a tool for verifier test.",
                    "search_query": "",
                }
            )
        elif "using only the provided session context window" in system_prompt:
            content = json.dumps({"answer": "我已把明天的讲座加入日历。"})
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_unsupported_calendar_claim_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _ContextAwareLLM:
    def __init__(self) -> None:
        self.route_contexts: list[dict] = []
        self.decision_calls = 0

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            payload = json.loads(user_prompt)
            self.route_contexts.append(payload["session_context_window"])
            content = json.dumps(
                {
                    "selected_package": "mail",
                    "reason": "Use mail package for context-window test.",
                    "search_query": "ICA",
                }
            )
        elif "Choose the next single action" in system_prompt:
            self.decision_calls += 1
            answer = (
                "第一轮回答：已记录ICA学生签证关注点。"
                if self.decision_calls == 1
                else "第二轮回答：我看到了上一轮关于ICA学生签证的近期问答。"
            )
            content = json.dumps(
                {
                    "action": "answer",
                    "answer": answer,
                    "reason": "Context window is enough for this test.",
                }
            )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_context_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _FollowUpContextAnswerLLM:
    def __init__(self) -> None:
        self.route_calls = 0
        self.context_answer_inputs: list[dict] = []

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            self.route_calls += 1
            if self.route_calls == 1:
                content = json.dumps(
                    {
                        "selected_package": None,
                        "reason": "No tool needed for seed answer.",
                        "search_query": "",
                    }
                )
            else:
                content = json.dumps(
                    {
                        "selected_package": None,
                        "reason": "Session context is sufficient.",
                        "search_query": "",
                    }
                )
        elif "using only the provided session context window" in system_prompt:
            payload = json.loads(user_prompt)
            self.context_answer_inputs.append(payload)
            answer = (
                "第一轮回答：ICA进度已整理。"
                if self.route_calls == 1
                else "第二轮回答：已完成申请递交；待完成OSE办理。"
            )
            content = json.dumps({"answer": answer})
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_follow_up_context_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _MailCacheReuseLLM:
    def __init__(self, sentinel: str) -> None:
        self.sentinel = sentinel
        self.route_calls = 0
        self.second_route_context: dict | None = None
        self.second_decision_observations: list[dict] = []

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            self.route_calls += 1
            payload = json.loads(user_prompt)
            if self.route_calls == 2:
                self.second_route_context = payload["session_context_window"]
            content = json.dumps(
                {
                    "selected_package": "mail",
                    "reason": "Use mail package for cache reuse test.",
                    "search_query": "NTUSO",
                }
            )
        elif "Tool Result Checker" in system_prompt:
            content = _tool_check_content()
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload["observations"]
            if self.route_calls == 1:
                content = self._first_turn_decision(observations)
            else:
                self.second_decision_observations = observations
                content = json.dumps(
                    {
                        "action": "answer",
                        "answer": "第二轮回答：我复用了缓存邮件正文。",
                        "reason": "Cached mail observation is sufficient.",
                    }
                )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_mail_cache_reuse_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )

    def _first_turn_decision(self, observations: list[dict]) -> str:
        if not observations:
            return json.dumps(
                {
                    "action": "call_tool",
                    "tool_name": "mail.search",
                    "tool_input": {"query": "NTUSO", "limit": 8},
                    "reason": "Search first.",
                }
            )
        if observations[-1]["tool_name"] == "mail.search":
            messages = observations[-1]["result"]["output"]["messages"]
            message_ids = [message["message_id"] for message in messages[:1]]
            return json.dumps(
                {
                    "action": "call_tool",
                    "tool_name": "mail.load_messages",
                    "tool_input": {"message_ids": message_ids},
                    "reason": "Load the matching message.",
                }
            )
        return json.dumps(
            {
                "action": "answer",
                "answer": f"第一轮回答：已读取邮件正文 {self.sentinel}",
                "reason": "Loaded message is enough.",
            }
        )


class _MalformedRouteLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = (
                '{"selected_package":"mail","reason":"needs NTU schedule mail",'
                '"search_query":"NTU schedule"'
            )
        elif "Choose the next single action" in system_prompt:
            content = json.dumps(
                {
                    "action": "answer",
                    "answer": "Recovered route answer.",
                    "reason": "Route recovery selected mail package.",
                }
            )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_malformed_route_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _PlainTextDecisionAnswerLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": "mail",
                    "reason": "Use mail for checklist.",
                    "search_query": "ICA Student Pass checklist",
                }
            )
        elif "Tool Result Checker" in system_prompt:
            content = _tool_check_content()
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload["observations"]
            if not observations:
                content = json.dumps(
                    {
                        "action": "call_tool",
                        "tool_name": "mail.search",
                        "tool_input": {
                            "query": "ICA Student Pass checklist",
                            "limit": 8,
                        },
                        "reason": "Search checklist mail.",
                    }
                )
            elif observations[-1]["tool_name"] == "mail.search":
                messages = observations[-1]["result"]["output"]["messages"]
                message_ids = [message["message_id"] for message in messages[:1]]
                content = json.dumps(
                    {
                        "action": "call_tool",
                        "tool_name": "mail.load_messages",
                        "tool_input": {"message_ids": message_ids},
                        "reason": "Load full checklist mail body.",
                    }
                )
            else:
                content = "纯文本最终回答：带 IPA、护照、SGAC 和照片。"
        elif "Use the loaded local mail messages as observations" in system_prompt:
            content = "纯文本最终回答：带 IPA、护照、SGAC 和照片。"
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_plain_text_decision_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _PlainTextProgressBeforeToolLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": "mail",
                    "reason": "Use mail for NTUSO audition.",
                    "search_query": "NTUSO audition",
                }
            )
        elif "Choose the next single action" in system_prompt:
            content = "搜索本地邮箱中与 NTUSO audition 相关的邮件。"
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_plain_text_progress_before_tool_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _PlainTextProgressAfterSearchLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": "mail",
                    "reason": "Use mail for NTUSO audition.",
                    "search_query": "NTUSO audition",
                }
            )
        elif "Tool Result Checker" in system_prompt:
            content = _tool_check_content()
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload["observations"]
            if not observations:
                content = json.dumps(
                    {
                        "action": "call_tool",
                        "tool_name": "mail.search",
                        "tool_input": {
                            "query": "NTUSO audition",
                            "limit": 8,
                        },
                        "reason": "Search for NTUSO audition mail.",
                    }
                )
            else:
                content = "找到一封相关的邮件“Fw: NTUSO Audition”，正在读取完整内容以获取 audition 要求。"
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_plain_text_progress_after_search_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _EnvelopeDecisionLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": "mail",
                    "reason": "Use mail for NTUSO.",
                    "search_query": "NTUSO",
                }
            )
        elif "Tool Result Checker" in system_prompt:
            content = _tool_check_content()
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            if not payload["observations"]:
                content = json.dumps(
                    {
                        "operation": {
                            "type": "tool_call",
                            "tool_name": "mail.search",
                            "tool_input": {"query": "NTUSO", "limit": 8},
                            "final_answer": None,
                            "reason": "Search relevant local mail first.",
                            "confidence": "high",
                        },
                        "assistant_message": "我先检索相关邮件。",
                    }
                )
            else:
                content = json.dumps(
                    {
                        "operation": {
                            "type": "final_answer",
                            "tool_name": None,
                            "tool_input": {},
                            "final_answer": "Envelope final answer.",
                            "reason": "Search result is enough for this test.",
                            "confidence": "high",
                        },
                        "assistant_message": "Envelope final answer.",
                    }
                )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_envelope_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _MissingFinalAnswerEnvelopeLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": "mail",
                    "reason": "Use mail for missing final answer test.",
                    "search_query": "NTUSO",
                }
            )
        elif "Tool Result Checker" in system_prompt:
            content = _tool_check_content()
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            if not payload["observations"]:
                content = json.dumps(
                    {
                        "operation": {
                            "type": "tool_call",
                            "tool_name": "mail.search",
                            "tool_input": {"query": "NTUSO", "limit": 8},
                            "final_answer": None,
                            "reason": "Search relevant local mail first.",
                            "confidence": "high",
                        },
                        "assistant_message": "我先检索相关邮件。",
                    }
                )
            else:
                content = json.dumps(
                    {
                        "operation": {
                            "type": "final_answer",
                            "tool_name": None,
                            "tool_input": {},
                            "final_answer": None,
                            "reason": "Missing final answer should be rejected.",
                            "confidence": "medium",
                        },
                        "assistant_message": "这是 assistant_message，不应该成为最终答案。",
                    }
                )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_missing_final_answer_envelope_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _MalformedToolCallDecisionLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": "matter",
                    "reason": "User asks to create a matter.",
                    "search_query": "NTU",
                }
            )
        elif "You repair one malformed Main Agent Brain decision" in system_prompt:
            content = "I cannot safely repair the incomplete tool call."
        elif "Choose the next single action" in system_prompt:
            content = (
                '{"action":"call_tool","tool_name":"matter.create",'
                '"tool_input":{"title":"NTU matter","summary":"broken'
            )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_malformed_tool_call_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _CrossPackageMatterLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": "mail",
                    "reason": "Read email evidence first.",
                    "search_query": "ICA Student Pass appointment",
                }
            )
        elif "Tool Result Checker" in system_prompt:
            content = _tool_check_content()
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload["observations"]
            expanded_package_names = payload["expanded_package_names"]
            if not observations:
                content = json.dumps(
                    {
                        "assistant_message": "先检索 ICA 邮件。",
                        "operation": {
                            "type": "tool_call",
                            "package_name": None,
                            "tool_name": "mail.search",
                            "tool_input": {
                                "query": "ICA Student Pass appointment",
                                "limit": 8,
                            },
                            "final_answer": None,
                            "reason": "Find candidate mail.",
                            "confidence": "high",
                        },
                    }
                )
            elif observations[-1].get("tool_name") == "mail.search":
                messages = observations[-1]["result"]["output"]["messages"]
                content = json.dumps(
                    {
                        "assistant_message": "加载 ICA 邮件正文。",
                        "operation": {
                            "type": "tool_call",
                            "package_name": None,
                            "tool_name": "mail.load_messages",
                            "tool_input": {
                                "message_ids": [messages[0]["message_id"]],
                            },
                            "final_answer": None,
                            "reason": "Need full evidence.",
                            "confidence": "high",
                        },
                    }
                )
            elif "matter" not in expanded_package_names:
                content = json.dumps(
                    {
                        "assistant_message": "邮件证据已读取，展开事务工具。",
                        "operation": {
                            "type": "expand_package",
                            "package_name": "matter",
                            "tool_name": None,
                            "tool_input": {},
                            "final_answer": None,
                            "reason": "Need matter tools to persist extracted schedule.",
                            "confidence": "high",
                        },
                    }
                )
            elif not any(
                observation.get("tool_name") == "matter.create_many"
                for observation in observations
            ):
                loaded = next(
                    observation
                    for observation in observations
                    if observation.get("tool_name") == "mail.load_messages"
                )
                message_id = loaded["result"]["output"]["messages"][0]["message_id"]
                content = json.dumps(
                    {
                        "assistant_message": "写入本地事务。",
                        "operation": {
                            "type": "tool_call",
                            "package_name": None,
                            "tool_name": "matter.create_many",
                            "tool_input": {
                                "matters": [
                                    {
                                        "title": "ICA Student Pass appointment",
                                        "summary": "Attend ICA formalities on 2026-08-14 13:00.",
                                        "status": "open",
                                        "priority": "normal",
                                        "due_at": "2026-08-14T13:00:00+08:00",
                                        "tags": ["ICA"],
                                        "source_links": [
                                            {
                                                "source_type": "mail_message",
                                                "source_id": message_id,
                                                "reason": "Extracted from ICA email.",
                                            }
                                        ],
                                        "metadata": {"source": "agent_turn_test"},
                                    }
                                ],
                            },
                            "final_answer": None,
                            "reason": "Persist extracted matter.",
                            "confidence": "high",
                        },
                    }
                )
            else:
                content = json.dumps(
                    {
                        "assistant_message": "已写入 ICA 事务。",
                        "operation": {
                            "type": "final_answer",
                            "package_name": None,
                            "tool_name": None,
                            "tool_input": {},
                            "final_answer": "已写入 ICA 事务。",
                            "reason": "matter.create_many completed.",
                            "confidence": "high",
                        },
                    }
                )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_cross_package_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _InvalidMatterInputThenRepairLLM:
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": "matter",
                    "reason": "User asks to create a local matter.",
                    "search_query": "NTUSO audition",
                }
            )
        elif "Tool Result Checker" in system_prompt:
            payload = json.loads(user_prompt)
            status = payload["tool_result"]["status"]
            content = json.dumps(
                {
                    "status": "accepted" if status == "completed" else "failed",
                    "message": f"Tool result status is {status}.",
                    "remaining_work": (
                        "Fix invalid enum values and retry."
                        if status != "completed"
                        else "Answer the user."
                    ),
                }
            )
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload["observations"]
            rejected = any(
                observation.get("tool_name") == "matter.create_many"
                and observation.get("result", {}).get("status") == "rejected"
                for observation in observations
            )
            completed = any(
                observation.get("tool_name") == "matter.create_many"
                and observation.get("result", {}).get("status") == "completed"
                for observation in observations
            )
            if completed:
                content = json.dumps(
                    {
                        "operation": {
                            "type": "final_answer",
                            "package_name": None,
                            "tool_name": None,
                            "tool_input": {},
                            "final_answer": "已创建 NTUSO audition 准备事项。",
                            "reason": "matter.create_many completed after repair.",
                            "confidence": "high",
                        },
                        "assistant_message": "已创建事项。",
                    }
                )
            else:
                priority = "normal" if rejected else "medium"
                content = json.dumps(
                    {
                        "operation": {
                            "type": "tool_call",
                            "package_name": "matter",
                            "tool_name": "matter.create_many",
                            "tool_input": {
                                "matters": [
                                    {
                                        "title": "NTUSO audition preparation",
                                        "summary": (
                                            "Prepare G major scale, excerpts, and "
                                            "choice piece."
                                        ),
                                        "status": "open",
                                        "priority": priority,
                                        "tags": ["NTUSO"],
                                        "source_links": [],
                                        "metadata": {"source": "agent_turn_test"},
                                    }
                                ]
                            },
                            "final_answer": None,
                            "reason": "Create the requested local matter.",
                            "confidence": "high",
                        },
                        "assistant_message": "正在创建 NTUSO audition 准备事项。",
                    }
                )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_invalid_matter_input_then_repair_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _MatterFeedbackSummaryLLM:
    def __init__(self) -> None:
        self.checker_payloads: list[dict] = []

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": "matter",
                    "reason": "User asks for local NTU matters.",
                    "search_query": "NTU",
                }
            )
        elif "Tool Result Checker" in system_prompt:
            payload = json.loads(user_prompt)
            self.checker_payloads.append(payload)
            content = json.dumps(
                {
                    "status": "accepted",
                    "message": "Tool ran; domain summary is authoritative.",
                    "remaining_work": "Answer from matter results.",
                }
            )
        elif "Choose the next single action" in system_prompt:
            payload = json.loads(user_prompt)
            observations = payload["observations"]
            if not observations:
                content = json.dumps(
                    {
                        "operation": {
                            "type": "tool_call",
                            "package_name": "matter",
                            "tool_name": "matter.search",
                            "tool_input": {"query": "NTU", "limit": 10},
                            "final_answer": None,
                            "reason": "Search local NTU matters.",
                            "confidence": "high",
                        },
                        "assistant_message": "正在查找 NTU 事项。",
                    }
                )
            else:
                content = json.dumps(
                    {
                        "operation": {
                            "type": "final_answer",
                            "package_name": None,
                            "tool_name": None,
                            "tool_input": {},
                            "final_answer": "找到 2 个 NTU 事项。",
                            "reason": "matter.search completed.",
                            "confidence": "high",
                        },
                        "assistant_message": "已找到 NTU 事项。",
                    }
                )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_matter_feedback_summary_llm",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


class _ContextSummarizingLLM:
    def __init__(self) -> None:
        self.summary_inputs: list[dict] = []

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_summary: str,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        if "Choose at most one tool package" in system_prompt:
            content = json.dumps(
                {
                    "selected_package": None,
                    "reason": "No package needed for context summarize test.",
                    "search_query": "",
                }
            )
        elif "Session Context Compressor" in system_prompt:
            payload = json.loads(user_prompt)
            self.summary_inputs.append(payload)
            content = json.dumps(
                {
                    "summary": "LLM SUMMARY: prior first topic."
                }
            )
        else:
            content = "Unexpected prompt."
        return LLMResponse(
            provider="fake_context_summarizer",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
        )


def _tool_check_content() -> str:
    return json.dumps(
        {
            "status": "accepted",
            "message": "Tool result is a valid observation.",
            "remaining_work": "Continue the agent loop.",
        }
    )
