"""Synthetic hub/node integration through durable generic Agent admission, without network or models."""
from contextlib import closing

import pytest

from app.core.agent_runs import AgentRunCancelled
from app.core.config import Settings
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.safety import SafetyReviewDecision, SafetyReviewRequest
from app.core.tools import ToolContext
from app.domains.devices import DeviceConfig, DeviceService, DeviceUnavailableError
from app.domains.remote_execution import (
    ExecutionReport,
    RemoteExecutionConfig,
    RemoteExecutionConflict,
    RemoteExecutionService,
)
from app.domains.shared_sync import SharedSyncConfig
from app.integrations.remote_execution import RemoteExecutionNode
from tests.test_management_api import runtime as runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def hub(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def setup_remote(hub, tmp_path, monkeypatch):
    devices = DeviceService(hub._conn, DeviceConfig(enabled=True))
    enrolled = devices.enroll({"device_id": "pc", "name": "Work PC", "platform": "linux",
        "systems": [{"environment_id": "linux", "workspace_ids": ["project"]}],
        "workspace_ids": ["project"], "capabilities": ["files.read"],
        "scopes": ["heartbeat", "execution.poll", "execution.run", "execution.result"]})
    principal = devices.authenticate(enrolled["token"])
    devices.heartbeat(principal)
    service = RemoteExecutionService(hub._conn, devices, hub_id="test-hub", managed_tasks=hub.managed_tasks,
        sessions=hub.session_service, run_store=hub.agent_run_store,
        run_manager=hub.agent_run_manager, tool_executor=hub.tool_executor, assignment_scope="general")
    service.ensure_schema()
    hub.managed_tasks.remote_execution = service
    sid = hub.management_service.create()["session"]["session_id"]
    hub.management_service.update(sid, expected_revision=1, status="active")
    parent = hub.create_agent_run(session_id=sid, user_input="Read this project only. Do not deploy.", resumable=True)
    hub.agent_run_manager.mark_running(parent.run_id)
    hub.session_service.append_message(session_id=sid, role="user", content=parent.user_input,
        payload={"trace_id": parent.trace_id}, message_id="source-user-message")
    context = ToolContext(session_id=sid, run_id=parent.run_id, trace_id=parent.trace_id, safety_review_approved=True)
    node = LocalKnowledgeAgentRuntime(Settings(LKA_DATA_DIR=tmp_path / "node-data", LKA_WORKSPACE_ROOTS=str(tmp_path)))
    monkeypatch.setenv("TEST_EXECUTION_TOKEN", enrolled["token"])

    def transport(method, path, payload, token):
        assert method == "POST"
        current = devices.authenticate(token)
        if current is None:
            raise PermissionError("device credential revoked")
        if path.endswith("/claim"):
            return service.claim(current, **payload)
        if path.endswith("/start"):
            return service.check(current, payload, start=True)
        if path.endswith("/check"):
            return service.check(current, payload)
        if path.endswith("/report"):
            return service.report(current, payload)
        raise AssertionError(path)

    worker = RemoteExecutionNode(node, RemoteExecutionConfig(enabled=True, environment_id="linux",
        workspace_mappings={"project": str(tmp_path)}, allowed_tools=["filesystem.read_file"]),
        SharedSyncConfig(hub_id="test-hub", token_env="TEST_EXECUTION_TOKEN"), transport=transport)
    dispatch_guard = node.agent_turn_loop.execution_guard
    node.agent_turn_loop.execution_guard = lambda run_id: (dispatch_guard(run_id), worker.check_run(run_id))
    worker.run_once()  # First empty claim advertises this credential-bound environment.
    yield {"hub": hub, "node": node, "service": service, "devices": devices,
           "worker": worker, "context": context, "principal": principal}
    worker.stop()
    node.stop()


def start(env, command_id="task", **target):
    return env["hub"].managed_tasks.start_task(env["context"], command_id=command_id,
        purpose="Read the project and report evidence", execution_target=target)


def test_idle_claim_skips_writes_until_candidate_or_presence_due(setup_remote, monkeypatch):
    from datetime import UTC, datetime, timedelta
    e = setup_remote
    service, principal = e["service"], e["principal"]
    writes = 0
    original = service._connection
    from contextlib import contextmanager
    @contextmanager
    def counted(*, write=False, task_admission=False):
        nonlocal writes
        writes += int(write)
        with original(write=write, task_admission=task_admission) as conn:
            yield conn
    monkeypatch.setattr(service, "_connection", counted)
    # Fixture advertises once; subsequent empty claims must be read-only.
    before = service.claim(principal, environment_id="linux")
    assert before["task"] is None and before["poll_after_seconds"] == 15
    assert writes == 0
    for _ in range(20):
        assert service.claim(principal, environment_id="linux")["task"] is None
    assert writes == 0
    with closing(e["hub"]._conn()) as conn:
        seen = conn.execute("SELECT last_seen FROM remote_execution_workers WHERE node_id='pc' AND environment_id='linux'").fetchone()[0]
    assert service._worker_online(e["hub"]._conn(), "pc", "linux", principal["credential_version"])
    with closing(e["hub"]._conn()) as conn, conn:
        conn.execute("UPDATE remote_execution_workers SET last_seen=? WHERE node_id='pc' AND environment_id='linux'",
                     ((datetime.now(UTC) - timedelta(seconds=16)).isoformat(),))
    service.claim(principal, environment_id="linux")
    assert writes == 1
    with closing(e["hub"]._conn()) as conn:
        refreshed = conn.execute("SELECT last_seen FROM remote_execution_workers WHERE node_id='pc' AND environment_id='linux'").fetchone()[0]
    assert refreshed > seen


def test_idle_claim_presence_hint_respects_short_online_threshold(setup_remote):
    from app.domains.devices import DeviceConfig
    e = setup_remote
    e["devices"].config = DeviceConfig(enabled=True, online_threshold_seconds=5)
    response = e["service"].claim(e["principal"], environment_id="linux")
    assert response["task"] is None
    assert response["poll_after_seconds"] == pytest.approx(5 / 3)
    with closing(e["hub"]._conn()) as conn:
        row = conn.execute("SELECT last_seen FROM remote_execution_workers WHERE node_id='pc' AND environment_id='linux'").fetchone()
    from datetime import UTC, datetime
    assert (datetime.now(UTC) - datetime.fromisoformat(row["last_seen"])).total_seconds() < 5


def test_background_idle_claim_backoff_preserves_explicit_probe(setup_remote, monkeypatch):
    from app.integrations import remote_execution
    worker = setup_remote['worker']
    clock, requests = [100.0], []
    worker.config.poll_seconds = 3
    worker._idle_claim_delay = 3
    worker._next_idle_claim = 0
    monkeypatch.setattr(remote_execution.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker, '_request', lambda *args: (requests.append(args), {'task': None, 'poll_after_seconds': 15})[1])
    worker.run_once(_background=True)
    assert worker._idle_claim_delay == 6
    worker.run_once(_background=True)
    assert len(requests) == 1
    worker.run_once()
    assert len(requests) == 2
    clock[0] = worker._next_idle_claim
    worker.run_once(_background=True)
    assert worker._idle_claim_delay == 15
    # A deliberately longer operator interval must remain longer.
    worker.config.poll_seconds = 25
    worker.run_once()
    assert worker._idle_claim_delay == 25


def test_old_hub_without_backoff_hint_keeps_configured_poll(setup_remote, monkeypatch):
    from app.integrations import remote_execution
    worker = setup_remote['worker']
    worker.config.poll_seconds = 3
    requests = []
    monkeypatch.setattr(remote_execution.time, 'monotonic', lambda: 100.0)
    monkeypatch.setattr(worker, '_request', lambda *args: (requests.append(args), {'task': None})[1])
    worker.run_once()
    assert worker._idle_claim_delay == 3
    assert worker._next_idle_claim == 0
    worker.run_once(_background=True)
    assert len(requests) == 2


def test_claim_admission_resumes_active_loop_interval():
    from types import SimpleNamespace
    worker = RemoteExecutionNode.__new__(RemoteExecutionNode)
    state = {'active': False, 'stopped': False, 'waits': []}
    worker.config = SimpleNamespace(poll_seconds=3)
    worker._idle_claim_delay = 15
    worker._active = lambda: [object()] if state['active'] else []
    worker.run_once = lambda **kwargs: state.update(active=True)
    def wait(delay):
        state['waits'].append(delay)
        state['stopped'] = True
    worker._stop = SimpleNamespace(is_set=lambda: state['stopped'], wait=wait)
    worker._loop()
    assert state['waits'] == [3]


def test_management_discovery_includes_local_hub_without_changing_remote_items(setup_remote):
    e = setup_remote
    targets = e["hub"].managed_tasks.list_execution_targets(e["context"])
    assert targets["local_execution"]["name"] == "Cloud hub"
    assert targets["local_execution"]["execution_target"] is None
    assert targets["local_execution"]["default"]
    assert targets["local_execution"]["available"]
    assert [item["device_id"] for item in targets["items"]] == ["pc"]
    assert targets["assignment_policy"]["scope"] == "general"
    assert targets["items"][0]["execution_role"] == "general_executor"
    e["hub"].managed_tasks.remote_execution = None
    local_only = e["hub"].managed_tasks.list_execution_targets(e["context"])
    assert local_only["local_execution"]["available"]
    assert local_only["local_execution"]["execution_target"] is None
    assert local_only["items"] == [] and not local_only["enabled"]


def test_default_workspace_policy_rejects_untyped_and_service_targets_before_admission(setup_remote):
    e = setup_remote
    service = RemoteExecutionService(e["hub"]._conn, e["devices"], hub_id="test-hub",
        managed_tasks=e["hub"].managed_tasks, sessions=e["hub"].session_service,
        run_store=e["hub"].agent_run_store, run_manager=e["hub"].agent_run_manager,
        tool_executor=e["hub"].tool_executor)
    e["hub"].managed_tasks.remote_execution = service
    targets = service.execution_targets()
    assert targets["assignment_policy"] == {
        "scope": "workspace", "default_execution_target": None,
        "required_remote_target_fields": ["workspace_id", "resource_kind"],
        "remote_resource_kind": "workspace",
    }
    assert targets["items"][0]["execution_role"] == "workspace_executor"
    with e["hub"]._conn() as conn:
        before = conn.execute("SELECT count(*) FROM agent_sessions").fetchone()[0]
    for target in ({}, {"resource_kind": "service"},
                   {"resource_kind": "workspace"}, {"resource_kind": "service", "workspace_id": "project"}):
        with pytest.raises(RemoteExecutionConflict, match="omit execution_target"):
            start(e, command_id="invalid-" + str(len(target)) + str(target), **target)
    with e["hub"]._conn() as conn:
        after = conn.execute("SELECT count(*) FROM agent_sessions").fetchone()[0]
    assert after == before
    with e["hub"]._conn() as conn:
        assert conn.execute("SELECT count(*) FROM remote_execution_tasks").fetchone()[0] == 0


def test_workspace_policy_accepts_explicit_workspace_and_preserves_retry_receipt(setup_remote):
    e = setup_remote
    service = RemoteExecutionService(e["hub"]._conn, e["devices"], hub_id="test-hub",
        managed_tasks=e["hub"].managed_tasks, sessions=e["hub"].session_service,
        run_store=e["hub"].agent_run_store, run_manager=e["hub"].agent_run_manager,
        tool_executor=e["hub"].tool_executor)
    e["hub"].managed_tasks.remote_execution = service
    first = start(e, command_id="workspace", resource_kind="workspace", workspace_id="project")
    assert start(e, command_id="workspace", resource_kind="workspace", workspace_id="project") == first
    stored = service.get(first["remote_execution"]["task_id"])
    assert stored["workspace_id"] == "project"
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Workspace checked"), "Workspace result."])
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("workspace-task")
    e["worker"].run_once()
    assert service.get(first["remote_execution"]["task_id"])["state"] == "completed"


def test_concurrent_identical_remote_admissions_return_one_receipt(setup_remote, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    e = setup_remote
    barrier = Barrier(2)
    prepare = e["hub"].managed_tasks.prepare_task

    def synchronized_prepare(*args, **kwargs):
        barrier.wait(timeout=5)
        return prepare(*args, **kwargs)

    monkeypatch.setattr(e["hub"].managed_tasks, "prepare_task", synchronized_prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start, e, command_id="concurrent",
                               resource_kind="workspace", workspace_id="project")
                   for _ in range(2)]
        first, second = [future.result(timeout=10) for future in futures]
    assert first == second
    with e["hub"]._conn() as conn:
        assert conn.execute("SELECT count(*) FROM remote_execution_tasks").fetchone()[0] == 1


def test_workspace_policy_rejects_legacy_remote_followup_but_keeps_prior_receipt(setup_remote):
    e = setup_remote
    receipt = start(e, command_id="legacy-session")
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Done"), "Result."])
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("legacy-initial")
    e["worker"].run_once()
    args = {"command_id": "legacy-followup", "session_id": receipt["session_id"],
            "expected_run_id": receipt["run_id"], "purpose": "Explain the existing result"}
    followup = e["hub"].managed_tasks.send_instruction(e["context"], **args)
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Done"), "Followup result."])
    e["worker"].run_once()
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("legacy-followup")
    e["worker"].run_once()
    e["service"].assignment_scope = "workspace"
    assert start(e, command_id="legacy-session") == receipt
    assert e["hub"].managed_tasks.send_instruction(e["context"], **args) == followup
    with pytest.raises(RemoteExecutionConflict, match="outside the current workspace-only policy"):
        e["hub"].managed_tasks.send_instruction(e["context"],
            command_id="new-legacy-followup", session_id=receipt["session_id"],
            expected_run_id=followup["run_id"], purpose="Continue the service task")


def test_node_progress_is_projected_into_hub_run_events(setup_remote, tmp_path):
    import asyncio

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.routes.agent import router as agent_router

    e = setup_remote
    (tmp_path / "progress.txt").write_text("PRIVATE_FILE_BODY_9281", encoding="utf-8")
    e["context"] = e["context"].model_copy(update={"safety_review_mode": "skip"})
    e["worker"].config.review_policy = "inherit"
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="filesystem"),
        operation("tool_call", tool_name="filesystem.read_file", tool_input={"path": str(tmp_path / "progress.txt")}),
        operation("final_answer", reason="The file was read."), "Read complete.",
    ])
    receipt = start(e, command_id="progress", workspace_id="project")
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("progress")
    assert e["node"].agent_run_manager.get_run(receipt["run_id"]).status.value == "completed"
    e["worker"].run_once()

    events = e["hub"].agent_run_manager.list_events(receipt["run_id"])
    tool_events = [event for event in events if event.type in {"tool_started", "tool_completed"}]
    assert [event.type for event in tool_events] == ["tool_started", "tool_completed"]
    assert all(event.payload.get("remote_origin", {}).get("node_sequence") for event in tool_events)
    assert all("PRIVATE_FILE_BODY_9281" not in str(event.model_dump(mode="json")) for event in events)
    assert all("input" not in event.payload and "result" not in event.payload for event in tool_events)
    assert events[-1].type == "run_completed"

    app = FastAPI()
    app.state.runtime = e["hub"]
    app.include_router(agent_router)
    async def read_events():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(f"/agent/runs/{receipt['run_id']}/events")
    response = asyncio.run(read_events())
    assert response.status_code == 200
    public_tools = [event for event in response.json()["events"] if event["type"] in {"tool_started", "tool_completed"}]
    assert len(public_tools) == 2
    assert public_tools[0]["payload"]["remote_origin"]["node_sequence"] == tool_events[0].payload["remote_origin"]["node_sequence"]
    assert public_tools[0]["created_at"] <= public_tools[0]["payload"]["remote_origin"]["relayed_at"]


def test_progress_report_retry_and_terminal_pages_are_durable(setup_remote, monkeypatch):
    from app.domains.remote_execution import _REMOTE_PROGRESS, remote_progress_projection

    e = setup_remote
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Done."), "Finished.",
    ])
    receipt = start(e, command_id="progress-pages", workspace_id="project")
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("progress-pages")
    run_id = receipt["run_id"]
    # Model a busy but content-free event stream; private deltas are cursor-drained too.
    for index in range(70):
        event_type = "llm_started" if index % 2 == 0 else "llm_delta"
        e["node"].agent_run_manager.append_event(
            run_id, event_type, "ignored node text", stage="answer" if event_type == "llm_delta" else "decision",
            payload={"secret": "PRIVATE_CONTROL_OR_ANSWER", "audit_record": {"prompt": "private"}},
        )
    expected_source_sequences = [
        event.sequence for event in e["node"].agent_run_manager.list_events(run_id)
        if remote_progress_projection(event.model_dump(mode="json")) is not None
    ]

    original_transport = e["worker"]._transport
    lost_once = {"value": True}
    def lose_first_report_reply(method, path, payload, token):
        response = original_transport(method, path, payload, token)
        if path.endswith("/report") and lost_once["value"]:
            lost_once["value"] = False
            raise OSError("response lost after hub commit")
        return response
    monkeypatch.setattr(e["worker"], "_transport", lose_first_report_reply)

    with pytest.raises(OSError):
        e["worker"].run_once()
    with closing(e["node"]._conn()) as conn:
        pending = dict(conn.execute("SELECT event_cursor,pending_event_cursor,pending_report FROM remote_execution_node_tasks WHERE run_id=?", (run_id,)).fetchone())
    first_page = __import__("json").loads(pending["pending_report"])
    assert len(first_page["progress_events"]) <= 64
    assert first_page["status"] == "running"
    first_page_id = first_page["report_id"]
    # The response retry is byte-identical and does not duplicate imported events.
    restarted_worker = RemoteExecutionNode(
        e["node"], e["worker"].config, e["worker"].sync_config, transport=original_transport,
    )
    restarted_worker.run_once()
    with closing(e["node"]._conn()) as conn:
        state = dict(conn.execute("SELECT event_cursor,pending_event_cursor,pending_report FROM remote_execution_node_tasks WHERE run_id=?", (run_id,)).fetchone())
    assert state["pending_report"] is None
    assert state["event_cursor"] > pending["pending_event_cursor"]
    assert first_page_id

    # The replacement worker drains the remaining source page before terminal publication.
    task = e["service"].get(receipt["remote_execution"]["task_id"])
    assert task["state"] == "completed"
    assert any(report["status"] == "running" for report in task["reports"])
    assert any(report["status"] == "completed" for report in task["reports"])
    hub_events = e["hub"].agent_run_manager.list_events(run_id)
    origins = [event.payload["remote_origin"]["node_sequence"] for event in hub_events if "remote_origin" in event.payload]
    assert origins == expected_source_sequences
    assert all(event.message == _REMOTE_PROGRESS[event.type][1] for event in hub_events if "remote_origin" in event.payload)
    assert all("PRIVATE_CONTROL_OR_ANSWER" not in str(event.model_dump(mode="json")) for event in hub_events)
    restarted_worker.stop()


def test_progress_projection_rejects_wrong_run_spoof_and_oversize(setup_remote):
    from app.domains.remote_execution import RemoteProgressEvent

    e = setup_remote
    receipt = start(e, command_id="progress-validation", workspace_id="project")
    e["worker"].run_once()
    with closing(e["node"]._conn()) as conn:
        fence = __import__("json").loads(conn.execute(
            "SELECT fence FROM remote_execution_node_tasks WHERE run_id=?", (receipt["run_id"],)
        ).fetchone()[0])
    event = {"run_id": "spoofed-run", "source_sequence": 1, "source_created_at": "2026-10-25T00:00:00Z",
             "type": "tool_started",
             "stage": "tool", "message": "A tool started.", "payload": {}}
    with pytest.raises(PermissionError, match="run identity"):
        e["service"].report(e["principal"], {**fence, "report_id": "wrong-run", "status": "running",
            "progress_cursor": 1, "progress_events": [event]})
    event["run_id"] = receipt["run_id"]
    event["payload"] = {"input": "private"}
    with pytest.raises(ValueError, match="private fields"):
        RemoteProgressEvent.model_validate(event)
    with pytest.raises(ValueError, match="1 MiB"):
        e["service"].report(e["principal"], {**fence, "report_id": "oversize", "status": "running",
            "result": {"answer": "x" * 1_100_000}})


def test_legacy_node_report_without_progress_fields_remains_accepted(setup_remote):
    e = setup_remote
    receipt = start(e, command_id="legacy-report", workspace_id="project")
    e["worker"].run_once()
    with closing(e["node"]._conn()) as conn:
        fence = __import__("json").loads(conn.execute(
            "SELECT fence FROM remote_execution_node_tasks WHERE run_id=?", (receipt["run_id"],)
        ).fetchone()[0])
    report = {**fence, "report_id": "legacy-report", "status": "running"}
    assert e["service"].report(e["principal"], report)["accepted"]
    with closing(e["hub"]._conn()) as conn:
        stored = __import__("json").loads(conn.execute(
            "SELECT payload FROM remote_execution_reports WHERE report_id='legacy-report'"
        ).fetchone()[0])
    assert "progress_cursor" not in stored and "progress_events" not in stored


def test_paused_node_run_drains_progress_before_hub_closes_stream(setup_remote):
    e = setup_remote
    receipt = start(e, command_id="paused-progress", workspace_id="project")
    e["worker"].run_once()
    run_id = receipt["run_id"]
    for _ in range(70):
        e["node"].agent_run_manager.append_event(
            run_id, "llm_started", "private model details", stage="decision",
            payload={"audit_record": {"prompt": "private"}},
        )
    e["node"].agent_run_manager.request_pause(run_id)
    e["node"].agent_run_manager.acknowledge_pause(run_id)

    e["worker"].run_once()
    assert e["hub"].agent_run_manager.get_run(run_id).status.value == "running"
    first_report = e["service"].get(receipt["remote_execution"]["task_id"])["reports"][-1]
    assert first_report["status"] == "running"
    assert e["worker"].status()["active_tasks"] == 1

    e["worker"].run_once()
    assert e["hub"].agent_run_manager.get_run(run_id).status.value == "paused"
    task = e["service"].get(receipt["remote_execution"]["task_id"])
    assert task["state"] == "paused"


def test_oversized_session_message_does_not_advance_unsent_message_cursor(setup_remote):
    e = setup_remote
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Done."), "Finished.",
    ])
    receipt = start(e, command_id="oversized-message", workspace_id="project")
    e["worker"].run_once()
    e["node"].session_service.append_message(
        session_id=receipt["session_id"], role="agent", content="x" * (400 * 1024),
        message_id="oversized-source-message",
    )
    assert e["node"].agent_dispatch.run_one("oversized-message")
    captured = {}
    def unavailable_report(method, path, payload, token):
        captured.update(payload)
        raise OSError("keep pending report for inspection")
    e["worker"]._transport = unavailable_report
    row = e["worker"]._active()[0]
    with pytest.raises(OSError):
        e["worker"]._relay(row)
    with closing(e["node"]._conn()) as conn:
        pending = dict(conn.execute(
            "SELECT message_cursor,pending_cursor,pending_report FROM remote_execution_node_tasks WHERE run_id=?",
            (receipt["run_id"],),
        ).fetchone())
    report = __import__("json").loads(pending["pending_report"])
    assert report["status"] == "failed"
    assert report["error_type"] == "remote_message_too_large"
    assert "exceeds the relay page limit" in report["result"]["answer"]
    assert pending["pending_cursor"] == pending["message_cursor"] == 0
    assert captured == report


@pytest.mark.parametrize("explicit_null", [False, True])
def test_tool_dispatch_without_device_requirement_stays_on_hub(setup_remote, explicit_null):
    e = setup_remote
    inputs = {"command_id": "backend-task", "purpose": "Inspect records stored on this backend"}
    if explicit_null:
        inputs["execution_target"] = None
    context = e["context"]
    review = e["hub"].agent_run_manager.create_safety_review(SafetyReviewRequest(
        review_id="local-choice-review", invocation_id="local-choice", tool_name="management.start_task",
        tool_input=inputs, run_id=context.run_id, session_id=context.session_id, trace_id=context.trace_id,
        user_request=inputs["purpose"], mode="skip", reason="Synthetic dispatch authorization",
        created_at=e["hub"].agent_run_manager.get_run(context.run_id).created_at,
    ))
    e["hub"].agent_run_manager.decide_safety_review(review_id=review.review_id,
        decision=SafetyReviewDecision.APPROVE, decided_by="test", reason="Synthetic dispatch authorization")
    context = context.model_copy(update={"safety_review_id": review.review_id})
    result = e["hub"].tool_executor.execute(invocation_id="local-choice",
        tool_name="management.start_task", tool_input=inputs, context=context)
    assert result.status == "completed", result.error
    run = e["hub"].agent_run_manager.get_run(result.output["run_id"])
    assert run.metadata.get("remote_execution") is None
    with e["hub"]._conn() as conn:
        assert conn.execute("SELECT count(*) FROM remote_execution_tasks").fetchone()[0] == 0
    assert e["node"].agent_run_manager.get_run(run.run_id) is None


def test_real_dispatch_round_trip_and_immutable_command(setup_remote):
    e = setup_remote
    receipt = start(e, device_id="pc", environment_id="linux", workspace_id="project")
    assert start(e, device_id="pc", environment_id="linux", workspace_id="project") == receipt
    with pytest.raises(RemoteExecutionConflict, match="different arguments"):
        start(e, device_id="other")
    with closing(e["hub"]._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind='agent_dispatch' AND scope_id=?", (receipt["session_id"],)).fetchone()[0] == 0
    e["node"].agent_turn_loop.llm_client = ScriptedClient([operation("final_answer", reason="Bounded task completed"), "Project checked."])
    e["worker"].run_once()
    local = e["node"].agent_run_manager.get_run(receipt["run_id"])
    assert local.metadata["session_policy"]["safety_review_mode"] == "manual"
    assert local.metadata["session_policy"]["allowed_tools"] == ["filesystem.read_file"]
    view = local.metadata["context_views"]["tool"]
    assert not view["full_data_authority"] and not view["full_workspace_authority"]
    assert view["allowed_paths"]
    assert e["node"].agent_dispatch.run_one("test-node")
    assert e["node"].agent_run_manager.get_run(receipt["run_id"]).status.value == "completed"
    e["worker"].run_once()
    remote = e["service"].get(receipt["remote_execution"]["task_id"])
    assert remote["state"] == "completed"
    hub_run = e["hub"].agent_run_manager.get_run(receipt["run_id"])
    assert hub_run.status.value == "completed" and hub_run.result_snapshot["answer"] == "Project checked."
    messages = e["hub"].get_session(session_id=receipt["session_id"]).messages
    assert any(m.role == "agent" and m.content == "Project checked." for m in messages)
    assert hub_run.metadata["input_envelope"]["original_user_message_refs"] == ["source-user-message"]


@pytest.mark.parametrize(("node_policy", "requested_mode", "effective_mode", "source"), [
    ("manual", "skip", "manual", "node_policy"),
    ("inherit", "skip", "skip", "request"),
    ("inherit", "llm", "llm", "request"),
    ("inherit", "manual", "manual", "request"),
])
def test_remote_review_policy_is_explicit_and_frozen(setup_remote, node_policy, requested_mode, effective_mode, source):
    e = setup_remote
    e["context"] = e["context"].model_copy(update={"safety_review_mode": requested_mode})
    e["worker"].config.review_policy = node_policy
    receipt = start(e, device_id="pc", workspace_id="project")
    task_id = receipt["remote_execution"]["task_id"]
    queued = e["service"].get(task_id)
    assert queued["approval"]["requested_mode"] == requested_mode
    assert queued["approval"]["effective_mode"] is None
    assert queued["title"] == "管理任务" and queued["session_id"] == receipt["session_id"]
    e["worker"].run_once()
    local = e["node"].agent_run_manager.get_run(receipt["run_id"])
    assert local.metadata["session_policy"]["safety_review_mode"] == effective_mode
    expected = {"requested_mode": requested_mode, "effective_mode": effective_mode, "source": source}
    assert local.metadata["remote_approval"] == expected
    e["worker"].run_once()  # Publish the node's effective policy on its next outbound report.
    assert e["service"].get(task_id)["approval"] == expected
    assert e["service"].list()["items"][0]["approval"] == expected
    # Changing node configuration affects future admissions, not this run.
    e["worker"].config.review_policy = "manual" if node_policy == "inherit" else "inherit"
    e["worker"].run_once()
    assert e["node"].agent_run_manager.get_run(receipt["run_id"]).metadata["remote_approval"] == expected


def test_inherited_skip_executes_workspace_write_and_still_records_review(setup_remote, tmp_path):
    e = setup_remote
    e["context"] = e["context"].model_copy(update={"safety_review_mode": "skip"})
    e["worker"].config.review_policy = "inherit"
    e["worker"].config.allowed_tools = ["filesystem.create_file"]
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="filesystem"),
        operation("tool_call", tool_name="filesystem.create_file", tool_input={"path": "automatic.txt", "content": "automatic review recorded"}),
        operation("final_answer", reason="File creation verified"), "File created.",
    ])
    receipt = start(e, device_id="pc", workspace_id="project")
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("automatic-review")
    run = e["node"].agent_run_manager.get_run(receipt["run_id"])
    assert run.status.value == "completed", run.model_dump(mode="json")
    assert (tmp_path / "automatic.txt").read_text() == "automatic review recorded"
    reviews = e["node"].agent_run_manager.list_safety_reviews(run.run_id)
    assert any(review.tool_name == "filesystem.create_file" and review.status.value == "approved"
               and review.mode.value == "skip" for review in reviews)
    e["worker"].run_once()
    assert e["service"].get(receipt["remote_execution"]["task_id"])["approval"]["effective_mode"] == "skip"


def test_inherit_does_not_automatically_approve_native_host_execution(setup_remote):
    e = setup_remote
    e["context"] = e["context"].model_copy(update={"safety_review_mode": "skip"})
    e["worker"].config.review_policy = "inherit"
    e["worker"].config.shell_trust = "host_user"
    e["worker"].config.allowed_tools = ["bash.run"]
    receipt = start(e, device_id="pc", workspace_id="project")
    e["worker"].run_once()
    run = e["node"].agent_run_manager.get_run(receipt["run_id"])
    assert run.metadata["context_views"]["tool"]["host_execution_authority"] is True
    assert run.metadata["session_policy"]["safety_review_mode"] == "manual"
    assert run.metadata["remote_approval"] == {"requested_mode": "skip", "effective_mode": "manual", "source": "host_execution"}


def test_target_failure_does_not_fall_back(setup_remote):
    e = setup_remote
    for target in ({"device_id": "missing"}, {"environment_id": "windows"}, {"workspace_id": "secret"}, {"capabilities": ["unapproved"]}):
        with pytest.raises(DeviceUnavailableError):
            start(e, **target)
    with closing(e["hub"]._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM remote_execution_tasks").fetchone()[0] == 0
    assert start(e, command_id="auto")["remote_execution"]["node_id"] == "pc"


def test_revocation_fences_actual_execution_and_result(setup_remote):
    e = setup_remote
    receipt = start(e, device_id="pc", workspace_id="project")
    e["worker"].run_once()
    with closing(e["node"]._conn()) as conn:
        journal = dict(conn.execute("SELECT * FROM remote_execution_node_tasks").fetchone())
    e["devices"].revoke("pc", 1)
    with pytest.raises(PermissionError):
        e["service"].control(receipt["remote_execution"]["task_id"], command_id="revoked-continuation", action="pause")
    with pytest.raises(AgentRunCancelled):
        e["worker"].check_run(receipt["run_id"])
    import json
    with pytest.raises(PermissionError):
        e["service"].report(e["principal"], ExecutionReport(**json.loads(journal["fence"]), report_id="late", status="completed", result={"answer": "late"}))
    assert e["service"].get(receipt["remote_execution"]["task_id"])["state"] == "unknown"
    assert e["hub"].agent_run_manager.get_run(receipt["run_id"]).status.value == "failed"


def test_cancel_and_expiry_are_fenced_never_replayed(setup_remote):
    e = setup_remote
    receipt = start(e, command_id="cancel")
    claim = e["service"].claim(e["principal"], environment_id="linux")
    e["service"].cancel(receipt["remote_execution"]["task_id"])
    with pytest.raises((PermissionError, RemoteExecutionConflict)):
        e["service"].check(e["principal"], claim["fence"], start=True)
    second = start(e, command_id="expire")
    claim = e["service"].claim(e["principal"], environment_id="linux")
    with closing(e["hub"]._conn()) as conn, conn:
        conn.execute("UPDATE remote_execution_tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?", (second["remote_execution"]["task_id"],))
    assert e["service"].claim(e["principal"], environment_id="linux")["task"] is None
    assert e["service"].get(second["remote_execution"]["task_id"])["state"] == "unknown"


def test_unmapped_workspace_and_outage_never_execute(setup_remote):
    e = setup_remote
    receipt = start(e, workspace_id="project")
    e["worker"].config.workspace_mappings = {}
    with pytest.raises(PermissionError, match="local mapping"):
        e["worker"].run_once()
    assert e["node"].agent_run_manager.get_run(receipt["run_id"]) is None
    next_receipt = start(e, command_id="outage")
    e["worker"].run_once()
    def offline(*args):
        raise OSError("synthetic outage")
    e["worker"]._transport = offline
    with pytest.raises(AgentRunCancelled):
        e["worker"].check_run(next_receipt["run_id"])
    assert e["node"].agent_run_manager.is_cancel_requested(next_receipt["run_id"])


def test_hub_shadow_cannot_resume_locally_or_reset_budget(setup_remote):
    e = setup_remote
    receipt = start(e)
    with pytest.raises(RemoteExecutionConflict, match="no longer live"):
        e["hub"].managed_tasks.control_run(e["context"], command_id="resume", run_id=receipt["run_id"], action="resume")
    e["hub"].agent_run_manager._update_run(e["context"].run_id,
        status=e["hub"].agent_run_manager.get_run(e["context"].run_id).status,
        metadata_patch={"work_budget_scopes": [{"scope_id": "capped", "token_limit": 1}]})
    with pytest.raises(RemoteExecutionConflict, match="budgets"):
        start(e, command_id="budget")


def test_manual_review_relay_requires_user_decision_before_effect(setup_remote):
    from tests.test_management_delegate_approval import register
    e = setup_remote
    effects = []
    register(e["hub"], [])
    register(e["node"], effects)
    for runtime in (e["hub"], e["node"]):
        runtime.tool_registry.register_effect_constraint("probe-source", blocked_domains=("messages",))
        runtime.tool_registry.get_tool("approval_probe.write").spec.origin_constraints = ("probe-source",)
    e["worker"].config.allowed_tools = ["approval_probe.write"]
    receipt = start(e)
    task_id = receipt["remote_execution"]["task_id"]
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="approval_probe"),
        operation("tool_call", tool_name="approval_probe.write", tool_input={}),
    ])
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("test-node")
    assert e["node"].agent_run_manager.get_run(receipt["run_id"]).status.value == "waiting_confirmation"
    assert effects == []
    e["worker"].run_once()
    remote = e["service"].get(task_id)
    assert remote["state"] == "waiting_confirmation"
    review = remote["reports"][-1]["reviews"][0]
    e["service"].decide_review(task_id, review["review_id"], decision="approve", decided_by="user")
    e["worker"].run_once()
    assert effects == []
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Explicit user approval consumed"), "Approved operation complete."])
    assert e["node"].agent_dispatch.run_one("test-node-resume")
    assert effects == [receipt["run_id"]]
    e["worker"].run_once()
    assert e["service"].get(task_id)["state"] == "completed"
    assert "probe-source" in e["hub"].tool_executor.constraint_store.get([("session", e["context"].session_id)])


def test_lost_terminal_ack_retries_durable_result_without_reexecution(setup_remote):
    e = setup_remote
    receipt = start(e)
    e["node"].agent_turn_loop.llm_client = ScriptedClient([
        operation("final_answer", reason="Done once"), "One result."])
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("test-node")
    transport = e["worker"]._transport
    dropped = []
    def lose_ack(method, path, payload, token):
        result = transport(method, path, payload, token)
        if path.endswith("/report") and payload["status"] == "completed" and not dropped:
            dropped.append(True)
            raise OSError("result committed, response lost")
        return result
    e["worker"]._transport = lose_ack
    with pytest.raises(OSError):
        e["worker"].run_once()
    assert e["service"].get(receipt["remote_execution"]["task_id"])["state"] == "completed"
    e["worker"].run_once()
    assert not e["worker"]._active()
    with closing(e["hub"]._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM remote_execution_reports").fetchone()[0] == 1
    assert not e["node"].agent_dispatch.run_one("must-not-execute-again")


def test_scope_removal_and_command_delivery_dedup(setup_remote):
    from app.domains.devices import DeviceEnrollment
    e = setup_remote
    receipt = start(e)
    claim = e["service"].claim(e["principal"], environment_id="linux")
    e["worker"]._admit(claim)
    e["worker"]._admit(claim)
    with closing(e["node"]._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM remote_execution_node_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM agent_runs WHERE run_id=?", (receipt["run_id"],)).fetchone()[0] == 1
    current = e["devices"].get("pc")
    profile = {key: current[key] for key in DeviceEnrollment.model_fields}
    profile["scopes"] = ["heartbeat", "execution.poll", "execution.run"]
    e["devices"].update("pc", {**profile, "expected_revision": current["revision"]})
    with pytest.raises(AgentRunCancelled):
        e["worker"].check_run(receipt["run_id"])
    assert e["service"].get(receipt["remote_execution"]["task_id"])["state"] == "unknown"


def test_api_task_listing_is_bounded_and_reports_require_account(setup_remote):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.routes.remote_execution import list_tasks
    e = setup_remote
    receipt = start(e)
    e["hub"].remote_execution_service = e["service"]
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=e["hub"])),
                              state=SimpleNamespace())
    with pytest.raises(HTTPException) as failure:
        list_tasks(request, owner_session_id=None, limit=100, offset=0)
    assert failure.value.status_code == 401
    request.state.account = {"user_id": "owner"}
    task = list_tasks(request, owner_session_id=None, limit=100, offset=0)["items"][0]
    assert task["session_id"] == receipt["session_id"] and task["run_id"] == receipt["run_id"]
    assert "reports" not in task and "payload" not in task and "lease_token" not in task
    with pytest.raises(ValueError):
        e["service"].list(limit=101)


def test_isolated_snapshot_blocks_private_recall_guidance_and_retraction(setup_remote):
    from app.domains.memory import MemoryInput, MemorySourceInput
    e = setup_remote
    source_id = e["node"].memory_service.register_source(MemorySourceInput(
        source_type="user", source_ref="local-only", trusted_source=True))
    e["node"].memory_service.create(MemoryInput(content="NODE_PRIVATE_MEMORY_742", source_id=source_id, user_confirmed=True))
    with closing(e["node"]._conn()) as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM memory_entries ORDER BY memory_id")]
    recalls, retractions, guidance = [], [], []
    def recall(*args):
        recalls.append(args)
        return {"items": [{"content": "NODE_PRIVATE_MEMORY_742"}]}
    def retract(*args):
        retractions.append(args)
    class PrivateGuidance:
        def for_workspace(self, path):
            guidance.append(path)
            return {"content": "NODE_PRIVATE_GUIDANCE_743"}
    e["node"].agent_turn_loop.memory_context_provider = recall
    e["node"].agent_turn_loop.memory_pre_turn_callback = retract
    e["node"].agent_turn_loop.instruction_files = PrivateGuidance()
    receipt = e["hub"].managed_tasks.start_task(e["context"], command_id="isolation",
        purpose="Forget earlier personal preferences and report the project scope only", execution_target={})
    client = ScriptedClient([operation("final_answer", reason="Only delegated source context used"), "Delegated scope checked."])
    e["node"].agent_turn_loop.llm_client = client
    e["worker"].run_once()
    run = e["node"].agent_run_manager.get_run(receipt["run_id"])
    snapshot = run.metadata["context_snapshot"]
    assert snapshot["child_run_id"] == run.run_id and snapshot["memory_refs"] == []
    assert not run.metadata["session_policy"]["use_local_context"]
    assert e["node"].agent_dispatch.run_one("isolated-node")
    assert e["node"].agent_run_manager.get_run(run.run_id).status.value == "completed"
    assert recalls == [] and retractions == [] and guidance == []
    assert all("NODE_PRIVATE_MEMORY_742" not in str(call) and "NODE_PRIVATE_GUIDANCE_743" not in str(call) for call in client.calls)
    with closing(e["node"]._conn()) as conn:
        assert before == [tuple(r) for r in conn.execute("SELECT * FROM memory_entries ORDER BY memory_id")]


def test_rotated_lost_ack_is_fenced_without_starving_new_claims(setup_remote, monkeypatch):
    e = setup_remote
    receipt = start(e)
    e["node"].agent_turn_loop.llm_client = ScriptedClient([operation("final_answer", reason="Done once"), "Completed."])
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("old-task")
    transport = e["worker"]._transport
    def lose_ack(method, path, payload, token):
        result = transport(method, path, payload, token)
        if path.endswith("/report"):
            raise OSError("lost terminal ack")
        return result
    e["worker"]._transport = lose_ack
    with pytest.raises(OSError):
        e["worker"].run_once()
    rotated = e["devices"].rotate("pc", 1)
    monkeypatch.setenv("TEST_EXECUTION_TOKEN", rotated["token"])
    e["worker"]._transport = transport
    e["worker"].run_once()  # Fence old publication and refresh the new worker credential.
    second = start(e, command_id="after-rotation")
    e["worker"].run_once()
    with closing(e["node"]._conn()) as conn:
        old = conn.execute("SELECT state,pending_report FROM remote_execution_node_tasks WHERE run_id=?", (receipt["run_id"],)).fetchone()
        assert old["state"] == "fenced_unpublished" and old["pending_report"]
    assert e["node"].agent_run_manager.get_run(second["run_id"]) is not None


def test_only_live_execution_environment_is_selected(setup_remote):
    from app.domains.devices import DeviceEnrollment
    e = setup_remote
    current = e["devices"].get("pc")
    profile = {key: current[key] for key in DeviceEnrollment.model_fields}
    profile["systems"] = [
        {"environment_id": "aaa-windows", "workspace_ids": ["project"]},
        {"environment_id": "linux", "workspace_ids": ["project"]}]
    e["devices"].update("pc", {**profile, "expected_revision": current["revision"]})
    assert start(e, command_id="auto-live")["remote_execution"]["environment_id"] == "linux"
    with pytest.raises(DeviceUnavailableError):
        start(e, command_id="unserved-windows", environment_id="aaa-windows")
    targets = e["service"].execution_targets()["items"][0]
    assert {system["environment_id"]: system["execution_online"] for system in targets["systems"]} == {"aaa-windows": False, "linux": True}
    with closing(e["hub"]._conn()) as conn, conn:
        conn.execute("UPDATE remote_execution_workers SET last_seen='2000-01-01T00:00:00+00:00'")
    with pytest.raises(DeviceUnavailableError):
        start(e, command_id="stale-worker")


def test_checkpoint_pause_resume_preserves_run_epoch_and_single_effect(setup_remote):
    from tests.test_management_delegate_approval import register
    e = setup_remote
    effects = []
    register(e["hub"], [])
    register(e["node"], effects)
    e["worker"].config.allowed_tools = ["approval_probe.write"]
    receipt = start(e, workspace_id="project")
    task_id = receipt["remote_execution"]["task_id"]
    client = ScriptedClient([operation("expand_package", package_name="approval_probe"),
        operation("tool_call", tool_name="approval_probe.write", tool_input={})])
    e["node"].agent_turn_loop.llm_client = client
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("review")
    e["worker"].run_once()
    review = e["service"].get(task_id)["reports"][-1]["reviews"][0]
    e["service"].decide_review(task_id, review["review_id"], decision="approve", decided_by="user")
    e["worker"].run_once()
    tool = e["node"].tool_registry.get_tool("approval_probe.write")
    invoke = tool.invoke

    def pause_after_effect(**kwargs):
        result = invoke(**kwargs)
        e["service"].control(task_id, command_id="pause-once", action="pause")
        return result

    tool.invoke = pause_after_effect
    assert e["node"].agent_dispatch.run_one("effect-once")
    assert effects == [receipt["run_id"]]
    assert e["node"].agent_run_manager.get_run(receipt["run_id"]).status.value == "paused"
    transport = e["worker"]._transport
    dropped = []

    def lose_pause_ack(method, path, payload, token):
        result = transport(method, path, payload, token)
        if path.endswith("/report") and payload["status"] == "paused" and not dropped:
            dropped.append(True)
            raise OSError("pause report committed; ack lost")
        return result

    e["worker"]._transport = lose_pause_ack
    with pytest.raises(OSError):
        e["worker"].run_once()
    e["worker"].run_once()
    before = e["service"].get(task_id)
    assert before["state"] == "paused"
    assert e["hub"].agent_run_manager.get_run(receipt["run_id"]).metadata["paused_from_status"] == "running"
    command = e["hub"].managed_tasks.control_run(e["context"], command_id="resume-once", run_id=receipt["run_id"], action="resume")
    assert e["hub"].managed_tasks.control_run(e["context"], command_id="resume-once", run_id=receipt["run_id"], action="resume") == command
    e["worker"].run_once()
    assert e["service"].get(task_id)["state"] == "resuming"
    import json
    paused_report = e["service"].get(task_id)["reports"][-1]
    with closing(e["node"]._conn()) as conn:
        fence = json.loads(conn.execute("SELECT fence FROM remote_execution_node_tasks WHERE run_id=?", (receipt["run_id"],)).fetchone()[0])
    e["service"].report(e["principal"], {**paused_report, **fence, "report_id": "distinct-paused-snapshot"})
    assert e["service"].get(task_id)["state"] == "resuming"
    with pytest.raises(RemoteExecutionConflict, match="checkpoint state"):
        e["service"].control(task_id, command_id="second-resume", action="resume")
    e["worker"].run_once()
    e["node"].agent_turn_loop.llm_client = ScriptedClient([operation("final_answer", reason="Resumed after completed effect"), "Finished once."])
    assert e["node"].agent_dispatch.run_one("resume-once")
    e["worker"].run_once()
    after = e["service"].get(task_id)
    assert after["state"] == "completed" and after["lease_epoch"] == before["lease_epoch"]
    assert effects == [receipt["run_id"]]
    assert not e["node"].agent_dispatch.run_one("no-second-resume")


def test_followup_uses_same_session_durable_command_and_frozen_workspace(setup_remote):
    e = setup_remote
    receipt = start(e, workspace_id="project")
    e["node"].agent_turn_loop.llm_client = ScriptedClient([operation("final_answer", reason="Done"), "First result."])
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("first")
    e["worker"].run_once()
    args = {"command_id": "followup", "session_id": receipt["session_id"], "expected_run_id": receipt["run_id"], "purpose": "Explain the existing result without repeating operations"}
    followup = e["hub"].managed_tasks.send_instruction(e["context"], **args)
    assert e["hub"].managed_tasks.send_instruction(e["context"], **args) == followup
    assert followup["session_id"] == receipt["session_id"] and followup["run_id"] != receipt["run_id"]
    e["node"].agent_turn_loop.llm_client = ScriptedClient([operation("final_answer", reason="Followup only"), "Second result."])
    e["worker"].run_once()
    e["worker"].run_once()
    assert e["node"].agent_dispatch.run_one("followup")
    local_followup = e["node"].agent_run_manager.get_run(followup["run_id"])
    assert local_followup.status.value == "completed", local_followup.model_dump(mode="json")
    e["worker"].run_once()
    assert e["service"].get(followup["remote_execution"]["task_id"])["state"] == "completed"
    messages = e["hub"].get_session(session_id=receipt["session_id"]).messages
    assert [m.content for m in messages if m.role == "agent"] == ["First result.", "Second result."]
    with closing(e["node"]._conn()) as conn:
        assert conn.execute("SELECT count(*) FROM agent_sessions WHERE session_id=?", (receipt["session_id"],)).fetchone()[0] == 1
    second = e["service"].account_instruction(followup["remote_execution"]["task_id"], command_id="account-next", expected_run_id=followup["run_id"], purpose="Check prior evidence", actor="owner")
    new_dir = e["node"].settings.data_dir / "other-workspace"
    new_dir.mkdir()
    e["worker"].config.workspace_mappings["project"] = str(new_dir)
    with pytest.raises(PermissionError, match="workspace mapping changed"):
        e["worker"].run_once()
    assert e["node"].agent_run_manager.get_run(second["run_id"]) is None


def test_continuation_rejects_unknown_revocation_and_changed_owner(setup_remote):
    e = setup_remote
    receipt = start(e)
    task_id = receipt["remote_execution"]["task_id"]
    e["worker"].run_once()
    with closing(e["hub"]._conn()) as conn, conn:
        conn.execute("UPDATE remote_execution_tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?", (task_id,))
    with pytest.raises(RemoteExecutionConflict, match="no longer live"):
        e["service"].control(task_id, command_id="unknown", action="resume")
    with pytest.raises(RemoteExecutionConflict, match="unknown effects"):
        e["hub"].managed_tasks.send_instruction(e["context"], command_id="unknown-followup", session_id=receipt["session_id"], expected_run_id=receipt["run_id"], purpose="Do it again")
    e["worker"].run_once()
    next_receipt = start(e, command_id="new")
    e["worker"].run_once()
    with closing(e["hub"]._conn()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET creator_session_id='other-manager' WHERE session_id=?", (next_receipt["session_id"],))
    with pytest.raises(PermissionError, match="owner changed"):
        e["service"].control(next_receipt["remote_execution"]["task_id"], command_id="changed-owner", action="pause")
    with pytest.raises(AgentRunCancelled):
        e["worker"].check_run(next_receipt["run_id"])


def test_native_question_answer_relay_once_survives_runtime_restart(setup_remote):
    from app.core.multi_agent import (
        ForkPolicy,
        Plan,
        PlanStatus,
        PlanStep,
        PlanStepStatus,
        ScopeGrant,
    )
    e = setup_remote
    receipt = start(e)
    task_id, run_id = receipt["remote_execution"]["task_id"], receipt["run_id"]
    e["worker"].run_once()
    runtime = e["node"]
    loop = runtime.agent_turn_loop
    plan = Plan(correlation_id="question", plan_id="question-plan", parent_run_id=run_id,
        session_id=receipt["session_id"], objective="Resolve one question", status=PlanStatus.RUNNING,
        steps=(PlanStep(correlation_id="question", step_id="root_coordinator", objective="Resolve question",
            output_contract="Answer once", status=PlanStepStatus.RUNNING),))
    runtime.agent_run_manager.record_multi_agent_plan(run_id, event_type="plan_created",
        payload={"plan_id": plan.plan_id}, plan=plan.model_dump(mode="json"))
    current = runtime.agent_run_manager.get_run(run_id)
    runtime.agent_run_manager._update_run(run_id, status=current.status, metadata_patch={"multi_agent_replan_required": True})
    loop.fork_policy = ForkPolicy(max_depth=0, max_children=0, max_fork_size=1, allowed_scope=ScopeGrant())
    observations = []

    def decide(**kwargs):
        if any(o.get("action") == "user_answer" for o in kwargs["observations"]):
            return {"action": "final_answer", "reason": "Use the journaled answer"}
        return {"action": "plan_patch", "operation": {"patch_id": "ask-region", "plan_id": plan.plan_id,
            "expected_revision": 0, "operation": "ask_user", "reason": "Need region", "user_question": "Which region?"}}

    def answer(**kwargs):
        observations.extend(kwargs["observations"])
        return "Northern region selected."

    loop._decide_next_action = decide
    loop._answer_with_llm = answer
    loop.llm_client = ScriptedClient([])
    assert runtime.agent_dispatch.run_one("ask")
    local = runtime.agent_run_manager.get_run(run_id)
    assert local.status.value == "waiting_user", local.error
    e["worker"].run_once()
    remote = e["service"].get(task_id)
    question = remote["reports"][-1]["pending_user_question"]
    assert question["question"] == "Which region?"
    args = {"command_id": "answer-once", "action": "answer", "question_id": question["question_id"], "answer": "Northern region"}
    accepted = e["service"].control(task_id, **args)
    assert e["service"].control(task_id, **args) == accepted
    with pytest.raises(RemoteExecutionConflict, match="different arguments"):
        e["service"].control(task_id, **{**args, "answer": "Different answer"})
    e["worker"].run_once()
    # Reconstruct runtime, graph, private answer journal and outbound delivery
    # from disk after acceptance, before checkpoint consumption.
    worker = e["worker"]
    settings = runtime.settings
    runtime.remote_execution_worker = worker
    runtime.stop()
    restarted = LocalKnowledgeAgentRuntime(settings)
    try:
        resumed_worker = RemoteExecutionNode(restarted, worker.config, worker.sync_config, transport=worker._transport)
        restarted.remote_execution_worker = resumed_worker
        dispatch_guard = restarted.agent_turn_loop.execution_guard
        restarted.agent_turn_loop.execution_guard = lambda rid: (dispatch_guard(rid), resumed_worker.check_run(rid))
        restarted.agent_turn_loop._decide_next_action = decide
        restarted.agent_turn_loop._answer_with_llm = answer
        restarted.agent_turn_loop.llm_client = ScriptedClient([])
        resumed_worker.run_once()
        assert restarted.agent_dispatch.run_one("answer")
        assert restarted.agent_run_manager.get_run(run_id).status.value == "completed"
        resumed_worker.run_once()
        assert e["service"].get(task_id)["state"] == "completed"
        assert sum(o.get("action") == "user_answer" for o in observations) == 1
        assert not restarted.agent_dispatch.run_one("no-second-answer")
    finally:
        restarted.stop()


def test_continuation_api_requires_account_and_rejects_wrong_question(setup_remote):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.routes.remote_execution import (
        ControlCommand,
        FollowupInstruction,
        UserAnswer,
        answer_question,
        pause_task,
        resume_task,
        send_instruction,
    )
    e = setup_remote
    receipt = start(e)
    task_id = receipt["remote_execution"]["task_id"]
    e["hub"].remote_execution_service = e["service"]
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=e["hub"])), state=SimpleNamespace())
    for method, payload in ((pause_task, ControlCommand(command_id="pause")),
                            (resume_task, ControlCommand(command_id="resume")),
                            (answer_question, UserAnswer(command_id="answer", question_id="wrong", answer="No")),
                            (send_instruction, FollowupInstruction(command_id="next", expected_run_id=receipt["run_id"], purpose="Followup"))):
        with pytest.raises(HTTPException) as error:
            method(task_id, payload, request)
        assert error.value.status_code == 401
    request.state.account = {"user_id": "owner"}
    with pytest.raises(HTTPException) as error:
        resume_task(task_id, ControlCommand(command_id="resume"), request)
    assert error.value.status_code == 409


def test_remote_progress_preserves_tool_rejection_without_exposing_review_data():
    from app.domains.remote_execution import RemoteProgressEvent, remote_progress_projection

    projection = remote_progress_projection({
        "run_id": "synthetic_run", "sequence": 3, "created_at": "2026-10-25T14:00:00+00:00",
        "type": "tool_completed", "payload": {
            "tool_name": "filesystem.edit_file", "status": "rejected",
            "metadata": {"result": {"private": "raw review arguments"}},
        },
    })
    assert RemoteProgressEvent.model_validate(projection).payload == {
        "tool_name": "filesystem.edit_file", "status": "rejected",
    }
    assert "raw review arguments" not in str(projection)
