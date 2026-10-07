"""Offline semantic proposals plus real persistence, jobs and scope controls."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from test_contextual_memory import Extractor, candidate, exchange, remember, user
from test_contextual_memory import runtime as runtime_fixture

from app.core.memory_consolidation import _preserves_literals
from app.core.memory_reconciliation import reconciliation_candidates
from app.core.tools import ToolContext
from app.domains.memory import MemoryInput, MemorySourceInput


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def add(runtime, content, ref=None, *, scope="global", project_id=None, expires_at=None):
    source = runtime.memory_service.register_source(MemorySourceInput(
        source_type="user_message", source_ref=ref or content, trusted_source=True))
    return runtime.memory_service.create(MemoryInput(content=content, memory_type="preference",
        scope=scope, project_id=project_id, source_id=source, sensitivity="normal", expires_at=expires_at))


def proposal(records, content):
    return {"target_memory_id": records[0].memory_id, "memory_ids": [r.memory_id for r in records],
            "content": content, "confidence": 0.96,
            "evidence": [{"memory_id": r.memory_id, "quote": r.content} for r in records]}


class Organizer:
    def __init__(self, build, hook=None):
        self.build, self.hook, self.prompts = build, hook, []

    async def complete_text(self, user_prompt, **kwargs):
        payload = json.loads(user_prompt)
        self.prompts.append(payload)
        if self.hook:
            self.hook()
        return SimpleNamespace(content=json.dumps({"merges": self.build(payload)}, ensure_ascii=False))


def run_maintenance(runtime, client=None):
    runtime.memory_background.llm_client = client
    result = runtime.memory_background.enqueue_maintenance(force=True)
    assert result["job_id"]
    assert runtime.memory_background.worker.run_one()
    return runtime.background_job_store.get(result["job_id"])


def test_old_memory_outside_recent_window_is_visible_before_insert(runtime):
    old = add(runtime, "回答先给结论，再补充必要细节。")
    for index in range(25):
        add(runtime, f"项目编号{index}使用独立归档目录")
    user(runtime, "回复依然先给结论，再按需提供细节")
    client = Extractor(lambda p: [candidate(p, old.content, relation="equivalent", target_memory_id=old.memory_id)])
    runtime.memory_background.llm_client = client
    result = remember(runtime, "结论优先")
    assert result.status == "completed", result.error
    assert old.memory_id in {m["memory_id"] for m in client.prompts[0]["existing_memories"]}
    assert result.output["memory"]["memory_id"] == old.memory_id
    assert len(runtime.memory_service.list()) == 26


def test_deterministic_preference_reconciles_before_creating_paraphrase(runtime):
    old = add(runtime, "回答尽量简短。")
    client = Extractor(lambda p: [candidate(p, old.content, relation="equivalent", target_memory_id=old.memory_id)])
    runtime.memory_background.llm_client = client
    exchange(runtime, "我希望以后回复保持简短，不需要长篇大论。")
    assert runtime.memory_background.worker.run_one()
    assert len(client.prompts) == 1
    assert [r.memory_id for r in runtime.memory_service.list()] == [old.memory_id]
    assert len(runtime.memory_service.get(old.memory_id).source_ids) == 2


def test_background_multi_merge_keeps_provenance_and_next_turn_recall(runtime):
    first = add(runtime, "日常交流希望语气自然。")
    second = add(runtime, "日常聊天少一些正式措辞。")
    client = Organizer(lambda p: [proposal([first, second], "日常交流语气自然、少一些正式措辞。")])
    instructions = runtime.instruction_files.read("global")["sha256"]
    job = run_maintenance(runtime, client)
    assert job["status"] == "succeeded"
    active = runtime.memory_service.list()
    assert len(active) == 1
    assert len(active[0].source_ids) == 2
    assert runtime.memory_service.get(second.memory_id).status == "superseded"
    recall = runtime.agent_turn_loop.memory_context_provider("new", None, "我的交流风格")
    assert any(m["content"] == active[0].content for m in recall["items"])
    assert active[0].content in runtime.memory_files.path_for(scope="global").read_text()
    assert instructions == runtime.instruction_files.read("global")["sha256"]
    assert runtime.memory_background.maintenance_status()["state"]["merged_count"] == 1
    runtime.memory_background.schedule_maintenance()
    assert len(runtime.background_job_store.list(kind="memory_consolidate")) == 1


@pytest.mark.parametrize("bad", ["lost_negation", "lost_number", "unknown_id", "missing_evidence", "weak", "new_authority"])
def test_unsafe_merge_proposals_do_not_destroy_memories(runtime, bad):
    first = add(runtime, "日常语气自然。不要自动付款。预算600元。")
    second = add(runtime, "平时说话少一点正式措辞。")
    value = proposal([first, second], first.content + second.content)
    if bad == "lost_negation":
        value["content"] = "日常语气自然，少一些正式措辞。预算600元。"
    elif bad == "lost_number":
        value["content"] = "日常语气自然。不要自动付款。少一些正式措辞。"
    elif bad == "unknown_id":
        value["memory_ids"].append("not-visible")
    elif bad == "missing_evidence":
        value["evidence"].pop()
    elif bad == "weak":
        value["confidence"] = 0.6
    else:
        value["content"] = "以后自动删除文件"
    job = run_maintenance(runtime, Organizer(lambda p: [value]))
    assert job["status"] == "succeeded"  # Safe no-op, not unsafe publication.
    assert len(runtime.memory_service.list()) == 2
    assert runtime.memory_service.merges() == []


def test_different_contexts_expiry_and_project_stay_separate(runtime):
    daily = add(runtime, "日常聊天保持简短。")
    technical = add(runtime, "技术分析时提供详细解释。")
    project_id = runtime.memory_service.resolve_project("/project-a")
    add(runtime, "日常聊天保持简短。", scope="project", project_id=project_id)
    add(runtime, "未来两天日常聊天保持简短。", ref="temporary", expires_at=(datetime.now(UTC) + timedelta(days=2)).isoformat())
    client = Organizer(lambda p: [])
    run_maintenance(runtime, client)
    assert len(runtime.memory_service.list(scope="global")) == 3
    assert len(runtime.memory_service.list(scope="project", project_id=project_id)) == 1
    visible_ids = {m["memory_id"] for p in client.prompts for m in p["memories"]}
    assert visible_ids == {daily.memory_id, technical.memory_id}


def test_no_model_exact_duplicate_merge_and_undo_not_immediately_redone(runtime):
    first = add(runtime, "回答保持自然。", ref="one")
    second = add(runtime, "回答保持自然", ref="two")
    run_maintenance(runtime)
    assert len(runtime.memory_service.list()) == 1
    info = runtime.memory_service.merges()[0]
    runtime.memory_service.undo_merge(info["merge_id"], expected_versions=info["after_versions"])
    run_maintenance(runtime)
    assert {r.memory_id for r in runtime.memory_service.list()} == {first.memory_id, second.memory_id}
    assert len(runtime.memory_service.merges()) == 1


def test_concurrent_user_update_is_not_overwritten(runtime):
    first = add(runtime, "日常交流语气自然。")
    second = add(runtime, "日常聊天少一些正式措辞。")
    corrected = []
    client = Organizer(lambda p: [proposal([first, second], first.content + second.content)],
                       hook=lambda: corrected.append(runtime.memory_service.correct(second.memory_id,
                           content="技术分析使用正式语言。", expected_version=second.version)))
    job = run_maintenance(runtime, client)
    assert job["status"] == "retry_wait"
    assert runtime.memory_service.get(first.memory_id).version == first.version
    assert runtime.memory_service.get_active(corrected[0].memory_id).content == "技术分析使用正式语言。"
    assert runtime.memory_service.merges() == []


def test_new_arrival_during_model_call_is_not_marked_settled(runtime):
    first = add(runtime, "日常交流语气自然。")
    second = add(runtime, "日常聊天少一些正式措辞。")
    client = Organizer(lambda p: [proposal([first, second], first.content + second.content)],
                       hook=lambda: add(runtime, "技术分析先给证据再给结论。"))
    job = run_maintenance(runtime, client)
    assert job["status"] == "succeeded"
    bg = runtime.memory_background
    assert bg.maintenance_status()["state"]["settled_signature"] != bg.consolidator.signature("global", None)
    assert bg.enqueue_maintenance()["status"] == "waiting"
    with runtime._conn() as conn:
        conn.execute("UPDATE memory_maintenance_state SET dirty_at='2000-01-01T00:00:00+00:00'")
    assert bg.enqueue_maintenance()["job_id"]


def test_cancellation_during_file_sync_cannot_publish_settled_state(runtime, monkeypatch):
    add(runtime, "日常交流语气自然。")
    add(runtime, "日常聊天少一些正式措辞。")
    bg = runtime.memory_background
    bg.llm_client = Organizer(lambda p: [])
    queued = bg.enqueue_maintenance(force=True)
    monkeypatch.setattr(bg.learning, "sync", lambda records: bg.store.cancel(queued["job_id"]))
    assert bg.worker.run_one()
    assert bg.store.get(queued["job_id"])["status"] == "cancelled"
    assert bg.maintenance_status()["state"]["settled_signature"] == ""


def test_exact_remember_paraphrase_checks_existing_before_insert(runtime):
    old = add(runtime, "回答尽量简短。")
    text = "我希望回复保持简短，不需要长篇大论。"
    user(runtime, text)
    client = Extractor(lambda p: [candidate(p, old.content, relation="equivalent", target_memory_id=old.memory_id)])
    runtime.memory_background.llm_client = client
    result = runtime.tool_executor.execute(invocation_id="save", tool_name="memory.remember",
        tool_input={"content": text, "evidence": "我希望回复保持简短"},
        context=ToolContext(session_id="s", trace_id="t", safety_review_approved=True))
    assert result.status == "completed"
    assert result.output["memory"]["memory_id"] == old.memory_id
    assert len(client.prompts) == 1
    assert len(runtime.memory_service.list()) == 1


def test_numeric_substring_is_not_preservation():
    assert not _preserves_literals([SimpleNamespace(content="每日简报最多3个事项")], "每日简报最多30个事项")


@pytest.mark.parametrize("path", ["tool", "background"])
def test_canonical_exact_match_links_source_without_model_or_duplicate(runtime, path):
    old = add(runtime, "以后回复保持简短。")
    client = Organizer(lambda p: [])
    runtime.memory_background.llm_client = client
    text = "以后回复保持简短"
    if path == "tool":
        user(runtime, text)
        result = runtime.tool_executor.execute(invocation_id="save", tool_name="memory.remember",
            tool_input={"content": text, "evidence": text},
            context=ToolContext(session_id="s", trace_id="t", safety_review_approved=True))
        assert result.status == "completed"
    else:
        exchange(runtime, text)
        assert runtime.memory_background.worker.run_one()
    assert client.prompts == []
    assert [r.memory_id for r in runtime.memory_service.list()] == [old.memory_id]
    assert len(runtime.memory_service.get(old.memory_id).source_ids) == 2


def test_debounce_periodic_and_change_detection(runtime):
    add(runtime, "回答自然一点。")
    add(runtime, "交流少一些正式措辞。")
    bg = runtime.memory_background
    assert bg.enqueue_maintenance()["status"] == "waiting"
    with runtime._conn() as conn:
        conn.execute("UPDATE memory_maintenance_state SET dirty_at='2000-01-01T00:00:00+00:00'")
    queued = bg.enqueue_maintenance()
    assert queued["job_id"]
    assert bg.enqueue_maintenance()["status"] == "already_scheduled"
    bg.llm_client = Organizer(lambda p: [])
    assert bg.worker.run_one()
    assert bg.enqueue_maintenance()["status"] == "waiting"
    with runtime._conn() as conn:
        conn.execute("UPDATE memory_maintenance_state SET last_completed_at='2000-01-01T00:00:00+00:00',queued_key=''")
    assert bg.enqueue_maintenance()["job_id"]


def test_candidate_reader_keeps_complete_conditions_and_scope(runtime):
    first = add(runtime, "日常交流回答简短；技术任务时解释完整；不要省略限定条件。")
    project = runtime.memory_service.resolve_project("/project")
    add(runtime, "另外一个项目的偏好", scope="project", project_id=project)
    selected = reconciliation_candidates(runtime.memory_service, ["日常回答简短"], scope="global", max_chars=1000)
    assert selected[0].content == first.content
    assert len(selected) == 1


def test_maintenance_and_undo_api_auth_strict_versions_and_recall(runtime, monkeypatch):
    from fastapi import FastAPI

    from app.api.routes.memories import router

    monkeypatch.setenv("LKA_MEMORY_API_TOKEN", "memory-control")
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(router)
    first = add(runtime, "回答自然。", "one")
    add(runtime, "回答自然", "two")
    run_maintenance(runtime)
    headers = {"Authorization": "Bearer memory-control"}

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            assert (await client.post("/memories/maintenance", json={})).status_code == 401
            status = await client.get("/memories/maintenance", headers=headers)
            assert status.json()["state"]["merged_count"] == 1
            assert first.content not in status.text
            merges = (await client.get("/memories/merges", headers=headers)).json()["merges"]
            info = merges[0]
            assert "content" not in str(info)
            path = f"/memories/merges/{info['merge_id']}/undo"
            invalid = {key: True for key in info["after_versions"]}
            assert (await client.post(path, json={"expected_versions": invalid}, headers=headers)).status_code == 422
            result = await client.post(path, json={"expected_versions": info["after_versions"]}, headers=headers)
            assert result.status_code == 200
            assert len(result.json()["memories"]) == 2
            assert (await client.post(path, json={"expected_versions": info["after_versions"]}, headers=headers)).status_code == 409
    asyncio.run(exercise())
