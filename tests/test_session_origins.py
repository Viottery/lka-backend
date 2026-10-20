"""Session creation source persistence and compatibility contracts."""

import asyncio
import json
import sqlite3

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.sessions import router as sessions_router
from app.core.sessions import SessionService
from app.storage.db import connect, init_db
from tests.test_management_tasks import manager_context as manager_context_fixture
from tests.test_management_tasks import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def manager_context(runtime):
    return manager_context_fixture.__wrapped__(runtime)


def test_create_and_ensure_persist_source_and_do_not_reclassify_existing(runtime):
    service = runtime.session_service
    human = service.create_session(title="Human", metadata={"creation_source": "system"}).session
    assert human.creation_source == "user"
    assert human.creator_session_id is None

    assistant = service.create_session(
        title="Assistant task", creation_source="assistant", creator_session_id="manager-1"
    ).session
    assert assistant.creation_source == "assistant"
    assert assistant.creator_session_id == "manager-1"

    created_by_ensure = service.ensure_session(session_id="ensured-new")
    assert created_by_ensure.creation_source == "user"
    service.ensure_session(
        session_id="ensured-new", creation_source="assistant", creator_session_id="spoofed"
    )
    current = service.get_session_or_none(session_id="ensured-new")
    assert current.creation_source == "user"
    assert current.creator_session_id is None


def test_manager_task_and_delegate_origins_are_assistant_with_manager_creator(runtime, manager_context):
    parent_id = manager_context.session_id
    task = runtime.managed_tasks.start_task(
        manager_context, command_id="origin-task", purpose="Inspect one task"
    )
    task_session = runtime.session_service.get_session_or_none(session_id=task["session_id"])
    assert task_session.creation_source == "assistant"
    assert task_session.creator_session_id == parent_id
    assert runtime.managed_tasks.start_task(
        manager_context, command_id="origin-task", purpose="Inspect one task"
    ) == task

    delegated = runtime.management_delegates.delegate_work(
        manager_context, command_id="origin-delegate", objective="Inspect delegated work"
    )
    delegate_session = runtime.session_service.get_session_or_none(session_id=delegated["session_id"])
    assert delegate_session.kind == "manager"
    assert delegate_session.creation_source == "assistant"
    assert delegate_session.creator_session_id == parent_id


def test_user_created_manager_and_existing_human_session_keep_user_origin(runtime):
    manager = runtime.management_service.create()
    manager_id = manager["session"]["session_id"]
    manager_session = runtime.session_service.get_session_or_none(session_id=manager_id)
    assert manager_session.kind == "manager"
    assert manager_session.creation_source == "user"
    assert manager_session.creator_session_id is None

    human = runtime.session_service.create_session(title="Existing human thread").session
    runtime.managed_tasks.send_instruction(
        manager_context_fixture.__wrapped__(runtime), command_id="intervene-human",
        session_id=human.session_id, expected_run_id=None, purpose="Continue the existing thread",
    )
    current = runtime.session_service.get_session_or_none(session_id=human.session_id)
    assert current.creation_source == "user"
    assert current.creator_session_id is None


def test_session_api_filters_sources_before_pagination_and_rejects_invalid_values(runtime):
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(sessions_router)
    user_ids = [
        runtime.create_session(title=f"User {index}").session.session_id
        for index in range(2)
    ]
    assistant_ids = [
        runtime.session_service.create_session(
            title=f"Assistant {index}", creation_source="assistant", creator_session_id="manager-x"
        ).session.session_id
        for index in range(2)
    ]
    deleted_id = runtime.session_service.create_session(
        title="Deleted assistant", creation_source="assistant", creator_session_id="manager-x"
    ).session.session_id
    runtime.delete_session(session_id=deleted_id)

    async def exercise():
        transport = ASGITransport(app=app, client=("127.0.0.1", 9000))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/sessions", params={
                "creation_source": "assistant", "creator_session_id": "manager-x", "limit": 1,
            })
            assert first.status_code == 200, first.text
            first_json = first.json()["sessions"]
            assert len(first_json) == 1
            assert first_json[0]["creation_source"] == "assistant"
            second = await client.get("/sessions", params={
                "creation_source": "assistant", "creator_session_id": "manager-x", "limit": 1, "offset": 1,
            })
            assert second.status_code == 200
            second_ids = {item["session_id"] for item in second.json()["sessions"]}
            assert len(second_ids) == 1
            assert not second_ids.intersection({deleted_id})
            assert first_json[0]["session_id"] not in second_ids

            deleted = await client.get("/sessions/deleted", params={
                "creation_source": "assistant", "creator_session_id": "manager-x",
            })
            assert deleted.status_code == 200
            assert [item["session_id"] for item in deleted.json()["sessions"]] == [deleted_id]

            all_sessions = await client.get("/sessions")
            returned_ids = {item["session_id"] for item in all_sessions.json()["sessions"]}
            assert set(user_ids + assistant_ids) <= returned_ids
            for params in ({"creation_source": "forged"}, {"creator_session_id": ""}):
                invalid = await client.get("/sessions", params=params)
                assert invalid.status_code == 422
                invalid_deleted = await client.get("/sessions/deleted", params=params)
                assert invalid_deleted.status_code == 422

            created = await client.post("/sessions", json={
                "title": "Untrusted origin", "creation_source": "system",
                "creator_session_id": "forged", "metadata": {"creation_source": "assistant"},
            })
            assert created.status_code == 200, created.text
            assert created.json()["session"]["creation_source"] == "user"
            assert created.json()["session"]["creator_session_id"] is None

    asyncio.run(exercise())


def test_legacy_origin_migration_is_conservative_idempotent_and_preserves_rows(tmp_path):
    path = tmp_path / "legacy-origins.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE agent_sessions (session_id TEXT PRIMARY KEY,title TEXT NOT NULL,"
            "status TEXT NOT NULL,metadata TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        sessions = [
            ("manager", "Manager", "active", "{}", "created-m", "updated-m"),
            ("task", "Task", "deleted", '{"keep":true}', "created-t", "updated-t"),
            ("work", "Work", "active", "{}", "created-work", "updated-work"),
            ("queued", "Queued", "active", "{}", "created-q", "updated-q"),
            ("delegate", "Delegate", "active", "{}", "created-d", "updated-d"),
            ("child", "Child", "active", "{}", "created-c", "updated-c"),
            ("watch", "Watch", "active", "{}", "created-w", "updated-w"),
            ("instruction", "Instruction", "active", "{}", "created-i", "updated-i"),
            ("fake-metadata", "Fake", "active", '{"creation_source":"assistant"}', "created-f", "updated-f"),
        ]
        conn.executemany("INSERT INTO agent_sessions VALUES(?,?,?,?,?,?)", sessions)
        conn.execute(
            "CREATE TABLE agent_session_messages (message_id TEXT PRIMARY KEY,session_id TEXT,role TEXT,"
            "content TEXT,payload TEXT,created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO agent_session_messages VALUES('kept-message','task','user','audit text','{}','message-time')"
        )
        conn.execute(
            "CREATE TABLE agent_runs (run_id TEXT PRIMARY KEY,session_id TEXT,status TEXT,"
            "record_payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO agent_runs VALUES(?,?, 'completed', ?, 'run-created', 'run-updated')",
            [
                ("manager-run", "manager", json.dumps({"run_id": "manager-run"})),
                ("task-run", "task", json.dumps({"run_id": "task-run"})),
                ("work-run", "work", json.dumps({"run_id": "work-run"})),
                ("queue-run", "queued", json.dumps({"run_id": "queue-run"})),
                ("child-run", "child", json.dumps({"run_id": "child-run", "parent_run_id": "manager-run"})),
                ("watch-run", "watch", json.dumps({"run_id": "watch-run"})),
                ("instruction-run", "instruction", json.dumps({"run_id": "instruction-run"})),
            ],
        )
        conn.execute(
            "CREATE TABLE management_commands (manager_session_id TEXT,command_id TEXT,fingerprint TEXT,"
            "action TEXT,receipt TEXT,created_at TEXT,PRIMARY KEY(manager_session_id,command_id))"
        )
        conn.execute(
            "CREATE TABLE managed_session_links (manager_session_id TEXT,task_session_id TEXT,run_id TEXT,"
            "command_id TEXT,created_at TEXT,PRIMARY KEY(manager_session_id,command_id))"
        )
        for action, command, task_session, run_id in (
            ("start_task", "task-command", "task", "task-run"),
            ("start_work", "work-command", "work", "work-run"),
            ("queue_work", "queue-command", "queued", "queue-run"),
            ("send_instruction", "instruction-command", "instruction", "instruction-run"),
        ):
            conn.execute(
                "INSERT INTO management_commands VALUES('manager',?,'fingerprint',?,'{}','command-time')",
                (command, action),
            )
            conn.execute(
                "INSERT INTO managed_session_links VALUES('manager',?,?,?,'link-time')",
                (task_session, run_id, command),
            )
        conn.execute(
            "CREATE TABLE manager_delegates (delegate_id TEXT PRIMARY KEY,session_id TEXT,parent_session_id TEXT)"
        )
        conn.execute("INSERT INTO manager_delegates VALUES('delegate-id','delegate','manager')")
        conn.execute(
            "CREATE TABLE watch_occurrences (occurrence_id TEXT PRIMARY KEY,run_id TEXT)"
        )
        conn.execute("INSERT INTO watch_occurrences VALUES('occurrence','watch-run')")

    init_db(path)
    init_db(path)
    service = SessionService(lambda: connect(path))
    expected = {
        "manager": ("unknown", None),
        "task": ("assistant", "manager"),
        "work": ("assistant", "manager"),
        "queued": ("assistant", "manager"),
        "delegate": ("assistant", "manager"),
        "child": ("assistant", "manager"),
        "watch": ("system", None),
        "instruction": ("unknown", None),
        "fake-metadata": ("unknown", None),
    }
    for session_id, (source, creator) in expected.items():
        if session_id == "task":
            continue  # get_session_or_none intentionally hides soft-deleted rows
        session = service.get_session_or_none(session_id=session_id)
        assert (session.creation_source, session.creator_session_id) == (source, creator)
    deleted = service.list_deleted_sessions(creation_source="assistant", creator_session_id="manager")
    assert [session.session_id for session in deleted.sessions] == ["task"]
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT status,metadata,created_at,updated_at FROM agent_sessions WHERE session_id='task'"
        ).fetchone()
        assert row == ("deleted", '{"keep":true}', "created-t", "updated-t")
        message = conn.execute(
            "SELECT message_id,content,created_at FROM agent_session_messages WHERE session_id='task'"
        ).fetchone()
        assert message == ("kept-message", "audit text", "message-time")



def test_source_filters_compose_with_project_and_literal_message_search(runtime):
    service = runtime.session_service
    selected = None
    for source, creator, project in (
        ("user", None, "project-a"),
        ("assistant", "owner-a", "project-b"),
        ("assistant", "owner-b", "project-a"),
        ("assistant", "owner-a", "project-a"),
    ):
        session = service.create_session(
            title="Search candidate", creation_source=source, creator_session_id=creator,
            metadata={"project_id": project},
        ).session
        service.append_message(session_id=session.session_id, role="user", content="literal 50%_case")
        selected = session
    result = service.list_sessions(
        creation_source="assistant", creator_session_id="owner-a",
        project_id="project-a", q="50%_case", limit=1,
    )
    assert [item.session_id for item in result.sessions] == [selected.session_id]
    assert service.list_sessions(
        creation_source="assistant", creator_session_id="owner-a",
        project_id="project-a", q="50%_case", limit=1, offset=1,
    ).sessions == []


@pytest.mark.parametrize("executor_kind", ["react", "mail"])
def test_child_executors_record_origin_when_creating_their_session(tmp_path, monkeypatch, executor_kind):
    from types import SimpleNamespace

    from app.core.agent_turn import AgentTurnResult
    from app.core.child_agent import ChildAgentExecutor
    from app.experts.mail import MailIntent
    from tests.test_mail_expert import _case
    from tests.test_mail_expert import setup as mail_setup

    setup = mail_setup.__wrapped__(tmp_path)
    expert, child, snapshot, views, _ = _case(setup, 1, "list")
    manager = setup[2]
    sessions = SessionService(lambda: connect(tmp_path / "mail_expert.sqlite3"))
    parent = manager.get_run(child.parent_run_id)
    sessions.ensure_session(session_id=parent.session_id)

    if executor_kind == "mail":
        expert.session_service = sessions

        async def route(*args):
            return MailIntent(mode="unsupported", reason="No mail action needed for this persistence test")

        monkeypatch.setattr(expert, "_route", route)
        result = asyncio.run(expert.execute(child_run_id=child.run_id, snapshot=snapshot, views=views))
        assert result.status.value == "partial"
    else:
        class Runner:
            turn_loop = SimpleNamespace(session_service=sessions)

            async def run_async(self, **kwargs):
                manager.complete_child_run(child.run_id, result_snapshot={"answer": "Child finished"})
                return AgentTurnResult(
                    run_id=child.run_id, session_id=child.session_id,
                    trace_id=child.trace_id, answer="Child finished",
                )

        result = asyncio.run(ChildAgentExecutor(runner=Runner(), run_manager=manager).execute(
            child_run_id=child.run_id, snapshot=snapshot, views=views,
        ))
        assert result.status.value == "completed"

    session = sessions.get_session(session_id=child.session_id).session
    assert session.creation_source == "assistant"
    assert session.creator_session_id == parent.session_id
    assert sessions.get_session(session_id=parent.session_id).session.creation_source == "user"
