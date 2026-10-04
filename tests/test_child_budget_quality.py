from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.agent_graph import AgentGraphRunner
from app.core.agent_runs import InMemoryAgentRunManager
from app.core.agent_turn import (
    AgentTurnLoop,
    AgentTurnResult,
    _turn_inference_snapshot,
    _turn_run_id,
    _turn_run_manager,
)
from app.core.child_agent import ChildAgentExecutor, _child_prompt
from app.core.config import Settings
from app.core.context_driver import (
    AgentView,
    AuditView,
    ContextDriver,
    ContextRequest,
    ContextViews,
    PlannerView,
    ToolView,
)
from app.core.llm import LLMResponse
from app.core.multi_agent import ForkPolicy, Plan, PlanStatus, PlanStep, RuntimeBudget, ScopeGrant
from app.core.prompt_tokens import PromptTokenCounter
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.sessions import SessionWorkspace
from app.core.tools import ToolPackageSpec, ToolResult, ToolSpec


@contextmanager
def child_loop(tmp_path, *, depth=1, max_depth=1, kind="coordinator", budget=None):
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="parent", user_input="Inspect independent sources.")
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan",
        step_id="part",
        attempt=1,
        user_input="Inspect one source.",
    )
    manager.mark_child_running(child.run_id)
    scope = ScopeGrant()
    step = PlanStep(
        correlation_id=parent.trace_id,
        step_id="part",
        objective=child.user_input,
        output_contract="Return findings, cited evidence and unknowns.",
        fork_operation_id="fork",
        fork_parent_step_id="root_coordinator",
        fork_depth=depth,
        created_by_run_id=parent.run_id,
        agent_kind=kind,
    )
    plan = Plan(
        correlation_id=parent.trace_id,
        plan_id="plan",
        parent_run_id=parent.run_id,
        session_id=parent.session_id,
        objective=parent.user_input,
        steps=(step,),
        status=PlanStatus.RUNNING,
    )
    manager._update_run(
        parent.run_id,
        status=parent.status,
        metadata_patch={"multi_agent_plan": plan.model_dump(mode="json")},
    )
    manager._update_run(
        child.run_id,
        status=manager.get_run(child.run_id).status,
        metadata_patch={
            "context_snapshot": {
                "agent_kind": kind,
                "budget": budget
                or {
                    "max_tokens": 32768,
                    "max_llm_calls": 12,
                },
            }
        },
    )
    loop = AgentTurnLoop(
        session_service=None,
        tool_executor=None,
        llm_client=SimpleNamespace(supports_function_calling=False),
        log_dir=tmp_path,
        run_manager=manager,
    )
    loop.fork_policy = ForkPolicy(
        max_depth=max_depth, max_children=4, max_fork_size=3, allowed_scope=scope
    )
    loop._agent_catalog_for_prompt = lambda: [{"agent_id": "unused-specialist"}]
    prompts = []

    def complete(**kwargs):
        prompts.append(kwargs)
        return SimpleNamespace(content='{"operation":{"type":"final_answer","reason":"done"}}')

    loop._complete_text_with_retry = complete
    tokens = [
        (variable, variable.set(value))
        for variable, value in ((_turn_run_id, child.run_id), (_turn_run_manager, manager))
    ]
    try:
        yield loop, manager, child, prompts
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def decide(loop, observations=None):
    return loop._decide_next_action(
        user_input="Inspect one source.",
        route={},
        context_window={},
        package_catalog=[],
        expanded_package_names=[],
        expanded_tools=[],
        observations=observations if observations is not None else [],
        llm_events=[],
    )


@pytest.mark.parametrize(
    "kind,max_depth,visible",
    [
        ("coordinator", 1, False),
        ("leaf", 3, False),
        ("coordinator", 2, True),
    ],
)
def test_fork_visibility_uses_server_role_and_remaining_depth(tmp_path, kind, max_depth, visible):
    with child_loop(tmp_path, kind=kind, max_depth=max_depth) as (loop, _, _, prompts):
        decide(loop)
        definitions, _ = loop._native_decision_tools(package_catalog=[], expanded_tools=[])
        assert ("agent_fork_subtasks" in [d.name for d in definitions]) is visible
        assert ("Minimal fork example" in prompts[0]["system_prompt"]) is visible
        payload = json.loads(prompts[0]["user_prompt"])
        assert bool(payload["agent_catalog"]) is visible
        assert payload["child_budget"]["max_tokens"] == 32768
        assert payload["child_budget"]["finish_output_reserve_tokens"] > 0


def test_hidden_fork_is_still_rejected_by_the_server(tmp_path):
    with child_loop(tmp_path) as (loop, manager, child, _):
        outcome = loop._handle_fork_subtasks_decision(
            run_id=child.run_id,
            user_input="Inspect one source.",
            operation={
                "operation": "fork_subtasks",
                "operation_id": "forbidden",
                "correlation_id": child.trace_id,
                "parent_step_id": "root_coordinator",
                "subtasks": [
                    {
                        "step_id": "descendant",
                        "objective": "Inspect another source.",
                        "output_contract": "Cited findings.",
                    }
                ],
            },
        )
        assert outcome["status"] == "rejected"
        assert "max_depth" in outcome["message"]
        assert manager.get_run(child.run_id).child_run_ids == ()


def test_last_call_slot_is_saved_for_separate_answer_with_explicit_unknowns(tmp_path):
    with child_loop(tmp_path, budget={"max_tokens": 32768, "max_llm_calls": 2}) as (
        loop,
        manager,
        child,
        prompts,
    ):
        manager.append_event(child.run_id, "llm_started", "Prior decision.")
        observations = [{"result": {"status": "completed", "output": "One known fact."}}]
        decision = decide(loop, observations)
        assert decision["action"] == "final_answer"
        assert prompts == []
        assert observations[-1]["action"] == "child_budget_finish"
        assert "unfulfilled" in observations[-1]["instruction"]
        assert manager.list_events(child.run_id)[-1].type == "child_budget_finish"


def test_token_pressure_finishes_before_another_large_decision(tmp_path):
    with child_loop(tmp_path) as (loop, manager, child, prompts):
        manager.append_event(
            child.run_id,
            "llm_completed",
            "Charged prior work.",
            payload={"budget_token_count": 30000},
        )
        decision = decide(loop, [{"result": {"status": "completed", "output": "Known fact."}}])
        assert decision["action"] == "final_answer"
        assert prompts == []
        assert (
            manager.get_run(child.run_id).metadata["context_snapshot"]["budget"]["max_tokens"]
            == 32768
        )


def test_finish_preflight_never_bypasses_pending_replan(tmp_path):
    with child_loop(tmp_path) as (loop, manager, child, prompts):
        run = manager.get_run(child.run_id)
        manager._update_run(
            child.run_id, status=run.status, metadata_patch={"multi_agent_replan_required": True}
        )
        manager.append_event(
            child.run_id,
            "llm_completed",
            "Charged prior work.",
            payload={"budget_token_count": 30000},
        )
        decide(loop)
        assert len(prompts) == 1
        assert not any(e.type == "child_budget_finish" for e in manager.list_events(child.run_id))


def test_provider_envelope_margin_keeps_the_existing_total_budget_strict(tmp_path):
    with child_loop(tmp_path, budget={"max_tokens": 500, "max_llm_calls": 12}) as (loop, _, _, _):
        loop._selected_session_counter = lambda: SimpleNamespace(
            count_text=lambda _: SimpleNamespace(conservative=False)
        )
        cap = loop._enforce_child_runtime_budget(prompt_estimate=200)
        assert cap <= 236
        with pytest.raises(RuntimeError, match="token budget"):
            loop._enforce_child_runtime_budget(prompt_estimate=450)


def test_answer_reserve_does_not_charge_decision_schemas_twice(tmp_path):
    with child_loop(tmp_path) as (loop, manager, child, _):
        loop.llm_generation_token_budget = 1024
        manager.append_event(
            child.run_id, "llm_completed", "Prior work.", payload={"budget_token_count": 16024}
        )
        observations = [
            {
                "_result_cache": {"artifact_id": "preview"},
                "result": {"output": "Partial index preview."},
            },
            {"result": {"output": "Evidence from one retrieved page; more pages remain."}},
        ]
        payload = {
            "user_input": "Inspect all authorized pages and disclose unknowns.",
            "session_context_window": {},
            "route_context": {},
            "expanded_tools": [{"name": "generic.read", "input_schema": {}}],
            "package_catalog": [{"name": "generic"}],
            "observations": observations,
        }
        fitted_payloads = []

        def fit(**kwargs):
            projected = json.loads(kwargs["user_prompt"])
            fitted_payloads.append(projected)
            return SimpleNamespace(
                input_tokens=8634 if "expanded_tools" in projected else 4000,
                output_reserve_tokens=1024,
                conservative=False,
            )

        loop._budget_llm_prompt = fit
        decision, cap = loop._child_decision_preflight(
            system_prompt="Control instructions and schemas.",
            user_prompt=json.dumps(payload),
            observations=observations,
        )
        assert decision is None  # 16,744 remains: another control call plus delivery fits.
        assert cap == 1024
        delivery = next(p for p in fitted_payloads if "answer_stage_decision" in p)
        assert "expanded_tools" not in delivery and "package_catalog" not in delivery
        assert (
            delivery["observations"] == observations
        )  # Neither evidence nor cache handles were fabricated/dropped.
        assert not any(e.type == "child_budget_finish" for e in manager.list_events(child.run_id))


def test_control_output_is_clamped_before_forcing_premature_finish(tmp_path):
    with child_loop(tmp_path) as (loop, manager, child, _):
        loop.llm_generation_token_budget = 1024
        manager.append_event(
            child.run_id, "llm_completed", "Prior work.", payload={"budget_token_count": 16024}
        )
        loop._budget_llm_prompt = lambda **kwargs: SimpleNamespace(
            input_tokens=5000
            if "answer_stage_decision" in json.loads(kwargs["user_prompt"])
            else 8634,
            output_reserve_tokens=1024,
            conservative=False,
        )
        decision, cap = loop._child_decision_preflight(
            system_prompt="Control.",
            user_prompt=json.dumps(
                {
                    "user_input": "Read remaining pages.",
                    "observations": [],
                    "session_context_window": {},
                    "route_context": {},
                }
            ),
            observations=[],
        )
        assert decision is None
        assert 256 <= cap < 1024
        assert (
            manager.get_run(child.run_id).metadata["context_snapshot"]["budget"]["max_tokens"]
            == 32768
        )


def test_insufficient_control_allowance_still_finishes_honestly(tmp_path):
    with child_loop(tmp_path) as (loop, manager, child, _):
        loop.llm_generation_token_budget = 1024
        manager.append_event(
            child.run_id, "llm_completed", "Prior work.", payload={"budget_token_count": 16024}
        )
        loop._budget_llm_prompt = lambda **kwargs: SimpleNamespace(
            input_tokens=5300
            if "answer_stage_decision" in json.loads(kwargs["user_prompt"])
            else 8634,
            output_reserve_tokens=1024,
            conservative=False,
        )
        decision, _ = loop._child_decision_preflight(
            system_prompt="Control.",
            user_prompt=json.dumps({"observations": []}),
            observations=[],
        )
        assert decision["action"] == "final_answer"
        assert "unfulfilled" in decision["reason"]
        event = manager.list_events(child.run_id)[-1]
        assert event.payload["control_output_allowance"] < 256
        assert event.payload["answer_input_estimate"] == 5812


def test_delivery_projection_keeps_frozen_contract_and_partial_evidence(tmp_path):
    with child_loop(tmp_path) as (loop, _, _, _):
        fitted_payloads = []

        def fit(**kwargs):
            fitted_payloads.append((kwargs, json.loads(kwargs["user_prompt"])))
            return SimpleNamespace(input_tokens=4000, conservative=False)

        loop._budget_llm_prompt = fit
        contract = "Return valid JSON with cited facts and explicit unknowns."
        observations = [
            {
                "_result_cache": {"artifact_id": "preview"},
                "_prompt_compaction": {"omitted_count": 40},
                "result": {
                    "status": "completed",
                    "output": {"visible_count": 20, "total_count": 60},
                },
            }
        ]
        token = _turn_inference_snapshot.set(SimpleNamespace(output_contract=contract))
        try:
            estimate = loop._child_answer_input_estimate(
                user_prompt=json.dumps(
                    {
                        "user_input": "Inspect authorized sources.",
                        "output_contract": "Not the frozen contract.",
                        "expanded_tools": [{"input_schema": {}}],
                    }
                ),
                observations=observations,
                fallback=8634,
            )
        finally:
            _turn_inference_snapshot.reset(token)
        assert estimate == 4512
        kwargs, payload = fitted_payloads[0]
        assert kwargs["tools"] is None
        assert payload["output_contract"] == contract
        assert payload["observations"] == observations
        assert "expanded_tools" not in payload


def test_unstructured_forecast_falls_back_without_optimistic_compaction(tmp_path):
    with child_loop(tmp_path) as (loop, _, _, _):
        assert (
            loop._child_answer_input_estimate(
                user_prompt="Not JSON.",
                observations=[],
                fallback=8634,
            )
            == 8634
        )


def test_child_assignment_encourages_bounded_evidence_then_honest_delivery():
    views = ContextViews(
        agent=AgentView(
            objective="Inspect one source.",
            output_contract="Cited facts and unknowns.",
            mode="working",
        ),
        tool=ToolView(snapshot_id="snapshot", side_effect_level="read"),
        planner=PlannerView(plan_id="plan", step_id="part"),
        audit=AuditView(
            snapshot_id="snapshot",
            child_run_id="child",
            session_id="session",
            policy_version="v1",
            workspace_version="v1",
            permission_version="v1",
        ),
    )
    prompt = _child_prompt(views)
    assert "fewest" in prompt
    assert "unfulfilled" in prompt
    assert "Cited facts and unknowns." in prompt


def test_terminal_child_without_answer_explicitly_reports_missing_output():
    manager = InMemoryAgentRunManager()
    parent = manager.create_run(session_id="parent", user_input="Collect independent evidence.")
    child = manager.create_child_run(
        parent_run_id=parent.run_id,
        plan_id="plan",
        step_id="part",
        attempt=1,
        user_input="Inspect one source.",
    )
    scope = ScopeGrant()
    derived = asyncio.run(
        ContextDriver().derive(
            ContextRequest(
                snapshot_id="snapshot",
                child_run_id=child.run_id,
                parent_run_id=parent.run_id,
                session_id=child.session_id,
                plan_id="plan",
                plan_step=PlanStep(
                    correlation_id=parent.trace_id,
                    step_id="part",
                    objective="Inspect one source.",
                    output_contract="Cited findings and unknowns.",
                ),
                parent_effective_scope=scope,
                session_scope=scope,
                workspace_scope=scope,
                policy_scope=scope,
                budget=RuntimeBudget(max_tokens=32768),
                policy_version="v1",
                workspace_version="v1",
                permission_version="v1",
            )
        )
    )

    class EmptyRunner:
        async def run_async(self, **kwargs):
            manager.complete_child_run(child.run_id, result_snapshot={"answer": ""})
            return AgentTurnResult(
                run_id=child.run_id, session_id=child.session_id, trace_id=child.trace_id, answer=""
            )

    result = asyncio.run(
        ChildAgentExecutor(runner=EmptyRunner(), run_manager=manager).execute(
            child_run_id=child.run_id,
            snapshot=derived.snapshot,
            views=derived.views,
        )
    )
    assert result.missing_requirements == ("child_answer_missing",)
    assert result.verification is None


def test_three_concurrent_tiny_audits_deliver_through_real_child_graph_under_32k(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(3):
        (workspace / f"fact_{index}.txt").write_text(f"Source {index}: fact {index}.\n")
    runtime = LocalKnowledgeAgentRuntime(
        Settings(
            LKA_DATA_DIR=tmp_path / "data",
            LKA_LOCAL_CONFIG=tmp_path / "missing.toml",
            LKA_WORKSPACE_ROOTS=str(workspace),
        )
    )

    class ReadEvidence:
        spec = ToolSpec(
            name="evidence.read",
            package="evidence",
            type="local_tool",
            read_only=True,
            description="Read one exact evidence source.",
            input_schema={
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
            },
            scope_path_fields=("path",),
            scope_uses_workspace=True,
        )

        def invoke(self, *, invocation, context):
            text = (workspace / invocation.input["path"]).read_text()
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=self.spec.name,
                status="completed",
                output={"text": text},
            )

    runtime.tool_registry.register_package(
        ToolPackageSpec(name="evidence", description="Evidence.")
    )
    runtime.tool_registry.register_tool(ReadEvidence())
    counter = PromptTokenCounter()

    class ScriptedModel:
        supports_function_calling = False

        def complete_text(self, **kwargs):
            payload = json.loads(kwargs["user_prompt"])
            if kwargs["metadata"]["stage"] == "decision":
                read_count = len(payload["completed_tool_calls"])
                operation = (
                    {
                        "type": "tool_call",
                        "tool_name": "evidence.read",
                        "tool_input": {"path": f"fact_{read_count}.txt"},
                    }
                    if read_count < 3
                    else {"type": "final_answer", "reason": "Three sources read."}
                )
                content = json.dumps({"operation": operation})
            elif kwargs["metadata"]["stage"] == "answer":
                results = [
                    o for o in payload["observations"] if o.get("tool_name") == "evidence.read"
                ]
                assert len(results) == 3
                content = "Three cited source findings: fact 0, fact 1, fact 2. No independent verification claimed."
            else:
                raise AssertionError(f"Unexpected model stage: {kwargs['metadata']['stage']}")
            input_tokens = counter.count_request(
                kwargs["system_prompt"], kwargs["user_prompt"]
            ).count
            output_tokens = counter.count_text(content).count
            assert output_tokens <= kwargs["max_output_tokens"]
            return LLMResponse(
                provider="scripted",
                status="completed",
                finish_reason="stop",
                content=content,
                prompt_summary=kwargs["prompt_summary"],
                usage={
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            )

    loop = runtime.agent_turn_loop
    loop.llm_client = ScriptedModel()
    manager = runtime.agent_run_manager
    parent = manager.create_run(session_id="parent", user_input="Three independent audits.")
    manager.mark_running(parent.run_id)
    parent_session = runtime.session_service.ensure_session(session_id=parent.session_id)
    runtime.session_service.set_workspace(
        session_id=parent_session.session_id,
        workspace=SessionWorkspace(
            path=str(workspace),
            backend_path=str(workspace),
            platform="linux",
        ),
    )
    scope = ScopeGrant(
        workspace_paths=(str(workspace),),
        allowed_packages=("evidence",),
        allowed_tools=("evidence.read",),
        side_effect_level="read",
    )
    loop.fork_policy = ForkPolicy(max_depth=1, max_children=4, max_fork_size=3, allowed_scope=scope)
    executor = ChildAgentExecutor(runner=AgentGraphRunner(loop), run_manager=manager)

    async def audit(index):
        child = manager.create_child_run(
            parent_run_id=parent.run_id,
            plan_id="plan",
            step_id=f"audit_{index}",
            attempt=1,
            user_input="Inspect sources.",
        )
        derived = await ContextDriver().derive(
            ContextRequest(
                snapshot_id=f"snapshot_{index}",
                child_run_id=child.run_id,
                parent_run_id=parent.run_id,
                session_id=child.session_id,
                plan_id="plan",
                plan_step=PlanStep(
                    correlation_id=parent.trace_id,
                    step_id=f"audit_{index}",
                    agent_kind="leaf",
                    objective="Read fact_0.txt, fact_1.txt and fact_2.txt; report supported facts.",
                    output_contract="Concise findings citing each source; list remaining unknowns.",
                    allowed_packages=scope.allowed_packages,
                    allowed_tools=scope.allowed_tools,
                    side_effect_level="read",
                ),
                parent_effective_scope=scope,
                session_scope=scope,
                workspace_scope=scope,
                policy_scope=scope,
                budget=RuntimeBudget(max_tokens=32768, max_llm_calls=12, max_tool_calls=6),
                policy_version="v1",
                workspace_version="v1",
                permission_version="v1",
            )
        )
        result = await executor.execute(
            child_run_id=child.run_id, snapshot=derived.snapshot, views=derived.views
        )
        events = manager.list_events(child.run_id)
        assert result.status.value == "completed", result.failure
        assert "fact 2" in result.summary
        assert result.verification is None
        assert (
            sum(e.payload["budget_token_count"] for e in events if e.type == "llm_completed")
            < 32768
        )
        assert sum(e.type == "tool_completed" for e in events) == 3
        assert any(e.stage == "answer" and e.type == "llm_completed" for e in events)
        return result

    async def run_audits():
        return await asyncio.gather(*(audit(index) for index in range(3)))

    assert len(asyncio.run(run_audits())) == 3
