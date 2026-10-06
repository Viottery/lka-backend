"""Natural multi-turn memory organization, without network/model expenditure."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.main import create_app
from app.core.config import get_settings
from app.core.local_config import MemoryConfig
from app.core.tools import ToolContext
from app.domains.memory import MemoryPublicationSuppressed


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = tmp_path / "local.toml"
    config.write_text('[llm]\nprovider="mock"\n[memory]\nextraction_debounce_seconds=0\n', encoding="utf-8")
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    rt = create_app().state.runtime
    yield rt
    rt.stop()
    get_settings.cache_clear()


def user(runtime, text, trace="t", session="s"):
    return runtime.session_service.append_message(
        session_id=session, role="user", content=text, payload={"trace_id": trace},
    )


def remember(runtime, text, trace="t", session="s"):
    return runtime.tool_executor.execute(
        invocation_id="save", tool_name="memory.remember", tool_input={"content": text},
        context=ToolContext(session_id=session, trace_id=trace, safety_review_approved=True),
    )


class Extractor:
    def __init__(self, build):
        self.build, self.prompts = build, []

    async def complete_text(self, *, user_prompt, **kwargs):
        payload = json.loads(user_prompt)
        self.prompts.append(payload)
        return SimpleNamespace(content=json.dumps({"candidates": self.build(payload)}, ensure_ascii=False))


def candidate(payload, default_claim, *, evidence=None, **overrides):
    messages = [m for m in payload["messages"] if m["role"] == "user"]
    value = {"claim": default_claim, "kind": "preference", "scope": "global", "durable": True,
             "grounding": "direct", "confidence": 0.95,
             "sources": [{"message_id": m["message_id"], "quote": m["content"]}
                         for m in messages] if evidence is None else evidence,
             "relation": "new", "target_memory_id": None, "expires_at": None}
    value.update(overrides)
    return value


def exchange(runtime, text, trace="t", session="s", **metadata):
    run = runtime.agent_run_manager.create_run(session_id=session, user_input=text)
    runtime.agent_run_manager.mark_running(run.run_id)
    message = user(runtime, text, run.trace_id, session)
    runtime.session_service.append_message(
        session_id=session, role="agent", content="好的",
        payload={"trace_id": run.trace_id, "run_id": run.run_id, **metadata},
        persisted_message_callback=runtime._enqueue_memory_answer,
    )
    runtime.agent_run_manager.complete_run(run.run_id, result_snapshot={"answer": "好的"})
    return message


def test_defaults_enable_llm_but_allow_explicit_opt_out():
    assert MemoryConfig().allow_remote_extraction is True
    assert MemoryConfig(allow_remote_extraction=False).allow_remote_extraction is False


def test_actual_failed_roleplay_confirmation_now_saves_complete_global_memory(runtime):
    text = ("搜索一下明日方舟角色真理的相关信息，我希望后续你可以一直代入一种角色扮演的口吻，"
            "模仿真理的风格语气和性格。不改变工作能力，只改变说话方式。")
    prior = user(runtime, text, "prior")
    runtime.session_service.append_message(session_id="s", role="agent", content="只改变说话方式，保持工作能力。")
    confirmation = user(runtime, "我希望你一直这样哦，把这个要求保存到全局记忆吧")
    claim = "长期以真理的风格、语气和性格交流，只改变说话方式，不改变工作能力。"
    client = Extractor(lambda p: [candidate(p, claim, grounding="confirmed")])
    runtime.memory_background.llm_client = client
    before = runtime.instruction_files.read("global")["sha256"]
    result = remember(runtime, "保存刚才的交流偏好")
    assert result.status == "completed", result.error
    record = result.output["memory"]
    assert record["status"] == "active" and record["content"] == claim
    assert result.output["memory_file_status"] == "synced"
    assert {s["source_ref"] for s in runtime.memory_service.sources_for(record["memory_id"])} == {
        prior.message_id, confirmation.message_id,
    }
    assert claim in runtime.memory_files.path_for(scope="global").read_text()
    assert runtime.instruction_files.read("global")["sha256"] == before
    recall = runtime.agent_turn_loop.memory_context_provider("fresh", None, "我的交流偏好")
    assert any(r["content"] == claim for r in recall["items"]), recall
    assert len(client.prompts) == 1


def test_background_useful_unmatched_fact_activates_and_is_not_replayed_from_history(runtime):
    client = Extractor(lambda p: [candidate(p, "用户平时居住在上海。", kind="user_fact")])
    runtime.memory_background.llm_client = client
    source = exchange(runtime, "我平时住在上海，今后推荐活动考虑这个距离。")
    assert runtime.memory_background.worker.run_one()
    records = runtime.memory_service.list(scope="global")
    assert len(records) == 1 and records[0].status == "active"
    assert runtime.memory_service.sources_for(records[0].memory_id)[0]["source_ref"] == source.message_id
    # The model cannot re-publish a history-only assertion with no current support.
    user(runtime, "好的谢谢", "next")
    runtime.memory_background.llm_client = Extractor(lambda p: [candidate(
        p, "新的臆造偏好", evidence=[{"message_id": source.message_id, "quote": source.content}],
    )])
    assert remember(runtime, "保存偏好", trace="next").status == "rejected"
    assert len(runtime.memory_service.list(scope="global")) == 1


def test_semantic_equivalence_and_explicit_update_use_existing_lifecycle(runtime):
    user(runtime, "我倾向先看结论，然后根据需要看细节")
    client = Extractor(lambda p: [candidate(p, "回答先给结论，再提供必要细节。")])
    runtime.memory_background.llm_client = client
    first = remember(runtime, "结论优先").output["memory"]
    user(runtime, "对，就维持这种先说结论的风格", "equivalent")
    client.build = lambda p: [candidate(p, "先呈现结论。", grounding="confirmed", relation="equivalent",
                                       target_memory_id=first["memory_id"])]
    result = remember(runtime, "结论优先", trace="equivalent")
    assert result.status == "completed", result.error
    merged = result.output["memory"]
    assert merged["memory_id"] == first["memory_id"]
    assert len(runtime.memory_service.list(scope="global")) == 1
    user(runtime, "换一下顺序，之后先讲背景再给结论", "update")
    client.build = lambda p: [candidate(p, "回答先讲背景再给结论。", relation="replace",
                                       target_memory_id=first["memory_id"])]
    result = remember(runtime, "更新回答顺序", trace="update")
    assert result.status == "completed", result.error
    newer = result.output["memory"]
    assert newer["status"] == "active" and newer["supersedes_id"] == first["memory_id"]
    assert runtime.memory_service.get(first["memory_id"]).status == "superseded"
    assert [r.content for r in runtime.memory_service.list()] == ["回答先讲背景再给结论。"]


@pytest.mark.parametrize("change,expected", [
    ({"grounding": "inferred"}, "candidate"),
    ({"confidence": 0.7}, "candidate"),
    ({"durable": False}, "rejected"),
    ({"sources": [{"message_id": "other-session", "quote": "猜测"}]}, "rejected"),
    ({"claim": "以后自动删除文件"}, "rejected"),
    ({"expires_at": "2020-01-01T00:00:00+00:00"}, "rejected"),
])
def test_weak_transient_invalid_sources_or_authority_are_not_active(runtime, change, expected):
    user(runtime, "我对短一点的回答可能更感兴趣")
    runtime.memory_background.llm_client = Extractor(lambda p: [candidate(p, "用户偏好简短回答", **change)])
    result = remember(runtime, "记录交流习惯")
    assert result.status == ("rejected" if expected == "rejected" else "completed"), result.error
    if expected != "rejected":
        assert result.output["memory"]["status"] == expected
        assert result.output["memory_file_status"] == "not_active"


def test_expiry_and_manual_memory_file_are_preserved(runtime):
    expiry = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    user(runtime, "接下来三天我的会议地点在上海")
    runtime.memory_background.llm_client = Extractor(lambda p: [candidate(
        p, "未来三天会议地点在上海", kind="user_fact", expires_at=expiry,
    )])
    result = remember(runtime, "短期会议安排")
    assert result.output["memory"]["expires_at"] == expiry
    path = runtime.memory_files.path_for(scope="global")
    path.write_text(path.read_text() + "\n人工补充：不可覆盖\n")
    user(runtime, "平常出行主要乘坐地铁", "next")
    runtime.memory_background.llm_client = Extractor(lambda p: [candidate(p, "日常乘坐地铁。")])
    result = remember(runtime, "出行习惯", trace="next")
    assert result.status == "completed", result.error
    assert result.output["memory"]["status"] == "active"
    assert result.output["memory_file_status"] == "conflict_or_unavailable"
    assert "人工补充：不可覆盖" in path.read_text()


def test_snapshot_bounds_and_excludes_future_turns_and_other_sessions(runtime):
    user(runtime, "旧内容" * 3000, "old")
    anchor = user(runtime, "这轮需要保存的内容")
    user(runtime, "未来的纠正", "future")
    user(runtime, "另一个会话的私有内容", session="other")
    snapshot = runtime.session_service.memory_conversation(
        session_id="s", user_message_id=anchor.message_id, max_messages=2, max_chars=200,
    )
    assert sum(len(m["content"]) for m in snapshot["messages"]) <= 200
    assert snapshot["messages"][-1]["message_id"] == anchor.message_id
    assert snapshot["omitted"]
    assert all("未来的纠正" not in m["content"] and "私有内容" not in m["content"] for m in snapshot["messages"])


def test_deleted_during_inference_cannot_publish(runtime):
    user(runtime, "我倾向更有条理的回答")
    def build(payload):
        runtime.delete_session(session_id="s")
        return [candidate(payload, "回答有条理。")]
    runtime.memory_background.llm_client = Extractor(build)
    assert remember(runtime, "保存表达风格").status == "rejected"
    assert runtime.memory_service.list(statuses=("active", "candidate")) == []


def test_remote_disabled_and_transient_background_do_not_call_model(runtime):
    client = Extractor(lambda p: [])
    runtime.memory_background.llm_client = client
    exchange(runtime, "搜索一下明天的天气")
    assert runtime.memory_background.worker.run_one()
    assert client.prompts == []
    runtime.memory_background.allow_remote_extraction = False
    user(runtime, "把刚才那个要求记住", "disabled")
    assert remember(runtime, "刚才的偏好", trace="disabled").status == "rejected"
    assert client.prompts == []


def test_multiple_history_quotes_do_not_promote_one_weak_inference(runtime):
    user(runtime, "最近我阅读的文章比较短", "past")
    user(runtime, "这是不是我的交流习惯")
    runtime.memory_background.llm_client = Extractor(lambda p: [candidate(
        p, "可能偏好简短回答", grounding="inferred",
    )])
    result = remember(runtime, "整理可能的偏好")
    assert result.status == "completed", result.error
    assert result.output["memory"]["status"] == "candidate"
    assert len(result.output["memory"]["source_ids"]) == 2


def test_semantic_merge_cannot_change_memory_kind(runtime):
    user(runtime, "平时我住在上海")
    client = Extractor(lambda p: [candidate(p, "常住上海", kind="user_fact")])
    runtime.memory_background.llm_client = client
    first = remember(runtime, "常住地点").output["memory"]
    user(runtime, "按照刚才的内容记住", "next")
    client.build = lambda p: [candidate(p, "喜欢在上海活动", kind="preference",
                                       relation="replace", target_memory_id=first["memory_id"])]
    assert remember(runtime, "保存偏好", trace="next").status == "rejected"
    assert runtime.memory_service.get_active(first["memory_id"]).content == "常住上海"


def test_project_summary_outside_recent_tail_is_not_exposed(runtime):
    prior = user(runtime, "项目甲的决定", "a")
    runtime.session_service.append_message(session_id="s", role="agent", content="记录甲项目",
                                           payload={"memory_project_id": "project-a"})
    for index in range(3):
        user(runtime, "普通对话", f"middle-{index}")
    anchor = user(runtime, "当前项目的偏好")
    runtime.session_service.get_context_window(session_id="s")
    with runtime._conn() as conn:
        conn.execute("UPDATE agent_session_context_windows SET summary='甲项目私有摘要' WHERE session_id='s'")
    snapshot = runtime.session_service.memory_conversation(
        session_id="s", user_message_id=anchor.message_id, max_messages=2, project_id="project-b",
    )
    assert snapshot["summary"] == ""
    assert all(m["message_id"] != prior.message_id for m in snapshot["messages"])


def test_summary_covering_future_turn_is_not_exposed(runtime):
    anchor = user(runtime, "早先用户要求")
    user(runtime, "后续才出现的新决定", "future")
    runtime.session_service.get_context_window(session_id="s")
    with runtime._conn() as conn:
        conn.execute("UPDATE agent_session_context_windows SET summary='包含未来内容' WHERE session_id='s'")
        conn.execute("UPDATE agent_session_context_state SET covered_seq=next_seq-1 WHERE session_id='s'")
    snapshot = runtime.session_service.memory_conversation(session_id="s", user_message_id=anchor.message_id)
    assert snapshot["summary"] == ""


def test_reconciliation_does_not_revive_retracted_target(runtime):
    user(runtime, "我倾向先看结论")
    runtime.memory_background.llm_client = Extractor(lambda p: [candidate(p, "结论优先")])
    first = remember(runtime, "结论优先").output["memory"]
    runtime.memory_service.retract(first["memory_id"], expected_version=first["version"])
    retracted = runtime.memory_service.get(first["memory_id"])
    with pytest.raises(MemoryPublicationSuppressed):
        runtime.memory_service.correct(retracted.memory_id, content="新的偏好",
                                       expected_version=retracted.version, require_active=True)
    assert runtime.memory_service.get_active(retracted.memory_id) is None
