from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.agent_turn import AgentTurnLoop
from app.core.context_driver import (
    ContextDerivationStatus,
    ContextDriver,
    ContextRequest,
    ContextViewMode,
    EvidenceCandidate,
    ToolView,
    mark_snapshot_stale,
    snapshot_is_stale,
)
from app.core.local_config import AgentInferenceProfile
from app.core.multi_agent import (
    ForkCallerKind,
    MemoryReference,
    PlanStep,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    TaskResult,
    TaskResultStatus,
)
from app.core.tools import (
    ToolContext,
    ToolExecutor,
    ToolPackageSpec,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


def _step(**overrides) -> PlanStep:
    return PlanStep(
        correlation_id="context_trace",
        step_id="research",
        objective="Research the task",
        output_contract="report",
        **overrides,
    )


def _request(step: PlanStep | None = None, **overrides) -> ContextRequest:
    scope = ScopeGrant(
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )
    values = {
        "snapshot_id": "snapshot_1",
        "child_run_id": "child_1",
        "parent_run_id": "parent_1",
        "session_id": "session_1",
        "plan_id": "plan_1",
        "plan_step": step or _step(allowed_packages=("knowledge",), allowed_tools=("knowledge.search",), side_effect_level=SideEffectLevel.READ),
        "parent_effective_scope": scope,
        "session_scope": scope,
        "workspace_scope": scope,
        "policy_scope": scope,
        "budget": RuntimeBudget(max_tokens=100, max_tool_calls=2),
        "policy_version": "policy_1",
        "workspace_version": "workspace_1",
        "permission_version": "permission_1",
    }
    values.update(overrides)
    return ContextRequest(**values)


def test_context_driver_creates_scoped_immutable_snapshot() -> None:
    result = asyncio.run(
        ContextDriver().derive(_request(parent_snapshot_id="snapshot_parent"))
    )
    assert result.status == ContextDerivationStatus.READY
    assert result.snapshot is not None
    assert result.snapshot.child_run_id == "child_1"
    assert result.snapshot.parent_snapshot_id == "snapshot_parent"
    assert result.snapshot.effective_scope.allowed_packages == ("knowledge",)
    assert result.snapshot.full_data_authority is True
    assert result.snapshot.full_workspace_authority is False
    assert result.views is not None
    assert result.views.tool.allows_tool(
        tool_name="knowledge.search", package="knowledge", read_only=True
    )
    assert not result.views.tool.allows_tool(
        tool_name="matter.create", package="matter", read_only=False
    )
    with pytest.raises(ValueError):
        result.snapshot.step_id = "changed"


def test_context_driver_blocks_incomplete_dependencies() -> None:
    dependency = TaskResult(
        correlation_id="context_trace",
        result_id="result_1",
        child_run_id="child_dep",
        plan_id="plan_1",
        step_id="dep",
        snapshot_id="snapshot_dep",
        status=TaskResultStatus.FAILED,
        summary="failed",
        failure={"category": "tool", "code": "failed", "message": "failed"},
    )
    result = asyncio.run(ContextDriver().derive(_request(dependency_results=(dependency,))))
    assert result.status == ContextDerivationStatus.BLOCKED_DEPENDENCY


def test_context_driver_exposes_completed_dependency_summary_to_child() -> None:
    dependency = TaskResult(
        correlation_id="context_trace",
        result_id="result_completed",
        child_run_id="child_dependency",
        plan_id="plan_1",
        step_id="prior_research",
        snapshot_id="snapshot_dependency",
        status=TaskResultStatus.COMPLETED,
        summary="The source confirms the requested date is 2026-09-28.",
        artifact_refs=("artifact:research_summary",),
        warnings=("The source date was normalized to UTC.",),
    )
    result = asyncio.run(
        ContextDriver().derive(_request(dependency_results=(dependency,)))
    )
    assert result.views is not None
    assert result.views.agent.dependency_result_refs == ("result_completed",)
    assert result.views.agent.dependency_results[0].step_id == "prior_research"
    assert result.views.agent.dependency_results[0].summary == dependency.summary
    assert result.views.agent.dependency_results[0].artifact_refs == (
        "artifact:research_summary",
    )


def test_context_driver_preserves_server_role_and_only_coordinator_can_fork() -> None:
    coordinator = asyncio.run(
        ContextDriver().derive(
            _request(_step(agent_kind=ForkCallerKind.COORDINATOR))
        )
    )
    leaf = asyncio.run(
        ContextDriver().derive(_request(_step(agent_kind=ForkCallerKind.LEAF)))
    )
    assert coordinator.snapshot is not None and coordinator.views is not None
    assert leaf.snapshot is not None and leaf.views is not None
    assert coordinator.snapshot.agent_kind == ForkCallerKind.COORDINATOR
    assert coordinator.views.planner.agent_kind == ForkCallerKind.COORDINATOR
    assert coordinator.views.planner.can_fork is True
    assert leaf.snapshot.agent_kind == ForkCallerKind.LEAF
    assert leaf.views.planner.agent_kind == ForkCallerKind.LEAF
    assert leaf.views.planner.can_fork is False


def test_context_driver_freezes_profile_or_explicit_request_selection_and_rejects_drift():
    profile = AgentInferenceProfile(
        profile_id="careful",
        client_name="primary",
        model="reasoner-v2",
        reasoning_effort=None,
    )
    step = _step(
        inference_profile_id="careful",
        inference_client_name="primary",
        inference_model="reasoner-v2",
    )
    profile_snapshot = asyncio.run(
        ContextDriver().derive(_request(step, inference_profile=profile))
    )
    assert profile_snapshot.status == ContextDerivationStatus.READY
    assert profile_snapshot.snapshot is not None
    assert profile_snapshot.snapshot.inference_profile_id == "careful"
    assert profile_snapshot.snapshot.inference_selection_source == "server_profile"
    assert profile_snapshot.snapshot.inference_client_name == "primary"
    assert profile_snapshot.snapshot.inference_reasoning_effort is None

    request_snapshot = asyncio.run(
        ContextDriver().derive(
            _request(
                step,
                inference_profile=profile,
                request_llm_client_name="request-client",
                request_llm_model="request-model",
            )
        )
    )
    assert request_snapshot.snapshot is not None
    assert request_snapshot.snapshot.inference_selection_source == "request"
    assert request_snapshot.snapshot.inference_profile_id is None
    assert request_snapshot.snapshot.inference_client_name == "request-client"
    assert request_snapshot.snapshot.inference_model == "request-model"

    changed_profile = profile.model_copy(update={"reasoning_effort": "high"})
    drift = asyncio.run(
        ContextDriver().derive(_request(step, inference_profile=changed_profile))
    )
    assert drift.status == ContextDerivationStatus.SCOPE_DENIED
    assert "changed after" in drift.reason


def test_context_driver_denies_scope_outside_parent_access() -> None:
    request = _request(
        _step(allowed_packages=("filesystem",), side_effect_level=SideEffectLevel.READ),
    )
    result = asyncio.run(ContextDriver().derive(request))
    assert result.status == ContextDerivationStatus.SCOPE_DENIED


def test_empty_step_scope_does_not_inherit_parent_tool_permissions() -> None:
    broad_scope = ScopeGrant(
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )
    result = asyncio.run(
        ContextDriver().derive(
            _request(
                _step(),
                parent_effective_scope=broad_scope,
                session_scope=broad_scope,
                workspace_scope=broad_scope,
                policy_scope=broad_scope,
            )
        )
    )
    assert result.status == ContextDerivationStatus.READY
    assert result.snapshot is not None
    assert result.snapshot.effective_scope.allowed_packages == ()
    assert result.snapshot.effective_scope.allowed_tools == ()


def test_context_driver_intersects_budget_and_supports_expiry() -> None:
    result = asyncio.run(ContextDriver().derive(_request(budget=RuntimeBudget(max_tokens=1))))
    assert result.status == ContextDerivationStatus.READY
    assert result.snapshot is not None
    assert snapshot_is_stale(
        result.snapshot,
        now=(result.snapshot.expires_at or datetime.now(UTC)) + timedelta(seconds=1),
    ) is False
    expired = result.snapshot.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    assert snapshot_is_stale(expired)
    assert mark_snapshot_stale(result.snapshot).stale is True
    assert snapshot_is_stale(result.snapshot, session_id="another_session")
    assert snapshot_is_stale(result.snapshot, policy_version="policy_changed")


def test_context_views_keep_reference_and_working_content_bounded() -> None:
    candidate = EvidenceCandidate(
        evidence={
            "evidence_id": "evidence_1",
            "source_ref": "source_1",
            "untrusted_data": True,
        },
        summary="A useful summary",
        excerpt="short excerpt",
        full_content="full content that must not appear in reference view",
    )
    base_scope = ScopeGrant(
        source_ids=("source_1",),
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )
    reference = asyncio.run(
        ContextDriver().derive(
            _request(
                view_mode=ContextViewMode.REFERENCE,
                evidence_candidates=(candidate,),
                parent_effective_scope=base_scope,
                session_scope=base_scope,
                workspace_scope=base_scope,
                policy_scope=base_scope,
            )
        )
    )
    assert reference.views is not None
    assert reference.views.agent.evidence_summaries == ("A useful summary",)
    assert reference.views.agent.evidence_content == ()
    assert reference.views.audit.untrusted_evidence_refs == ("evidence_1",)

    full = asyncio.run(
        ContextDriver().derive(
            _request(
                view_mode=ContextViewMode.FULL,
                evidence_candidates=(candidate,),
                parent_effective_scope=base_scope,
                session_scope=base_scope,
                workspace_scope=base_scope,
                policy_scope=base_scope,
            )
        )
    )
    assert full.views is not None
    assert full.views.agent.evidence_content == (
        "full content that must not appear in reference view",
    )


def test_context_driver_budgets_evidence_and_resolves_only_authorized_refs() -> None:
    allowed = ScopeGrant(
        source_ids=("source_1",), account_ids=("account_1",),
        allowed_packages=("knowledge",), allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )
    candidates = (
        EvidenceCandidate(
            evidence={"evidence_id": "ok", "source_ref": "source_1", "account_ref": "account_1", "untrusted_data": True},
            summary="A" * 400,
        ),
        EvidenceCandidate(
            evidence={"evidence_id": "denied", "source_ref": "source_1", "account_ref": "account_denied"},
            summary="must remain hidden",
        ),
    )
    loaded = []

    async def resolve(reference):
        loaded.append(reference.evidence_id)
        return EvidenceCandidate(evidence=reference, summary="ignored", excerpt="B" * 500)

    result = asyncio.run(
        ContextDriver().derive(
            _request(
                evidence_candidates=candidates,
                evidence_budget_tokens=140,
                parent_effective_scope=allowed,
                session_scope=allowed,
                workspace_scope=allowed,
                policy_scope=allowed,
            ),
            evidence_resolver=resolve,
        )
    )
    assert result.snapshot is not None and result.views is not None
    assert [ref.evidence_id for ref in result.snapshot.evidence_refs] == ["ok"]
    assert loaded == ["ok"]
    assert "denied" not in repr(result.model_dump())
    assert len(result.views.agent.evidence_content[0]) <= 140 * 4
    assert result.views.audit.untrusted_evidence_refs == ("ok",)


def test_context_driver_filters_unauthorized_sources_and_paths() -> None:
    allowed = ScopeGrant(
        source_ids=("source_allowed",),
        account_ids=("account_allowed",),
        workspace_paths=("/workspace/project",),
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )
    result = asyncio.run(
        ContextDriver().derive(
            _request(
                parent_effective_scope=allowed,
                session_scope=allowed,
                workspace_scope=allowed,
                policy_scope=allowed,
                evidence_candidates=(
                    EvidenceCandidate(
                        evidence={"evidence_id": "allowed", "source_ref": "source_allowed", "account_ref": "account_allowed"},
                        summary="allowed",
                    ),
                    EvidenceCandidate(
                        evidence={"evidence_id": "denied", "source_ref": "source_allowed", "account_ref": "account_denied"},
                        summary="denied",
                    ),
                ),
            )
        )
    )
    assert result.views is not None
    assert result.views.audit.evidence_refs == ("allowed",)
    assert result.views.tool.allowed_paths == ("/workspace/project",)


def test_context_driver_does_not_expose_evidence_with_empty_data_scope() -> None:
    result = asyncio.run(
        ContextDriver().derive(
            _request(
                evidence_candidates=(
                    EvidenceCandidate(
                        evidence={"evidence_id": "private", "source_ref": "private_source"},
                        summary="private evidence",
                        excerpt="private body",
                    ),
                ),
            ),
        )
    )
    assert result.views is not None
    assert result.views.agent.evidence_refs == ()
    assert result.views.agent.evidence_content == ()


def test_context_driver_authorizes_citation_by_source_id_not_display_ref() -> None:
    scope = ScopeGrant(
        source_ids=("source_allowed",),
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )
    candidate = EvidenceCandidate(
        evidence={
            "evidence_id": "chunk_1", "source_ref": "workspace://readable/path#chunk=0",
            "source_id": "source_allowed", "untrusted_data": True,
        },
        summary="relevant evidence",
    )
    result = asyncio.run(ContextDriver().derive(_request(
        evidence_candidates=(candidate,),
        parent_effective_scope=scope, session_scope=scope,
        workspace_scope=scope, policy_scope=scope,
    )))
    assert result.views is not None
    assert result.views.agent.evidence_refs == ("chunk_1",)


def test_full_authority_markers_do_not_follow_a_narrowed_parent_scope() -> None:
    trusted = ScopeGrant(
        source_ids=("s1", "s2"), account_ids=("a1", "a2"),
        workspace_paths=("/workspace",), allowed_packages=("filesystem",),
        allowed_tools=("filesystem.read_file",), side_effect_level=SideEffectLevel.READ,
    )
    narrowed_parent = trusted.model_copy(update={
        "source_ids": ("s1",), "account_ids": ("a1",),
        "workspace_paths": ("/workspace/private",),
    })
    child_scope = narrowed_parent.model_copy(update={
        "allowed_packages": ("filesystem",),
        "allowed_tools": ("filesystem.read_file",),
    })
    result = asyncio.run(
        ContextDriver().derive(
            _request(
                _step(effective_scope=child_scope),
                parent_effective_scope=narrowed_parent,
                session_scope=trusted,
                workspace_scope=trusted,
                policy_scope=trusted,
            )
        )
    )
    assert result.snapshot is not None
    assert result.snapshot.effective_scope.source_ids == ("s1",)
    assert result.snapshot.full_data_authority is False
    assert result.snapshot.full_workspace_authority is False


def test_context_driver_preserves_selected_workspace_under_configured_root(tmp_path) -> None:
    selected = tmp_path / "configured" / "project"
    selected_scope = ScopeGrant(
        workspace_paths=(str(selected),),
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )
    policy_scope = selected_scope.model_copy(
        update={"workspace_paths": (str(tmp_path / "configured"),)}
    )
    result = asyncio.run(
        ContextDriver().derive(
            _request(
                _step(
                    allowed_packages=("knowledge",),
                    allowed_tools=("knowledge.search",),
                    side_effect_level=SideEffectLevel.READ,
                ),
                parent_effective_scope=selected_scope,
                session_scope=selected_scope,
                workspace_scope=selected_scope,
                policy_scope=policy_scope,
            )
        )
    )
    assert result.views is not None
    assert result.views.tool.allowed_paths == (str(selected.resolve(strict=False)),)


def test_context_driver_consumes_full_plan_step_effective_scope_and_degradation_note():
    scope = ScopeGrant(
        workspace_paths=("/workspace/child",),
        source_ids=("source_child",),
        account_ids=("account_child",),
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )
    ceilings = ScopeGrant(
        workspace_paths=("/workspace",),
        source_ids=("source_child", "source_other"),
        account_ids=("account_child",),
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.EXTERNAL,
    )
    step = _step(
        effective_scope=scope,
        allowed_packages=scope.allowed_packages,
        allowed_tools=scope.allowed_tools,
        side_effect_level=scope.side_effect_level,
        degraded_dependency_notes=("upstream: continue with partial source coverage",),
    )
    result = asyncio.run(
        ContextDriver().derive(
            _request(
                step,
                parent_effective_scope=ceilings,
                session_scope=ceilings,
                workspace_scope=ceilings,
                policy_scope=ceilings,
            )
        )
    )
    assert result.snapshot is not None and result.views is not None
    assert result.snapshot.effective_scope == scope.model_copy(
        update={"workspace_paths": ("/workspace/child",)}
    )
    assert result.views.agent.degraded_dependency_notes == (
        "upstream: continue with partial source coverage",
    )


def test_tool_executor_rechecks_immutable_tool_view() -> None:
    class ReadTool:
        spec = ToolSpec(
            name="knowledge.search",
            type="function",
            description="search",
            package="knowledge",
            read_only=True,
        )

        def invoke(self, *, invocation, context) -> ToolResult:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=invocation.tool.name,
                status="completed",
            )

    registry = ToolRegistry()
    registry.register_tool(ReadTool())
    executor = ToolExecutor(registry)
    allowed = asyncio.run(ContextDriver().derive(_request())).views.tool
    assert allowed is not None
    context = ToolContext(
        session_id="session_1",
        context_id="snapshot_1",
        tool_view=allowed.model_copy(update={"allowed_tools": ("other.tool",)}),
    )
    result = executor.execute(
        invocation_id="invocation_1",
        tool_name="knowledge.search",
        tool_input={},
        context=context,
    )
    assert result.status == "rejected"


def test_tool_view_filters_child_package_catalog_and_expansion() -> None:
    class ReadTool:
        def __init__(self, name: str, package: str) -> None:
            self.spec = ToolSpec(
                name=name,
                type="function",
                description=name,
                package=package,
                read_only=True,
            )

        def invoke(self, *, invocation, context) -> ToolResult:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=invocation.tool.name,
                status="completed",
            )

    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name="knowledge", description="knowledge"))
    registry.register_package(ToolPackageSpec(name="mail", description="mail"))
    registry.register_tool(ReadTool("knowledge.search", "knowledge"))
    registry.register_tool(ReadTool("mail.search", "mail"))
    loop = AgentTurnLoop.__new__(AgentTurnLoop)
    loop.tool_executor = ToolExecutor(registry)
    view = ToolView(
        snapshot_id="snapshot_scoped",
        allowed_packages=("knowledge",),
        allowed_tools=("knowledge.search",),
        side_effect_level=SideEffectLevel.READ,
    )

    assert [item["name"] for item in loop._package_catalog(view)] == ["knowledge"]
    assert [item["name"] for item in loop._tool_payloads_for_package("knowledge", tool_view=view)] == [
        "knowledge.search"
    ]
    assert loop._tool_payloads_for_package("mail", tool_view=view) == []


def test_child_memories_are_explicit_versioned_and_bounded():
    ref = MemoryReference(memory_id="mem-1", version=2, content="Prefer source links", scope="global", updated_at="now")
    request = _request(_step(input_refs=("memory:mem-1@2",)), memory_candidates=(ref,),
                       budget=RuntimeBudget(max_tokens=1000))
    result = asyncio.run(ContextDriver().derive(request))
    assert result.status == ContextDerivationStatus.READY
    assert result.snapshot.memory_refs == (ref,)
    assert result.views.agent.memory_refs == result.views.tool.memory_refs == (ref,)
    empty = asyncio.run(ContextDriver().derive(_request(memory_candidates=(ref,))))
    assert empty.snapshot.memory_refs == ()
    stale = request.model_copy(update={"plan_step": _step(input_refs=("memory:mem-1@1",))})
    assert asyncio.run(ContextDriver().derive(stale)).status == ContextDerivationStatus.SCOPE_DENIED
    tiny = request.model_copy(update={"budget": RuntimeBudget(max_tokens=10)})
    assert asyncio.run(ContextDriver().derive(tiny)).status == ContextDerivationStatus.BUDGET_EXCEEDED


def test_parallel_child_memory_snapshots_do_not_follow_later_parent_updates():
    original = MemoryReference(memory_id="shared", version=1, content="Original preference", scope="global", updated_at="first")

    async def derive_children():
        return await asyncio.gather(*[
            ContextDriver().derive(_request(
                _step(input_refs=("memory:shared@1",)), memory_candidates=(original,),
                snapshot_id=f"snapshot_{index}", child_run_id=f"child_{index}", budget=RuntimeBudget(max_tokens=1000),
            )) for index in range(2)
        ])

    children = asyncio.run(derive_children())
    updated = original.model_copy(update={"version": 2, "content": "New preference"})
    next_turn = asyncio.run(ContextDriver().derive(_request(
        _step(input_refs=("memory:shared@2",)), memory_candidates=(updated,), budget=RuntimeBudget(max_tokens=1000),
    )))
    assert next_turn.snapshot.memory_refs == (updated,)
    assert all(child.snapshot.memory_refs == child.views.agent.memory_refs == (original,) for child in children)


def test_child_memory_budget_counts_provenance_metadata_not_only_content():
    ref = MemoryReference(memory_id="repeated", version=1, content="Short preference", scope="global",
                          source_ids=tuple(f"source-{index}-" + "x" * 80 for index in range(50)), updated_at="now")
    result = asyncio.run(ContextDriver().derive(_request(
        _step(input_refs=("memory:repeated@1",)), memory_candidates=(ref,), budget=RuntimeBudget(max_tokens=1000),
    )))
    assert result.status == ContextDerivationStatus.BUDGET_EXCEEDED


def test_duplicate_explicit_memory_refs_are_charged_once():
    ref = MemoryReference(memory_id="duplicate", version=1, content="Preference", scope="global", updated_at="now")
    budget = len(ref.model_dump_json().encode("utf-8"))
    result = asyncio.run(ContextDriver().derive(_request(
        _step(input_refs=("memory:duplicate", "memory:duplicate@1")), memory_candidates=(ref,),
        memory_budget_tokens=budget, budget=RuntimeBudget(max_tokens=1000),
    )))
    assert result.status == ContextDerivationStatus.READY
    assert result.snapshot.memory_refs == (ref,)
