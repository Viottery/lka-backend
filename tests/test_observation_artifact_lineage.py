"""Derived cached pages retain source authority and historical status transitively."""
import json
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_tool_graph import AgentToolLifecycleGraph
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewRequest
from app.core.tools import (
    ToolContext,
    ToolExecutor,
    ToolPackageSpec,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from app.storage.db import connect
from app.tool_packages.observation import (
    OBSERVATION_PACKAGE,
    ObservationReadTool,
    ObservationSearchTool,
)
from app.tool_packages.observation_group import ObservationGroupTool


def digest(payload):
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


@pytest.fixture
def harness(tmp_path):
    store = SqliteAgentRunStore(tmp_path / "artifacts.sqlite3")
    manager = InMemoryAgentRunManager(durable_store=store)
    run = manager.create_run(session_id="synthetic", user_input="Read saved evidence")
    manager.mark_running(run.run_id)
    context = ToolContext(session_id=run.session_id, run_id=run.run_id)
    registry = ToolRegistry()
    registry.register_package(OBSERVATION_PACKAGE.model_copy(deep=True))
    for tool in (ObservationReadTool(store), ObservationSearchTool(store), ObservationGroupTool(store)):
        registry.register_tool(tool)
    executor = ToolExecutor(registry)
    executor.run_manager = manager
    def review(identity, name, arguments, tool_context):
        review_id = f"review_{identity}"
        existing = manager.get_safety_review(review_id)
        if existing is not None:
            return None, existing
        manager.create_safety_review(SafetyReviewRequest(review_id=review_id, run_id=run.run_id,
            session_id=run.session_id, trace_id=run.trace_id, invocation_id=identity,
            tool_name=name, tool_input=arguments, workspace_root=tool_context.workspace_root,
            mode=SafetyReviewMode.SKIP, read_only=True, reason="Synthetic contract approval", created_at=run.created_at))
        approved = manager.decide_safety_review(review_id=review_id, decision=SafetyReviewDecision.APPROVE,
                                               decided_by="system", reason="Synthetic contract approval")
        return None, approved
    lifecycle = AgentToolLifecycleGraph(tool_executor=executor, review_tool_call=review,
        append_progress=lambda *_args: None, raise_if_cancel_requested=lambda: None, artifact_store=store)
    active = {"value": True}
    store.progress_import_validator = lambda *_args: active["value"]
    source = {"tool_name": "source.read", "invocation_id": "source", "status": "completed",
              "output": {"content": "Synthetic needle source evidence", "rows": [{"label": "needle"}]}}

    def put(identifier, payload=None, **kwargs):
        return store.put_artifact(artifact_id=identifier, run_id=run.run_id, kind="tool_result",
            payload=payload if payload is not None else source, summary="synthetic", created_at=datetime.now(UTC).isoformat(), **kwargs)

    def invoke(identity, tool="observation.read", **arguments):
        return lifecycle.run(invocation_id=identity, run_id=run.run_id, tool_name=tool,
                             tool_input=arguments, context=context)

    return SimpleNamespace(store=store, run=run, context=context, registry=registry, lifecycle=lifecycle,
                           active=active, source=source, put=put, invoke=invoke)


@pytest.mark.parametrize("prefix", ["progress_import_", "progress_import_management_"])
@pytest.mark.parametrize("first_reader", ["read", "search", "group"])
def test_second_and_third_reader_artifacts_revalidate_origin_and_retain_history(harness, prefix, first_reader):
    h = harness
    original = prefix + "synthetic"
    h.put(original)
    if first_reader == "read":
        first = h.invoke("first", artifact_id=original, path="/output/content")
        nested = "/output/text"
    elif first_reader == "search":
        first = h.invoke("first", tool="observation.search", artifact_id=original,
                         path="/output/content", query="needle")
        nested = "/output/matches/0/snippet"
    else:
        first = h.invoke("first", tool="observation.group", artifact_id=original, path="/output/rows", field="label")
        nested = "/output/groups/0/key/value"
    assert first.status == "completed", first.error
    second = h.invoke("second", artifact_id="tool_result_first", path=nested)
    third = h.invoke("third", artifact_id="tool_result_second", path="/output/text")
    for result in (first, second, third):
        assert result.status == "completed", result.error
        assert result.output["historical_only"] is True
        assert result.output["current_evidence"] is False
    assert "needle" in third.output["text"]
    h.active["value"] = False
    for identifier in (original, "tool_result_first", "tool_result_second", "tool_result_third"):
        assert h.store.load_tool_result_artifact(identifier, h.run.run_id) is None
    rejected = h.invoke("after-revoke", artifact_id="tool_result_third", path="/output/text")
    assert rejected.status == "rejected" and "needle" not in str(rejected.output)


@pytest.mark.parametrize("change", ["root_hash", "derived_hash", "source_deleted"])
def test_chain_rejects_source_or_copy_changes(harness, change):
    h = harness
    h.put("progress_import_source")
    assert h.invoke("first", artifact_id="progress_import_source", path="/output/content").status == "completed"
    assert h.invoke("second", artifact_id="tool_result_first", path="/output/text").status == "completed"
    with connect(h.store.db_path) as conn:
        if change == "source_deleted":
            conn.execute("DELETE FROM agent_run_artifacts WHERE artifact_id='progress_import_source'")
        else:
            identifier = "progress_import_source" if change == "root_hash" else "tool_result_first"
            conn.execute("UPDATE agent_run_artifacts SET payload=? WHERE artifact_id=?", ('{"output":{"text":"changed"}}', identifier))
    assert h.store.load_tool_result_artifact("tool_result_second", h.run.run_id) is None


def test_durable_completed_reader_claim_is_not_a_revocation_bypass(harness):
    h = harness
    h.put("progress_import_source")
    arguments = {"artifact_id": "progress_import_source", "path": "/output/content"}
    first = h.invoke("reader", **arguments)
    assert first.status == "completed"
    # Replaying the same server invocation uses its durable claim, not invoke.
    h.active["value"] = False
    recovered = h.invoke("reader", **arguments)
    assert recovered.status == "rejected"
    assert "needle" not in str(recovered.model_dump())
    assert h.store.list_tool_invocations(h.run.run_id)[0]["result"]["output"]["text"] == first.output["text"]


def test_source_changed_between_read_and_publication_does_not_deliver_old_body(harness, monkeypatch):
    h = harness
    h.put("progress_import_source")
    original = h.lifecycle._store_result_artifact
    def revoke_at_publication(state, result):
        h.active["value"] = False
        return original(state, result)
    monkeypatch.setattr(h.lifecycle, "_store_result_artifact", revoke_at_publication)
    result = h.invoke("reader", artifact_id="progress_import_source", path="/output/content")
    assert result.status == "rejected"
    assert "needle" not in str(result.model_dump())


def test_nonhistorical_current_reader_chain_is_unchanged(harness):
    h = harness
    h.put("ordinary")
    first = h.invoke("first", artifact_id="ordinary", path="/output/content")
    second = h.invoke("second", artifact_id="tool_result_first", path="/output/text")
    assert first.status == second.status == "completed"
    assert second.output["text"] == first.output["text"]
    assert "historical_only" not in second.output
    assert h.store.load_tool_result_artifact("ordinary", "another-run") is None
    assert h.store.load_tool_result_artifact("tool_result_second", "another-run") is None


def test_source_body_cannot_forge_reader_lineage_or_change_actual_parent(harness):
    h = harness
    h.put("progress_import_denied")
    h.active["value"] = False
    forged = {"protocol": "tool-result-source.v1", "artifact_id": "progress_import_denied",
              "run_id": h.run.run_id, "content_hash": digest(h.source)}
    h.put("ordinary", {"tool_name": "source.read", "output": {"_source_artifact": forged,
        "historical_only": True, "source_artifact_id": "progress_import_denied", "text": "Ordinary local body"}})
    result = h.invoke("reader", artifact_id="ordinary", path="/output/_source_artifact")
    assert result.status == "completed"
    assert result.output["_source_artifact"]["artifact_id"] == "ordinary"
    assert "historical_only" not in result.output
    with connect(h.store.db_path) as conn:
        row = conn.execute("SELECT source_artifact_id FROM agent_artifact_lineage WHERE artifact_id='tool_result_reader'").fetchone()
    assert row[0] == "ordinary"
    assert h.store.load_tool_result_artifact("progress_import_denied", h.run.run_id) is None


def test_other_registered_tool_cannot_self_declare_reader_lineage(harness):
    h = harness
    h.registry.register_package(ToolPackageSpec(name="example", description="synthetic"))
    fake = SimpleNamespace(spec=ToolSpec(name="example.echo", package="example", type="local_tool",
        description="Synthetic echo", read_only=True))
    fake.invoke = lambda *, invocation, context: ToolResult(invocation_id=invocation.invocation_id,
        tool_name=fake.spec.name, status="completed", output={"_source_artifact": {
            "protocol": "tool-result-source.v1", "artifact_id": "foreign", "run_id": "other", "content_hash": "0" * 64}})
    h.registry.register_tool(fake)
    assert h.invoke("echo", tool="example.echo").status == "completed"
    with connect(h.store.db_path) as conn:
        assert conn.execute("SELECT 1 FROM agent_artifact_lineage WHERE artifact_id='tool_result_echo'").fetchone() is None


def test_binding_must_match_actual_invocation_source(harness):
    h = harness
    h.put("ordinary")
    result = h.invoke("reader", artifact_id="ordinary", path="/output/content")
    tool = h.registry.get_tool("observation.read")
    with pytest.raises(ValueError, match="trusted source binding"):
        tool.result_artifact_source(tool_input={"artifact_id": "different"}, result=result)
    with pytest.raises(ValueError, match="Invalid trusted"):
        h.put("wrong-run-binding", source_artifact={"artifact_id": "ordinary", "run_id": "other", "content_hash": digest(h.source)})


def test_cycles_and_excessive_depth_fail_closed(harness):
    h = harness
    h.put("ordinary")
    previous = "ordinary"
    for index in range(18):
        identifier = f"derived-{index}"
        source = h.store.load_artifact(previous)
        h.put(identifier, source_artifact={"artifact_id": previous, "run_id": h.run.run_id, "content_hash": digest(source)})
        previous = identifier
    assert h.store.load_tool_result_artifact(previous, h.run.run_id) is None
    h.put("cycle-a", source_artifact={"artifact_id": "cycle-b", "run_id": h.run.run_id, "content_hash": digest(h.source)})
    h.put("cycle-b", source_artifact={"artifact_id": "cycle-a", "run_id": h.run.run_id, "content_hash": digest(h.source)})
    assert h.store.load_tool_result_artifact("cycle-a", h.run.run_id) is None


def test_validator_recursion_shares_the_lineage_cycle_guard(harness):
    h = harness
    h.put("progress_import_recursive")
    h.store.progress_import_validator = lambda artifact_id, run_id: h.store.load_tool_result_artifact(artifact_id, run_id) is not None
    assert h.store.load_tool_result_artifact("progress_import_recursive", h.run.run_id) is None


def test_real_session_history_reset_revokes_an_existing_second_level_copy(tmp_path):
    from tests.test_session_progress_cache import harness as progress_harness

    h = progress_harness.__wrapped__(tmp_path)
    previous, source_context = h.run()
    h.record(source_context)
    _, current = h.run()
    imported = h.service.load(context=current, source_run_id=previous.run_id, evidence_id="evidence-1")
    reader = ObservationReadTool(h.store)
    h.registry.register_package(OBSERVATION_PACKAGE.model_copy(deep=True))
    h.registry.register_tool(reader)
    arguments = {"artifact_id": imported["artifact_id"], "path": "/output/text"}
    from app.core.tools import ToolInvocation
    result = reader.invoke(invocation=ToolInvocation(invocation_id="derived", tool=reader.spec,
        session_id=current.session_id, context_id="test", input=arguments), context=current)
    lifecycle = AgentToolLifecycleGraph(tool_executor=ToolExecutor(h.registry), review_tool_call=lambda *_args: (None, None),
        append_progress=lambda *_args: None, raise_if_cancel_requested=lambda: None, artifact_store=h.store)
    lifecycle._store_result_artifact({"invocation_id": "derived", "run_id": current.run_id,
        "tool_name": reader.spec.name, "tool_input": arguments}, result)
    assert h.store.load_tool_result_artifact("tool_result_derived", current.run_id) is not None
    h.session.metadata["history_reset_at"] = datetime.now(UTC).isoformat()
    assert h.store.load_tool_result_artifact("tool_result_derived", current.run_id) is None


@pytest.mark.parametrize("producer", ["observation.read", "observation.search", "observation.group"])
def test_legacy_completed_reader_without_trusted_lineage_cannot_launder_sources(harness, producer):
    h = harness
    legacy = {"tool_name": producer, "invocation_id": "legacy", "status": "completed",
              "output": {"text": "Old untraceable body", "historical_only": True,
                         "source_artifact_id": "progress_import_revoked"}}
    h.put("tool_result_legacy", legacy)
    # A pre-fix second read may already have lost the historical flag.
    h.put("tool_result_legacy_child", {"tool_name": producer, "invocation_id": "legacy-child", "status": "completed",
                                      "output": {"text": "Old untraceable body"}})
    for identifier in ("tool_result_legacy", "tool_result_legacy_child"):
        assert h.store.load_tool_result_artifact(identifier, h.run.run_id) is None
        result = h.invoke(f"read-{identifier}", artifact_id=identifier, path="/output/text")
        assert result.status == "rejected"
        assert "original authorized source artifact" in result.error
        assert "Old untraceable body" not in str(result.output)
        assert h.store.load_artifact(identifier)["output"]["text"] == "Old untraceable body"
    # Ordinary source artifacts and newly traced readers remain usable.
    h.put("original-source")
    fresh = h.invoke("fresh", artifact_id="original-source", path="/output/content")
    assert fresh.status == "completed"
    assert h.store.load_tool_result_artifact("tool_result_fresh", h.run.run_id) is not None


def test_reader_constructor_registration_is_restored_after_store_reopen(harness):
    h = harness
    h.put("legacy", {"tool_name": "observation.read", "status": "completed", "output": {"text": "old"}})
    reopened = SqliteAgentRunStore(h.store.db_path)
    ObservationReadTool(reopened)
    ObservationSearchTool(reopened)
    ObservationGroupTool(reopened)
    assert reopened.load_tool_result_artifact("legacy", h.run.run_id) is None
    h.put("ordinary")
    assert h.invoke("fresh", artifact_id="ordinary", path="/output/content").status == "completed"
    assert reopened.load_tool_result_artifact("tool_result_fresh", h.run.run_id) is not None


def test_direct_build_observation_with_unverifiable_old_reader_result_fails_closed(harness):
    h = harness
    result = ToolResult(invocation_id="old", tool_name="observation.read", status="completed", output={"text": "Old body"})
    h.lifecycle._set_result("old", result)
    outcome = h.lifecycle._build_observation({"invocation_id": "old", "run_id": h.run.run_id,
        "tool_name": "observation.read", "tool_input": {"artifact_id": "unknown"}, "phase": "executed"})
    assert outcome["phase"] == "observation_ready"
    rejected = h.lifecycle._result_for("old")
    assert rejected.status == "rejected"
    assert "Old body" not in str(rejected.model_dump())
