"""Offline durable delegation and publication contracts."""
import json
from contextlib import closing

import pytest

from app.core.agent_runs import AgentRunStatus
from app.core.tools import ToolContext, ToolInvocation
from app.domains.management import ManagementConflictError
from app.domains.management_delegates import ManagementDelegateService
from app.tool_packages.management_delegates import management_delegate_tools
from tests.test_management_tasks import manager_context as manager_context_fixture
from tests.test_management_tasks import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def manager_context(runtime):
    return manager_context_fixture.__wrapped__(runtime)


@pytest.fixture
def delegates(runtime):
    service = ManagementDelegateService(runtime._conn, tasks=runtime.managed_tasks,
        management=runtime.management_service, goals=runtime.management_goals, registry=runtime.tool_registry)
    service.ensure_schema()
    return service


def child_context(runtime, sid):
    run = runtime.create_agent_run(session_id=sid, user_input='Bound delegate execution', resumable=True)
    runtime.agent_run_manager.mark_running(run.run_id)
    run = runtime.agent_run_manager._update_run(run.run_id, status=AgentRunStatus.RUNNING, metadata_patch={'manager_execution_id': 'execution-child', 'manager_explicit_delegation': True})
    with closing(runtime._conn()) as conn, conn:
        conn.execute("INSERT INTO manager_executions VALUES(?,?,?,'bound',1,'[]','{}','now','now')",
                     ('execution-child', sid, run.run_id))
    return ToolContext(session_id=sid, run_id=run.run_id, trace_id=run.trace_id, safety_review_approved=True)


def test_creation_is_durable_bounded_independent_and_idempotent(runtime, manager_context, delegates):
    caller = manager_context.model_copy(update={"llm_reasoning_level": "high", "llm_reasoning_source": "request"})
    receipt = delegates.delegate_work(caller, command_id='one', objective='Inspect state')
    assert delegates.delegate_work(caller, command_id='one', objective='Inspect state') == receipt
    with closing(runtime._conn()) as conn:
        item = delegates.get_for_session(conn, receipt['session_id'])
        assert conn.execute("SELECT reasoning_level FROM manager_controllers WHERE session_id=?",
                            (receipt['session_id'],)).fetchone()[0] == "high"
        assert item['parent_session_id'] == manager_context.session_id
        assert all(runtime.tool_registry.get_tool(name).spec.read_only for name in item['allowed_tools'])
        assert not conn.execute('SELECT 1 FROM agent_runs WHERE session_id=?', (receipt['session_id'],)).fetchone()
        assert not conn.execute('SELECT 1 FROM managed_session_links WHERE task_session_id=?', (receipt['session_id'],)).fetchone()
        event = dict(conn.execute('SELECT * FROM manager_inbox WHERE session_id=?', (receipt['session_id'],)).fetchone())
        assert delegates.validate_event(conn, event)
    assert receipt['delegate_id'] in str(delegates.list_delegates(manager_context))
    assert 'kickoff_envelope' not in delegates.read_delegate(manager_context, delegate_id=receipt['delegate_id'])
    with pytest.raises(ManagementConflictError):
        delegates.delegate_work(manager_context, command_id='one', objective='Different')


def test_reports_publish_atomically_and_replay_once(runtime, manager_context, delegates):
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    context = child_context(runtime, receipt['session_id'])
    with pytest.raises(ManagementConflictError, match='nested'):
        delegates.delegate_work(context, command_id='nested', objective='Inspect')
    report = delegates.report_delegate(context, command_id='report', kind='completed', summary='Done')
    assert report['status'] == 'proposed'
    assert delegates.report_delegate(context, command_id='report', kind='completed', summary='Done') == report
    with closing(runtime._conn()) as conn, conn:
        assert conn.execute('SELECT count(*) FROM manager_delegate_reports').fetchone()[0] == 0
        delegates.apply_reports(conn, 'execution-child', context.session_id)
        delegates.apply_reports(conn, 'execution-child', context.session_id)
        assert conn.execute('SELECT count(*) FROM manager_delegate_reports').fetchone()[0] == 1
        assert delegates.get_for_session(conn, context.session_id)['state'] == 'completed'
        event = dict(conn.execute('SELECT * FROM manager_inbox WHERE session_id=?', (manager_context.session_id,)).fetchone())
        assert delegates.validate_event(conn, event)
        with pytest.raises(ManagementConflictError):
            delegates.check_session(conn, context.session_id)


def test_report_requires_child_execution_and_owned_evidence(runtime, manager_context, delegates):
    with pytest.raises(ManagementConflictError, match='Only a delegate'):
        delegates.report_delegate(manager_context, command_id='r', kind='progress', summary='Ready')
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    context = child_context(runtime, receipt['session_id'])
    with pytest.raises(ManagementConflictError, match='ordinary task'):
        delegates.report_delegate(context, command_id='r', kind='progress', summary='Ready',
                                 evidence=[{'session_id': manager_context.session_id}])
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE manager_controllers SET status='paused' WHERE session_id=?", (manager_context.session_id,))
        with pytest.raises(ManagementConflictError, match='active'):
            delegates.check_session(conn, context.session_id)


def test_delegate_tools_keep_review_gate(manager_context, delegates):
    tools = {tool.spec.name: tool for tool in management_delegate_tools(delegates)}
    assert tools['management.read_delegate'].spec.read_only
    tool = tools['management.delegate_work']
    assert tool.spec.read_only is False
    context = manager_context.model_copy(update={'safety_review_approved': False})
    with pytest.raises(ValueError, match='safety review'):
        tool.invoke(invocation=ToolInvocation(invocation_id='invoke', tool=tool.spec, session_id=context.session_id, context_id='ctx', input={'command_id': 'x', 'objective': 'Read'}), context=context)


def test_execution_failure_and_missing_report_are_durable_events(runtime, manager_context, delegates):
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    context = child_context(runtime, receipt['session_id'])
    runtime.agent_run_manager._update_run(context.run_id, status=AgentRunStatus.FAILED)
    assert delegates.capture_events() == 1
    assert delegates.capture_events() == 0
    with closing(runtime._conn()) as conn:
        event = dict(conn.execute('SELECT * FROM manager_inbox WHERE session_id=?', (manager_context.session_id,)).fetchone())
        assert delegates.validate_event(conn, event)
        assert 'failed' in event['payload']
    receipt2 = delegates.delegate_work(manager_context, command_id='two', objective='Inspect another state')
    run = runtime.create_agent_run(session_id=receipt2['session_id'], user_input='Work', resumable=True)
    runtime.agent_run_manager._update_run(run.run_id, status=AgentRunStatus.COMPLETED)
    assert delegates.capture_events() == 1
    with closing(runtime._conn()) as conn:
        event = dict(conn.execute('SELECT * FROM manager_inbox WHERE session_id=? ORDER BY event_id DESC LIMIT 1',
                                 (manager_context.session_id,)).fetchone())
        assert delegates.validate_event(conn, event)
        assert 'missing_report' in event['payload']


def test_parent_instruction_has_cas_stable_receipt_and_current_event(runtime, manager_context, delegates):
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    args = {'command_id': 'follow', 'delegate_id': receipt['delegate_id'], 'expected_revision': 1,
            'instruction': 'Focus on current evidence'}
    revised = delegates.instruct_delegate(manager_context, **args)
    assert revised['revision'] == 2
    assert delegates.instruct_delegate(manager_context, **args) == revised
    with closing(runtime._conn()) as conn:
        event = dict(conn.execute('SELECT * FROM manager_inbox WHERE session_id=? ORDER BY event_id DESC LIMIT 1',
                                 (receipt['session_id'],)).fetchone())
        assert delegates.validate_event(conn, event)
    with pytest.raises(ManagementConflictError, match='revision'):
        delegates.instruct_delegate(manager_context, **{**args, 'command_id': 'stale'})


def test_parent_instruction_rejects_unfinished_child_root(runtime, manager_context, delegates):
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    runtime.create_agent_run(session_id=receipt['session_id'], user_input='Unfinished', resumable=True)
    with pytest.raises(ManagementConflictError, match='unfinished'):
        delegates.instruct_delegate(manager_context, command_id='follow', delegate_id=receipt['delegate_id'],
                                    expected_revision=1, instruction='Continue')


def test_completion_rejects_unfinished_owned_task_and_report_rollback(runtime, manager_context, delegates):
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    context = child_context(runtime, receipt['session_id'])
    task = runtime.managed_tasks.start_task(context, command_id='task', purpose='Inspect one source')
    with pytest.raises(ManagementConflictError, match='unfinished ordinary'):
        delegates.report_delegate(context, command_id='done', kind='completed', summary='Finished')
    delegates.report_delegate(context, command_id='decision', kind='needs_decision', summary='Need user input',
                              evidence=[{'session_id': task['session_id'], 'run_id': task['run_id']}])
    with pytest.raises(RuntimeError, match='publication failed'), closing(runtime._conn()) as conn, conn:
        delegates.apply_reports(conn, 'execution-child', context.session_id)
        raise RuntimeError('publication failed')
    with closing(runtime._conn()) as conn:
        assert conn.execute('SELECT count(*) FROM manager_delegate_reports').fetchone()[0] == 0
        assert not conn.execute('SELECT 1 FROM manager_inbox WHERE session_id=?', (manager_context.session_id,)).fetchone()
        policy = delegates.policy_for_session(conn, receipt['session_id'])
        assert policy['allow_default_workspace'] is False and policy['allowed_paths'] == []
        assert 'management.delegate_work' not in policy['allowed_tools']


def test_cancel_delegate_fences_and_cancels_child_and_owned_roots(runtime, manager_context, delegates):
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    child = child_context(runtime, receipt['session_id'])
    task = runtime.managed_tasks.start_task(child, command_id='task', purpose='Inspect one source')
    args = {'command_id': 'cancel', 'delegate_id': receipt['delegate_id'], 'expected_revision': 1, 'action': 'cancel'}
    controlled = delegates.control_delegate(manager_context, **args)
    assert controlled['state'] == 'cancelled' and controlled['revision'] == 2
    assert {root['run_id'] for root in controlled['affected_roots']} == {child.run_id, task['run_id']}
    assert delegates.control_delegate(manager_context, **args) == controlled
    for run_id in (child.run_id, task['run_id']):
        assert runtime.agent_run_manager.get_run(run_id).status == AgentRunStatus.CANCELLED
    with closing(runtime._conn()) as conn, pytest.raises(ManagementConflictError, match='active'):
        delegates.check_session(conn, child.session_id)
    with pytest.raises(ManagementConflictError, match='unavailable'):
        delegates.instruct_delegate(manager_context, command_id='revive', delegate_id=receipt['delegate_id'],
                                    expected_revision=2, instruction='Continue')


def test_pause_cancellation_replays_after_commit_and_can_reactivate(runtime, manager_context, delegates, monkeypatch):
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    child = runtime.create_agent_run(session_id=receipt['session_id'], user_input='Queued child', resumable=True)
    args = {'command_id': 'pause', 'delegate_id': receipt['delegate_id'], 'expected_revision': 1, 'action': 'pause'}
    original = runtime.agent_run_manager.request_cancel

    def fail(*args, **kwargs):
        raise RuntimeError('signaling interrupted')

    monkeypatch.setattr(runtime.agent_run_manager, 'request_cancel', fail)
    with pytest.raises(RuntimeError, match='signaling interrupted'):
        delegates.control_delegate(manager_context, **args)
    observed = delegates.read_delegate(manager_context, delegate_id=receipt['delegate_id'])
    assert observed['revision'] == 2 and observed['controller_status'] == 'paused'
    monkeypatch.setattr(runtime.agent_run_manager, 'request_cancel', original)
    controlled = delegates.control_delegate(manager_context, **args)
    assert controlled['state'] == 'active' and controlled['revision'] == 2
    assert runtime.agent_run_manager.get_run(child.run_id).status == AgentRunStatus.CANCELLED
    resumed = delegates.instruct_delegate(manager_context, command_id='resume', delegate_id=receipt['delegate_id'],
                                         expected_revision=2, instruction='Continue bounded investigation')
    assert resumed['revision'] == 3
    assert delegates.read_delegate(manager_context, delegate_id=receipt['delegate_id'])['controller_status'] == 'active'


def test_control_delegate_rejects_unowned_and_stale_commands(runtime, manager_context, delegates):
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    with pytest.raises(ManagementConflictError, match='revision'):
        delegates.control_delegate(manager_context, command_id='pause', delegate_id=receipt['delegate_id'],
                                   expected_revision=2, action='pause')
    with pytest.raises(ManagementConflictError, match='not owned'):
        delegates.control_delegate(manager_context, command_id='missing', delegate_id='delegate-unknown',
                                   expected_revision=1, action='cancel')


@pytest.mark.parametrize('action', ['instruct', 'cancel'])
def test_bound_parent_can_consume_only_its_own_lifecycle_mutation(runtime, manager_context, delegates, action):
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    run = runtime.create_agent_run(session_id=receipt['session_id'], user_input='Inspect', resumable=True)
    runtime.agent_run_manager._update_run(run.run_id, status=AgentRunStatus.COMPLETED)
    assert delegates.capture_events() == 1
    with closing(runtime._conn()) as conn:
        event = dict(conn.execute('SELECT * FROM manager_inbox WHERE session_id=?', (manager_context.session_id,)).fetchone())
        assert delegates.validate_event(conn, event)
    if action == 'instruct':
        delegates.instruct_delegate(manager_context, command_id='follow', delegate_id=receipt['delegate_id'],
                                    expected_revision=1, instruction='Continue')
    else:
        delegates.control_delegate(manager_context, command_id='cancel', delegate_id=receipt['delegate_id'],
                                   expected_revision=1, action='cancel')
    with closing(runtime._conn()) as conn, conn:
        assert not delegates.validate_event(conn, event)
        assert delegates.validate_event(conn, event, consuming_run_id=manager_context.run_id)
        assert not delegates.validate_event(conn, event, consuming_run_id='another-parent-run')
        conn.execute('UPDATE manager_delegates SET revision=revision+1 WHERE delegate_id=?', (receipt['delegate_id'],))
        assert not delegates.validate_event(conn, event, consuming_run_id=manager_context.run_id)


def test_parent_instruction_preserves_existing_inherited_allowance(runtime, manager_context, delegates):
    runtime.agent_run_manager._update_run(manager_context.run_id, status=AgentRunStatus.RUNNING,
        metadata_patch={'work_budget_scopes': [{'scope_id': 'bounded-parent', 'max_tokens': 100}]})
    receipt = delegates.delegate_work(manager_context, command_id='one', objective='Inspect state')
    runtime.agent_run_manager._update_run(manager_context.run_id, status=AgentRunStatus.RUNNING,
        metadata_patch={'work_budget_scopes': [{'scope_id': 'bounded-parent', 'max_tokens': 200}]})
    delegates.instruct_delegate(manager_context, command_id='follow', delegate_id=receipt['delegate_id'],
                                expected_revision=1, instruction='Continue')
    with closing(runtime._conn()) as conn:
        delegate = delegates.get_for_session(conn, receipt['session_id'])
        inherited = delegate['kickoff_envelope']['debug_info']['inherited_work_metadata']
        assert inherited['work_budget_scopes'] == [{'scope_id': 'bounded-parent', 'max_tokens': 100}]


@pytest.mark.parametrize('kind', ['completed', 'progress', 'blocked', 'needs_decision'])
@pytest.mark.parametrize('evidence', [None, []])
def test_task_based_reports_require_explicit_evidence(runtime, manager_context, delegates, kind, evidence):
    receipt = delegates.delegate_work(manager_context, command_id='line', objective='Inspect state')
    context = child_context(runtime, receipt['session_id'])
    task = runtime.managed_tasks.start_task(context, command_id='task', purpose='Inspect source')
    runtime.agent_run_manager._update_run(task['run_id'], status=AgentRunStatus.COMPLETED)
    with pytest.raises(ManagementConflictError, match='read_task_result'):
        delegates.report_delegate(context, command_id='report', kind=kind,
                                  summary='Verified result from ' + task['run_id'], evidence=evidence)
    with closing(runtime._conn()) as conn:
        assert not conn.execute('SELECT 1 FROM manager_delegate_report_proposals').fetchone()
        assert not conn.execute("SELECT 1 FROM management_commands WHERE action='report_delegate'").fetchone()
    # Rejected proposals do not consume command IDs; repair with the real structured reference.
    ref = {'session_id': task['session_id'], 'run_id': task['run_id']}
    assert delegates.report_delegate(context, command_id='report', kind=kind,
                                     summary='Verified result from ' + task['run_id'], evidence=[ref])['status'] == 'proposed'


@pytest.mark.parametrize('kind', ['progress', 'completed', 'blocked', 'needs_decision'])
def test_reports_without_execution_tasks_keep_optional_evidence(runtime, manager_context, delegates, kind):
    receipt = delegates.delegate_work(manager_context, command_id='line', objective='Inspect state')
    context = child_context(runtime, receipt['session_id'])
    assert delegates.report_delegate(context, command_id='report', kind=kind,
                                     summary='Independent work or no-execution blocker')['status'] == 'proposed'


def test_blocker_accepts_owned_pending_task_and_session_only_refs(runtime, manager_context, delegates):
    receipt = delegates.delegate_work(manager_context, command_id='line', objective='Inspect state')
    context = child_context(runtime, receipt['session_id'])
    task = runtime.managed_tasks.start_task(context, command_id='task', purpose='Inspect source')
    for index, ref in enumerate([{'session_id': task['session_id']}, {'run_id': task['run_id']}]):
        assert delegates.report_delegate(context, command_id='report' + str(index), kind='blocked',
                                         summary='Awaiting execution', evidence=[ref])['status'] == 'proposed'


def test_deleted_tasks_do_not_require_unavailable_evidence(runtime, manager_context, delegates):
    receipt = delegates.delegate_work(manager_context, command_id='line', objective='Inspect state')
    context = child_context(runtime, receipt['session_id'])
    task = runtime.managed_tasks.start_task(context, command_id='task', purpose='Inspect source')
    runtime.agent_run_manager._update_run(task['run_id'], status=AgentRunStatus.CANCELLED)
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (task['session_id'],))
    with pytest.raises(ManagementConflictError, match='ordinary task'):
        delegates.report_delegate(context, command_id='obsolete', kind='blocked', summary='Source unavailable',
                                  evidence=[{'run_id': task['run_id']}])
    args = {'command_id': 'report', 'kind': 'blocked', 'summary': 'All execution sources are unavailable'}
    report = delegates.report_delegate(context, **args)
    assert report['status'] == 'proposed'
    assert delegates.report_delegate(context, **args) == report
    with closing(runtime._conn()) as conn, conn:
        delegates.apply_reports(conn, 'execution-child', context.session_id)
        assert conn.execute('SELECT count(*) FROM manager_delegate_reports').fetchone()[0] == 1


def test_report_rejects_foreign_task_and_mismatched_message_provenance(runtime, manager_context, delegates):
    foreign = runtime.managed_tasks.start_task(manager_context, command_id='foreign', purpose='Other work')
    receipt = delegates.delegate_work(manager_context, command_id='line', objective='Inspect state')
    context = child_context(runtime, receipt['session_id'])
    task = runtime.managed_tasks.start_task(context, command_id='task', purpose='Inspect source')
    with pytest.raises(ManagementConflictError, match='ordinary task'):
        delegates.report_delegate(context, command_id='foreign', kind='progress', summary='Result',
                                  evidence=[{'run_id': foreign['run_id']}])
    message = runtime.session_service.append_message(session_id=task['session_id'], role='agent',
                                                     content='Result', payload={'run_id': 'another-run'})
    with pytest.raises(ManagementConflictError, match='provenance'):
        delegates.report_delegate(context, command_id='mismatch', kind='progress', summary='Result',
                                  evidence=[{'run_id': task['run_id'], 'message_id': message.message_id}])
    valid_message = runtime.session_service.append_message(session_id=task['session_id'], role='agent',
                                                           content='Result', payload={'run_id': task['run_id']})
    assert delegates.report_delegate(context, command_id='valid', kind='progress', summary='Result',
        evidence=[{'run_id': task['run_id'], 'message_id': valid_message.message_id}])['status'] == 'proposed'


def test_publication_revalidates_task_evidence_requirement(runtime, manager_context, delegates):
    receipt = delegates.delegate_work(manager_context, command_id='line', objective='Inspect state')
    context = child_context(runtime, receipt['session_id'])
    delegates.report_delegate(context, command_id='report', kind='progress', summary='Before execution')
    runtime.managed_tasks.start_task(context, command_id='task', purpose='New execution')
    with closing(runtime._conn()) as conn, conn, pytest.raises(ManagementConflictError, match='nonempty evidence'):
        delegates.apply_reports(conn, 'execution-child', context.session_id)
    with closing(runtime._conn()) as conn:
        assert not conn.execute('SELECT 1 FROM manager_delegate_reports').fetchone()


def test_full_delegate_requirements_and_source_are_readable_through_pagination(runtime, manager_context, delegates):
    objective = 'Required condition. ' * 700 + 'FINAL REQUIRED CONDITION'
    receipt = delegates.delegate_work(manager_context, command_id='line', objective=objective)
    context = child_context(runtime, receipt['session_id'])
    with closing(runtime._conn()) as conn, conn:
        envelope = delegates.get_for_session(conn, receipt['session_id'])['kickoff_envelope']
        envelope['original_user_quotes'] = ['Source requirement. ' * 300 + 'FINAL SOURCE REQUIREMENT']
        envelope['debug_info']['private_marker'] = 'PRIVATE_BUDGET'
        conn.execute('UPDATE manager_delegates SET kickoff_envelope=? WHERE delegate_id=?',
                     (json.dumps(envelope), receipt['delegate_id']))
        purpose = delegates.purpose_for_session(conn, context.session_id, 'Current event')
    assert 'next_char' in purpose and 'source_envelope' in purpose
    content, start = '', 0
    while True:
        page = delegates.read_delegate(context, delegate_id=receipt['delegate_id'], start_char=start)
        content += page['content']
        if page['next_char'] is None:
            break
        assert page['next_char'] > start
        start = page['next_char']
    decoded = json.loads(content)
    assert decoded['objective'] == objective
    assert decoded['source_envelope']['original_user_quotes'] == envelope['original_user_quotes']
    assert 'PRIVATE_BUDGET' not in content and 'debug_info' not in decoded['source_envelope']
