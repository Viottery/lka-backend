import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.sessions import SessionService
from app.storage.db import connect, init_db


def test_existing_database_migration_indexes_parent_lookup_and_guards_completion(tmp_path):
    db_path = tmp_path / "lka.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("DROP INDEX idx_agent_runs_parent_run_id")

    store = SqliteAgentRunStore(db_path)
    with connect(db_path) as conn:
        index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_agent_runs_parent_run_id",),
        ).fetchone()
        assert index is not None
        assert "json_extract(record_payload, '$.parent_run_id')" in index[0]

        seed_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT run_id,status FROM agent_runs "
            "WHERE json_extract(record_payload,'$.parent_run_id')=?",
            ("root",),
        ).fetchall()
        recursive_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT child.run_id,child.status FROM agent_runs child "
            "JOIN (SELECT 'root' AS run_id) parent "
            "ON json_extract(child.record_payload,'$.parent_run_id')=parent.run_id"
        ).fetchall()
        assert any("SEARCH agent_runs USING INDEX idx_agent_runs_parent_run_id" in row[3] for row in seed_plan)
        assert any("SEARCH child USING INDEX idx_agent_runs_parent_run_id" in row[3] for row in recursive_plan)

    sessions = SessionService(lambda: connect(db_path))
    sessions.ensure_session(session_id="parent-lookup-session")
    manager = InMemoryAgentRunManager(durable_store=store)
    root = manager.create_run(session_id="parent-lookup-session", user_input="root")
    manager.mark_running(root.run_id)
    child = manager.create_child_run(
        parent_run_id=root.run_id, plan_id="plan", step_id="child", attempt=1,
        user_input="child",
    )
    manager.mark_child_running(child.run_id)

    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="unfinished descendants"):
            store.complete_run_in_transaction(conn, root.run_id, result_snapshot={"answer": "done"})
        conn.rollback()

    manager.complete_child_run(child.run_id, result_snapshot={"answer": "child done"})
    completed = manager.complete_run(root.run_id, result_snapshot={"answer": "done"})
    assert completed.status.value == "completed"
