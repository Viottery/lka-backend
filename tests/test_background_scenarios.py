import json
import sqlite3
from types import SimpleNamespace

from app.api.main import create_app
from app.core.config import get_settings
from app.core.llm.errors import LLMTimeoutError


def _runtime(tmp_path, monkeypatch):
    config = tmp_path / "local.toml"
    config.write_text("[memory]\nextraction_debounce_seconds=0\n", encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    return create_app().state.runtime


def _exchange(runtime, text, *, session="daily", project=None):
    runtime.session_service.ensure_session(session_id=session)
    run = runtime.agent_run_manager.create_run(session_id=session, user_input=text)
    runtime.agent_run_manager.mark_running(run.run_id)
    runtime.session_service.append_message(
        session_id=session, role="user", content=text, payload={"trace_id": run.trace_id},
    )
    runtime.session_service.append_message(
        session_id=session, role="agent", content="好的", payload={
            "trace_id": run.trace_id, "run_id": run.run_id,
            "workspace_backend_path": str(project) if project else None,
            "memory_project_id": runtime.memory_service.resolve_project(project) if project else None,
        }, persisted_message_callback=runtime._enqueue_memory_answer,
    )
    runtime.agent_run_manager.complete_run(run.run_id, result_snapshot={"answer": "好的"})


def test_burst_is_one_durable_job_and_repeated_preference_learns_once(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    _exchange(runtime, "我更喜欢简洁回答")
    _exchange(runtime, "我更喜欢简洁回答")
    _exchange(runtime, "你好")
    jobs = runtime.background_job_store.list()
    assert len(jobs) == 1
    assert len(runtime.background_job_store.get(jobs[0]["job_id"], include_payload=True)["payload"]["message_ids"]) == 3
    assert runtime.memory_background.worker.run_one()
    records = runtime.memory_service.list(scope="global")
    assert len(records) == 1
    assert records[0].status == "active" and len(records[0].source_ids) == 2
    version = records[0].version
    runtime.memory_background.recover_missing_jobs()
    assert not runtime.memory_background.worker.run_one()
    assert runtime.memory_service.get(records[0].memory_id).version == version


def test_multiple_direct_preferences_all_publish_without_provider(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    _exchange(runtime, "我希望以后先给结论；我希望每次附上来源")
    assert runtime.memory_background.worker.run_one()
    assert len(runtime.memory_service.list(scope="global")) == 2


def test_explicit_expired_fact_is_not_recalled_after_background_publication(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    _exchange(runtime, "记住：旧发布窗口有效至2000-01-01")
    assert runtime.memory_background.worker.run_one()
    assert runtime.memory_service.list(scope="global") == []
    assert runtime.agent_turn_loop.memory_context_provider("next", None, "发布")["items"] == []


def test_small_remote_burst_batches_and_preserves_each_original_source(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    texts = ["我倾向先给结论", "我倾向附上来源", "我倾向列出风险"]
    for text in texts:
        _exchange(runtime, text)

    class Client:
        calls = 0

        def complete_text(self, **kwargs):
            self.calls += 1
            data = json.loads(kwargs["user_prompt"])
            assert data["user_messages"] == texts
            return SimpleNamespace(content=json.dumps({"candidates": [
                {"claim": text, "evidence": text, "kind": "preference", "explicit": False, "confidence": .9}
                for text in texts
            ]}, ensure_ascii=False))

    client = Client()
    runtime.memory_background.llm_client = client
    runtime.memory_background.allow_remote_extraction = True
    assert runtime.memory_background.worker.run_one()
    assert client.calls == 1
    records = runtime.memory_service.list(scope="global", statuses=("candidate",))
    assert len(records) == 3
    assert all(len(record.source_ids) == 1 for record in records)
    assert len({record.source_ids[0] for record in records}) == 3


def test_partial_burst_retry_does_not_repeat_successful_model_inputs(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    texts = [f"我倾向采用方案{index}" for index in range(6)]
    for text in texts:
        _exchange(runtime, text)

    class Client:
        def __init__(self):
            self.requests = []

        def complete_text(self, **kwargs):
            group = json.loads(kwargs["user_prompt"])["user_messages"]
            self.requests.append(group)
            if len(self.requests) == 2:
                raise LLMTimeoutError("temporary provider timeout")
            return SimpleNamespace(content=json.dumps({"candidates": [
                {"claim": text, "evidence": text, "kind": "preference", "explicit": False, "confidence": .9}
                for text in group
            ]}, ensure_ascii=False))

    client = Client()
    runtime.memory_background.llm_client = client
    runtime.memory_background.allow_remote_extraction = True
    assert runtime.memory_background.worker.run_one()
    job = runtime.background_job_store.list()[0]
    assert job["status"] == "retry_wait"
    assert len(runtime.background_job_store.completed_inputs(job["job_id"])) == 3
    with sqlite3.connect(runtime.memory_background.db_path) as conn:
        conn.execute("UPDATE background_jobs SET available_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job["job_id"],))
    assert runtime.memory_background.worker.run_one()
    assert client.requests == [texts[:3], texts[3:], texts[3:]]
    assert runtime.background_job_store.get(job["job_id"])["status"] == "succeeded"
    records = runtime.memory_service.list(scope="global", statuses=("candidate",))
    assert len(records) == 6 and all(record.version == 1 for record in records)


def test_compaction_publishes_during_new_turn_and_retains_complete_tail(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    service = runtime.session_service
    service.ensure_session(session_id="long")
    callback = runtime.memory_background.enqueue_compaction
    for index in range(4):
        service.record_context_exchange(
            session_id="long", user_input=f"必须保留来源 {index}" + "资料" * 200,
            agent_answer="已经核对" * 100, trace_id=f"old-{index}", token_budget=1000,
            background_enqueue=callback,
        )

    class ConcurrentClient:
        def complete_text(self, **kwargs):
            service.record_context_exchange(
                session_id="long", user_input="新消息不要丢失", agent_answer="新的回答",
                trace_id="new-during-summary", token_budget=1000,
            )
            return SimpleNamespace(content='{"summary":"已核对此前资料，仍需保留来源"}')

    runtime.memory_background.llm_client = ConcurrentClient()
    assert runtime.memory_background.worker.run_one()
    window = service.get_context_window(session_id="long")
    assert "此前资料" in window.summary
    assert "必须保留来源" in window.summary
    assert window.summary_metadata["method"] == "model"
    assert window.summary_metadata["lossy_fallback_possible"] is True
    assert window.summary_metadata["input_trace_ids"]
    contents = [message.content for message in window.recent_messages]
    assert "新消息不要丢失" in contents and "新的回答" in contents
    assert any("保留来源 3" in value for value in contents)


def test_bounded_compaction_continuation_accepts_equal_watermark_order(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    service = runtime.session_service
    session_id = "segmented-compaction"
    runtime.memory_background.max_job_tokens = 3000
    for index in range(6):
        service.record_context_exchange(
            session_id=session_id,
            user_input=f"user-{index} " + "u" * 900,
            agent_answer=f"answer-{index} " + "a" * 600,
            trace_id=f"segment-{index}", token_budget=2500,
            background_enqueue=runtime.memory_background.enqueue_compaction,
        )

    jobs = runtime.background_job_store.list(
        status="queued", scope_id=session_id, kind="context_compact",
    )
    assert len(jobs) == 1
    target = runtime.background_job_store.get(jobs[0]["job_id"], include_payload=True)["payload"]["target_seq"]

    class CompactClient:
        def complete_text(self, **_kwargs):
            return SimpleNamespace(content='{"summary":"bounded prefix retained"}')

    runtime.memory_background.llm_client = CompactClient()
    for _ in range(12):
        if not runtime.memory_background.worker.run_one():
            break
    with sqlite3.connect(runtime.memory_background.db_path) as conn:
        covered = conn.execute(
            "SELECT covered_seq FROM agent_session_context_state WHERE session_id=?", (session_id,)
        ).fetchone()[0]
    # The compactor deliberately leaves the newest complete exchange in the raw tail.
    assert covered >= target - 2
    assert not runtime.background_job_store.list(
        status="queued", scope_id=session_id, kind="context_compact",
    )
