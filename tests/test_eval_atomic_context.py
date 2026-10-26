"""Private replay selection and no-dispatch defaults."""

import json
from types import SimpleNamespace

import pytest

from app.core.sessions import SessionService
from app.storage.context_records import ContextRecordStore
from app.storage.db import connect, init_db
from evals.lka_evals.live_budget import LiveBudgetExceeded
from scripts import eval_atomic_context


def source_session(tmp_path):
    path = tmp_path / "source.sqlite3"
    init_db(path)
    sessions = SessionService(lambda: connect(path))
    identity = sessions.create_session(title="Replay").session.session_id
    for index in range(3):
        sessions.record_context_exchange(session_id=identity, user_input=f"task {index}",
                                         agent_answer=f"answer {index}", trace_id=f"trace-{index}")
    return path, identity


def test_frozen_prefix_copy_is_readonly_and_session_only(tmp_path):
    source, session = source_session(tmp_path)
    second = SessionService(lambda: connect(source)).create_session(title="Other").session.session_id
    copied = tmp_path / "copy.sqlite3"
    eval_atomic_context.copy_session(source, session, copied, through_seq=4)
    with connect(source) as conn:
        assert conn.execute("SELECT count(*) FROM agent_session_context_messages").fetchone()[0] == 6
    with connect(copied) as conn:
        assert conn.execute("SELECT count(*) FROM agent_session_context_messages").fetchone()[0] == 4
        assert conn.execute("SELECT next_seq FROM agent_session_context_state").fetchone()[0] == 5
        assert conn.execute("SELECT 1 FROM agent_sessions WHERE session_id=?", (second,)).fetchone() is None


def test_copy_refuses_production_as_destination(tmp_path):
    source, session = source_session(tmp_path)
    with pytest.raises(ValueError, match="source database"):
        eval_atomic_context.copy_session(source, session, source)


def test_offline_replay_does_not_load_provider_configuration(tmp_path, monkeypatch):
    source, session = source_session(tmp_path)
    def forbidden():
        raise AssertionError("Offline replay must not load model configuration")
    monkeypatch.setattr(eval_atomic_context, "Settings", forbidden)
    args = SimpleNamespace(source=source, session=session, output=tmp_path / "report", through_seq=4,
                           keep_tail=2, run=False, profile="handoff", batch_messages=2,
                           projection_tokens=2048)
    eval_atomic_context.run(args)
    report = json.loads((args.output / "report.json").read_text())
    assert report["profile"] == "handoff"
    assert report["batch_messages"] == 2
    assert report["semantic_quality"] == "structure_only_requires_separate_semantic_review"
    with connect(source) as conn:
        assert conn.execute("SELECT count(*) FROM agent_session_context_messages").fetchone()[0] == 6


def test_batch_targets_bound_new_messages_and_require_even_size():
    target = eval_atomic_context.next_batch_target
    assert target(0, 10, 4) == 4
    assert target(4, 10, 4) == 8
    assert target(8, 10, 4) == 10
    assert target(4, 10, None) == 10
    with pytest.raises(ValueError, match="even"):
        target(0, 10, 3)


def test_replay_task_budget_is_stable_for_retry_and_changes_after_publication():
    state = {"session_id": "s", "covered_seq": 0, "messages": [{"seq": 1, "content": "new"}]}
    first = eval_atomic_context.replay_task_id(state, "handoff")
    assert first == eval_atomic_context.replay_task_id({**state, "messages": [*state["messages"], {"seq": 2}]}, "handoff")
    assert first != eval_atomic_context.replay_task_id({**state, "covered_seq": 1}, "handoff")
    assert first != eval_atomic_context.replay_task_id(state, "general")
    trial = eval_atomic_context.replay_task_id(state, "handoff", "default24k")
    assert trial != first
    assert trial == eval_atomic_context.replay_task_id(state, "handoff", "default24k")
    assert trial != eval_atomic_context.replay_task_id(state, "handoff", "bulk125k")


def test_budget_failure_reports_committed_prefix_without_publishing_draft(tmp_path, monkeypatch):
    source, session = source_session(tmp_path)
    provider = SimpleNamespace(name="test")
    registry = SimpleNamespace(get=lambda name: provider, register_client=lambda client: None)
    service = SimpleNamespace(registry=registry)
    config = SimpleNamespace(
        memory=SimpleNamespace(background_client_name=None),
        llm=SimpleNamespace(default_client="test", resolve_model_config=lambda name, model:
            SimpleNamespace(tokenizer_json_path=None, context_window_tokens=24000)))
    monkeypatch.setattr(eval_atomic_context, "Settings", lambda: SimpleNamespace(load_local_config=lambda: config))
    monkeypatch.setattr(eval_atomic_context, "build_llm_service", lambda config: service)
    monkeypatch.setattr(eval_atomic_context, "ReplayClient", lambda provider, *args, **kwargs: provider)
    monkeypatch.setattr(eval_atomic_context, "SelectedBackgroundClient", lambda *args, **kwargs: object())
    calls = []

    def bounded_proposal(client, snapshot, messages, **kwargs):
        calls.append(messages[0]["seq"])
        if len(calls) == 2:
            raise LiveBudgetExceeded("test stop before external dispatch")
        return [], SimpleNamespace(usage={})

    monkeypatch.setattr(eval_atomic_context, "propose_delta", bounded_proposal)
    args = SimpleNamespace(source=source, session=session, output=tmp_path / "report", through_seq=6,
        keep_tail=2, run=True, profile="handoff", batch_messages=2, projection_tokens=2048,
        model="deepseek-flash", ledger=tmp_path / "ledger.sqlite3", usd_limit=2, call_limit=4,
        input_tokens=24000)
    with pytest.raises(LiveBudgetExceeded):
        eval_atomic_context.run(args)
    report = json.loads((args.output / "report.json").read_text())
    assert report["completed"] is False and report["covered_seq"] == 2 and report["target_seq"] == 4
    assert report["error_type"] == "LiveBudgetExceeded"
    assert (args.output / "partial_projection.txt").is_file()
    assert calls == [1, 3] and report["budget"]["groups"] == {}
    assert ContextRecordStore(lambda: connect(args.output / "isolated.sqlite3")).snapshot(session)["covered_seq"] == 2


def test_missing_watermark_cannot_create_gapped_sequence(tmp_path):
    source, session = source_session(tmp_path)
    with pytest.raises(ValueError, match="watermark"):
        eval_atomic_context.copy_session(source, session, tmp_path / "missing.sqlite3", through_seq=256)


def test_resume_only_copies_selected_atomic_state_and_preserves_sources(tmp_path):
    source, session = source_session(tmp_path)
    store = ContextRecordStore(lambda: connect(source))
    store.ensure_schema()
    snapshot = store.snapshot(session, target_seq=2)
    store.publish(snapshot, [])
    other = SessionService(lambda: connect(source)).create_session(title="Other").session.session_id
    with connect(source) as conn:
        conn.execute("INSERT INTO context_record_heads VALUES(?,?,?,NULL,?)", (other, 9, 99, "now"))
    copied = tmp_path / "copy.sqlite3"
    eval_atomic_context.copy_session(source, session, copied)
    ContextRecordStore(lambda: connect(copied)).ensure_schema()
    eval_atomic_context.copy_atomic_state(source, session, copied)
    state = ContextRecordStore(lambda: connect(copied)).snapshot(session)
    assert state["covered_seq"] == 2 and len(state["messages"]) == 6
    with connect(copied) as conn:
        assert conn.execute("SELECT count(*) FROM context_record_heads").fetchone()[0] == 1
    assert store.snapshot(session)["covered_seq"] == 2
