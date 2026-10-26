import sqlite3

import pytest

from app.domains.management import ManagementService


@pytest.fixture
def management_and_conn(tmp_path):
    path = tmp_path / "management.sqlite3"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY, kind TEXT, status TEXT);"
        "CREATE TABLE manager_controllers(session_id TEXT PRIMARY KEY, "
        "llm_answer_client_name TEXT, llm_answer_model TEXT);"
    )
    conn.execute("BEGIN")
    conn.execute("INSERT INTO agent_sessions VALUES ('manager','manager','active')")
    conn.execute("INSERT INTO manager_controllers VALUES ('manager',NULL,NULL)")

    def unexpected_connection():
        raise AssertionError("input_answer_selection must use the caller connection")

    service = ManagementService(unexpected_connection, session_service=None)
    yield service, conn
    conn.rollback()
    conn.close()


def test_input_answer_selection_preserves_nulls_and_caller_transaction(management_and_conn):
    service, conn = management_and_conn

    assert service.input_answer_selection(conn, "manager") == {
        "llm_answer_client_name": None,
        "llm_answer_model": None,
    }
    conn.execute(
        "UPDATE manager_controllers SET llm_answer_client_name=?,llm_answer_model=? "
        "WHERE session_id='manager'",
        ("provider", "answer-model"),
    )
    assert service.input_answer_selection(conn, "manager") == {
        "llm_answer_client_name": "provider",
        "llm_answer_model": "answer-model",
    }
    assert conn.in_transaction


@pytest.mark.parametrize("session_id,status", [("missing", None), ("manager", "deleted")])
def test_input_answer_selection_rejects_missing_or_deleted_session(
    management_and_conn, session_id, status,
):
    service, conn = management_and_conn
    if status is not None:
        conn.execute("UPDATE agent_sessions SET status=? WHERE session_id='manager'", (status,))

    with pytest.raises(KeyError, match=session_id):
        service.input_answer_selection(conn, session_id)
    assert conn.in_transaction


def test_input_answer_selection_rejects_non_manager_session(management_and_conn):
    service, conn = management_and_conn
    conn.execute("UPDATE agent_sessions SET kind='ordinary' WHERE session_id='manager'")

    with pytest.raises(KeyError, match="manager"):
        service.input_answer_selection(conn, "manager")
