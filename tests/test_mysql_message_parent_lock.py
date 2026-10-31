"""Real-MySQL regression for parent/session lock order during message writes."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from tests.test_mysql_runtime_live import runtime as base_runtime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from base_runtime.__wrapped__(tmp_path, monkeypatch)


class _InsertObservedConnection:
    def __init__(self, conn, observed: Event, *, pause: Event | None = None):
        self._conn = conn
        self._observed = observed
        self._pause = pause

    def execute(self, statement, parameters=()):
        result = self._conn.execute(statement, parameters)
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("insert") and "agent_session_messages" in normalized:
            self._observed.set()
            if self._pause is not None and not self._pause.wait(10):
                raise TimeoutError("reply insert test gate was not released")
        return result

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_reply_and_queued_input_lock_parent_before_message_insert(runtime, monkeypatch):
    session_id = runtime.management_service.create(
        title="MySQL parent lock " + uuid4().hex[:12], make_default=True,
    )["session"]["session_id"]
    root = runtime.agent_run_manager.create_run(session_id=session_id, user_input="active root")
    runtime.agent_run_manager.mark_running(root.run_id)

    reply_inserted, release_reply = Event(), Event()
    input_started, input_inserted = Event(), Event()
    original_factory = runtime.session_service._conn_factory
    original_append_in_transaction = runtime.session_service.append_message_in_transaction
    original_authorize = runtime.session_inputs.authorize

    def reply_connection_factory():
        return _InsertObservedConnection(
            original_factory(), reply_inserted, pause=release_reply,
        )

    def observe_input_append(conn, **kwargs):
        observed = _InsertObservedConnection(conn, input_inserted)
        return original_append_in_transaction(observed, **kwargs)

    def observe_input_authorize(conn, sid, *, accepting=False):
        if sid == session_id and not accepting:
            input_started.set()
        return original_authorize(conn, sid, accepting=accepting)

    monkeypatch.setattr(runtime.session_service, "_conn_factory", reply_connection_factory)
    monkeypatch.setattr(runtime.session_service, "append_message_in_transaction", observe_input_append)
    monkeypatch.setattr(runtime.session_inputs, "authorize", observe_input_authorize)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reply_future = pool.submit(
            runtime.session_service.append_message,
            session_id=session_id,
            role="agent",
            content="synthetic application reply",
            message_id="reply_" + uuid4().hex,
            payload={"run_id": root.run_id, "trace_id": root.trace_id},
        )
        input_future = None
        reached_child_insert_before_reply_release = False
        try:
            assert reply_inserted.wait(5), "reply did not reach its child-message insert"
            input_future = pool.submit(
                runtime.session_inputs.submit,
                session_id,
                client_message_id="client_" + uuid4().hex,
                user_input="synthetic queued input",
                attachment_ids=[],
            )
            assert input_started.wait(5), "queued input did not start acceptance"
            # With parent-X-first ordering, the input blocks before its child
            # insert while the reply transaction is paused. Without it, both
            # inserts obtain FK S locks and their later parent UPDATEs deadlock.
            reached_child_insert_before_reply_release = input_inserted.wait(3)
        finally:
            release_reply.set()

        errors = []
        reply_message = None
        receipt = None
        for future, label in ((reply_future, "reply"), (input_future, "input")):
            if future is None:
                continue
            try:
                value = future.result(timeout=15)
                if label == "reply":
                    reply_message = value
                else:
                    receipt = value
            except Exception as exc:  # noqa: BLE001 - preserve concurrent DB failures for assertion.
                errors.append((label, exc))

    assert not errors, (
        f"concurrent message writes failed: {errors!r}; queued-input child insert observed="
        f"{reached_child_insert_before_reply_release}"
    )
    assert not reached_child_insert_before_reply_release, (
        "queued input inserted its child message before acquiring the parent-session X lock"
    )
    assert reply_message is not None and receipt is not None

    monkeypatch.setattr(runtime.session_service, "_conn_factory", original_factory)
    monkeypatch.setattr(runtime.session_service, "append_message_in_transaction", original_append_in_transaction)
    monkeypatch.setattr(runtime.session_inputs, "authorize", original_authorize)

    replay = runtime.session_inputs.submit(
        session_id,
        client_message_id=receipt["input"]["client_message_id"],
        user_input="synthetic queued input",
        attachment_ids=[],
    )
    assert replay["input"] == receipt["input"]

    rollback_message_id = "rollback_" + uuid4().hex
    callback_called = Event()

    def fail_callback(_conn, _message):
        callback_called.set()
        raise RuntimeError("synthetic callback failure")

    with pytest.raises(RuntimeError, match="synthetic callback failure"):
        runtime.session_service.append_message(
            session_id=session_id,
            role="agent",
            content="must roll back",
            message_id=rollback_message_id,
            persisted_message_callback=fail_callback,
        )
    assert callback_called.is_set()

    with runtime._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM session_inputs WHERE session_id=? AND "
            "client_message_id=?", (session_id, receipt["input"]["client_message_id"]),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_session_messages WHERE session_id=? AND message_id=?",
            (session_id, rollback_message_id),
        ).fetchone()[0] == 0
        active = conn.execute(
            "SELECT run_id,record_payload FROM agent_runs WHERE session_id=? "
            "AND status NOT IN ('completed','failed','cancelled','timed_out')",
            (session_id,),
        ).fetchall()
    active_roots = [row["run_id"] for row in active
                    if json.loads(row["record_payload"]).get("parent_run_id") is None]
    assert active_roots == [root.run_id]
