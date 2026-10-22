"""Persistent work-line subagents across actual harness turns and reconstruction."""
import asyncio
from contextlib import closing

import pytest

from app.core.agent_runs import AgentRunStatus
from app.core.llm_workloads import LLMWorkloadController, workload_scope
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.safety import SafetyReviewMode
from app.core.sessions import SessionRunPolicy
from app.core.tools import ToolContext
from app.domains.management import ManagementConflictError
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_coordinator import finalize
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def script(runtime, operations, answer):
    runtime.agent_turn_loop.llm_client = ScriptedClient([
        operation('expand_package', package_name='management'),
        *[operation('tool_call', tool_name=name, tool_input=inputs) for name, inputs in operations],
        operation('final_answer', reason='This turn is complete'), answer,
    ])


def bind_all(runtime):
    runtime.management_coordinator.tick()
    while runtime.management_coordinator.run_one():
        pass


def queued(runtime, sid):
    with closing(runtime._conn()) as conn:
        row = conn.execute("SELECT run_id FROM agent_runs WHERE session_id=? AND status='queued'", (sid,)).fetchone()
    return runtime.agent_run_manager.get_run(row[0])


def dispatch(runtime):
    assert runtime.agent_dispatch.run_one()


def test_two_delegates_manage_four_tasks_then_wake_parent_after_reopen(runtime):
    assert not runtime.local_app_config.management.autonomous_enabled
    parent = runtime.management_service.create()['session']['session_id']
    runtime.management_service.update(parent, expected_revision=1, status='active')
    script(runtime, [('management.delegate_work', {'command_id': name, 'title': name,
        'objective': 'Read two independent results for ' + name, 'allowed_tools': []}) for name in ('venue', 'equipment')],
        'Two work lines delegated.')
    result = runtime.run_agent_turn(session_id=parent, user_input='Manage two work lines.',
                                    safety_review_mode=SafetyReviewMode.SKIP)
    assert runtime.agent_run_manager.get_run(result.run_id).status.value == 'completed'
    with closing(runtime._conn()) as conn:
        children = [dict(r) for r in conn.execute('SELECT * FROM manager_delegates ORDER BY created_at')]
        assert len(children) == 2
        assert conn.execute("SELECT count(*) FROM agent_sessions WHERE kind='ordinary'").fetchone()[0] == 0
    runtime.stop()
    reopened = LocalKnowledgeAgentRuntime(runtime.settings)
    try:
        reopened.local_app_config.safety.tool_review_mode = 'skip'
        bind_all(reopened)
        for child in sorted(children, key=lambda c: (queued(reopened, c['session_id']).created_at,
                                                     queued(reopened, c['session_id']).run_id)):
            run = queued(reopened, child['session_id'])
            assert run.parent_run_id is None and run.metadata['manager_explicit_delegation']
            script(reopened, [('management.start_task', {'command_id': key, 'purpose': key}) for key in ('first', 'second')],
                   'Waiting for my task events.')
            dispatch(reopened)
            assert reopened.agent_run_manager.get_run(run.run_id).status.value == 'completed'
        # Execute all four ordinary roots before another coordinator tick.
        for index in range(4):
            reopened.agent_turn_loop.llm_client = ScriptedClient([
                operation('final_answer', reason='Bounded evidence'), 'Evidence ' + str(index)])
            dispatch(reopened)
        bind_all(reopened)
        for child in sorted(children, key=lambda c: (queued(reopened, c['session_id']).created_at,
                                                     queued(reopened, c['session_id']).run_id)):
            context_run = queued(reopened, child['session_id'])
            with closing(reopened._conn()) as conn:
                tasks = [dict(r) for r in conn.execute('SELECT task_session_id,run_id FROM managed_session_links '
                                                      'WHERE manager_session_id=?', (child['session_id'],))]
            script(reopened,
                [('management.read_session', {'session_id': t['task_session_id']}) for t in tasks] +
                [('management.report_delegate', {'command_id': 'finish', 'kind': 'completed',
                    'summary': 'Both task results verified.',
                    'evidence': [{'session_id': t['task_session_id'], 'run_id': t['run_id']} for t in tasks]})],
                'Verified and reported to parent.')
            dispatch(reopened)
            assert reopened.agent_run_manager.get_run(context_run.run_id).status.value == 'completed'
        bind_all(reopened)
        parent_run = queued(reopened, parent)
        script(reopened, [('management.read_delegate', {'delegate_id': c['delegate_id']}) for c in children] +
               [('management.decide_notification', {'command_id': 'notify-final', 'notify_user': True,
                                                     'reason': 'Both requested work lines are verified.'})],
               'Both independent lines verified.')
        dispatch(reopened)
        assert reopened.agent_run_manager.get_run(parent_run.run_id).status.value == 'completed'
        assert parent_run.run_id != result.run_id
        assert len(reopened.management_goals.list_deliveries(parent)['deliveries']) == 1
        assert reopened.management_coordinator.tick()['scheduled'] == 0
        with closing(reopened._conn()) as conn:
            assert conn.execute('SELECT count(*) FROM manager_delegate_reports').fetchone()[0] == 2
            assert conn.execute("SELECT count(*) FROM manager_delegates WHERE state='completed'").fetchone()[0] == 2
    finally:
        reopened.stop()


def test_scoped_ownership_workspace_replacement_and_parent_pause(runtime, tmp_path):
    context = context_fixture.__wrapped__(runtime)
    workspace = tmp_path / 'delegated'
    workspace.mkdir()
    receipt = runtime.management_delegates.delegate_work(context, command_id='d', objective='Read a scoped file',
        workspace_path=str(workspace), allowed_tools=['filesystem.read_file'])
    bind_all(runtime)
    run = queued(runtime, receipt['session_id'])
    runtime.agent_run_manager.mark_running(run.run_id)
    child = ToolContext(session_id=run.session_id, run_id=run.run_id, safety_review_approved=True)
    task = runtime.managed_tasks.start_task(child, command_id='own', purpose='Read scoped file')
    other = runtime.session_service.create_session().session.session_id
    with pytest.raises(ManagementConflictError, match='own execution'):
        runtime.managed_tasks.read_session(child, session_id=other)
    assert [s['session_id'] for s in runtime.managed_tasks.list_sessions(child)['sessions']] == [task['session_id']]
    relocated = runtime.managed_tasks.start_task(
        child, command_id='other-directory', purpose='Read other workspace', workspace_path=str(tmp_path),
    )
    relocated_run = runtime.agent_run_manager.get_run(relocated['run_id'])
    assert relocated_run.metadata['execution_workspace']['backend_path'] == str(tmp_path)
    assert relocated_run.metadata['session_policy']['allowed_tools'] == ['filesystem.read_file']
    task_run = runtime.agent_run_manager.get_run(task['run_id'])
    policy = task_run.metadata['session_policy']
    assert policy['allowed_tools'] == ['filesystem.read_file']
    assert policy['allowed_paths'] is None
    assert not policy['allow_fork']
    view = runtime.agent_turn_loop._tool_view_for_run(task_run.run_id)
    assert view.full_workspace_authority and view.allowed_paths == ()
    runtime.agent_run_manager._update_run(task_run.run_id, status=AgentRunStatus.PAUSED)
    replaced = runtime.replace_agent_run(task_run.run_id, user_input='Same scope', command_id='r', request_fingerprint='r')
    assert SessionRunPolicy.model_validate(replaced.metadata['session_policy']) == SessionRunPolicy.model_validate(policy)
    runtime.management_service.update(context.session_id, expected_revision=2, status='paused')
    with pytest.raises(ManagementConflictError, match='active'):
        runtime.management_coordinator.check_run(replaced.run_id)


def test_report_publication_failure_rolls_back_parent_event(runtime, monkeypatch):
    context = context_fixture.__wrapped__(runtime)
    receipt = runtime.management_delegates.delegate_work(context, command_id='d', objective='Inspect')
    bind_all(runtime)
    run = queued(runtime, receipt['session_id'])
    runtime.agent_run_manager.mark_running(run.run_id)
    child = ToolContext(session_id=run.session_id, run_id=run.run_id, safety_review_approved=True)
    runtime.management_delegates.report_delegate(child, command_id='done', kind='completed', summary='Verified.')
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic publication failure')
    monkeypatch.setattr(runtime.session_service, 'append_message_in_transaction', fail)
    with pytest.raises(RuntimeError, match='publication failure'):
        finalize(runtime, run)
    with closing(runtime._conn()) as conn:
        assert conn.execute('SELECT count(*) FROM manager_delegate_reports').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM manager_inbox WHERE session_id=?', (context.session_id,)).fetchone()[0] == 0
        assert conn.execute('SELECT state FROM manager_delegates').fetchone()[0] == 'active'


def test_background_agents_overlap_without_io_pool_cap_and_keep_shared_control(tmp_path):
    controller = LLMWorkloadController(tmp_path / 'usage.sqlite3', max_concurrency=3, interactive_reserved=1, io_concurrency=1)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def call(index):
            nonlocal count
            with workload_scope('background_agent', task_id=str(index)):
                async with controller.admit(input_tokens=1, output_tokens=1) as ticket:
                    ticket.dispatched = True
                    count += 1
                    if count == 2:
                        entered.set()
                    await release.wait()
        tasks = [asyncio.create_task(call(i)) for i in range(2)]
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert controller.health()['active_by_pool']['background_agent'] == 2
        # An ordinary foreground call still has its existing reserved capacity.
        async with controller.admit(input_tokens=1, output_tokens=1):
            pass
        release.set()
        await asyncio.gather(*tasks)
    asyncio.run(scenario())


def test_key_decision_report_wakes_muted_parent_without_ordinary_autonomy(runtime):
    context = context_fixture.__wrapped__(runtime)
    receipt = runtime.management_delegates.delegate_work(context, command_id='d', objective='Check approval')
    runtime.agent_run_manager.complete_run(context.run_id)
    runtime.management_delivery.configure(context.session_id, 0, muted=True, allow_decisions=True)
    bind_all(runtime)
    child_run = queued(runtime, receipt['session_id'])
    runtime.agent_run_manager.mark_running(child_run.run_id)
    child = ToolContext(session_id=child_run.session_id, run_id=child_run.run_id, safety_review_approved=True)
    runtime.management_delegates.report_delegate(child, command_id='decision', kind='needs_decision',
                                                  summary='Current approval is missing; ask the user.')
    assert finalize(runtime, child_run)
    bind_all(runtime)
    parent_run = queued(runtime, context.session_id)
    runtime.agent_run_manager.mark_running(parent_run.run_id)
    assert finalize(runtime, parent_run)
    assert len(runtime.management_goals.list_deliveries(context.session_id)['deliveries']) == 1
    assert runtime.management_coordinator.tick()['scheduled'] == 0


def test_waiting_child_question_uses_parent_delivery_exception_and_stays_unanswered(runtime):
    context = context_fixture.__wrapped__(runtime)
    receipt = runtime.management_delegates.delegate_work(context, command_id='d', objective='Resolve choice')
    runtime.agent_run_manager.complete_run(context.run_id)
    runtime.management_delivery.configure(context.session_id, 0, muted=True, allow_decisions=True)
    bind_all(runtime)
    child_run = queued(runtime, receipt['session_id'])
    runtime.agent_run_manager._update_run(child_run.run_id, status=AgentRunStatus.WAITING_USER,
        metadata_patch={'pending_user_question': {'question_id': 'decision-1', 'question': 'Which revision is approved?'}})
    bind_all(runtime)
    parent_run = queued(runtime, context.session_id)
    runtime.agent_run_manager.mark_running(parent_run.run_id)
    assert finalize(runtime, parent_run)
    assert runtime.agent_run_manager.get_run(child_run.run_id).status.value == 'waiting_user'
    assert len(runtime.management_goals.list_deliveries(context.session_id)['deliveries']) == 1
    assert runtime.management_coordinator.tick()['scheduled'] == 0
