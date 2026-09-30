from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.api.main import create_app
from app.api.routes.agent import (
    continue_agent_run,
    decide_agent_safety_review,
    list_agent_safety_review_queue,
)
from app.api.schemas import ContinueAgentRunRequest, SafetyReviewDecisionRequest
from app.core.agent_graph import (
    AgentGraphRunner,
    AgentTurnWaitingForConfirmation,
    AgentTurnWaitingForUser,
)
from app.core.config import get_settings
from app.core.multi_agent import Plan, PlanStatus, PlanStep, PlanStepStatus
from app.core.safety import SafetyReviewDecision
from app.core.tools import ToolContext, ToolPackageSpec, ToolResult, ToolSpec
from app.domains.knowledge import KnowledgeDocumentInput, KnowledgeSourceInput
from app.domains.mail import MailAccountInput, MailMessageInput


class _ApprovalWriteTool:
    invocations = 0
    spec = ToolSpec(
        name="integration.write",
        type="local_tool",
        package="integration",
        description="Write a test record after safety approval.",
        read_only=False,
        input_schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        output_schema={"saved": "boolean", "value": "string"},
    )

    def invoke(self, *, invocation, context: ToolContext) -> ToolResult:
        _ = context
        type(self).invocations += 1
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={"saved": True, "value": invocation.input["value"]},
        )


@pytest.mark.parametrize("nested", [False, True], ids=["direct-child", "coordinator-to-leaf"])
def test_root_react_fork_child_review_resume_and_parent_answer(tmp_path, monkeypatch, nested):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        '[agent]\norchestrator = "langgraph"\ncheckpoint_backend = "sqlite"\n'
        "multi_agent_planning_enabled = true\nmax_decision_steps = 8\n"
        '[safety]\ntool_review_mode = "manual"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    app = create_app()
    _ApprovalWriteTool.invocations = 0
    runtime = app.state.runtime
    manager = runtime.agent_run_manager
    loop = runtime.agent_turn_loop
    runner = runtime.agent_turn_runner
    assert isinstance(runner, AgentGraphRunner)

    answer_observations: list[dict] = []

    def route(**kwargs):
        _ = kwargs
        return {
        "selected_package": "integration",
        "reason": "The registered integration test tool is required.",
        }

    def decide(**kwargs):
        user_input = kwargs["user_input"]
        observations = kwargs["observations"]
        is_child = user_input.startswith("Execute only the assigned child task.")
        if is_child:
            fork_observation = next(
                (item for item in observations if item.get("action") == "fork_subtasks"), None
            )
            if nested and "COORDINATOR ASSIGNMENT" in user_input:
                if fork_observation is not None:
                    return {"action": "final_answer", "reason": "The leaf result is available."}
                return {
                    "action": "fork_subtasks",
                    "operation": {
                        "correlation_id": "nested_leaf_trace",
                        "operation_id": "nested_leaf_fork",
                        "parent_step_id": "root_coordinator",
                        "subtasks": [{
                            "step_id": "write_record",
                            "objective": "LEAF ASSIGNMENT: save the assigned integration value.",
                            "output_contract": "Report whether the value was saved.",
                        }],
                    },
                    "reason": "Delegate the isolated write to a leaf Agent.",
                }
            if nested and "LEAF ASSIGNMENT" in user_input and fork_observation is None:
                # A real leaf ReAct graph attempts to fork. The runtime must
                # reject this before creating a grandchild, then continue its
                # ordinary decision loop with the rejection observation.
                return {
                    "action": "fork_subtasks",
                    "operation": {
                        "correlation_id": "forbidden_leaf_trace",
                        "operation_id": "forbidden_leaf_fork",
                        "parent_step_id": "root_coordinator",
                        "subtasks": [{
                            "step_id": "forbidden_grandchild",
                            "objective": "Create an unauthorized grandchild task.",
                            "output_contract": "This must never run.",
                        }],
                    },
                    "reason": "Try a forbidden leaf fork for the integration assertion.",
                }
            if any(item.get("tool_name") == "integration.write" for item in observations):
                return {"action": "final_answer", "reason": "The approved write completed."}
            if fork_observation is not None and nested:
                return {
                    "action": "call_tool",
                    "tool_name": "integration.write",
                    "tool_input": {"value": "saved by leaf"},
                    "reason": "Continue after the server rejected the leaf fork.",
                }
            return {
                "action": "call_tool",
                "tool_name": "integration.write",
                "tool_input": {"value": "saved by child"},
                "reason": "Perform the assigned test write.",
            }
        fork_observation = next(
            (item for item in observations if item.get("action") == "fork_subtasks"), None
        )
        if fork_observation is not None:
            return {"action": "final_answer", "reason": "The child result is available."}
        objective = (
            "COORDINATOR ASSIGNMENT: delegate a leaf to save the assigned value."
            if nested
            else "Use integration.write to save the assigned test value."
        )
        return {
            "action": "fork_subtasks",
            "operation": {
                "correlation_id": "integration_fork_trace",
                "operation_id": "integration_fork_operation",
                "parent_step_id": "root_coordinator",
                "subtasks": [
                    ({
                        "step_id": "coordinate_write",
                        "objective": objective,
                        "output_contract": "Coordinate the write and report the result.",
                    } if nested else {
                        "step_id": "write_record",
                        "objective": objective,
                        "output_contract": "Report whether the value was saved.",
                    })
                ],
            },
            "reason": "Delegate the isolated write to a child Agent.",
        }

    def answer(**kwargs):
        user_input = kwargs["user_input"]
        if not user_input.startswith("Execute only the assigned child task."):
            answer_observations.extend(kwargs["observations"])
            return "Parent synthesized the completed child result."
        if "COORDINATOR ASSIGNMENT" in user_input:
            return "Coordinator synthesized the completed leaf result."
        if "LEAF ASSIGNMENT" in user_input:
            return "Leaf saved the requested value."
        return "Child saved the requested value."

    def install_runtime_components(target_runtime):
        target_runtime.tool_registry.register_package(
            ToolPackageSpec(name="integration", description="Integration test package.")
        )
        target_runtime.tool_registry.register_tool(_ApprovalWriteTool())
        target_loop = target_runtime.agent_turn_loop
        current_scope = target_loop.fork_policy.allowed_scope
        target_loop.fork_policy = target_loop.fork_policy.model_copy(
            update={
                "allowed_scope": current_scope.model_copy(
                    update={
                        "allowed_packages": tuple(
                            sorted((*current_scope.allowed_packages, "integration"))
                        ),
                        "allowed_tools": tuple(
                            sorted((*current_scope.allowed_tools, "integration.write"))
                        ),
                    }
                )
            }
        )
        target_loop._route = route
        target_loop._decide_next_action = decide
        target_loop._answer_with_llm = answer

    install_runtime_components(runtime)
    run = runner.create_run_for_turn(
        session_id="multi_agent_integration_parent",
        user_input="Run the integrated child task.",
    )
    request = SimpleNamespace(app=app)

    async def execute_and_approve() -> None:
        nonlocal runtime, manager, loop, runner, request
        try:
            await runner.run_async(
                session_id=run.session_id,
                user_input=run.user_input,
                existing_run_id=run.run_id,
            )
        except AgentTurnWaitingForConfirmation:
            pass
        else:
            current = manager.get_run(run.run_id)
            descendants = manager.child_tree(run.run_id)
            raise AssertionError(
                f"Root turn unexpectedly returned: status={current.status.value}, "
                f"error={current.error}, children={[(x.status.value, x.error, x.error_type, x.metadata.get('multi_agent_plan')) for x in descendants]}, "
                f"events={[(x.run_id, x.type, x.payload) for x in manager.list_events(run.run_id)]}"
            )

        pending = manager.list_pending_safety_reviews()
        assert len(pending) == 1
        review = pending[0]
        child = manager.get_run(review.run_id)
        assert child is not None
        if nested:
            coordinator = manager.get_run(child.parent_run_id)
            assert coordinator is not None and coordinator.parent_run_id == run.run_id
            assert child.step_id == "write_record"
            assert coordinator.status.value == "waiting_confirmation"
        else:
            coordinator = None
            assert child.parent_run_id == run.run_id
        assert child.status.value == "waiting_confirmation"
        assert manager.get_run(run.run_id).status.value == "waiting_confirmation"
        try:
            await runtime.resume_agent_run_async(child.run_id)
        except AgentTurnWaitingForConfirmation:
            pass
        else:
            raise AssertionError("A stale resume must not bypass pending manual approval.")
        assert manager.get_run(child.run_id).status.value == "waiting_confirmation"
        queue = await list_agent_safety_review_queue(request)
        assert [item.review_id for item in queue.reviews] == [review.review_id]
        assert queue.reviews[0].child_run_id == child.run_id

        runtime.stop()
        get_settings.cache_clear()
        restarted_app = create_app()
        runtime = restarted_app.state.runtime
        manager = runtime.agent_run_manager
        loop = runtime.agent_turn_loop
        runner = runtime.agent_turn_runner
        assert isinstance(runner, AgentGraphRunner)
        install_runtime_components(runtime)
        request = SimpleNamespace(app=restarted_app)
        restored_child = manager.get_run(child.run_id)
        assert restored_child is not None
        assert restored_child.status.value == "waiting_confirmation"
        restored_queue = await list_agent_safety_review_queue(request)
        assert [item.review_id for item in restored_queue.reviews] == [review.review_id]

        decided = await decide_agent_safety_review(
            review.review_id,
            SafetyReviewDecisionRequest(
                decision=SafetyReviewDecision.APPROVE,
                reason="Approve the integration smoke write.",
            ),
            request,
        )
        assert decided.status == "approved"

        # The API schedules a resume in the background. A recovery caller may
        # race that task; both share the runner's per-run execution lease.
        await asyncio.gather(
            runtime.resume_agent_run_async(child.run_id),
            runtime.resume_agent_run_async(child.run_id),
        )

        for _ in range(300):
            current_parent = manager.get_run(run.run_id)
            if (
                current_parent is not None
                and current_parent.status.value == "completed"
                and not getattr(runtime, "_agent_turn_tasks", {})
            ):
                return
            await asyncio.sleep(0.01)
        descendants = manager.child_tree(run.run_id)
        raise AssertionError(
            "Child approval did not resume the child and parent Agent graphs: "
            f"parent={manager.get_run(run.run_id).status.value}, "
            f"children={[(item.status.value, item.error, item.error_type) for item in descendants]}, "
            f"tasks={list(getattr(runtime, '_agent_turn_tasks', {}))}"
        )

    try:
        asyncio.run(execute_and_approve())
        child = manager.child_tree(run.run_id)[0]
        descendants = manager.child_tree(run.run_id)
        assert all(item.status.value == "completed" for item in descendants)
        if nested:
            coordinator, leaf = descendants
            assert coordinator.step_id == "coordinate_write"
            assert leaf.step_id == "write_record"
            assert leaf.parent_run_id == coordinator.run_id
            assert coordinator.result_snapshot["answer"] == "Coordinator synthesized the completed leaf result."
            child_aggregate = coordinator.metadata["multi_agent_aggregate"]
            assert child_aggregate["status"] == "complete", child_aggregate
            child_results = child_aggregate["task_results"]
            assert len(child_results) == 1
            assert child_results[0]["status"] == "completed"
            assert child_results[0]["summary"] == "Leaf saved the requested value."
            leaf_rejections = [
                event for event in manager.list_events(leaf.run_id)
                if event.type == "fork_subtasks_rejected"
                and "requested_operation" in event.payload
            ]
            assert len(leaf_rejections) == 1, [event.payload for event in leaf_rejections]
            assert "Leaf Agents cannot fork" in leaf_rejections[0].payload["error"]
            assert manager.child_tree(leaf.run_id) == []
        else:
            coordinator = child
            leaf = child
        assert _ApprovalWriteTool.invocations == 1
        assert runner.get_state(leaf.run_id).values["phase"] == "finalized"
        if nested:
            assert runner.get_state(coordinator.run_id).values["phase"] == "finalized"
        assert runner.get_state(run.run_id).values["phase"] == "finalized"
        assert runtime.agent_run_manager.get_run(run.run_id).result_snapshot["answer"] == (
            "Parent synthesized the completed child result."
        )
        fork_observations = [
            observation
            for observation in answer_observations
            if observation.get("action") == "fork_subtasks"
        ]
        assert len(fork_observations) == 1
        result_observations = [
            result
            for result in fork_observations[0].get("task_results", [])
        ]
        assert len(result_observations) == 1
        assert result_observations[0]["status"] == "completed"
        expected_summary = (
            "Coordinator synthesized the completed leaf result."
            if nested
            else "Child saved the requested value."
        )
        assert result_observations[0]["summary"] == expected_summary
        assert fork_observations[0]["aggregate"]["status"] == "complete", fork_observations[0]["aggregate"]
        assert fork_observations[0]["verification"]["status"] == "inconclusive"
        assert fork_observations[0]["replan_required"] is False
        assert manager.list_pending_safety_reviews() == []
    finally:
        runtime.stop()


def test_fork_scope_resolver_uses_live_local_source_and_mail_inventory(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        '[agent]\norchestrator = "langgraph"\ncheckpoint_backend = "sqlite"\n'
        "multi_agent_planning_enabled = true\n"
        '[safety]\ntool_review_mode = "skip"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    try:
        imported = runtime.knowledge_service.import_text_document(
            KnowledgeDocumentInput(
                source=KnowledgeSourceInput(display_name="Inventory source", uri="inventory://source"),
                title="Inventory", text="authorized inventory document",
            )
        )
        account = runtime.mail_service.import_messages(
            account=MailAccountInput(email_address="inventory@example.test"),
            messages=[MailMessageInput(external_id="inventory-message", subject="inventory")],
        )
        runtime.mail_knowledge_mirror.sync(account_id=account.account_id)
        run = runtime.agent_run_manager.create_run(
            session_id="scope_inventory_session", user_input="scope inventory"
        )
        parent_scope, session_scope, workspace_scope = runtime.fork_scope_resolver(run.run_id)
        mirrored_source = runtime.mail_knowledge_mirror.source_id_for_account(account.account_id)
        for scope in (parent_scope, session_scope, workspace_scope):
            assert imported.source_id in scope.source_ids
            assert mirrored_source in scope.source_ids
            assert account.account_id in scope.account_ids
    finally:
        runtime.stop()
        get_settings.cache_clear()


def test_plan_patch_ask_user_survives_restart_and_idempotent_continue(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        '[agent]\norchestrator = "langgraph"\ncheckpoint_backend = "sqlite"\n'
        "multi_agent_planning_enabled = true\nmax_decision_steps = 8\n"
        '[safety]\ntool_review_mode = "skip"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    manager = runtime.agent_run_manager
    loop = runtime.agent_turn_loop
    runner = runtime.agent_turn_runner
    assert isinstance(runner, AgentGraphRunner)
    input_seen_by_answer: list[dict] = []

    def install(target_runtime):
        target_loop = target_runtime.agent_turn_loop
        target_loop._route = lambda **_: {"selected_package": "knowledge", "reason": "test"}

        def decide(**kwargs):
            observations = kwargs["observations"]
            if any(item.get("action") == "user_answer" for item in observations):
                return {"action": "final_answer", "reason": "The answer was received."}
            plan = target_runtime.agent_run_manager.get_run(run.run_id).metadata[
                "multi_agent_plan"
            ]
            return {
                "action": "plan_patch",
                "operation": {
                    "patch_id": "ask_region",
                    "plan_id": plan["plan_id"],
                    "expected_revision": 0,
                    "operation": "ask_user",
                    "reason": "The task needs a region.",
                    "user_question": "Which region should I use?",
                },
                "reason": "Ask for the missing region.",
            }

        def answer(**kwargs):
            input_seen_by_answer.extend(kwargs["observations"])
            return "Completed using the selected region."

        target_loop._decide_next_action = decide
        target_loop._answer_with_llm = answer

    run = loop.create_run_for_turn(
        session_id="ask_user_restart_session",
        user_input="Find the regional information.",
    )
    plan = Plan(
        correlation_id=run.trace_id,
        plan_id="ask_user_restart_plan",
        parent_run_id=run.run_id,
        session_id=run.session_id,
        objective=run.user_input,
        steps=(PlanStep(
            correlation_id=run.trace_id,
            step_id="root_coordinator",
            objective=run.user_input,
            output_contract="Produce the user-facing response.",
            status=PlanStepStatus.RUNNING,
        ),),
        status=PlanStatus.RUNNING,
    )
    manager.record_multi_agent_plan(
        run.run_id,
        event_type="plan_created",
        payload={"plan_id": plan.plan_id},
        plan=plan.model_dump(mode="json"),
    )
    manager._update_run(
        run.run_id,
        status=run.status,
        metadata_patch={"multi_agent_replan_required": True},
    )
    install(runtime)

    async def pause_then_restart_and_continue():
        nonlocal app, runtime, manager, runner
        try:
            try:
                await runner.run_async(
                    session_id=run.session_id,
                    user_input=run.user_input,
                    existing_run_id=run.run_id,
                )
            except AgentTurnWaitingForUser:
                pass
            else:
                current = manager.get_run(run.run_id)
                raise AssertionError(
                    f"ASK_USER must pause the graph; status={current.status.value}, "
                    f"error={current.error}, events={[event.type for event in manager.list_events(run.run_id)]}"
                )
            pending = manager.get_run(run.run_id)
            assert pending.status.value == "waiting_user"
            assert pending.metadata["pending_user_question"]["patch_id"] == "ask_region"
            saved_plan = Plan.model_validate(pending.metadata["multi_agent_plan"])
            assert saved_plan.status == PlanStatus.WAITING_USER
            assert saved_plan.patch_revision == 1
            assert manager.child_tree(run.run_id) == []

            # Simulate a crash after the answer transaction commits but before
            # LangGraph consumes it. A duplicate command after restart must
            # replay the same private journal answer and resume exactly once.
            manager.continue_user_question(
                run_id=run.run_id,
                command_id="answer_region_once",
                answer="Northern region",
            )
            runtime.stop()
            get_settings.cache_clear()
            app = create_app()
            runtime = app.state.runtime
            manager = runtime.agent_run_manager
            runner = runtime.agent_turn_runner
            assert isinstance(runner, AgentGraphRunner)
            install(runtime)
            request = SimpleNamespace(app=app)
            response = await continue_agent_run(
                run.run_id,
                ContinueAgentRunRequest(
                    command_id="answer_region_once", answer="Northern region"
                ),
                request,
            )
            assert response.replayed is True
            assert response.resume_scheduled is True
            task = runtime._agent_turn_tasks[run.run_id]
            try:
                await task
            except Exception as exc:
                state = runner.get_state(run.run_id)
                raise AssertionError(
                    f"user continuation task failed: {exc}; state={state.values}; "
                    f"status={manager.get_run(run.run_id).status.value}; "
                    f"events={[event.type for event in manager.list_events(run.run_id)]}"
                ) from exc

            completed = manager.get_run(run.run_id)
            assert completed.status.value == "completed", completed.error
            final_plan = Plan.model_validate(completed.metadata["multi_agent_plan"])
            assert final_plan.status == PlanStatus.COMPLETED
            assert final_plan.patch_revision == 1
            assert len(final_plan.patch_history) == 1
            assert sum(
                event.type == "multi_agent_user_question_answered"
                for event in manager.list_events(run.run_id)
            ) == 1
            answers = [
                item for item in input_seen_by_answer
                if item.get("action") == "user_answer"
            ]
            assert len(answers) == 1
            assert answers[0]["answer"] == "Northern region"
            assert completed.result_snapshot["answer"] == "Completed using the selected region."
        finally:
            runtime.stop()
            get_settings.cache_clear()

    try:
        asyncio.run(pause_then_restart_and_continue())
    finally:
        if app.state.runtime is not runtime:
            app.state.runtime.stop()


def test_nested_child_user_wait_does_not_release_ancestors_while_running(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        '[agent]\norchestrator = "langgraph"\ncheckpoint_backend = "sqlite"\n'
        "multi_agent_planning_enabled = true\n"
        '[safety]\ntool_review_mode = "skip"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    manager = runtime.agent_run_manager
    runner = runtime.agent_turn_runner
    assert isinstance(runner, AgentGraphRunner)
    root = runtime.create_agent_run(
        session_id="nested_user_root", user_input="Coordinate nested question."
    )
    manager.mark_running(root.run_id)
    coordinator = manager.create_child_run(
        parent_run_id=root.run_id,
        plan_id="outer_plan",
        step_id="nested_coordinator",
        attempt=1,
        user_input="Coordinate a nested task.",
    )
    manager.mark_child_running(coordinator.run_id)
    question_child = manager.create_child_run(
        parent_run_id=coordinator.run_id,
        plan_id="outer_plan",
        step_id="ask_region",
        attempt=1,
        user_input="Ask the user for a region.",
    )
    manager.mark_child_running(question_child.run_id)
    manager.mark_waiting_user(
        question_child.run_id,
        question_id="nested_question",
        patch_id="ask_region_patch",
        question="Which region?",
    )
    manager.mark_waiting_for_child_user(
        coordinator.run_id, (question_child.run_id,)
    )
    manager.mark_waiting_for_child_user(root.run_id, (coordinator.run_id,))
    resume_calls: list[str] = []

    async def resume(run_id: str):
        resume_calls.append(run_id)
        current = manager.get_run(run_id)
        if current is not None and current.parent_run_id is not None:
            manager.complete_child_run(run_id, result_snapshot={"answer": "done"})

    runner.resume_async = resume

    async def exercise_nested_wait():
        try:
            manager.continue_user_question(
                run_id=question_child.run_id,
                command_id="nested_answer_once",
                answer="Northern region",
            )
            # The child has accepted an answer but has not yet run to terminal.
            # Neither coordinator may pass its graph interrupt at this point.
            assert manager.get_run(question_child.run_id).status.value == "running"
            await runtime.resume_multi_agent_parent_async(coordinator.run_id)
            assert manager.get_run(coordinator.run_id).status.value == "waiting_user"
            assert manager.get_run(root.run_id).status.value == "waiting_user"
            assert resume_calls == []

            # Resuming the grandchild to terminal wakes its immediate parent;
            # completion then propagates one level and wakes the root.
            await runtime.resume_multi_agent_user_question_async(
                question_child.run_id, "nested_answer_once"
            )
            assert resume_calls == [
                question_child.run_id,
                coordinator.run_id,
                root.run_id,
            ]
            assert manager.get_run(coordinator.run_id).status.value == "completed"
            assert manager.get_run(root.run_id).status.value == "running"
            assert "waiting_child_user_run_ids" not in manager.get_run(root.run_id).metadata
        finally:
            runtime.stop()
            get_settings.cache_clear()

    try:
        asyncio.run(exercise_nested_wait())
    finally:
        if app.state.runtime is not runtime:
            app.state.runtime.stop()
