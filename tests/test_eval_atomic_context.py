"""Private replay selection and no-dispatch defaults."""

from types import SimpleNamespace

import pytest

from app.core.sessions import SessionService
from app.storage.context_records import ContextRecordStore
from app.storage.db import connect, init_db
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
    args = SimpleNamespace(source=source, session=session, output=tmp_path / "report", through_seq=4, keep_tail=2, run=False)
    eval_atomic_context.run(args)
    assert (args.output / "report.json").is_file()


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
