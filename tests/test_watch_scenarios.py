from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core import watch_execution
from app.core.config import get_settings
from app.core.llm.models import LLMResponse
from app.core.watch_scheduler import WatchScheduler
from app.domains.mail import MailAccountInput, MailMessageInput
from app.domains.watch import WatchInput
from app.domains.watch_briefing import normalize_briefing
from tests.test_child_budget_quality import deterministic_prompt_counter  # noqa: F401

SOURCE = {
    "kind": "source",
    "ref": "https://news.example/story/42",
    "evidence_id": "story-42",
    "source_time": "2026-10-02T07:00:00+00:00",
    "excerpt": "Tickets are available. Security release note.",
}


def _json(changes=(), unchanged=(), unconfirmed=(), decisions=(), summary="Daily check"):
    import json

    return json.dumps(
        {
            "summary": summary,
            "changes": list(changes),
            "unchanged": list(unchanged),
            "unconfirmed": list(unconfirmed),
            "decisions": list(decisions),
        }
    )


def test_new_and_unchanged_event_fingerprints_are_stable_and_state_sensitive():
    first = normalize_briefing(
        summary=_json(
            changes=[
                {
                    "event_id": "ticket-status",
                    "claim": "Tickets are available",
                    "current_observation": "Tickets are available",
                    "evidence_refs": [SOURCE["ref"]],
                }
            ]
        ),
        evidence=[SOURCE],
        previous=None,
        importance_rules={},
        now=datetime(2026, 10, 2, 8, tzinfo=UTC),
    )
    second = normalize_briefing(
        summary=_json(
            unchanged=[
                {
                    "event_id": "ticket-status",
                    "claim": "Tickets are available",
                    "current_observation": "Tickets are available",
                    "evidence_refs": [SOURCE["ref"]],
                }
            ]
        ),
        evidence=[SOURCE],
        previous=first,
        importance_rules={},
        now=datetime(2026, 10, 3, 8, tzinfo=UTC),
    )
    third = normalize_briefing(
        summary=_json(
            changes=[
                {
                    "event_id": "ticket-status",
                    "claim": "Tickets sold out",
                    "current_observation": "Tickets sold out",
                    "evidence_refs": [SOURCE["ref"]],
                }
            ]
        ),
        evidence=[{**SOURCE, "excerpt": "Tickets sold out."}],
        previous=second,
        importance_rules={},
        now=datetime(2026, 10, 3, 8, tzinfo=UTC),
    )
    assert len(first["changes"]) == 1
    assert len(second["changes"]) == 0
    assert second["unchanged"][0]["previous_observation"] == "Tickets are available"
    assert len(third["changes"]) == 1
    assert third["changes"][0]["previous_observation"] == "Tickets are available"
    assert first["fingerprint"] != third["fingerprint"]


def test_missing_or_forged_citations_and_zero_results_are_unconfirmed():
    result = normalize_briefing(
        summary=_json(
            changes=[{"claim": "Venue changed", "evidence_refs": ["https://fake.example"]}],
            unchanged=[{"claim": "No changes", "evidence_refs": [SOURCE["ref"]]}],
        ),
        evidence=[],
        previous=None,
        importance_rules={},
    )
    assert result["changes"] == []
    assert result["unchanged"] == []
    assert len(result["unconfirmed"]) == 2


def test_retrieval_failure_is_not_reported_as_no_change():
    result = normalize_briefing(
        summary=_json(summary="No results were returned."),
        evidence=[],
        previous=None,
        importance_rules={},
        retrieval_failures=["web.search:failed"],
    )
    assert result["unchanged"] == []
    assert any("Retrieval failed" in item["reason"] for item in result["unconfirmed"])


def test_importance_rules_and_freshness_are_applied_to_evidenced_changes():
    old = datetime(2026, 9, 29, tzinfo=UTC)
    item = {
        "event_id": "release",
        "claim": "Security release note",
        "importance": 2,
        "evidence_refs": [SOURCE["ref"]],
    }
    ignored = normalize_briefing(
        summary=_json(changes=[item]),
        evidence=[SOURCE],
        previous=None,
        importance_rules={"include_keywords": ["security"], "minimum_importance": 4},
    )
    assert ignored["changes"] == []
    assert ignored["unchanged"][0]["importance_filtered"] is True

    stale_source = {**SOURCE, "source_time": old.isoformat(), "excerpt": "Security fix released."}
    stale = normalize_briefing(
        summary=_json(changes=[{**item, "claim": "Security fix released", "importance": 5}]),
        evidence=[stale_source],
        previous=None,
        importance_rules={"max_age_hours": 24},
        now=datetime(2026, 10, 2, 8, tzinfo=UTC),
    )
    assert stale["changes"] == []
    assert stale["unconfirmed"][0]["freshness"] == "stale_or_unknown"


def test_unstructured_model_output_is_preserved_as_unconfirmed_not_a_no_change():
    result = normalize_briefing(
        summary="I found nothing new today.",
        evidence=[SOURCE],
        previous=None,
        importance_rules={},
    )
    assert result["unchanged"] == []
    assert result["unconfirmed"][0]["claim"] == "I found nothing new today."


def test_prior_and_new_items_in_one_batch_keep_their_own_classification():
    prior = normalize_briefing(
        summary=_json(
            changes=[
                {
                    "event_id": "old",
                    "claim": "Tickets are available",
                    "current_observation": "Tickets are available",
                    "evidence_refs": [SOURCE["ref"]],
                }
            ]
        ),
        evidence=[SOURCE],
        previous=None,
        importance_rules={},
    )
    current = normalize_briefing(
        summary=_json(
            changes=[
                {
                    "event_id": "old",
                    "claim": "Tickets are available",
                    "current_observation": "Tickets are available",
                    "evidence_refs": [SOURCE["ref"]],
                },
                {
                    "event_id": "new",
                    "claim": "Security release note",
                    "current_observation": "Security release note",
                    "evidence_refs": [SOURCE["ref"]],
                },
            ]
        ),
        evidence=[SOURCE],
        previous=prior,
        importance_rules={},
    )
    assert [item["event_id"] for item in current["unchanged"]] == ["old"]
    assert [item["event_id"] for item in current["changes"]] == ["new"]
    assert "来源：https://news.example/story/42" in current["summary"]
    assert "Security release note" in current["summary"]


def test_citation_without_matching_excerpt_is_not_verified():
    result = normalize_briefing(
        summary=_json(
            changes=[
                {
                    "event_id": "claim-1",
                    "claim": "Tickets are sold out",
                    "evidence_refs": [SOURCE["ref"]],
                }
            ]
        ),
        evidence=[{**SOURCE, "excerpt": "Tickets are available."}],
        previous=None,
        importance_rules={},
    )
    assert result["changes"] == []
    assert result["unconfirmed"][0]["evidence_check"] == "unsupported_or_excerpt_missing"


def test_excerpt_support_fails_closed_for_negation_and_token_overlap():
    result = normalize_briefing(
        summary=_json(
            changes=[
                {
                    "event_id": "venue",
                    "claim": "The venue is not the North Hall",
                    "evidence_refs": [SOURCE["ref"]],
                }
            ]
        ),
        evidence=[{**SOURCE, "excerpt": "The venue is the North Hall."}],
        previous=None,
        importance_rules={},
    )
    assert result["changes"] == []
    assert result["unconfirmed"][0]["evidence_check"] == "unsupported_or_excerpt_missing"


class _WatchMailLLM:
    """Scripted turn client: it chooses real mail tools, then cites their returned records."""

    def __init__(self, counter):
        self.counter = counter
        self.requested_tools: list[str] = []
        self.answer_requests: list[dict] = []

    def complete_text(self, *, system_prompt, user_prompt, prompt_summary, **_kwargs):
        stage = _kwargs.get("metadata", {}).get("stage")
        if stage == "route":
            content = json.dumps(
                {"selected_package": "mail", "reason": "Read the authorized mailbox."}
            )
        elif stage == "tool_result_check":
            content = json.dumps(
                {
                    "status": "accepted",
                    "message": "Tool result recorded.",
                    "remaining_work": "Continue.",
                }
            )
        elif stage == "decision":
            observations = json.loads(user_prompt)["observations"]
            if not observations:
                tool_name = "mail.search"
                tool_input = {"query": "Daily watch", "limit": 3}
                operation = {
                    "type": "tool_call",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "final_answer": None,
                    "reason": "Search the authorized mailbox.",
                    "confidence": "high",
                }
            elif (
                observations[-1]["tool_name"] == "mail.search"
                and observations[-1]["result"]["status"] == "completed"
            ):
                messages = observations[-1]["result"]["output"].get("messages", [])
                tool_name = "mail.load_messages"
                tool_input = {"message_ids": [row["message_id"] for row in messages[:3]]}
                operation = {
                    "type": "tool_call",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "final_answer": None,
                    "reason": "Load the bounded matching messages.",
                    "confidence": "high",
                }
            else:
                operation = {
                    "type": "final_answer",
                    "final_answer": None,
                    "reason": "The available mail observations are sufficient.",
                    "confidence": "high",
                }
            if operation["type"] == "tool_call":
                self.requested_tools.append(operation["tool_name"])
            content = json.dumps(
                {"operation": operation, "assistant_message": "Checking authorized mail."}
            )
        elif stage == "answer":
            self.answer_requests.append(
                {
                    "require_json": _kwargs.get("require_json"),
                    "response_mode": getattr(
                        _kwargs.get("response_mode"), "value", _kwargs.get("response_mode")
                    ),
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "metadata": _kwargs.get("metadata"),
                }
            )
            observations = json.loads(user_prompt)["observations"]
            loaded = next(
                (
                    item["result"]["output"].get("messages", [])
                    for item in reversed(observations)
                    if item.get("tool_name") == "mail.load_messages"
                    and item["result"].get("status") == "completed"
                ),
                [],
            )
            changes = []
            for message in loaded:
                body = str(message.get("body_text") or "")
                sentence = body.splitlines()[0][:180].strip()
                if not sentence:
                    continue
                message_id = message["message_id"]
                changes.append(
                    {
                        "event_id": message_id,
                        "title": message.get("subject", "Mail update"),
                        "claim": sentence,
                        "current_observation": sentence,
                        "evidence_refs": [message_id],
                    }
                )
            content = json.dumps(
                {
                    "summary": "Mailbox observations",
                    "changes": changes,
                    "unchanged": [],
                    "unconfirmed": [],
                    "decisions": [],
                }
            )
        else:
            raise AssertionError(f"Unexpected prompt stage: {stage}")
        input_tokens = self.counter.count_request(system_prompt, user_prompt).count
        output_tokens = self.counter.count_text(content).count
        return LLMResponse(
            provider="watch_scenario_fake",
            status="completed",
            content=content,
            prompt_summary=prompt_summary,
            usage={"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                   "total_tokens": input_tokens + output_tokens},
        )


@pytest.mark.parametrize("token_budget", [None, 1], ids=["evidence_flow", "token_exhausted"])
def test_daily_mail_watch_real_tool_executor_child_and_evidence_flow(
    tmp_path, monkeypatch, token_budget, deterministic_prompt_counter,  # noqa: F811 - pytest fixture
):
    if token_budget is not None:
        monkeypatch.setattr(watch_execution, "WATCH_MAX_TOKENS", token_budget)
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    get_settings.cache_clear()
    from app.api.main import create_app

    app = create_app()
    runtime = app.state.runtime
    now = datetime.now(UTC)
    imported = runtime.import_mail(
        account=MailAccountInput(provider="local_json", email_address="watch@example.test"),
        messages=[
            MailMessageInput(
                external_id="daily-watch-001",
                folder="Inbox",
                subject="Daily watch: release freeze",
                sender="updates@example.test",
                to=["watch@example.test"],
                received_at=now.isoformat(),
                body_text="Release freeze starts on October 12.",
            )
        ],
    )
    account_id = imported.account_id
    source_id = runtime.mail_knowledge_mirror.source_id_for_account(account_id)
    service = runtime.watch_service
    watch = service.create(
        WatchInput(
            title="Release mail",
            goal="Track Daily watch mail changes",
            timezone="UTC",
            daily_time="08:00",
            categories=["mail"],
            scope={"source_ids": [source_id], "account_ids": [account_id]},
        )
    )
    client = _WatchMailLLM(deterministic_prompt_counter)
    runtime.agent_turn_loop.llm_client = client
    # Scripted provider usage and request estimates must use the same units.
    # Missing usage is deliberately conservative in production, not a fake
    # provider's permission to finish an arbitrarily long watch for free.
    runtime.agent_turn_loop._prompt_counters[None] = deterministic_prompt_counter
    runtime.agent_turn_loop._selected_session_counter = lambda: deterministic_prompt_counter
    scheduler = WatchScheduler(runtime, service)
    captured_views = []
    original_run_child = runtime.run_child_agent_async

    async def capture_view(**kwargs):
        captured_views.append(kwargs["views"].tool)
        return await original_run_child(**kwargs)

    runtime.run_child_agent_async = capture_view

    if token_budget == 1:
        service.create_occurrence(watch["watch_id"], now - timedelta(days=3))
        assert scheduler.run_one(owner="watch-scenario")
        occurrence = service.get_occurrence(watch["watch_id"], now - timedelta(days=3))
        assert occurrence["status"] == "failed"
        assert service.list_briefings(watch_id=watch["watch_id"]) == []
        child = runtime.agent_run_manager.get_run(captured_views[0].child_run_id)
        assert child.status.value == "failed"
        assert child.error == "Child Agent exceeded its token budget."
        assert client.requested_tools == []
        assert client.answer_requests == []
        assert not any(
            event.type == "llm_started"
            for event in runtime.agent_run_manager.list_events(child.run_id)
        )
        return

    def execute(slot):
        occurrence = service.create_occurrence(watch["watch_id"], slot)
        assert scheduler.run_one(owner="watch-scenario")
        current = service.get_occurrence(watch["watch_id"], slot)
        parent = runtime.agent_run_manager.get_run(current["run_id"])
        assert current["status"] == "succeeded", parent.error if parent else current
        return next(
            briefing for briefing in service.list_briefings(watch_id=watch["watch_id"])
            if briefing["occurrence_id"] == occurrence["occurrence_id"]
        )

    first = execute(now - timedelta(days=3))
    assert len(first["changes"]) == 1
    assert "October 12" in first["summary"]
    assert "mail_message:" in first["summary"]
    assert first["changes"][0]["evidence_check"] == "excerpt_match"

    second = execute(now - timedelta(days=2))
    assert len(second["changes"]) == 0
    assert len(second["unchanged"]) == 1

    runtime.import_mail(
        account=MailAccountInput(provider="local_json", email_address="watch@example.test"),
        messages=[
            MailMessageInput(
                external_id="daily-watch-002",
                folder="Inbox",
                subject="Daily watch: venue update",
                sender="updates@example.test",
                to=["watch@example.test"],
                received_at=(now + timedelta(minutes=1)).isoformat(),
                body_text="The launch venue is the North Hall.",
            )
        ],
    )
    third = execute(now - timedelta(days=1))
    assert {item["title"] for item in third["changes"]} == {"Daily watch: venue update"}
    assert {item["title"] for item in third["unchanged"]} == {"Daily watch: release freeze"}

    original_search = runtime.mail_knowledge_mirror.search

    def failed_search(**_kwargs):
        raise RuntimeError("simulated local index failure")

    runtime.mail_knowledge_mirror.search = failed_search
    failed = execute(now)
    runtime.mail_knowledge_mirror.search = original_search
    assert failed["changes"] == []
    assert any("mail.search:failed" in item["reason"] for item in failed["unconfirmed"])
    assert len({item["session_id"] for item in (first, second, third, failed)}) == 4

    assert len(captured_views) == 4
    for view in captured_views:
        assert view.side_effect_level.value == "read"
        assert set(view.allowed_source_ids) == {source_id}
        assert set(view.allowed_account_ids) == {account_id}
        assert "mail.search" in view.allowed_tools
        assert "mail.load_messages" in view.allowed_tools
        assert "mail.sync" not in view.allowed_tools
        assert "matter.create" not in view.allowed_tools
        child_events = runtime.agent_run_manager.list_events(view.child_run_id)
        assert any(event.payload.get("tool_name") == "mail.search" for event in child_events)
    assert client.requested_tools.count("mail.search") == 4
    assert client.requested_tools.count("mail.load_messages") == 3
    assert len(client.answer_requests) == 4
    for request in client.answer_requests:
        assert request["require_json"] is True
        assert request["response_mode"] == "json"
        assert "overrides ordinary prose-format defaults" in request["system_prompt"]
        assert "Do not wrap the answer in JSON" not in request["system_prompt"]
        answer_prompt = json.loads(request["user_prompt"])
        assert "Return one JSON object" in answer_prompt["output_contract"]
        schema = request["metadata"]["output_schema"]
        assert schema["required"] == [
            "summary",
            "changes",
            "unchanged",
            "unconfirmed",
            "decisions",
        ]
