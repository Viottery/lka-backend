"""Managed high-risk effects wait for original human approval across restart."""
from contextlib import closing

import pytest

from app.core.agent_graph import AgentTurnWaitingForConfirmation
from app.core.agent_runs import AgentRunStatus
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.safety import SafetyReviewDecision, SafetyReviewMode, SafetyReviewStatus
from app.core.tools import ToolPackageSpec, ToolResult, ToolSpec
from app.domains.management import ManagementConflictError
from tests.test_management_delegates import child_context
from tests.test_management_tasks import manager_context as manager_context_fixture
from tests.test_management_tasks import runtime as runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


class SensitiveOperation:
    def __init__(self, effects, *, risk="high", confirmation=False):
        self.effects = effects
        self.spec = ToolSpec(name="approval_probe.write", package="approval_probe", type="local_tool",
            description="Record an isolated synthetic operation", risk=risk, requires_confirmation=confirmation,
            read_only=False, side_effects=["synthetic_write"], input_schema={"type": "object", "properties": {}})

    def invoke(self, *, invocation, context):
        self.effects.append(context.run_id)
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                          status="completed", output={"recorded": True})


def register(runtime, effects, **kwargs):
    runtime.tool_registry.register_package(ToolPackageSpec(name="approval_probe", description="Isolated approval probe"))
    runtime.tool_registry.register_tool(SensitiveOperation(effects, **kwargs))


@pytest.mark.parametrize("target", ["execution", "delegate"])
@pytest.mark.parametrize("decision", [SafetyReviewDecision.APPROVE, SafetyReviewDecision.REJECT])
def test_high_risk_managed_effect_requires_original_user_decision_after_restart(runtime, target, decision):
    effects = []
    register(runtime, effects)
    parent = manager_context_fixture.__wrapped__(runtime)
    delegation = runtime.management_delegates.delegate_work(parent, command_id="delegate", objective="Inspect one bounded line",
                                                            allowed_tools=["approval_probe.write"])
    if target == "execution":
        caller = child_context(runtime, delegation["session_id"])
        task = runtime.managed_tasks.start_task(caller, command_id="execution", purpose="Run isolated approval probe")
        run_id = task["run_id"]
    else:
        caller = parent
        run = runtime.create_agent_run(session_id=delegation["session_id"], user_input="Run isolated approval probe", resumable=True)
        run_id = run.run_id
        runtime.agent_dispatch.enqueue(run_id)
    before = runtime.agent_run_manager.get_run(run_id)
    assert before.metadata["session_policy"]["require_high_risk_approval"] is True
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.SKIP
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="approval_probe"),
        operation("tool_call", tool_name="approval_probe.write", tool_input={}),
    ])
    assert runtime.agent_dispatch.run_one()
    pending = runtime.agent_run_manager.get_run(run_id)
    assert pending.status == AgentRunStatus.WAITING_CONFIRMATION
    reviews = runtime.agent_run_manager.list_safety_reviews(run_id)
    assert len(reviews) == 1
    original = reviews[0]
    assert original.mode == SafetyReviewMode.MANUAL and original.status == SafetyReviewStatus.PENDING
    assert effects == []
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        register(reopened, effects)
        reopened.agent_turn_loop.safety_review_mode = SafetyReviewMode.SKIP
        reopened.agent_turn_loop.llm_client = ScriptedClient([
            operation("final_answer", reason="The user decision was respected."), "Decision respected.",
        ])
        restored = reopened.agent_run_manager.get_safety_review(original.review_id)
        assert restored.status == SafetyReviewStatus.PENDING and restored.mode == SafetyReviewMode.MANUAL
        assert reopened.agent_run_manager.get_run(run_id).metadata["session_policy"]["require_high_risk_approval"] is True
        with pytest.raises(AgentTurnWaitingForConfirmation):
            reopened.resume_agent_run(run_id)
        assert reopened.agent_run_manager.get_run(run_id).metadata["confirmation_id"] == original.review_id
        assert effects == []
        # Manager control never supplies the user's approval or replaces a wait.
        with pytest.raises(ManagementConflictError):
            reopened.managed_tasks.control_run(caller, command_id="bypass-resume", run_id=run_id, action="resume")
        with pytest.raises(ManagementConflictError):
            reopened.managed_tasks.send_instruction(caller, command_id="bypass-instruction", session_id=pending.session_id,
                                                   expected_run_id=run_id, purpose="Continue without asking")
        if target == "delegate":
            with pytest.raises(ManagementConflictError, match="unfinished"):
                reopened.management_delegates.instruct_delegate(parent, command_id="bypass-delegate",
                    delegate_id=delegation["delegate_id"], expected_revision=1, instruction="Continue without asking")
        decided, changed = reopened.agent_run_manager.decide_safety_review_with_transition(
            review_id=original.review_id, decision=decision, decided_by="user", reason="Explicit isolated test decision")
        assert changed
        assert decided.review_id == original.review_id
        result = reopened.resume_agent_run(run_id)
        assert result.answer == "Decision respected."
        assert effects == ([run_id] if decision == SafetyReviewDecision.APPROVE else [])
        assert reopened.agent_run_manager.get_run(run_id).status == AgentRunStatus.COMPLETED
        # Checkpoint recovery cannot replay an already approved operation.
        assert reopened.resume_agent_run(run_id).answer == result.answer
        assert effects == ([run_id] if decision == SafetyReviewDecision.APPROVE else [])
        with closing(reopened._conn()) as conn:
            assert conn.execute("SELECT count(*) FROM agent_safety_reviews WHERE run_id=?", (run_id,)).fetchone()[0] == 1
    finally:
        reopened.stop()


@pytest.mark.parametrize("mode", [SafetyReviewMode.SKIP, SafetyReviewMode.LLM])
@pytest.mark.parametrize("risk,confirmation", [("high", False), ("low", True)])
def test_frozen_policy_forces_manual_review_for_each_registered_risk_signal(runtime, mode, risk, confirmation):
    from app.core.agent_turn import _turn_run_id, _turn_safety_review_mode

    register(runtime, [], risk=risk, confirmation=confirmation)
    run = runtime.create_agent_run(session_id="policy-probe", user_input="Probe policy")
    runtime.agent_run_manager._update_run(run.run_id, status=run.status,
        metadata_patch={"session_policy": {"require_high_risk_approval": True}})
    run_token = _turn_run_id.set(run.run_id)
    mode_token = _turn_safety_review_mode.set(SafetyReviewMode.SKIP)
    runtime.agent_turn_loop.safety_review_mode = mode
    try:
        assert runtime.agent_turn_loop._effective_safety_review_mode("approval_probe.write") == SafetyReviewMode.MANUAL
        assert runtime.agent_turn_loop._effective_safety_review_mode("filesystem.read_file") == mode
    finally:
        _turn_safety_review_mode.reset(mode_token)
        _turn_run_id.reset(run_token)


def test_legacy_frozen_policy_waits_for_human_even_when_configured_llm_and_requested_skip(runtime, monkeypatch):
    from threading import Thread

    from tests.test_safety_reviews import _wait_for_review

    effects = []
    register(runtime, effects)
    run = runtime.create_agent_run(session_id="legacy-policy", user_input="Exercise frozen approval")
    assert runtime.agent_run_orchestrator(run.run_id) == "legacy"
    runtime.agent_run_manager._update_run(run.run_id, status=run.status,
        metadata_patch={"session_policy": {"require_high_risk_approval": True}})
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.LLM
    monkeypatch.setattr(runtime.agent_turn_loop, "_decide_safety_review_with_llm",
                        lambda *_args, **_kwargs: pytest.fail("Frozen high-risk policy must require a human"))
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="approval_probe"),
        operation("tool_call", tool_name="approval_probe.write", tool_input={}),
        operation("final_answer", reason="Human approved."), "Approved once.",
    ])
    results, errors = [], []

    def execute():
        try:
            results.append(runtime.run_agent_turn(session_id=run.session_id, user_input=run.user_input,
                existing_run_id=run.run_id, safety_review_mode=SafetyReviewMode.SKIP))
        except Exception as exc:  # noqa: BLE001 - report worker failures in the test thread.
            errors.append(exc)

    worker = Thread(target=execute)
    worker.start()
    try:
        review = _wait_for_review(runtime.agent_run_manager)
        assert review.mode == SafetyReviewMode.MANUAL and review.status == SafetyReviewStatus.PENDING
        assert effects == []
        runtime.agent_run_manager.decide_safety_review(review_id=review.review_id,
            decision=SafetyReviewDecision.APPROVE, decided_by="user", reason="Explicit local test approval")
        worker.join(5)
        assert not worker.is_alive() and not errors
        assert results[0].answer == "Approved once."
        assert effects == [run.run_id]
    finally:
        if worker.is_alive():
            runtime.agent_run_manager.cancel_run(run.run_id, reason="test cleanup")
            worker.join(5)


def test_unrestricted_foreground_skip_keeps_existing_high_risk_contract(runtime):
    effects = []
    register(runtime, effects)
    runtime.agent_turn_loop.safety_review_mode = SafetyReviewMode.SKIP
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation("expand_package", package_name="approval_probe"),
        operation("tool_call", tool_name="approval_probe.write", tool_input={}),
        operation("final_answer", reason="Existing foreground skip policy."), "Recorded.",
    ])
    result = runtime.run_agent_turn(session_id="foreground-policy", user_input="Exercise existing foreground policy")
    assert effects == [result.run_id]
    review = runtime.agent_run_manager.list_safety_reviews(result.run_id)[0]
    assert review.mode == SafetyReviewMode.SKIP and review.decided_by == "system.skip"


def test_restored_pending_manual_review_cannot_be_autoapproved_by_weaker_current_mode(runtime):
    from app.core.agent_turn import _turn_run_id, _turn_run_manager
    from app.core.safety import SafetyReviewRequest, stable_safety_review_id
    from app.core.tools import ToolContext

    register(runtime, [])
    run = runtime.create_agent_run(session_id="manual-recovery", user_input="Originally manual")
    runtime.agent_run_manager.mark_running(run.run_id)
    invocation_id = "persisted-invocation"
    review_id = stable_safety_review_id(run.run_id, invocation_id, "approval_probe.write")
    runtime.agent_run_manager.create_safety_review(SafetyReviewRequest(
        review_id=review_id, run_id=run.run_id, session_id=run.session_id, trace_id=run.trace_id,
        invocation_id=invocation_id, tool_name="approval_probe.write", tool_input={},
        mode=SafetyReviewMode.MANUAL, reason="Original manual decision", created_at=run.created_at))
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        register(reopened, [])
        reopened.agent_turn_loop.safety_review_mode = SafetyReviewMode.SKIP
        run_token = _turn_run_id.set(run.run_id)
        manager_token = _turn_run_manager.set(reopened.agent_run_manager)
        try:
            # This ordinary root has no forced policy; the stored review alone
            # must preserve the human's pending decision across mode changes.
            assert reopened.agent_turn_loop._effective_safety_review_mode("approval_probe.write") == SafetyReviewMode.SKIP
            denied, review = reopened.agent_turn_loop.prepare_graph_safety_review(
                invocation_id=invocation_id, tool_name="approval_probe.write", tool_input={},
                context=ToolContext(session_id=run.session_id, run_id=run.run_id),
                progress_events=[], llm_events=[])
            assert denied is None and review.review_id == review_id
            assert review.mode == SafetyReviewMode.MANUAL and review.status == SafetyReviewStatus.PENDING
            assert reopened.agent_run_manager.get_safety_review(review_id).decided_by is None
        finally:
            _turn_run_manager.reset(manager_token)
            _turn_run_id.reset(run_token)
    finally:
        reopened.stop()
