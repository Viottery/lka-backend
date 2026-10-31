"""Session policy reads are request-local and turn creation freezes current authority."""
from __future__ import annotations

import json
from contextlib import closing

import pytest

from tests.test_management_api import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    generator = runtime_fixture.__wrapped__(tmp_path, monkeypatch)
    instance = next(generator)
    yield instance
    try:
        next(generator)
    except StopIteration:
        pass


def test_ordinary_session_policy_uses_one_database_connection(runtime, monkeypatch):
    session_id = runtime.session_service.create_session(title="ordinary").session.session_id
    original_connect = runtime._conn
    calls = 0

    def counted_connect():
        nonlocal calls
        calls += 1
        return original_connect()

    monkeypatch.setattr(runtime, "_conn", counted_connect)
    policy = runtime._session_run_policy(session_id)

    assert calls == 1
    assert policy.role_instructions is None


def test_preflight_reads_policy_once_and_next_request_observes_preference_change(runtime, monkeypatch):
    session_id = runtime.session_service.create_session(title="ordinary").session.session_id
    provider = runtime.session_service.run_policy_provider
    calls = 0

    def counted_policy(sid):
        nonlocal calls
        calls += 1
        return provider(sid)

    monkeypatch.setattr(runtime.session_service, "run_policy_provider", counted_policy)
    arguments = {
        "session_id": session_id, "attachment_ids": [], "llm_client_name": None, "llm_model": None,
        "llm_answer_client_name": None, "llm_answer_model": None, "existing_run_id": None,
        "llm_reasoning_level": None,
    }
    _, first_policy = runtime._preflight_agent_turn(**arguments)
    assert calls == 1

    with closing(runtime._conn()) as conn, conn:
        conn.execute("INSERT INTO agent_ui_preferences(id,preferences,updated_at) VALUES(1,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET preferences=excluded.preferences,updated_at=excluded.updated_at",
                     (json.dumps({"reasoning_level": "high"}), "2026-10-30T00:00:00+00:00"))
    _, second_policy = runtime._preflight_agent_turn(**arguments)

    assert calls == 2
    assert first_policy.llm_reasoning_level == "low"
    assert second_policy.llm_reasoning_level == "high"


def test_run_creation_freezes_latest_controller_authority_after_preflight(runtime):
    session_id = runtime.management_service.create()["session"]["session_id"]
    runtime.management_service.update(session_id, expected_revision=1, status="active")
    prepared, preflight_policy = runtime._preflight_agent_turn(
        session_id=session_id, attachment_ids=[], llm_client_name=None, llm_model=None,
        llm_answer_client_name=None, llm_answer_model=None, existing_run_id=None,
        llm_reasoning_level=None,
    )
    assert "management.start_task" in preflight_policy.allowed_tools

    runtime.management_service.update(session_id, expected_revision=2, status="paused")
    run = runtime.create_agent_run(
        session_id=session_id, user_input="Check frozen authority after pause",
        llm_reasoning_level=None,
    )
    frozen_policy = run.metadata["session_policy"]
    latest_policy = runtime.session_service.run_policy(session_id).model_dump(mode="json")

    assert prepared[0] == []
    assert frozen_policy["allowed_tools"] == latest_policy["allowed_tools"]
    assert "management.start_task" not in frozen_policy["allowed_tools"]
    assert frozen_policy["role_instructions"] == latest_policy["role_instructions"]


def test_admission_passes_its_fresh_policy_to_run_creation_once(runtime, monkeypatch):
    session_id = runtime.session_service.create_session(title="single admission policy read").session.session_id
    provider = runtime.session_service.run_policy_provider
    calls = 0

    def counted_policy(sid):
        nonlocal calls
        calls += 1
        return provider(sid)

    monkeypatch.setattr(runtime.session_service, "run_policy_provider", counted_policy)
    run = runtime.create_agent_run(session_id=session_id, user_input="freeze admission policy")

    assert calls == 1
    assert run.metadata["session_policy"]["llm_reasoning_level"] == "low"
