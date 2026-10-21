"""Offline provenance consumers retain every real user source in a turn."""

import hashlib
from contextlib import closing
from types import SimpleNamespace

import pytest

from app.core.llm_workloads import BackgroundTaskBudgetExceeded, current_workload
from app.core.tools import ToolContext, ToolInvocation
from app.domains.managed_tasks import ManagedTaskService
from app.tool_packages.instructions import UpdateInstructionsTool
from app.tool_packages.memory import RememberMemoryTool
from tests.test_contextual_memory import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def inputs(runtime, contents, trace="batch-trace"):
    runtime.session_service.ensure_session(session_id="batch")
    return [runtime.session_service.append_message(
        session_id="batch", role="user", content=content, payload={"trace_id": trace},
    ) for content in contents]


def completed_job(runtime, contents):
    runtime.session_service.ensure_session(session_id="batch")
    run = runtime.agent_run_manager.create_run(session_id="batch", user_input="batch")
    runtime.agent_run_manager.mark_running(run.run_id)
    messages = inputs(runtime, contents, run.trace_id)
    answer = runtime.session_service.append_message(
        session_id="batch", role="agent", content="好的",
        payload={"trace_id": run.trace_id, "run_id": run.run_id},
        persisted_message_callback=runtime._enqueue_memory_answer,
    )
    runtime.agent_run_manager.complete_run(run.run_id, result_snapshot={"answer": "好的"})
    job = runtime.memory_background.store.claim("batch-test", 600, kinds=("memory_extract",))
    assert job is not None
    return messages, answer, job


def test_plural_sources_are_real_ordered_and_single_source_stays_compatible(runtime):
    messages = inputs(runtime, ["first", "second"])
    inputs(runtime, ["unrelated"], "other-trace")
    sessions = runtime.session_service
    actual = sessions.get_turn_user_messages(session_id="batch", trace_id="batch-trace")
    assert [m.message_id for m in actual] == [m.message_id for m in messages]
    assert [m.content for m in actual] == ["first", "second"]
    assert sessions.get_turn_user_message(session_id="batch", trace_id="batch-trace") == messages[-1]
    assert sessions.get_turn_user_messages(session_id="batch", trace_id="") == []
    sessions.delete_session(session_id="batch")
    assert sessions.get_turn_user_messages(session_id="batch", trace_id="batch-trace") == []


def test_manager_envelope_quotes_each_source_and_discloses_bounds(runtime):
    messages = inputs(runtime, ["first", "second" * 2000, *[str(i) for i in range(15)]])
    service = SimpleNamespace(sessions=runtime.session_service)
    envelope = ManagedTaskService._envelope(
        service, ToolContext(session_id="batch"),
        SimpleNamespace(trace_id="batch-trace", run_id="run"), "command", "purpose",
    )
    assert envelope.original_user_message_refs == [m.message_id for m in messages[:16]]
    assert envelope.original_user_quotes == [m.content[:8000] for m in messages[:8]]
    assert envelope.debug_info["original_quote_truncated"] is True
    assert envelope.debug_info["original_refs_truncated"] is True


def test_guidance_gate_reads_first_source_and_preserves_child_restriction(runtime):
    inputs(runtime, ["请更新全局指导文件。", "顺便回答一个问题。"])
    files = runtime.instruction_files
    tool = UpdateInstructionsTool(files, runtime.session_service)
    invocation = ToolInvocation(
        invocation_id="write", tool=tool.spec, session_id="batch", context_id="ctx",
        input={"kind": "global", "content": "# Updated", "expected_sha256": files.read("global")["sha256"]},
    )
    context = ToolContext(session_id="batch", trace_id="batch-trace")
    assert tool.invoke(invocation=invocation, context=context).status == "completed"
    from tests.test_memory_tools import _child_context

    child = _child_context().model_copy(update={"session_id": "batch", "trace_id": "batch-trace"})
    assert tool.invoke(invocation=invocation, context=child).status == "rejected"


def test_remember_exact_evidence_uses_first_original_source(runtime):
    messages = inputs(runtime, ["记住：我希望回答简洁", "请继续。"])
    runtime.memory_background.allow_remote_extraction = False
    result = runtime.tool_executor.execute(
        invocation_id="remember", tool_name="memory.remember",
        tool_input={"evidence": messages[0].content},
        context=ToolContext(session_id="batch", trace_id="batch-trace", safety_review_approved=True),
    )
    assert result.status == "completed", result.error
    sources = runtime.memory_service.sources_for(result.output["memory"]["memory_id"])
    assert [source["source_ref"] for source in sources] == [messages[0].message_id]
    assert sources[0]["checksum"] == hashlib.sha256(messages[0].content.encode()).hexdigest()


def test_contextual_remember_receives_real_messages_and_retains_saved_file_status(runtime):
    messages = inputs(runtime, ["我平时居住在上海。", "请继续。"])
    observed = []
    record = SimpleNamespace(memory_id="saved", model_dump=lambda **_: {"memory_id": "saved"})

    def contextual(user, **_kwargs):
        observed.append(user)
        return ([record], "synced") if user.message_id == messages[0].message_id else ([], "not_saved")

    tool = RememberMemoryTool(runtime.memory_service, runtime.session_service,
                              runtime.memory_files, contextual_remember=contextual)
    result = tool.invoke(invocation=ToolInvocation(
        invocation_id="save", tool=tool.spec, session_id="batch", context_id="ctx",
        input={"content": "保存居住地"},
    ), context=ToolContext(session_id="batch", trace_id="batch-trace"))
    assert observed == messages
    assert result.status == "completed"
    assert result.output["memory_file_status"] == "synced"


def test_background_each_original_source_has_checksum_and_answer_checkpoint(runtime, monkeypatch):
    coordinator = runtime.memory_background
    coordinator.allow_remote_extraction = False
    messages, answer, job = completed_job(runtime, ["记住：我希望回答简洁", "记住：我希望使用中文"])
    original = coordinator._extract_user_source
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("retry after first source")
        return original(*args)

    monkeypatch.setattr(coordinator, "_extract_user_source", fail_second)
    with pytest.raises(RuntimeError, match="retry after first"):
        coordinator._bounded_extract(job)
    assert coordinator.store.completed_inputs(job["job_id"]) == set()
    monkeypatch.setattr(coordinator, "_extract_user_source", original)
    coordinator._bounded_extract(job)
    assert coordinator.store.completed_inputs(job["job_id"]) == {answer.message_id}
    records = runtime.memory_service.list(scope="global")
    assert len(records) == 2
    sources = [source for record in records for source in runtime.memory_service.sources_for(record.memory_id)]
    assert {(s["source_ref"], s["checksum"]) for s in sources} == {
        (message.message_id, hashlib.sha256(message.content.encode()).hexdigest()) for message in messages
    }
    with closing(runtime._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM memory_entry_sources").fetchone()[0] == 2


def test_background_remote_sources_share_budget_and_respect_disable_and_lease(runtime, monkeypatch):
    coordinator = runtime.memory_background
    coordinator.max_job_tokens = 4321
    messages, _, job = completed_job(runtime, ["我平时居住在上海。", "我的职业是教师。"])
    observed = []

    def organize(user, **kwargs):
        observed.append((user.message_id, user.content, kwargs["publication_lease"], current_workload()))
        return [], "not_saved"

    monkeypatch.setattr(coordinator.learning, "organize", organize)
    coordinator.allow_remote_extraction = False
    coordinator._extract_one({**job, "payload": {"message_id": job["payload"]["message_ids"][0]}})
    assert observed == []
    coordinator.allow_remote_extraction = True

    def exhausted_on_second(user, **kwargs):
        organize(user, **kwargs)
        if user.message_id == messages[1].message_id:
            raise BackgroundTaskBudgetExceeded("shared answer budget exhausted")

    monkeypatch.setattr(coordinator.learning, "organize", exhausted_on_second)
    with pytest.raises(BackgroundTaskBudgetExceeded, match="shared answer"):
        coordinator._bounded_extract(job)
    assert [m[0] for m in observed] == [m.message_id for m in messages]
    assert coordinator.store.completed_inputs(job["job_id"]) == set()
    monkeypatch.setattr(coordinator.learning, "organize", organize)
    observed.clear()
    coordinator._bounded_extract(job)
    assert [(m[0], m[1]) for m in observed] == [(m.message_id, m.content) for m in messages]
    assert all(m[2] == (job["job_id"], job["lease_owner"], job["lease_epoch"]) for m in observed)
    assert all(m[3].task_id == job["job_id"] and m[3].max_tokens == 4321
               and m[3].pool == "background_memory" for m in observed)
    observed.clear()
    monkeypatch.setattr(coordinator.store, "completed_inputs", lambda *_: set())
    monkeypatch.setattr(coordinator.store, "heartbeat", lambda *_, **__: False)
    coordinator._bounded_extract(job)
    assert [m[0] for m in observed] == [messages[0].message_id]
    observed.clear()
    runtime.memory_service.set_learning_enabled(scope="global", project_id=None, enabled=False)
    coordinator._bounded_extract(job)
    assert observed == []
