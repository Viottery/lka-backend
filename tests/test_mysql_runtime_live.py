"""Real-MySQL integration of the product runtime; all model outputs are synthetic."""
from __future__ import annotations

import json
import time
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.core.local_config import (
    AgentConfig,
    EmbeddingConfig,
    LLMProviderConfig,
    LocalAppConfig,
    ManagementConfig,
    MemoryConfig,
    MessageHistoryConfig,
    RerankerConfig,
)
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.tools import ToolContext
from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput
from app.domains.memory import MemoryInput, MemorySourceInput
from app.storage.connections import unregister_mysql
from tests.test_mysql_owner_live import _config
from tests.test_unified_entry_quality import ScriptedClient, operation

SYNTHETIC_REVIEW_PASS = json.dumps({
    "faithful": True, "hallucination_free": True, "request_aligned": True,
    "decision": "pass", "issues": [], "revision_instructions": "",
})


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = _config()
    local = LocalAppConfig(
        llm=LLMProviderConfig(provider="mock"),
        agent=AgentConfig(unified_entry_enabled=True, orchestrator="langgraph", checkpoint_backend="sqlite"),
        management=ManagementConfig(enabled=True, autonomous_enabled=False),
        memory=MemoryConfig(enabled=False, background_enabled=False),
        message_history=MessageHistoryConfig(enabled=False, background_enabled=False),
        embedding=EmbeddingConfig(enabled=False), reranker=RerankerConfig(enabled=False),
    )
    monkeypatch.setattr(Settings, "load_local_config", lambda _: local)
    data = tmp_path / "mysql-runtime"
    settings = Settings(_env_file=None, data_dir=data, storage_backend="mysql",
                        mysql_host=config.host, mysql_port=config.port, mysql_user=config.user,
                        mysql_password=config.password, mysql_database=config.database,
                        workspace_roots=str(tmp_path), db_timing_enabled=False)
    assert settings.data_dir == data
    instance = LocalKnowledgeAgentRuntime(settings)
    assert instance.db_path == data / "lka.sqlite3"
    assert not instance.db_path.exists(), "Main authority silently fell back to SQLite"
    assert instance._mysql_config == settings.mysql_config()
    try:
        yield instance
    finally:
        instance.stop()
        unregister_mysql(instance.db_path, expected=instance._mysql_config)


def test_mock_graph_turn_persists_reply_run_events_and_checkpoints(runtime):
    session = runtime.create_session(title="Synthetic MySQL turn " + uuid4().hex)
    client = ScriptedClient([operation("final_answer", reason="Synthetic isolated test"), "MYSQL_GRAPH_REPLY", SYNTHETIC_REVIEW_PASS])
    runtime.agent_turn_loop.llm_client = client
    result = runtime.run_agent_turn(session_id=session.session.session_id, user_input="Reply to this synthetic test.")
    assert result.answer == "MYSQL_GRAPH_REPLY"
    assert [call["metadata"]["stage"] for call in client.calls] == ["decision", "context_answer", "answer_review"]
    assert runtime.agent_run_manager.get_run(result.run_id).status.value == "completed"
    assert any(message.content == "MYSQL_GRAPH_REPLY" for message in runtime.get_session(session_id=session.session.session_id).messages)
    with runtime._conn() as conn:
        assert conn.dialect == "mysql"
        assert conn.execute("SELECT COUNT(*) FROM agent_run_events WHERE run_id=?", (result.run_id,)).fetchone()[0] > 0
        assert conn.execute("SELECT 1 FROM agent_runs WHERE run_id=? "
                            "AND json_extract(record_payload,'$.parent_run_id') IS NULL",
                            (result.run_id,)).fetchone() is not None
        assert conn.execute_native("SELECT COUNT(*) FROM checkpoints WHERE thread_id=%s", (result.run_id,)).fetchone()[0] > 0
    assert not runtime.db_path.exists()


def test_human_session_and_message_search_casefold_and_keep_literal_scope(runtime):
    suffix = uuid4().hex
    title_session = runtime.session_service.create_session(
        title=f"CaseProbeAlpha title {suffix} 100%_",
    ).session
    content_session = runtime.session_service.create_session(title=f"Unrelated title {suffix}").session
    runtime.session_service.append_message(
        session_id=content_session.session_id,
        role="user",
        content=f"BodyProbeAlpha content {suffix}",
    )
    wildcard_decoy = runtime.session_service.create_session(
        title=f"Unrelated 100XX {suffix}",
    ).session

    casefold_sessions = {item.session_id for item in runtime.session_service.list_sessions(q="caseprobealpha").sessions}
    assert casefold_sessions == {title_session.session_id}
    content_sessions = {item.session_id for item in runtime.session_service.list_sessions(q="bodyprobealpha").sessions}
    assert content_sessions == {content_session.session_id}
    literal_sessions = {item.session_id for item in runtime.session_service.list_sessions(q="%_").sessions}
    assert literal_sessions == {title_session.session_id}
    assert wildcard_decoy.session_id not in literal_sessions

    account_id = "search-account-" + suffix
    conversation_id = "search-conversation-" + suffix
    policy = runtime.message_history.set_policy({
        "platform": "synthetic", "account_id": account_id, "conversation_type": "private",
        "conversation_id": conversation_id, "expected_revision": 0,
        "record_enabled": True, "analysis_enabled": False,
    })
    imported = runtime.message_history.import_messages([
        {
            "platform": "synthetic", "account_id": account_id, "message_id": "message-case-" + suffix,
            "conversation_type": "private", "conversation_id": conversation_id,
            "sender_id": "sender-1", "sender_name": "CaseProbeAlpha Sender",
            "text": "CaseProbeAlpha literal 100%_done", "sent_at": 100, "received_at": 200,
        },
        {
            "platform": "synthetic", "account_id": account_id, "message_id": "message-decoy-" + suffix,
            "conversation_type": "private", "conversation_id": conversation_id,
            "sender_id": "sender-2", "sender_name": "Other Sender",
            "text": "Other literal 100XXdone", "sent_at": 101, "received_at": 201,
        },
    ])
    assert len(imported["acknowledged"]) == 2

    message_matches = runtime.message_history.search(
        "caseprobealpha", policy["conversation_key"], sender="caseprobealpha sender",
        allowed_sources=[policy["source_id"]], allowed_accounts=[policy["account_scope_id"]],
    )["messages"]
    assert [message["provider_message_id"] for message in message_matches] == ["message-case-" + suffix]
    literal_matches = runtime.message_history.search(
        "%_", policy["conversation_key"], allowed_sources=[policy["source_id"]],
        allowed_accounts=[policy["account_scope_id"]],
    )["messages"]
    assert [message["provider_message_id"] for message in literal_matches] == ["message-case-" + suffix]
    with pytest.raises(PermissionError):
        runtime.message_history.search(
            "caseprobealpha", policy["conversation_key"], allowed_sources=["wrong-source"],
            allowed_accounts=[policy["account_scope_id"]],
        )


def test_runtime_start_recovers_idle_mysql_workers_and_stops_cleanly(runtime, monkeypatch):
    """Exercise startup SQL without allowing shared lab work to be claimed."""
    from datetime import UTC, datetime, timedelta

    from app.core.scheduling_signals import RecoveryPage

    # These are the actual read paths used at startup. The page is not consumed
    # here, so any older synthetic queue rows in the shared lab remain untouched.
    page = runtime.session_inputs._recover_inputs(None, 8)
    assert isinstance(page, RecoveryPage)
    manager_id = runtime.management_service.create(
        title="MySQL startup candidate " + uuid4().hex[:12],
    )["session"]["session_id"]
    assert runtime.management_goals.capture_events(session_ids=[manager_id]) == 0

    # Claim loops remain real lifecycle threads, while the watch runner (which
    # is unrelated to this startup SQL regression) stays disabled.
    runtime.watch_scheduler.worker_count = 0

    # Runtime start compacts terminal job history. Bound its cutoff beyond any
    # plausible existing lab data and assert that this bounded statement cannot
    # match shared rows before the startup workers invoke it.
    cutoff = (datetime.now(UTC) - timedelta(days=365_000)).isoformat()
    with runtime._conn() as conn:
        old_terminal_rows = conn.execute(
            "SELECT COUNT(*) FROM background_jobs WHERE status IN "
            "('succeeded','failed','cancelled') AND finished_at<? AND payload_json!='{}'",
            (cutoff,),
        ).fetchone()[0]
    assert old_terminal_rows == 0
    original_compact = runtime.background_job_store.compact_history
    compaction_calls = []

    def bounded_compact_history(*, retention_days=30, limit=500):
        compaction_calls.append((retention_days, limit))
        return original_compact(retention_days=365_000, limit=limit)

    monkeypatch.setattr(runtime.background_job_store, "compact_history", bounded_compact_history)

    class IdleJobStore:
        """Keep real claim threads alive while fencing them from shared jobs."""

        def __init__(self, delegate):
            self.delegate = delegate
            self.claim_calls = 0

        def compact_history(self, **kwargs):
            return self.delegate.compact_history(**kwargs)

        def has_ready(self, **kwargs):
            return False

        def next_due(self, **kwargs):
            return None

        def claim(self, *args, **kwargs):
            self.claim_calls += 1

    dispatch_idle_store = IdleJobStore(runtime.agent_dispatch.worker.store)
    manager_idle_store = IdleJobStore(runtime.management_coordinator.worker.store)
    runtime.agent_dispatch.worker.store = dispatch_idle_store
    # Execution admission probes dispatcher.store directly, rather than the
    # worker's store. Fence that claim source too: this lifecycle test must not
    # consume another test's durable jobs from the shared synthetic schema.
    runtime.agent_dispatch.worker.claim_permitted = lambda _owner: (None, None, False)
    runtime.agent_dispatch.worker.reconcile = lambda: 0
    runtime.management_coordinator.worker.store = manager_idle_store

    # Startup's keyed scheduler can discover old pending rows; shadow input
    # scheduling performs the SQL scan but never binds or consumes those rows.
    runtime.session_inputs._scheduling_mode = "shadow"
    # Management startup's global scan is suppressed; the scoped candidate SQL
    # above is the actual tested wake-discovery path for this unique manager.
    monkeypatch.setattr(runtime.management_coordinator, "tick", lambda now=None: {
        "captured": 0, "scheduled": 0,
    })

    runtime.start()
    runtime.start()  # starting an already healthy runtime is idempotent
    assert runtime._runtime_started
    assert runtime.health()["status"] == "ok"
    assert runtime.health()["storage_backend"] == "mysql"
    assert compaction_calls
    assert runtime.scheduler.status()["started"]

    deadline = time.monotonic() + 3
    while runtime.scheduler.status()["inflight"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.scheduler.status()["inflight"] == 0
    assert any(thread.is_alive() for thread in runtime.agent_dispatch.worker._threads)
    assert any(thread.is_alive() for thread in runtime.management_coordinator.worker._threads)
    assert not runtime.watch_scheduler._threads
    assert dispatch_idle_store.claim_calls == 0
    assert manager_idle_store.claim_calls == 0

    runtime.stop()
    assert not runtime._runtime_started
    assert runtime._runtime_closed
    assert not runtime.scheduler.status()["started"]
    assert not runtime.agent_dispatch.worker._threads
    assert not runtime.management_coordinator.worker._threads
    # A stopped coordinator has released its authority; readiness must not
    # continue advertising writable MySQL service after that release.
    assert runtime.health()["status"] == "unavailable"


def test_filesystem_tool_uses_regular_audit_and_delivers_from_mysql(runtime, tmp_path):
    source = tmp_path / "synthetic.txt"
    source.write_text("MYSQL_TOOL_EVIDENCE", encoding="utf-8")
    session = runtime.create_session(title="Synthetic MySQL tool " + uuid4().hex)
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="filesystem"),
        operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(source)}),
        operation("final_answer", reason="The source was read"), "MYSQL_TOOL_EVIDENCE", SYNTHETIC_REVIEW_PASS,
    ])
    result = runtime.run_agent_turn(session_id=session.session.session_id, user_input="Read the synthetic file.")
    assert result.answer == "MYSQL_TOOL_EVIDENCE"
    assert [event.tool_name for event in result.tool_events] == ["filesystem.read_file"]
    with runtime._conn() as conn:
        row = conn.execute("SELECT status FROM agent_tool_invocations WHERE run_id=?", (result.run_id,)).fetchone()
        assert row and row[0] == "completed"


def test_memory_provenance_dedup_and_source_revoke(runtime):
    marker = "mysql-memory-" + uuid4().hex
    source = runtime.memory_service.register_source(MemorySourceInput(
        source_type="synthetic-test", source_ref=marker, trusted_source=True,
    ))
    payload = MemoryInput(content=marker + " 用户偏好已确认", source_id=source, user_confirmed=True)
    first = runtime.memory_service.create(payload)
    second = runtime.memory_service.create(payload)
    assert first.memory_id == second.memory_id
    assert any(item.memory_id == first.memory_id for item in runtime.memory_service.search(marker))
    runtime.memory_service.revoke_source(source)
    assert not runtime.memory_service.search(marker)


def test_knowledge_import_and_history_search_keep_session_boundaries(runtime):
    marker = "mysqlknowledge" + uuid4().hex
    imported = runtime.knowledge_service.import_text_document(KnowledgeDocumentInput(
        source=KnowledgeSourceInput(display_name=marker, uri="synthetic://" + marker),
        title=marker, text="Synthetic knowledge " + marker, uri="synthetic://" + marker,
    ))
    result = runtime.knowledge_service.search(query=marker)
    assert any(item.document_id == imported.document_id for item in result.results)
    first = runtime.create_session(title="Synthetic history " + marker)
    second = runtime.create_session(title="Other synthetic history " + marker)
    with runtime._conn() as conn:
        conn.execute("INSERT INTO agent_session_context_messages(session_id,seq,role,content,created_at,trace_id) "
                     "VALUES(?,1,'user',?,'2026-10-29T00:00:00+00:00',?)",
                     (first.session.session_id, marker, marker))
    from app.domains.conversation_history import ConversationHistoryService
    history = ConversationHistoryService(runtime._conn)
    assert history.search(ToolContext(session_id=first.session.session_id), query=marker)["messages"]
    assert not history.search(ToolContext(session_id=second.session.session_id), query=marker)["messages"]


def test_mail_import_is_idempotent_and_search_loads_the_same_message(runtime):
    from app.domains.mail import MailAccountInput, MailMessageInput

    marker = "mysqlmail" + uuid4().hex
    account = MailAccountInput(email_address=marker + "@example.invalid")
    message = MailMessageInput(external_id=marker, subject=marker, body_text="Synthetic body " + marker,
                               received_at="2026-10-29T00:00:00+00:00")
    first = runtime.mail_service.import_messages(account=account, messages=[message])
    second = runtime.mail_service.import_messages(account=account, messages=[message])
    assert first.account_id == second.account_id
    result = runtime.mail_service.search_messages(query=marker)
    assert len(result.messages) == 1
    assert result.messages[0].subject == marker
    with runtime._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mail_messages WHERE account_id=?", (first.account_id,)).fetchone()[0] == 1


def test_manager_publication_rolls_back_all_projections_and_retries_once(runtime, monkeypatch):
    from datetime import UTC, datetime

    from tests.test_management_coordinator import finalize

    runtime.local_app_config.management.autonomous_enabled = True
    runtime.local_app_config.management.min_wake_interval_seconds = 0
    manager = runtime.management_service.create(title="MySQL manager " + uuid4().hex[:12])
    sid = manager["session"]["session_id"]
    runtime.management_service.update(sid, expected_revision=1, status="active")
    runtime.management_goals.create_goal(sid, objective="Synthetic publication check",
                                         next_wake_at=datetime.now(UTC).isoformat())
    runtime.management_goals.capture_events()
    store = runtime.agent_dispatch.store
    kind = "test-manager-wake-" + uuid4().hex[:12]
    store.enqueue(kind, sid, "synthetic-wake", {"session_id": sid})
    job = store.claim("synthetic-manager-wake", 60, kinds=(kind,))
    assert job is not None
    runtime.management_coordinator._bind(job)
    assert store.complete(job["job_id"], job["lease_owner"], job["lease_epoch"])
    executions = runtime.management_goals.list_executions(sid)["executions"]
    assert len(executions) == 1
    run = runtime.agent_run_manager.get_run(executions[0]["run_id"])
    runtime.agent_run_manager.mark_running(run.run_id)
    original = runtime.session_service.append_message_in_transaction
    def fail_after_message(conn, *args, **kwargs):
        original(conn, *args, **kwargs)
        raise RuntimeError("injected publication failure before commit")
    monkeypatch.setattr(runtime.session_service, "append_message_in_transaction", fail_after_message)
    with pytest.raises(RuntimeError, match="injected publication failure"):
        finalize(runtime, run)
    assert runtime.management_goals.list_deliveries(sid)["deliveries"] == []
    assert runtime.get_session(session_id=sid).messages == []
    assert all(event["consumed_execution_id"] is None
               for event in runtime.management_goals.list_events(sid)["events"])
    assert runtime.agent_run_manager.get_run(run.run_id).status.value == "running"
    monkeypatch.setattr(runtime.session_service, "append_message_in_transaction", original)
    assert finalize(runtime, run)
    assert finalize(runtime, run)
    assert len(runtime.management_goals.list_deliveries(sid)["deliveries"]) == 1
    assert len(runtime.get_session(session_id=sid).messages) == 1
    assert runtime.agent_run_manager.get_run(run.run_id).status.value == "completed"
    # Keep the completed execution, audit and delivery for manual lab inspection.
    runtime.management_service.update(sid, expected_revision=2, status="paused")


def test_next_user_input_runs_after_the_prior_root_completes(runtime):
    session_id = runtime.management_service.create(
        title="MySQL input " + uuid4().hex[:12], make_default=True,
    )["session"]["session_id"]
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="First synthetic input"), "MYSQL_QUEUE_FIRST", SYNTHETIC_REVIEW_PASS,
        operation("final_answer", reason="Second synthetic input"), "MYSQL_QUEUE_SECOND", SYNTHETIC_REVIEW_PASS,
    ])
    for index in (1, 2):
        runtime.session_inputs.submit(session_id, client_message_id=f"test-input-{index}",
                                      user_input=f"Synthetic queued input {index}", attachment_ids=[])
    runtime.session_inputs._bind(session_id)
    first = runtime.session_inputs.snapshot(session_id)["active_run_id"]
    assert first
    assert runtime.run_agent_turn(session_id=session_id, user_input="Synthetic queued input 1",
                                  existing_run_id=first).answer == "MYSQL_QUEUE_FIRST"
    runtime.session_inputs._bind(session_id)
    second = runtime.session_inputs.snapshot(session_id)["active_run_id"]
    assert second and second != first
    assert runtime.run_agent_turn(session_id=session_id, user_input="Synthetic queued input 2",
                                  existing_run_id=second).answer == "MYSQL_QUEUE_SECOND"
    runtime.session_inputs._bind(session_id)
    state = runtime.session_inputs.snapshot(session_id)
    assert state["pending_count"] == 0
    assert state["active_run_id"] is None
    assert all(item["status"] == "completed" for item in state["inputs"])
