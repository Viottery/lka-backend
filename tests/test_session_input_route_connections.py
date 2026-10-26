from types import SimpleNamespace

import pytest
from fastapi import Request

from app.api.routes.session_inputs import SubmitSessionInput, submit_input


class TrackedConnection:
    def __init__(self, conn, *, fail_execute=False):
        self.conn = conn
        self.closed = False
        self.fail_execute = fail_execute

    def execute(self, *args, **kwargs):
        if self.fail_execute:
            raise RuntimeError("preflight failed")
        return self.conn.execute(*args, **kwargs)

    def close(self):
        self.closed = True
        self.conn.close()

    def __enter__(self):
        self.conn.__enter__()
        return self

    def __exit__(self, *args):
        return self.conn.__exit__(*args)


def setup_runtime(tmp_path, *, prior=False, fail_execute=False):
    import sqlite3

    path = tmp_path / "route.sqlite3"
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE session_inputs(session_id TEXT, client_message_id TEXT)")
    if prior:
        setup.execute("INSERT INTO session_inputs VALUES (?, ?)", ("session", "message"))
    setup.commit()
    setup.close()

    opened = []

    def connect():
        tracked = TrackedConnection(sqlite3.connect(path), fail_execute=fail_execute)
        opened.append(tracked)
        return tracked

    calls = []

    def validate(**_kwargs):
        assert opened[-1].closed
        calls.append("validate")

    def submit(_session_id, **_kwargs):
        assert opened[-1].closed
        calls.append("submit")
        return {"accepted": True}

    runtime = SimpleNamespace(
        _conn=connect,
        session_inputs=SimpleNamespace(submit=submit),
        validate_agent_reasoning_selection=validate,
        validate_agent_answer_selection=validate,
    )
    request = Request({
        "type": "http",
        "app": SimpleNamespace(state=SimpleNamespace(runtime=runtime)),
    })
    payload = SubmitSessionInput(
        client_message_id="message", user_input="hello", attachment_ids=[],
    )
    return runtime, request, payload, opened, calls


def test_submit_preflight_connection_closes_before_validation_and_submit(tmp_path):
    _, request, payload, opened, calls = setup_runtime(tmp_path)

    assert submit_input("session", payload, request) == {"accepted": True}

    assert opened[0].closed
    assert calls == ["validate", "validate", "submit"]


def test_repeat_key_closes_preflight_and_skips_validation(tmp_path):
    _, request, payload, opened, calls = setup_runtime(tmp_path, prior=True)

    assert submit_input("session", payload, request) == {"accepted": True}

    assert opened[0].closed
    assert calls == ["submit"]


def test_preflight_query_failure_still_closes_connection(tmp_path):
    _, request, payload, opened, calls = setup_runtime(tmp_path, fail_execute=True)

    with pytest.raises(RuntimeError, match="preflight failed"):
        submit_input("session", payload, request)

    assert opened[0].closed
    assert calls == []
