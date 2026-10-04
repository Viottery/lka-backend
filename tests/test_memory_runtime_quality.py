"""Actual Graph -> persisted answer -> threaded worker -> next-session recall.

Scripted inference proves wiring and publication policy, not model intelligence.
No direct memory creation, synthetic answer insertion, or network providers.
"""

import json
import sqlite3
import threading
import time
from collections import Counter

import pytest

from app.api.main import create_app
from app.core.config import get_settings
from app.core.llm import LLMResponse
from app.core.sessions import SessionWorkspace


class ScriptedClient:
    def __init__(self):
        self.calls = Counter()
        self.contexts = []
        self.search = False

    def complete_text(self, *, system_prompt, user_prompt, prompt_summary, **kwargs):
        self.calls[prompt_summary.split()[0]] += 1
        payload = json.loads(user_prompt)
        if "Choose at most one tool package" in system_prompt:
            value = {"selected_package": "memory" if self.search else None, "reason": "synthetic"}
        elif "Choose the next single action" in system_prompt:
            searched = any(o.get("tool_name") == "memory.search" for o in payload["observations"])
            op = ({"type": "tool_call", "tool_name": "memory.search",
                   "tool_input": {"query": "回答"}, "reason": "synthetic", "confidence": "high"}
                  if self.search and not searched else
                  {"type": "final_answer", "reason": "synthetic", "confidence": "high"})
            value = {"operation": op}
        elif "Tool Result Checker" in system_prompt:
            value = {"status": "completed", "reason": "synthetic", "retry_recommended": False}
        elif "Final Answer Writer" in system_prompt or "Answer the current user turn directly" in system_prompt:
            self.contexts.append(payload["session_context_window"])
            return self.response("已处理", prompt_summary)
        else:
            raise AssertionError("unexpected foreground inference stage")
        return self.response(json.dumps(value), prompt_summary)

    @staticmethod
    def response(content, summary):
        return LLMResponse(provider="scripted", model="offline", status="completed", content=content,
                           prompt_summary=summary, usage={"prompt_tokens": 20, "completion_tokens": 5})


@pytest.fixture
def harness(tmp_path, monkeypatch):
    config = tmp_path / "local.toml"
    config.write_text('''[llm]
provider="mock"
[agent]
orchestrator="langgraph"
mail_expert_enabled=false
[mail.outlook]
enabled=false
startup_sync_enabled=false
background_sync_enabled=false
[mail.imap]
enabled=false
[message_history]
enabled=false
background_enabled=false
[memory]
enabled=true
background_enabled=true
background_worker_count=1
extraction_debounce_seconds=0
allow_remote_extraction=false
''', encoding="utf-8")
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    runtime = create_app().state.runtime
    client = ScriptedClient()
    runtime.agent_turn_loop.llm_client = client
    worker = runtime.memory_background.worker
    worker.poll_seconds = 0.01
    started = time.monotonic()
    try:
        yield runtime, client, worker
    finally:
        runtime.stop()
        print(f"runtime_quality elapsed={time.monotonic()-started:.3f}s "
              f"scripted_calls={sum(client.calls.values())} "
              f"stages={dict(client.calls)} "
              f"jobs={Counter(j['status'] for j in runtime.background_job_store.list())}")
        get_settings.cache_clear()


def turn(runtime, session, text, *, project=None):
    if project:
        runtime.session_service.ensure_session(session_id=session)
        runtime.session_service.set_workspace(session_id=session, workspace=SessionWorkspace(
            path=str(project), backend_path=str(project), platform="linux",
        ))
    result = runtime.run_agent_turn(session_id=session, user_input=text)
    assert result.answer and result.run_id
    return result


def drain(runtime, worker):
    worker.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        jobs = runtime.background_job_store.list(kind="memory_extract")
        if jobs and all(j["status"] == "succeeded" for j in jobs):
            worker.stop(timeout=2)
            return
        time.sleep(0.01)
    worker.stop(timeout=2)
    raise AssertionError("memory worker failed to drain isolated queue")


def recalled(client):
    return client.contexts[-1]["recalled_memories"]["items"]


def test_ordinary_preference_reinforces_then_real_graph_tool_and_context_recall(harness):
    runtime, client, worker = harness
    turn(runtime, "first", "我喜欢简洁回答")
    drain(runtime, worker)
    records = runtime.memory_service.list(scope="global", statuses=("candidate",))
    assert len(records) == 1 and records[0].metadata["evidence"] == "我喜欢简洁回答"
    assert records[0].confidence == 0.78
    assert runtime.memory_service.list(scope="global") == []
    turn(runtime, "independent", "我喜欢简洁回答")
    drain(runtime, worker)
    active = runtime.memory_service.list(scope="global")
    assert len(active) == 1 and len(active[0].source_ids) == 2
    client.search = True
    result = turn(runtime, "new-session", "我之前的回答偏好是什么？")
    assert {i["memory_id"] for i in recalled(client)} == {active[0].memory_id}
    assert client.contexts[-1]["recent_messages"] == []
    events = [e for e in result.tool_events if e.tool_name == "memory.search"]
    assert len(events) == 1
    assert any(m["memory_id"] == active[0].memory_id for m in events[0].result["output"]["memories"])


def test_retraction_before_worker_does_not_publish_obsolete_queued_preference(harness):
    runtime, client, worker = harness
    turn(runtime, "pending", "我希望以后回答简洁")
    assert runtime.memory_service.list(scope="global") == []
    turn(runtime, "pending", "我不再喜欢简洁回答，请忘记简洁回答这个偏好")
    drain(runtime, worker)
    turn(runtime, "next-session", "我的回答偏好是什么？")
    assert recalled(client) == [], "queued earlier preference was published after explicit retraction"


def test_cross_session_correction_survives_runtime_reload_and_preserves_new_sources(harness):
    runtime, client, _worker = harness
    turn(runtime, "old-one", "我喜欢简洁回答")
    turn(runtime, "old-two", "我喜欢简洁回答")
    turn(runtime, "correction", "我不再喜欢简洁回答，请忘记简洁回答这个偏好")
    runtime.stop()
    restarted = create_app().state.runtime
    restarted.agent_turn_loop.llm_client = client
    restarted.memory_background.worker.poll_seconds = 0.01
    try:
        drain(restarted, restarted.memory_background.worker)
        assert restarted.memory_service.list(scope="global", statuses=("active", "candidate")) == []
        turn(restarted, "new-source", "我喜欢详细回答")
        drain(restarted, restarted.memory_background.worker)
        candidates = restarted.memory_service.list(scope="global", statuses=("candidate",))
        assert len(candidates) == 1 and "详细" in candidates[0].content
        assert restarted.memory_service.list(scope="global") == []
        turn(restarted, "new-source-two", "我喜欢详细回答")
        drain(restarted, restarted.memory_background.worker)
        turn(restarted, "recall", "我的回答偏好是什么？")
        assert len(recalled(client)) == 1 and "详细" in recalled(client)[0]["content"]
    finally:
        restarted.stop()


def test_inflight_project_extraction_cannot_publish_after_cross_session_withdrawal(harness, tmp_path):
    runtime, client, worker = harness
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    entered, release = threading.Event(), threading.Event()

    class DelayedExtractor:
        def complete_text(self, **kwargs):
            client.calls["background"] += 1
            entered.set()
            assert release.wait(3), "test provider was not released"
            return client.response(json.dumps({"candidates": [{
                "claim": "本项目使用 SQLite", "evidence": "本项目使用 SQLite",
                "kind": "project_decision", "explicit": True, "confidence": 0.95,
            }]}), "background")

    runtime.memory_background.llm_client = DelayedExtractor()
    runtime.memory_background.allow_remote_extraction = True
    turn(runtime, "old-a", "本项目使用 SQLite", project=a)
    worker.start()
    try:
        assert entered.wait(2), "actual worker did not dispatch extraction"
        turn(runtime, "withdraw-a", "请忘记本项目使用 SQLite这项记忆", project=a)
    finally:
        release.set()
    drain(runtime, worker)
    project_a = runtime.memory_service.resolve_project(a, create=False)
    assert runtime.memory_service.list(scope="project", project_id=project_a,
                                       statuses=("active", "candidate")) == []
    turn(runtime, "other-b", "本项目使用 SQLite", project=b)
    drain(runtime, worker)
    project_b = runtime.memory_service.resolve_project(b, create=False)
    assert len(runtime.memory_service.list(scope="project", project_id=project_b,
                                          statuses=("candidate",))) == 1
    turn(runtime, "recall-a", "SQLite 决策是什么？", project=a)
    assert recalled(client) == []


def test_project_decision_requires_real_confirmation_then_is_scope_filtered(harness, tmp_path):
    runtime, client, worker = harness
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    background_calls = []

    class ProjectExtractor:
        def complete_text(self, *, user_prompt, **kwargs):
            background_calls.append(user_prompt)
            client.calls["background"] += 1
            return client.response(json.dumps({"candidates": [{
                "claim": "本项目使用 SQLite", "evidence": "本项目使用 SQLite",
                "kind": "project_decision", "explicit": True, "confidence": 0.95,
            }]}), "background")

    runtime.memory_background.llm_client = ProjectExtractor()
    runtime.memory_background.allow_remote_extraction = True
    turn(runtime, "project-ordinary", "本项目使用 SQLite", project=a)
    drain(runtime, worker)
    project_id = runtime.memory_service.resolve_project(a, create=False)
    assert runtime.memory_service.list(scope="project", project_id=project_id) == []
    assert len(runtime.memory_service.list(scope="project", project_id=project_id, statuses=("candidate",))) == 1
    turn(runtime, "confirm-project", "记住：本项目使用 SQLite", project=a)
    drain(runtime, worker)
    assert len(runtime.memory_service.list(scope="project", project_id=project_id)) == 1
    turn(runtime, "global", "我希望以后回答简洁")
    drain(runtime, worker)
    turn(runtime, "fresh-a", "回答和 SQLite 决策是什么？", project=a)
    assert {i["scope"] for i in recalled(client)} == {"global", "project"}
    turn(runtime, "fresh-b", "回答和 SQLite 决策是什么？", project=b)
    assert {i["scope"] for i in recalled(client)} == {"global"}
    assert len(background_calls) == 1


def test_same_text_concurrent_corrections_use_exact_persisted_source(harness):
    runtime, _client, _worker = harness
    gate = runtime.agent_turn_loop.memory_pre_turn_callback
    first_entered, second_entered, release = (threading.Event() for _ in range(3))
    correction = "请忘记简洁回答这个偏好"
    ids, errors = [], []

    class DelayedGate:
        def on_persisted_user_message(self, workspace, text, message_id):
            if text == correction:
                ids.append(message_id)
                if len(ids) == 1:
                    first_entered.set()
                    assert second_entered.wait(3)
                else:
                    second_entered.set()
                    assert release.wait(3)
            return gate.on_persisted_user_message(workspace, text, message_id)

    def concurrent_turn(session):
        try:
            turn(runtime, session, correction)
        except Exception as exc:  # noqa: BLE001 - surface every worker-thread failure
            errors.append(exc)

    runtime.agent_turn_loop.memory_pre_turn_callback = DelayedGate()
    first = threading.Thread(target=concurrent_turn, args=("correction-first",))
    second = threading.Thread(target=concurrent_turn, args=("correction-second",))
    first.start()
    try:
        assert first_entered.wait(2)
        turn(runtime, "between", "我喜欢简洁回答")
        second.start()
        assert second_entered.wait(2)
        first.join(3)
        assert not first.is_alive() and not errors
        with sqlite3.connect(runtime.memory_service.db_path) as conn:
            expected = conn.execute("SELECT rowid FROM agent_session_messages WHERE message_id=?", (ids[0],)).fetchone()[0]
            assert conn.execute("SELECT watermark FROM memory_correction_fences WHERE scope_id='global'").fetchall() == [(expected,)]
        with pytest.raises(ValueError, match="does not match"):
            gate.on_persisted_user_message(None, correction + "wrong", ids[0])
    finally:
        release.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not errors


def test_specific_withdrawal_preserves_unrelated_published_and_queued_preferences(harness):
    runtime, _client, worker = harness
    turn(runtime, "published-running", "记住：我喜欢晚上跑步")
    drain(runtime, worker)
    turn(runtime, "pending-running", "我喜欢早上跑步")
    turn(runtime, "pending-answer", "我喜欢简洁回答")
    turn(runtime, "withdraw-answer", "我不再喜欢简洁回答，请忘记简洁回答这个偏好")
    drain(runtime, worker)
    records = runtime.memory_service.list(scope="global", statuses=("active", "candidate"))
    assert {r.content for r in records} == {"我喜欢晚上跑步", "喜欢早上跑步"}
    assert {r.metadata["evidence"] for r in records} == {"记住：我喜欢晚上跑步", "我喜欢早上跑步"}
