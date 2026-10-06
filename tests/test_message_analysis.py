"""Synthetic, offline checks for the committed-message -> analysis pipeline."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.background_jobs import BackgroundJobStore
from app.core.llm.errors import LLMNetworkError
from app.core.local_config import LocalAppConfig, MessageHistoryConfig
from app.core.message_analysis import MessageAnalysisCoordinator, message_chunks
from app.domains.message_history import AnalysisResult, ExtractedFact, MessageHistoryService

IDENTITY = {"platform": "mock", "account_id": "test-account", "conversation_type": "group",
            "conversation_id": "team"}


def test_blank_summary_and_inferred_corrections_are_not_publishable():
    with pytest.raises(ValidationError):
        AnalysisResult(batch_summary=" ", summary=" ")
    with pytest.raises(ValidationError):
        ExtractedFact(kind="correction", text="猜测时间改了", certainty="inferred",
                      source_message_ids=["m"], supersedes_fact_ids=["old-fact"])
    with pytest.raises(ValidationError, match="unknown LLM client"):
        LocalAppConfig(message_history=MessageHistoryConfig(background_client_name="not-configured"))


class SyntheticModel:
    def __init__(self, hook=None, bad_evidence=False):
        self.requests = []
        self.hook = hook
        self.bad_evidence = bad_evidence

    async def complete_text(self, **kwargs):
        value = json.loads(kwargs["user_prompt"])
        self.requests.append(value)
        if self.hook:
            self.hook()
        evidence = "fabricated-id" if self.bad_evidence else value["messages"][0]["message_id"]
        return SimpleNamespace(content=json.dumps({
            "batch_summary": "本批讨论了部署安排。", "summary": "项目群讨论部署；只包含收到的消息。",
            "facts": [{"kind": "task_candidate", "text": "核实部署时间。", "certainty": "inferred",
                       "source_message_ids": [evidence]}],
        }), metadata={})


def pipeline(tmp_path, *, model=None, batch_size=2, chunk_bytes=12000):
    store = BackgroundJobStore(tmp_path / "messages.sqlite3")
    service = MessageHistoryService(store.db_path, store)
    service.ensure_schema()
    policy = service.set_policy({**IDENTITY, "record_enabled": True, "analysis_enabled": True,
                                 "batch_size": batch_size, "max_batch_messages": batch_size,
                                 "min_interval_seconds": 0})
    coordinator = MessageAnalysisCoordinator(
        service=service, store=store,
        config=MessageHistoryConfig(input_chunk_bytes=chunk_bytes, max_job_tokens=500_000,
            max_job_calls=100, yield_delay_seconds=0,
            service_hourly_token_limit=0, service_daily_token_limit=0, service_hourly_call_limit=0,
            service_daily_call_limit=0, conversation_hourly_token_limit=0, conversation_daily_token_limit=0,
            conversation_hourly_call_limit=0, conversation_daily_call_limit=0), llm_client=model or SyntheticModel(),
    )
    return service, store, policy, coordinator


def imports(service, count, *, text="明天核实服务器部署时间。"):
    return service.import_messages([
        {**IDENTITY, "message_id": str(index), "sender_id": "member", "text": text,
         "sent_at": 1790920000 - index, "received_at": 1790920000 + index}
        for index in range(count)
    ])


def test_full_batches_publish_serially_and_keep_unsummarized_tail(tmp_path):
    service, store, policy, coordinator = pipeline(tmp_path)
    assert len(imports(service, 5)["acknowledged"]) == 5
    assert coordinator.worker.run_one()
    first = service.summary(policy["conversation_key"])
    assert first["covered_seq"] == 2
    service.schedule_pending(policy["conversation_key"])
    assert coordinator.worker.run_one()
    assert not coordinator.worker.run_one()
    latest = service.summary(policy["conversation_key"])
    assert latest["covered_seq"] == 4
    assert latest["summary_revision"] == 2
    assert latest["pending_count"] == 1
    assert len(latest["raw_tail"]) == 1
    assert len(store.list(status="succeeded")) == 2
    assert service.facts(policy["conversation_key"])["facts"][0]["source_message_ids"]


def test_long_unicode_and_escaped_text_is_fully_covered_before_publish(tmp_path):
    model = SyntheticModel()
    service, _, policy, coordinator = pipeline(tmp_path, model=model, batch_size=1, chunk_bytes=1024)
    original = '服务器\\路径\n"需要确认"🙂' * 150
    imports(service, 1, text=original)
    rows = service.history(policy["conversation_key"])["messages"]
    chunks = list(message_chunks(rows, 1024))
    assert len(chunks) > 1
    assert "".join(item["text"] for group in chunks for item in group) == original
    assert all(len(json.dumps(group, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024 for group in chunks)
    assert coordinator.worker.run_one()
    for _ in range(100):
        if not coordinator.worker.run_one():
            break
    assert len(model.requests) == len(chunks)
    assert service.summary(policy["conversation_key"])["covered_seq"] == 1


def test_unknown_evidence_fails_without_advancing_summary(tmp_path):
    service, store, policy, coordinator = pipeline(tmp_path, model=SyntheticModel(bad_evidence=True))
    imports(service, 2)
    assert coordinator.worker.run_one()
    assert service.summary(policy["conversation_key"])["covered_seq"] == 0
    assert store.list(status="failed")[0]["error_class"] == "model_output_invalid"
    assert not service.facts(policy["conversation_key"])["facts"]


def test_revocation_during_model_call_prevents_publication(tmp_path):
    model = SyntheticModel()
    service, store, policy, coordinator = pipeline(tmp_path, model=model)
    model.hook = lambda: service.set_policy({**IDENTITY, "record_enabled": False,
                                           "expected_revision": policy["revision"]})
    imports(service, 2)
    assert coordinator.worker.run_one()
    assert len(model.requests) == 1
    assert store.list(status="cancelled")
    assert service.recent()["messages"] == []
    with service._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_history_messages").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM message_history_batches").fetchone()[0] == 0


def test_provider_failure_preserves_batch_and_raw_messages(tmp_path):
    def unavailable():
        raise LLMNetworkError("synthetic network failure")

    service, store, policy, coordinator = pipeline(tmp_path, model=SyntheticModel(hook=unavailable))
    imports(service, 4)
    assert coordinator.worker.run_one()
    jobs = store.list(status="retry_wait")
    assert len(jobs) == 1 and jobs[0]["error_class"] == "provider_network"
    service.schedule_pending(policy["conversation_key"], force=True)
    assert len(store.list()) == 1
    assert service.summary(policy["conversation_key"])["covered_seq"] == 0
    assert len(service.history(policy["conversation_key"])["messages"]) == 4


def test_shutdown_during_model_call_defers_unpublished_batch(tmp_path):
    model = SyntheticModel()
    service, store, policy, coordinator = pipeline(tmp_path, model=model)
    model.hook = coordinator.worker._stop.set
    imports(service, 2)
    assert coordinator.worker.run_one()
    assert store.list(status="succeeded") == []
    assert len(store.list(status="retry_wait")) == 1
    assert service.summary(policy["conversation_key"])["covered_seq"] == 0


def test_runtime_registers_tools_and_worker_is_independent_of_personal_memory(tmp_path):
    from app.core.config import Settings
    from app.core.context_driver import ToolView
    from app.core.multi_agent import SideEffectLevel
    from app.core.runtime import LocalKnowledgeAgentRuntime
    from app.core.tools import ToolContext

    settings = Settings(_env_file=None, LKA_DATA_DIR=tmp_path / "runtime",
                        LKA_LOCAL_CONFIG=tmp_path / "missing.toml", LKA_WORKSPACE_ROOTS="")
    runtime = LocalKnowledgeAgentRuntime(settings)
    try:
        runtime.local_app_config.memory.enabled = False
        policy = runtime.message_history.set_policy({**IDENTITY, "record_enabled": True})
        imports(runtime.message_history, 1)
        assert policy["source_id"] in runtime._message_source_inventory()
        assert policy["account_scope_id"] in runtime._message_account_inventory()
        root = runtime.tool_executor.execute(
            invocation_id="root-read", tool_name="messages.recent", tool_input={},
            context=ToolContext(session_id="synthetic"),
        )
        assert root.status == "completed" and len(root.output["messages"]) == 1
        parent = runtime.agent_run_manager.create_run(session_id="synthetic", user_input="Read mock messages")
        child_run = runtime.agent_run_manager.create_child_run(
            parent_run_id=parent.run_id, plan_id="synthetic-plan", step_id="read-step",
            attempt=1, user_input="Read this authorized source",
        )
        denied = runtime.tool_executor.execute(
            invocation_id="child-denied", tool_name="messages.recent", tool_input={},
            context=ToolContext(session_id="synthetic", tool_view=ToolView(
                snapshot_id="snapshot", child_run_id=child_run.run_id,
                allowed_packages=("messages",), side_effect_level=SideEffectLevel.NONE,
            )),
        )
        assert denied.status == "rejected"
        granted = ToolContext(session_id=child_run.session_id, tool_view=ToolView(
            snapshot_id="granted-snapshot", child_run_id=child_run.run_id,
            allowed_packages=("messages",), side_effect_level=SideEffectLevel.NONE,
            allowed_source_ids=(policy["source_id"],),
            allowed_account_ids=(policy["account_scope_id"],),
        ))
        child = runtime.tool_executor.execute(
            invocation_id="child-read", tool_name="messages.recent", tool_input={}, context=granted,
        )
        assert child.status == "completed" and len(child.output["messages"]) == 1
        runtime.message_history.set_policy({**IDENTITY, "record_enabled": False, "expected_revision": 1})
        revoked = runtime.tool_executor.execute(
            invocation_id="child-revoked", tool_name="messages.recent", tool_input={}, context=granted,
        )
        assert revoked.status == "completed" and revoked.output["messages"] == []
        assert not runtime._message_source_inventory()
        runtime.start()
        assert runtime.message_analysis.worker._threads
        assert not runtime.memory_background.worker._threads
    finally:
        runtime.stop()
