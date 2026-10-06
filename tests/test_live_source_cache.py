"""Live source reuse gates are trusted implementation code, never output flags."""

from types import SimpleNamespace

from app.core.agent_turn import AgentTurnLoop
from app.core.tools import ToolContext


def cached(loop, monkeypatch):
    monkeypatch.setattr("app.core.agent_turn.cache_scope_compatible", lambda *args, **kwargs: True)
    return AgentTurnLoop._cached_tool_observations_from_session(
        loop, session_id="s", context=ToolContext(session_id="s"))


def loop_with(tool):
    event = {"tool_name": "read", "input": {}, "cache_metadata": {},
             "result": {"status": "completed", "output": {"allow_cache": True, "secret": "old evidence"}}}
    return SimpleNamespace(
        _cacheable_tool_names=lambda: {"read"},
        session_service=SimpleNamespace(get_session=lambda **kwargs: SimpleNamespace(
            messages=[SimpleNamespace(payload={"tool_events": [event]}, created_at="2026-10-05T00:00:00+00:00")])),
        _current_observation_cache_scope=lambda context: {},
        tool_executor=SimpleNamespace(registry=SimpleNamespace(get_tool=lambda name: tool)),
        _cached_observation_for_decision_prompt=lambda **kwargs: kwargs,
    )


def test_registered_live_cache_gate_rejects_old_body_despite_output_flag(monkeypatch):
    tool = SimpleNamespace(allow_cached_observation=lambda **kwargs: False)
    assert cached(loop_with(tool), monkeypatch) == []


def test_cache_gate_failures_and_non_boolean_approvals_fail_closed(monkeypatch):
    def unavailable(**kwargs):
        raise ValueError("cannot validate")

    assert cached(loop_with(SimpleNamespace(allow_cached_observation=unavailable)), monkeypatch) == []
    assert cached(loop_with(SimpleNamespace(allow_cached_observation=lambda **kwargs: "yes")), monkeypatch) == []


def test_ordinary_registered_tool_cache_behavior_is_unchanged(monkeypatch):
    assert len(cached(loop_with(SimpleNamespace()), monkeypatch)) == 1


def test_parent_evidence_rehydration_preserves_empty_provider_account_grant():
    from app.core.runtime import LocalKnowledgeAgentRuntime

    captured = {}

    def load(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(chunks=[])

    event = {"result": {"status": "completed", "output": {"results": [{
        "policy_decision": "redacted", "chunk_id": "live_chunk", "source_id": "source",
        "source_ref": "chat-message://evidence"}]}}}
    runtime = SimpleNamespace(
        agent_run_store=SimpleNamespace(list_completed_tool_results=lambda **kwargs: [event]),
        knowledge_service=SimpleNamespace(load_chunks=load))
    assert LocalKnowledgeAgentRuntime._parent_knowledge_evidence_candidates(
        runtime, "parent", source_ids=("source",), account_ids=()) == ()
    assert captured["provider_account_ids"] == []
