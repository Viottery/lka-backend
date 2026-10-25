"""Polling with no new delegation/watch events should not take SQLite's writer slot."""
from contextlib import closing

import pytest

from app.core.agent_runs import AgentRunStatus
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_delegates import child_context
from tests.test_management_tasks import manager_context as context_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def manager_context(runtime):
    return context_fixture.__wrapped__(runtime)


def count_write_begins(service, factory):
    result = {"count": 0}

    class Connection:
        def __init__(self):
            self.conn = factory()

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                result["count"] += 1
            return self.conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    service._conn = Connection
    return result


def test_empty_delegate_and_watch_polls_do_not_take_writer_slot(runtime):
    delegate_begins = count_write_begins(runtime.management_delegates, runtime._conn)
    watch_begins = count_write_begins(runtime.management_watch_subscriptions, runtime._conn)

    assert runtime.management_delegates.capture_events() == 0
    assert runtime.management_watch_subscriptions.tick() == 0
    assert delegate_begins["count"] == 0
    assert watch_begins["count"] == 0


def test_repolling_captured_failed_delegate_event_does_not_take_writer_slot(runtime, manager_context):
    receipt = runtime.management_delegates.delegate_work(
        manager_context, command_id="idle-failure", objective="Inspect state"
    )
    context = child_context(runtime, receipt["session_id"])
    runtime.agent_run_manager._update_run(context.run_id, status=AgentRunStatus.FAILED)
    assert runtime.management_delegates.capture_events() == 1

    begins = count_write_begins(runtime.management_delegates, runtime._conn)
    assert runtime.management_delegates.capture_events() == 0
    assert begins["count"] == 0
    with closing(runtime._conn()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM manager_inbox WHERE session_id=? AND dedup_key LIKE 'delegate_execution_issue:%'",
            (manager_context.session_id,),
        ).fetchone()[0] == 1
