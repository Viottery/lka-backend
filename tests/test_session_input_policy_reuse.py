from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

import pytest

from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.session_inputs import SessionInputConflict
from app.core.sessions import SessionRunPolicy
from tests.test_session_input_queue import runtime as base_runtime
from tests.test_session_input_queue import sid


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from base_runtime.__wrapped__(tmp_path, monkeypatch)


def test_policy_selection_preflight_uses_one_fresh_snapshot_per_input(runtime, monkeypatch):
    session_id = sid(runtime)
    current_policy = SessionRunPolicy(
        llm_client_name="primary-a", llm_model="model-a",
        llm_answer_client_name="answer-a", llm_answer_model="answer-model-a",
        llm_reasoning_level="low", llm_reasoning_source="session",
    )
    policy_reads = []
    reasoning_levels = []
    answer_selections = []
    opened = []
    original_connect = runtime.session_inputs._conn

    class TrackedConnection:
        def __init__(self, conn):
            self.conn = conn
            self.closed = False

        def execute(self, *args, **kwargs):
            return self.conn.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def close(self):
            self.closed = True
            self.conn.close()

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

    def tracked_connect():
        conn = TrackedConnection(original_connect())
        opened.append(conn)
        return conn

    runtime.session_inputs._conn = tracked_connect
    original_reasoning_validator = runtime.validate_agent_reasoning_selection
    original_answer_validator = runtime.validate_agent_answer_selection

    def get_policy(requested_session_id):
        assert requested_session_id == session_id
        policy_reads.append(current_policy.llm_reasoning_level)
        return current_policy.model_copy(deep=True)

    def capture_reasoning(*, frozen_policy=None, **kwargs):
        reasoning_levels.append(frozen_policy.llm_reasoning_level if frozen_policy else None)
        return original_reasoning_validator(frozen_policy=frozen_policy, **kwargs)

    def capture_answer(*, frozen_policy=None, **kwargs):
        answer_selections.append((frozen_policy.llm_answer_client_name,
                                  frozen_policy.llm_answer_model) if frozen_policy else None)
        return original_answer_validator(frozen_policy=frozen_policy, **kwargs)

    def validate_selection(**kwargs):
        assert opened and all(conn.closed for conn in opened)
        return runtime.validate_agent_input_selection(**kwargs)

    monkeypatch.setattr(runtime.session_service, "run_policy", get_policy)
    monkeypatch.setattr(runtime, "validate_agent_reasoning_selection", capture_reasoning)
    monkeypatch.setattr(runtime, "validate_agent_answer_selection", capture_answer)
    runtime.session_inputs.validate_selection = validate_selection
    runtime.session_inputs.validate_attachments = lambda **_kwargs: None
    attachment_ids = ["att_" + "a" * 32]

    first = runtime.session_inputs.submit(
        session_id, client_message_id="policy-a", user_input="first", attachment_ids=attachment_ids,
    )
    assert first["input"]["status"] == "queued"
    assert policy_reads == ["low"]
    assert reasoning_levels == ["low"]
    assert answer_selections == [("answer-a", "answer-model-a")]

    current_policy = current_policy.model_copy(update={
        "llm_answer_client_name": "answer-b", "llm_answer_model": "answer-model-b",
        "llm_reasoning_level": "high",
    })
    second = runtime.session_inputs.submit(
        session_id, client_message_id="policy-b", user_input="second", attachment_ids=attachment_ids,
    )
    assert second["input"]["status"] == "queued"
    assert policy_reads == ["low", "high"]
    assert reasoning_levels == ["low", "high"]
    assert answer_selections == [("answer-a", "answer-model-a"), ("answer-b", "answer-model-b")]

    def unexpected_revalidation(**_kwargs):
        raise AssertionError("An exact retry must not revalidate mutable policy or attachments.")

    runtime.session_inputs.validate_selection = unexpected_revalidation
    runtime.session_inputs.validate_attachments = unexpected_revalidation
    retry = runtime.session_inputs.submit(
        session_id, client_message_id="policy-a", user_input="first", attachment_ids=attachment_ids,
    )
    assert retry["input"] == first["input"]
    assert retry["queue"]["pending_count"] == 2


def test_answer_and_reasoning_request_overrides_keep_existing_precedence():
    class Resolver:
        session_service = SimpleNamespace(run_policy=lambda _session_id: pytest.fail("snapshot should be used"))

    policy = SessionRunPolicy(
        llm_answer_client_name="answer-preference",
        llm_answer_model="answer-preference-model",
        llm_reasoning_level="high",
        llm_reasoning_source="session",
    )

    resolve_answer = LocalKnowledgeAgentRuntime.resolve_agent_answer_model
    resolve_reasoning = LocalKnowledgeAgentRuntime.resolve_agent_reasoning_level
    resolver = Resolver()

    assert resolve_answer(resolver, session_id="session", frozen_policy=policy) == (
        "answer-preference", "answer-preference-model",
    )
    assert resolve_answer(
        resolver, session_id="session", requested_client="request-client",
        requested_model="request-model", frozen_policy=policy,
    ) == ("request-client", "request-model")
    assert resolve_reasoning(resolver, session_id="session", requested=None,
                             frozen_policy=policy) == ("high", "session")
    assert resolve_reasoning(resolver, session_id="session", requested="low",
                             frozen_policy=policy) == ("low", "request")


def test_session_input_authorization_precedes_selection_preflight(runtime, monkeypatch):
    other_session = runtime.session_service.create_session(title="Other session").session.session_id
    calls = []
    runtime.session_inputs.validate_selection = lambda **_kwargs: calls.append("validated")

    with pytest.raises(SessionInputConflict, match="default_manager"):
        runtime.session_inputs.submit(
            other_session, client_message_id="unauthorized", user_input="no", attachment_ids=[],
        )
    assert calls == []


def test_manager_policy_reads_one_connection_and_refreshes_controller_preferences(runtime, monkeypatch):
    session_id = sid(runtime)
    controller = runtime.management_service.get(session_id)["controller"]
    runtime.management_service.update(
        session_id, expected_revision=controller["revision"],
        llm_client_name="primary-current", llm_model="primary-model-current",
        llm_answer_client_name="answer-current", llm_answer_model="answer-model-current",
        reasoning_level="high",
    )
    original_connect = runtime._conn
    opened = []

    def tracked_connect():
        conn = original_connect()
        opened.append(conn)
        return conn

    monkeypatch.setattr(runtime, "_conn", tracked_connect)
    policy = runtime._session_run_policy(session_id)
    assert len(opened) == 1
    assert policy.llm_client_name == "primary-current"
    assert policy.llm_model == "primary-model-current"
    assert policy.llm_answer_client_name == "answer-current"
    assert policy.llm_answer_model == "answer-model-current"
    assert policy.llm_reasoning_level == "high"
    assert policy.llm_reasoning_source == "controller"

    monkeypatch.setattr(runtime, "_conn", original_connect)
    updated = runtime.management_service.get(session_id)["controller"]
    runtime.management_service.update(
        session_id, expected_revision=updated["revision"], reasoning_level=None,
    )
    with closing(runtime._conn()) as conn, conn:
        conn.execute(
            "INSERT INTO agent_ui_preferences(id,preferences,updated_at) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET preferences=excluded.preferences,updated_at=excluded.updated_at",
            ((
                '{"reasoning_level":"max","llm_answer_client":"ui-answer",'
                '"llm_answer_model":"ui-answer-model"}'
            ), "synthetic-now"),
        )
    policy = runtime._session_run_policy(session_id)
    assert policy.llm_reasoning_level == "max"
    assert policy.llm_reasoning_source == "ui_default"
    # Manager answer selection remains controlled by its controller row.
    assert policy.llm_answer_client_name == "answer-current"
    assert policy.llm_answer_model == "answer-model-current"


def test_session_input_authorization_keeps_default_and_delegate_rules(runtime):
    original_default = sid(runtime)
    other_manager = runtime.management_service.create()["session"]["session_id"]
    ordinary = runtime.session_service.create_session(title="Input authorization ordinary").session.session_id

    with closing(runtime._conn()) as conn, conn:
        conn.execute("BEGIN")
        runtime._authorize_session_input(conn, original_default, accepting=True)
        conn.execute("INSERT INTO session_input_queues(session_id) VALUES(?)", (original_default,))
        with pytest.raises(SessionInputConflict, match="default_manager"):
            runtime._authorize_session_input(conn, other_manager, accepting=False)
        conn.execute("INSERT INTO session_input_queues(session_id) VALUES(?)", (other_manager,))
        runtime._authorize_session_input(conn, other_manager, accepting=False)
        with pytest.raises(SessionInputConflict, match="default_manager"):
            runtime._authorize_session_input(conn, other_manager, accepting=True)
        with pytest.raises(SessionInputConflict, match="default_manager"):
            runtime._authorize_session_input(conn, ordinary, accepting=False)
        conn.commit()

    runtime.management_service.create(make_default=True)
    with closing(runtime._conn()) as conn, conn:
        conn.execute("BEGIN")
        runtime._authorize_session_input(conn, original_default, accepting=False)
        with pytest.raises(SessionInputConflict, match="default_manager"):
            runtime._authorize_session_input(conn, original_default, accepting=True)
        runtime._authorize_session_input(conn, sid(runtime), accepting=True)
        conn.rollback()

    from tests.test_management_tasks import manager_context

    parent = manager_context.__wrapped__(runtime)
    delegated = runtime.management_delegates.delegate_work(
        parent, command_id="auth-delegate", objective="Bounded authorization check",
    )
    with closing(runtime._conn()) as conn, conn:
        conn.execute("BEGIN")
        with pytest.raises(SessionInputConflict, match="default_manager"):
            runtime._authorize_session_input(conn, delegated["session_id"], accepting=False)
        conn.rollback()


@pytest.mark.parametrize('replace_run_id', [None, 'unopened-replacement'])
def test_normal_and_replacement_inputs_use_write_admission_before_control_transaction(runtime, monkeypatch, replace_run_id):
    from app.storage.keyed_write_admission import KeyedWriteAdmission, KeyedWriteAdmissionError

    session_id = sid(runtime)
    gate = KeyedWriteAdmission(capacity=1)
    runtime.session_inputs.write_admission = gate
    monkeypatch.setattr(runtime.session_inputs, '_submit_replacement',
                        lambda *_: pytest.fail('replacement bypassed admission'))
    with gate.admit(session_id), pytest.raises(KeyedWriteAdmissionError, match='full'):
        runtime.session_inputs.submit(session_id, client_message_id='not-accepted', user_input='hello',
                                      attachment_ids=[], replace_run_id=replace_run_id)
    with runtime._conn() as conn:
        assert conn.execute('SELECT count(*) FROM session_inputs WHERE session_id=?', (session_id,)).fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM agent_session_messages WHERE session_id=?', (session_id,)).fetchone()[0] == 0
    assert gate.snapshot() == {'keys': 0, 'tickets': 0}
