"""Isolation, publication and prompt contracts, never real provider dispatch."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.background_jobs import BackgroundJobStore
from app.core.context_projection import render_context
from app.core.context_reconcile import (
    propose_delta,
    select_prefix,
    select_record_candidates,
    validate_delta,
)
from app.core.context_record_models import ContextDelta
from app.core.context_sources import passage_catalog, source_passages
from app.core.local_config import MemoryConfig
from app.core.memory_background import MemoryBackgroundCoordinator
from app.core.prompt_tokens import PromptTokenCounter
from app.core.sessions import SessionService
from app.core.tools import ToolContext, ToolInvocation
from app.storage.context_records import ContextRecordConflict, ContextRecordStore, source_digest
from app.storage.db import connect, init_db
from app.tool_packages.context_records import ReadContextRecordsTool, ReadContextSourceTool


@pytest.fixture
def system(tmp_path):
    db = tmp_path / "isolated.sqlite3"
    init_db(db)
    factory = lambda: connect(db)
    sessions = SessionService(factory, context_token_counter=PromptTokenCounter())
    session = sessions.create_session(title="Test").session.session_id
    sessions.record_context_exchange(session_id=session, user_input="课程 A 考完了。现在学习课程 B。",
                                     agent_answer="概率材料只读了 200/1130 行。", trace_id="first")
    store = ContextRecordStore(factory)
    store.ensure_schema()
    return SimpleNamespace(db=db, factory=factory, sessions=sessions, session=session, store=store)


def proposal(*, kind="task", content="课程 A 考试已经结束", evidence="user_statement", seq=1,
             quote="课程 A 考完了", status="completed", **fields):
    value = {"kind": kind, "subject": "用户", "topic": "课程 A", "content": content,
             "evidence": evidence, "sources": [{"seq": seq, "quote": quote}], "priority": 3}
    if kind == "task":
        value["task_status"] = status
    value.update(fields)
    return {"op": "add", "reason": "Explicit source", "value": value}


def publish(system, proposals):
    snapshot = system.store.snapshot(system.session)
    delta = ContextDelta.model_validate({"changes": proposals})
    system.store.publish(snapshot, validate_delta(snapshot, delta))
    return system.store.snapshot(system.session)


def passage_proposal(system, **fields):
    p = proposal(**fields)
    snapshot = system.store.snapshot(system.session)
    row = next(r for r in snapshot["messages"] if r["seq"] == p["value"]["sources"][0]["seq"])
    p["value"]["sources"] = [{"passage_id": source_passages(system.session, row)[0]["passage_id"]}]
    return p


def append(system, text="现在重新准备课程 A。", reply="收到。"):
    system.sessions.record_context_exchange(session_id=system.session, user_input=text, agent_answer=reply, trace_id="second")


def test_closed_task_projection_and_partial_progress(system):
    snapshot = publish(system, [proposal(), proposal(kind="progress", content="概率只读了 200/1130 行",
                         evidence="assistant_report", seq=2, quote="概率材料只读了 200/1130 行", topic="课程 B")])
    view = render_context(snapshot["records"], covered_seq=2, budget=4096)
    assert "当前目标" not in view["text"]
    assert "已完成" in view["text"] and "200/1130" in view["text"]
    assert "未独立核实" in view["text"]
    assert "record_id" not in view["text"] and "task_status" not in view["text"]


@pytest.mark.parametrize("repair_valid", [True, False])
def test_passage_repair_is_bounded_and_revalidated(system, repair_valid):
    requests = []
    bad = passage_proposal(system)
    bad["value"]["sources"] = [{"passage_id": "p_999_0_1_0000000000000000"}]

    class Client:
        def complete_text(self, **kwargs):
            requests.append(kwargs)
            value = passage_proposal(system) if len(requests) == 2 and repair_valid else bad
            return SimpleNamespace(content=json.dumps({"changes": [value]}), metadata={})

    snapshot = system.store.snapshot(system.session)
    if repair_valid:
        changes, _ = propose_delta(Client(), snapshot, snapshot["messages"])
        assert changes[0]["value"]["sources"][0]["quote"] == snapshot["messages"][0]["content"]
    else:
        with pytest.raises(ValueError, match="Source passage"):
            propose_delta(Client(), snapshot, snapshot["messages"])
    assert len(requests) == 2
    assert "authorized supplied input interval" in requests[1]["user_prompt"]
    feedback = json.loads(requests[1]["user_prompt"].split("\n")[-1])
    assert "passage_id" in feedback["repair_instruction"]
    assert system.store.snapshot(system.session)["revision"] == 0


def test_role_violation_does_not_trigger_citation_repair(system):
    calls = []

    class Client:
        def complete_text(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=json.dumps({"changes": [passage_proposal(system, evidence="assistant_report")]}), metadata={})

    snapshot = system.store.snapshot(system.session)
    with pytest.raises(ValueError, match="Evidence role"):
        propose_delta(Client(), snapshot, snapshot["messages"])
    assert len(calls) == 1


def test_citation_repair_respects_input_capacity(system):
    calls = []

    class Counter:
        def count_request(self, _system, prompt):
            return SimpleNamespace(count=3000 if "rejected_delta" in prompt else 100)

    class Client:
        def complete_text(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=json.dumps({"changes": [proposal(quote="不存在的引用")]}), metadata={})

    snapshot = system.store.snapshot(system.session)
    with pytest.raises(ValueError, match="repair exceeds capacity"):
        propose_delta(Client(), snapshot, snapshot["messages"], input_tokens=2048, counter=Counter())
    assert len(calls) == 1
    assert system.store.snapshot(system.session)["covered_seq"] == 0


def test_schema_repair_does_not_accept_status_on_progress(system):
    calls = []
    bad = passage_proposal(system, kind="progress")
    bad["value"]["task_status"] = "completed"

    class Client:
        def complete_text(self, **kwargs):
            calls.append(kwargs)
            p = bad if len(calls) == 1 else passage_proposal(system, kind="progress")
            return SimpleNamespace(content=json.dumps({"changes": [p]}), metadata={})

    snapshot = system.store.snapshot(system.session)
    changes, _ = propose_delta(Client(), snapshot, snapshot["messages"])
    assert len(calls) == 2
    assert changes[0]["value"]["task_status"] is None
    assert "Only tasks require task_status" in calls[1]["user_prompt"]


def test_synthesized_content_resolves_original_passage_without_copying(system):
    p = passage_proposal(system, content="前一门课程的考试已结束，之后改学另一门课程。")
    state = publish(system, [p])
    record = state["records"][0]
    assert record["content"] not in state["messages"][0]["content"]
    assert record["sources"][0]["quote"] == state["messages"][0]["content"]
    assert record["sources"][0]["passage_id"] == p["value"]["sources"][0]["passage_id"]


@pytest.mark.parametrize("mutation", ["session", "content", "role", "time", "trace"])
def test_passage_id_cannot_cross_scope_or_changed_source(system, mutation):
    p = passage_proposal(system)
    snapshot = system.store.snapshot(system.session)
    if mutation == "session":
        snapshot["session_id"] = "other_session"
    else:
        key = {"content": "content", "role": "role", "time": "created_at", "trace": "trace_id"}[mutation]
        snapshot["messages"][0][key] += "changed"
    with pytest.raises(ValueError, match="Source passage"):
        validate_delta(snapshot, ContextDelta.model_validate({"changes": [p]}))


def test_visible_passage_fence_excludes_old_history(system):
    p = passage_proposal(system)
    append(system)
    snapshot = system.store.snapshot(system.session)
    visible = passage_catalog(system.session, snapshot["messages"][2:])
    with pytest.raises(ValueError, match="supplied input interval"):
        validate_delta(snapshot, ContextDelta.model_validate({"changes": [p]}), visible_passages=visible)


def test_source_partition_is_lossless_unicode_stable_and_bounded():
    text = "甲\n\n\n" + "读了一部分，并没有读完。🙂" * 200 + "\n\n尾部\n"
    row = {"seq": 1, "role": "user", "trace_id": "trace", "created_at": "now", "content": text}
    passages = source_passages("session", row)
    assert "".join(p["content"] for p in passages) == text
    assert all(0 < p["end"] - p["start"] <= 1200 for p in passages)
    assert len({p["passage_id"] for p in passages}) == len(passages)
    assert passages == source_passages("session", row)
    assert all(text[p["start"]:p["end"]] == p["content"] for p in passages)


def test_passage_archive_and_legacy_record_can_coexist(system):
    record = publish(system, [proposal()])["records"][0]
    append(system, "这个已完成事项不用继续保留。")
    snapshot = system.store.snapshot(system.session)
    ref = source_passages(system.session, snapshot["messages"][2])[0]["passage_id"]
    state = publish(system, [{"op": "archive", "record_id": record["record_id"], "expected_revision": 1,
                             "archive_sources": [{"passage_id": ref}], "reason": "Explicit withdrawal"}])
    assert state["records"][0]["validity"] == "archived"
    with system.factory() as conn:
        change = json.loads(conn.execute("SELECT change_json FROM context_record_events WHERE head_revision=2").fetchone()[0])
    assert change["archive_sources"][0]["quote"] == snapshot["messages"][2]["content"]


@pytest.mark.parametrize("invalid_review", [False, True])
def test_risky_progress_gets_one_bounded_source_review(system, invalid_review):
    calls = []
    draft = passage_proposal(system, kind="progress", evidence="assistant_report", seq=2,
                             content="概率材料只读了一部分")

    class Client:
        def complete_text(self, **kwargs):
            calls.append(kwargs)
            p = json.loads(json.dumps(draft))
            if len(calls) == 2:
                p["value"]["content"] = "概率材料只读了 200/1130 行，并没有读完"
                if invalid_review:
                    p["value"]["sources"] = [{"passage_id": "p_99_0_1_0000000000000000"}]
            return SimpleNamespace(content=json.dumps({"changes": [p]}), metadata={}, usage={})

    snapshot = system.store.snapshot(system.session)
    if invalid_review:
        with pytest.raises(ValueError, match="Source passage"):
            propose_delta(Client(), snapshot, snapshot["messages"])
    else:
        changes, response = propose_delta(Client(), snapshot, snapshot["messages"])
        assert "200/1130" in changes[0]["value"]["content"]
        assert response.metadata["atomic_source_review"] is True
    assert len(calls) == 2
    assert calls[1]["prompt_summary"] == "background_context_records_source_review"
    reviewed = json.loads(calls[1]["user_prompt"])
    assert len(reviewed["messages"]) == 1 and reviewed["messages"][0]["seq"] == 2
    assert system.store.snapshot(system.session)["covered_seq"] == 0


def test_simple_source_grounded_task_does_not_add_review_call(system):
    calls = []

    class Client:
        def complete_text(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=json.dumps({"changes": [passage_proposal(system)]}), metadata={})

    snapshot = system.store.snapshot(system.session)
    propose_delta(Client(), snapshot, snapshot["messages"])
    assert len(calls) == 1


@pytest.mark.parametrize("mutate", [
    lambda p: p["value"]["sources"][0].update(seq=900),
    lambda p: p["value"]["sources"][0].update(quote="用户不存在的身份"),
    lambda p: p["value"].update(evidence="assistant_report"),
    lambda p: p["value"].update(related_ids=["another_session_id"]),
    lambda p: p["value"].update(review_at="2026-10-10T01:00:00"),
])
def test_source_scope_role_and_time_rejection(system, mutate):
    p = proposal()
    mutate(p)
    snapshot = system.store.snapshot(system.session)
    with pytest.raises(ValueError):
        validate_delta(snapshot, ContextDelta.model_validate({"changes": [p]}))
    assert system.store.snapshot(system.session)["revision"] == 0


def test_closed_task_can_reopen_with_new_user_evidence(system):
    record = publish(system, [proposal()])["records"][0]
    append(system)
    p = proposal(content="课程 A 已重新开启准备", seq=3, quote="现在重新准备课程 A", status="active")
    p.update(op="update", record_id=record["record_id"], expected_revision=1)
    snapshot = publish(system, [p])
    assert snapshot["records"][0]["task_status"] == "active"
    assert len(snapshot["records"][0]["sources"]) == 2
    with system.factory() as conn:
        events = conn.execute("SELECT change_json FROM context_record_events ORDER BY head_revision").fetchall()
    assert json.loads(events[1][0])["before"]["task_status"] == "completed"


def test_nonuser_cannot_replace_user_correction(system):
    record = publish(system, [proposal(kind="correction")])["records"][0]
    append(system, "听听", "课程 A 没考完")
    p = proposal(kind="correction", evidence="assistant_report", seq=4, quote="课程 A 没考完", content="尚未考完")
    p.update(op="update", record_id=record["record_id"], expected_revision=1)
    with pytest.raises(ValueError, match="Non-user"):
        publish(system, [p])


def test_old_user_quote_cannot_launder_new_assistant_claim(system):
    record = publish(system, [proposal(kind="correction")])["records"][0]
    append(system, "继续", "课程 A 仍然没有考完")
    p = proposal(kind="correction", content="课程 A 尚未考完")
    p["value"]["sources"].append({"seq": 4, "quote": "课程 A 仍然没有考完"})
    p.update(op="update", record_id=record["record_id"], expected_revision=1)
    with pytest.raises(ValueError, match="Evidence role"):
        publish(system, [p])


def test_assistant_answer_can_close_task_as_report_not_verified(system):
    record = publish(system, [proposal(status="active")])["records"][0]
    append(system, "回答这个问题", "已经详细回答这个问题")
    p = proposal(evidence="assistant_report", seq=4, quote="已经详细回答这个问题", status="answered")
    p.update(op="update", record_id=record["record_id"], expected_revision=1)
    state = publish(system, [p])
    assert "未独立核实" in render_context(state["records"], covered_seq=4, budget=4096)["text"]


def test_stale_snapshot_never_overwrites(system):
    snapshot = system.store.snapshot(system.session)
    changes = validate_delta(snapshot, ContextDelta.model_validate({"changes": [proposal()]}))
    system.store.publish(snapshot, changes)
    with pytest.raises(ContextRecordConflict):
        system.store.publish(snapshot, changes)
    with system.factory() as conn:
        assert conn.execute("SELECT count(*) FROM context_record_events").fetchone()[0] == 1


def test_late_raw_message_remains_tail(system):
    snapshot = system.store.snapshot(system.session)
    changes = validate_delta(snapshot, ContextDelta.model_validate({"changes": [proposal()]}))
    append(system, "不用复习 A，只教 B 中新内容。")
    system.store.publish(snapshot, changes)
    system.sessions.atomic_context_store = system.store
    system.sessions.atomic_context_mode = "active"
    prompt = system.sessions.get_prompt_context_window(session_id=system.session)
    assert prompt.summary_metadata["covered_seq"] == 2
    assert len(prompt.recent_messages) == 2 and "只教 B" in prompt.recent_messages[0].content
    assert prompt.summary_metadata["method"] == "atomic_records"


def test_shadow_does_not_change_legacy_prompt(system):
    before = system.sessions.get_prompt_context_window(session_id=system.session)
    publish(system, [proposal()])
    system.sessions.atomic_context_store = system.store
    system.sessions.atomic_context_mode = "shadow"
    assert system.sessions.get_prompt_context_window(session_id=system.session) == before


def test_source_edit_invalidates_cached_records(system):
    publish(system, [proposal()])
    with system.factory() as conn:
        conn.execute("UPDATE agent_session_context_messages SET content='withdrawn' WHERE session_id=? AND seq=1", (system.session,))
    with pytest.raises(ContextRecordConflict, match="withdrawn"):
        system.store.snapshot(system.session)


def test_deleted_session_denies_context_tools(system):
    publish(system, [proposal()])
    system.sessions.delete_session(session_id=system.session)
    with pytest.raises(ContextRecordConflict, match="unavailable"):
        system.store.snapshot(system.session)


def test_scope_change_and_gap_reject_publication(system):
    snapshot = system.store.snapshot(system.session)
    changes = validate_delta(snapshot, ContextDelta.model_validate({"changes": [proposal()]}))
    with system.factory() as conn:
        conn.execute("UPDATE agent_sessions SET metadata=? WHERE session_id=?", ('{"project_id":"changed"}', system.session))
    with pytest.raises(ContextRecordConflict):
        system.store.publish(snapshot, changes)
    snapshot = system.store.snapshot(system.session)
    snapshot["messages"] = snapshot["messages"][1:]
    snapshot["source_digest"] = source_digest(snapshot["messages"])
    with pytest.raises(ContextRecordConflict):
        system.store.publish(snapshot, changes)


def test_overlapping_changes_rollback_everything(system):
    record = publish(system, [proposal()])["records"][0]
    append(system)
    p = proposal(seq=3, quote="现在重新准备课程 A", status="active")
    p.update(op="update", record_id=record["record_id"], expected_revision=1)
    with pytest.raises(ContextRecordConflict, match="Overlapping"):
        publish(system, [p, p])
    snapshot = system.store.snapshot(system.session)
    assert snapshot["revision"] == 1 and snapshot["covered_seq"] == 2
    assert snapshot["records"][0]["revision"] == 1


def test_stale_lease_rejects_without_partial_write(system):
    queue = BackgroundJobStore(system.db)
    queue.ensure_schema()
    snapshot = system.store.snapshot(system.session)
    changes = validate_delta(snapshot, ContextDelta.model_validate({"changes": [proposal()]}))
    with pytest.raises(ContextRecordConflict, match="lease"):
        system.store.publish(snapshot, changes, lease=("missing", "worker", 1))
    assert system.store.snapshot(system.session)["revision"] == 0


def test_whole_record_budget_retains_negation(system):
    record = publish(system, [proposal(content="不要讲课程 A 已经讲过的部分，但新的推导需要详细步骤。")])["records"][0]
    full = render_context([record], covered_seq=2, budget=4096)
    tiny = render_context([record], covered_seq=2, budget=160)
    assert "不要讲" in full["text"] and "新的推导需要详细步骤" in full["text"]
    assert record["content"] not in tiny["text"]
    assert tiny["omitted_ids"] == [record["record_id"]]
    assert record["validity"] == "current"


def test_elapsed_review_does_not_complete_task(system):
    record = publish(system, [proposal(status="active", review_at=(datetime.now(UTC)-timedelta(days=1)).isoformat())])["records"][0]
    text = render_context([record], covered_seq=2, budget=4096)["text"]
    assert "进行中" in text and "不代表完成" in text


@pytest.mark.parametrize("status", ["proposed", "unknown"])
def test_suggestions_and_unknown_tasks_are_not_current_goals(system, status):
    state = publish(system, [proposal(status=status)])
    text = render_context(state["records"], covered_seq=2, budget=4096)["text"]
    assert "当前目标" not in text and "待确认事项" in text


def test_oversized_message_does_not_advance_watermark(system):
    snapshot = system.store.snapshot(system.session)
    with pytest.raises(ValueError, match="exceed"):
        select_prefix(snapshot, target_seq=2, input_tokens=64)
    assert system.store.snapshot(system.session)["covered_seq"] == 0


def test_no_foreground_model_and_reader_pages_original(system):
    publish(system, [proposal()])
    reader = ReadContextRecordsTool(system.store)
    context = ToolContext(session_id=system.session)
    invocation = ToolInvocation(invocation_id="read", tool=reader.spec, session_id=system.session, context_id="ctx", input={"limit": 1})
    result = reader.invoke(invocation=invocation, context=context)
    assert result.status == "completed" and result.output["records"][0]["source_messages"] == [1]
    source = ReadContextSourceTool(system.store)
    invocation = invocation.model_copy(update={"tool": source.spec, "input": {"seq": 1, "max_chars": 4}})
    result = source.invoke(invocation=invocation, context=context)
    assert result.output["content"] == "课程 A" and result.output["next_offset"] == 4


def test_reader_exposes_fragment_receipt_and_reads_exact_passage(system):
    state = publish(system, [passage_proposal(system)])
    receipt = state["records"][0]["sources"][0]
    context = ToolContext(session_id=system.session)
    reader = ReadContextRecordsTool(system.store)
    invocation = ToolInvocation(invocation_id="read", tool=reader.spec, session_id=system.session, context_id="ctx", input={})
    result = reader.invoke(invocation=invocation, context=context)
    fragment = result.output["records"][0]["source_fragments"][0]
    assert fragment["passage_id"] == receipt["passage_id"] and "quote" not in fragment
    source = ReadContextSourceTool(system.store)
    invocation = invocation.model_copy(update={"tool": source.spec, "input": {"passage_id": fragment["passage_id"], "max_chars": 4}})
    first = source.invoke(invocation=invocation, context=context)
    assert first.status == "completed" and first.output["content"] == receipt["quote"][:4]
    assert first.output["next_offset"] == 4
    invocation = invocation.model_copy(update={"input": {"passage_id": fragment["passage_id"], "offset": 4}})
    second = source.invoke(invocation=invocation, context=context)
    assert first.output["content"] + second.output["content"] == receipt["quote"]
    assert second.output["next_offset"] is None
    changed = fragment["passage_id"][:-1] + ("0" if fragment["passage_id"][-1] != "0" else "1")
    invocation = invocation.model_copy(update={"input": {"passage_id": changed}})
    assert source.invoke(invocation=invocation, context=context).status == "rejected"


def test_source_passage_at_nonzero_span_has_relative_paging(system):
    text = "🙂" * 2401
    append(system, text)
    row = system.store.snapshot(system.session)["messages"][2]
    passage = source_passages(system.session, row)[1]
    source = ReadContextSourceTool(system.store)
    invocation = ToolInvocation(invocation_id="span", tool=source.spec, session_id=system.session, context_id="ctx",
                                input={"passage_id": passage["passage_id"], "offset": 3, "max_chars": 5})
    result = source.invoke(invocation=invocation, context=ToolContext(session_id=system.session))
    assert result.status == "completed"
    assert result.output["content"] == text[passage["start"]+3:passage["start"]+8]
    assert result.output["offset"] == 3 and result.output["next_offset"] == 8
    assert result.output["source_start"] == 1200 and result.output["total_chars"] == 1200


@pytest.mark.parametrize("input_value", [{}, {"seq": 1, "passage_id": "p_1_0_1_0000000000000000"},
    {"passage_id": "invalid"}, {"seq": 1, "max_chars": 8001}, {"seq": 1, "offset": -1},
    {"seq": 1, "max_chars": True}, {"seq": 1, "extra": "unrecognized"}])
def test_source_selector_and_paging_reject_invalid_input(system, input_value):
    source = ReadContextSourceTool(system.store)
    invocation = ToolInvocation(invocation_id="invalid", tool=source.spec, session_id=system.session,
                                context_id="ctx", input=input_value)
    assert source.invoke(invocation=invocation, context=ToolContext(session_id=system.session)).status == "rejected"


def test_old_passage_handle_is_invalid_after_source_edit(system):
    record = publish(system, [passage_proposal(system)])["records"][0]
    identity = record["sources"][0]["passage_id"]
    with system.factory() as conn:
        conn.execute("UPDATE agent_session_context_messages SET content=? WHERE session_id=? AND seq=1",
                     ("用户已经撤回了原来的表述。", system.session))
    source = ReadContextSourceTool(system.store)
    invocation = ToolInvocation(invocation_id="edited", tool=source.spec, session_id=system.session,
                                context_id="ctx", input={"passage_id": identity})
    assert source.invoke(invocation=invocation, context=ToolContext(session_id=system.session)).status == "rejected"
    with pytest.raises(ContextRecordConflict, match="Derived source changed"):
        system.store.snapshot(system.session)


def test_empty_delta_advances_valid_coverage_without_facts(system):
    snapshot = publish(system, [])
    assert snapshot["covered_seq"] == 2 and snapshot["records"] == []


def test_candidate_omission_does_not_archive_and_retains_operative_state(system):
    records = publish(system, [proposal(status="active"), proposal(topic="旧事项", content="不相关的旧事项", priority=0)])["records"]
    selected = select_record_candidates(records, [{"content": "当前任务"}], token_budget=1500, counter=PromptTokenCounter())
    assert selected and selected[0]["topic"] == "课程 A"
    assert all(record["validity"] == "current" for record in system.store.snapshot(system.session)["records"])


def test_model_cannot_target_a_record_omitted_from_input(system):
    record = publish(system, [proposal()])["records"][0]
    append(system)
    p = proposal(seq=3, quote="现在重新准备课程 A", status="active")
    p.update(op="update", record_id=record["record_id"], expected_revision=1)
    with pytest.raises(ValueError, match="candidate"):
        validate_delta(system.store.snapshot(system.session), ContextDelta.model_validate({"changes": [p]}), visible_record_ids=set())


def test_duplicate_fact_add_updates_sources_without_new_record(system):
    publish(system, [proposal()])
    append(system, "课程 A 考完了")
    snapshot = publish(system, [proposal(seq=3)])
    assert len(snapshot["records"]) == 1
    assert len(snapshot["records"][0]["sources"]) == 2


def test_archive_retains_event_and_requires_new_source(system):
    record = publish(system, [proposal()])["records"][0]
    append(system, "不需要保留课程 A 的任务")
    delta = {"op": "archive", "record_id": record["record_id"], "expected_revision": 1,
             "archive_sources": [{"seq": 3, "quote": "不需要保留课程 A 的任务"}], "reason": "User withdrawal"}
    state = publish(system, [delta])
    assert state["records"][0]["validity"] == "archived"
    assert record["content"] not in render_context(state["records"], covered_seq=4, budget=4096)["text"]


def test_merge_different_topics_rolls_back(system):
    state = publish(system, [proposal(), proposal(topic="课程 B", content="课程 B 考试准备中", status="active")])
    a, b = sorted(state["records"], key=lambda r: r["topic"])
    append(system)
    delta = proposal(seq=3, quote="现在重新准备课程 A", status="active")
    delta.update(op="merge", record_id=a["record_id"], expected_revision=1, merge_ids=[b["record_id"]], merge_revisions=[1])
    with pytest.raises(ContextRecordConflict, match="preserve"):
        publish(system, [delta])
    assert system.store.snapshot(system.session)["revision"] == 1


def test_child_cannot_read_parent_history(system):
    publish(system, [proposal()])
    child = ToolContext.model_construct(session_id=system.session, tool_view=object())
    reader = ReadContextRecordsTool(system.store)
    invocation = ToolInvocation(invocation_id="child", tool=reader.spec, session_id=system.session, context_id="ctx")
    assert reader.invoke(invocation=invocation, context=child).status == "rejected"
    source = ReadContextSourceTool(system.store)
    assert source.invoke(invocation=invocation.model_copy(update={"tool": source.spec, "input": {"seq": 1}}), context=child).status == "rejected"


def test_atomic_outbox_failure_keeps_intent_and_answer(system):
    queue = BackgroundJobStore(system.db)
    queue.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(db_path=str(system.db), memory=None, store=queue,
        session_service=system.sessions, atomic_context_mode="shadow", atomic_context_debounce_seconds=0)
    def fail(*args, **kwargs):
        raise RuntimeError("queue temporarily unavailable")
    system.sessions.atomic_context_enqueue = fail
    append(system)
    assert system.store.snapshot(system.session)["messages"][-1]["seq"] == 4
    assert coordinator.recover_atomic_context() == 1
    assert coordinator.recover_atomic_context() == 0


def test_automatic_enqueue_keeps_latest_exchange_verbatim(system):
    queue = BackgroundJobStore(system.db)
    queue.ensure_schema()
    MemoryBackgroundCoordinator(db_path=str(system.db), memory=None, store=queue,
        session_service=system.sessions, atomic_context_mode="shadow", atomic_context_debounce_seconds=0)
    append(system, "最新纠正不要压成摘要。")
    job = queue.claim("worker", 600, kinds=("context_records",))
    assert job["payload"]["target_seq"] == 2


def test_cancelled_job_not_resurrected_by_recovery(system):
    queue = BackgroundJobStore(system.db)
    queue.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(db_path=str(system.db), memory=None, store=queue,
        session_service=system.sessions, atomic_context_mode="shadow", atomic_context_debounce_seconds=0)
    append(system)
    job = queue.list(kind="context_records", limit=1)[0]
    result, _ = queue.cancel_controlled(job["job_id"], job["updated_at"])
    assert result == "cancelled"
    assert coordinator.recover_atomic_context() == 0


def test_invalid_model_response_does_not_publish(system):
    class BadModel:
        def complete_text(self, **kwargs):
            return SimpleNamespace(content='{"changes":[{"op":"add","reason":"invented"}]}',
                                   metadata={}, usage={}, finish_reason="stop", partial=False)
    queue = BackgroundJobStore(system.db)
    queue.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(db_path=str(system.db), memory=None, store=queue,
        session_service=system.sessions, llm_client=BadModel(), atomic_context_mode="shadow", atomic_context_debounce_seconds=0)
    with system.factory() as conn:
        coordinator.enqueue_atomic_context(system.session, 2, conn=conn)
    job = queue.claim("worker", 600, kinds=("context_records",))
    with pytest.raises(ValidationError):
        coordinator._bounded_atomic_context(job)
    assert system.store.snapshot(system.session)["covered_seq"] == 0


@pytest.mark.parametrize("value", ["off", "shadow", "active"])
def test_rollout_configuration(value):
    assert MemoryConfig(atomic_context_mode=value).atomic_context_mode == value
    with pytest.raises(ValidationError):
        MemoryConfig(atomic_context_mode="invalid")


def test_background_pipeline_uses_real_queue_and_lease(system):
    class FakeModel:
        def complete_text(self, **kwargs):
            assert kwargs["prompt_summary"] == "background_context_records"
            assert "messages" in json.loads(kwargs["user_prompt"])
            return SimpleNamespace(content=json.dumps({"changes": [passage_proposal(system)]}, ensure_ascii=False),
                                   metadata={}, usage={}, finish_reason="stop", partial=False)

    queue = BackgroundJobStore(system.db)
    queue.ensure_schema()
    coordinator = MemoryBackgroundCoordinator(db_path=str(system.db), memory=None, store=queue,
        session_service=system.sessions, llm_client=FakeModel(), atomic_context_mode="shadow", atomic_context_debounce_seconds=0)
    with system.factory() as conn:
        coordinator.enqueue_atomic_context(system.session, 2, conn=conn)
    job = queue.claim("test-worker", lease_seconds=600, kinds={"context_records"})
    coordinator._bounded_atomic_context(job)
    assert system.store.snapshot(system.session)["covered_seq"] == 2
