"""Offline evidence reads preserve management scope and bounded continuation."""
import json
from contextlib import closing

import pytest

from app.core.agent_runs import AgentRunStatus
from app.core.tools import ToolContext, ToolInvocation
from app.domains.management import ManagementConflictError
from app.domains.management_results import ManagementResultService
from app.tool_packages.management_results import management_result_tools
from tests.test_management_delegates import child_context
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_management_tasks import new_target
from tests.test_management_tasks import runtime as runtime_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def context(runtime):
    return context_fixture.__wrapped__(runtime)


@pytest.fixture
def service(runtime):
    return ManagementResultService(tasks=runtime.managed_tasks)


def task(runtime, context, command='task'):
    return runtime.managed_tasks.start_task(context, command_id=command, purpose='Produce bounded evidence')


def finish(runtime, receipt, events, answer='Completed operation'):
    runtime.agent_run_manager._update_run(receipt['run_id'], status=AgentRunStatus.COMPLETED,
        result_snapshot={'answer': answer, 'raw_prompt': 'PRIVATE_SNAPSHOT'})
    runtime.session_service.append_message(session_id=receipt['session_id'], role='agent', content=answer,
        payload={'run_id': receipt['run_id'], 'tool_events': events, 'raw_prompt': 'PRIVATE_MESSAGE'})


def event(content='actual contents', invocation='file-operation'):
    return {'tool_name': 'filesystem.create_file', 'input': {'raw_prompt': 'PRIVATE_INPUT'},
            'result': {'invocation_id': invocation, 'tool_name': 'filesystem.create_file',
                       'status': 'completed', 'execution_started': True,
                       'output': {'path': '/tmp/example.txt', 'content': content,
                                  'stdout': 'PRIVATE_STDOUT', 'metadata': {'content': 'PRIVATE_METADATA'},
                                  'raw_prompt': 'PRIVATE_OUTPUT'}}}


def read_all(service, context, run_id, section='evidence', max_chars=79):
    parts, start = [], 0
    while True:
        page = service.read_task_result(context, run_id=run_id, section=section,
                                        start_char=start, max_chars=max_chars)
        assert len(page['content']) <= max_chars
        parts.append(page['content'])
        if page['next_char'] is None:
            break
        assert page['next_char'] > start
        start = page['next_char']
    return ''.join(parts)


def test_read_answer_and_actual_evidence_with_complete_continuation(runtime, context, service):
    receipt = task(runtime, context)
    contents = '内容' * 5100
    finish(runtime, receipt, [event(contents)], answer='结果' * 4000)
    assert read_all(service, context, receipt['run_id'], 'answer') == '结果' * 4000
    raw = read_all(service, context, receipt['run_id'])
    result = json.loads(raw)
    assert result[0]['output']['content'] == contents
    assert result[0]['output']['path'] == '/tmp/example.txt'
    assert result[0]['execution_started'] is True
    assert 'PRIVATE' not in raw and 'stdout' not in raw and 'metadata' not in raw
    page = service.read_task_result(context, run_id=receipt['run_id'])
    assert page['business_completion'] == 'not_determined'
    assert page['terminal'] and page['observed_at']
    assert 'not current' in page['evidence_scope']


def test_cross_manager_unlinked_deleted_and_child_roots_rejected(runtime, context, service):
    receipt = task(runtime, context)
    other = context_fixture.__wrapped__(runtime)
    for target_context in (other,):
        with pytest.raises(ManagementConflictError):
            service.read_task_result(target_context, run_id=receipt['run_id'])
    unlinked = new_target(runtime)
    with pytest.raises(ManagementConflictError):
        service.read_task_result(context, run_id=unlinked.run_id)
    child = runtime.agent_run_manager.get_run(receipt['run_id']).model_copy(
        update={'run_id': 'child-result', 'parent_run_id': receipt['run_id']})
    runtime.agent_run_store.save_run(child.model_dump(mode='json'), updated_at=child.created_at)
    with pytest.raises(ManagementConflictError, match='independent root'):
        service.read_task_result(context, run_id=child.run_id)
    with closing(runtime._conn()) as conn, conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (receipt['session_id'],))
    with pytest.raises(KeyError):
        service.read_task_result(context, run_id=receipt['run_id'])


def test_ordinary_and_child_callers_never_gain_management_authority(runtime, context, service):
    receipt = task(runtime, context)
    run = runtime.agent_run_manager.get_run(receipt['run_id'])
    runtime.agent_run_manager.mark_running(run.run_id)
    ordinary = ToolContext(session_id=run.session_id, run_id=run.run_id)
    with pytest.raises(ManagementConflictError):
        service.read_task_result(ordinary, run_id=run.run_id)
    child = runtime.agent_run_manager.get_run(context.run_id).model_copy(
        update={'run_id': 'manager-child', 'parent_run_id': context.run_id})
    runtime.agent_run_store.save_run(child.model_dump(mode='json'), updated_at=child.created_at)
    with pytest.raises(ManagementConflictError):
        service.read_task_result(context.model_copy(update={'run_id': child.run_id}), run_id=run.run_id)


def test_delegate_scope_and_parent_owned_delegation(runtime, context, service):
    delegates = runtime.management_delegates
    own = delegates.delegate_work(context, command_id='line', objective='Inspect evidence')
    delegate_context = child_context(runtime, own['session_id'])
    own_task = task(runtime, delegate_context)
    parent_task = task(runtime, context)
    assert service.read_task_result(context, run_id=own_task['run_id'])['run']['run_id'] == own_task['run_id']
    assert service.read_task_result(context, run_id=delegate_context.run_id)['run']['session_id'] == own['session_id']
    assert service.read_task_result(delegate_context, run_id=own_task['run_id'])['terminal'] is False
    with pytest.raises(ManagementConflictError):
        service.read_task_result(delegate_context, run_id=parent_task['run_id'])
    other = context_fixture.__wrapped__(runtime)
    with pytest.raises(ManagementConflictError):
        service.read_task_result(other, run_id=own_task['run_id'])


def test_nonterminal_returns_state_without_partial_answer(runtime, context, service):
    receipt = task(runtime, context)
    page = service.read_task_result(context, run_id=receipt['run_id'], section='evidence')
    assert page['terminal'] is False and page['run']['status'] == 'queued'
    assert 'content' not in page


def test_artifacts_restore_full_results_and_failure_evidence(runtime, context, service):
    receipt = task(runtime, context)
    raw = event('unabridged ' * 1200)['result']
    runtime.agent_run_store.put_artifact(artifact_id='tool_result_file-operation', run_id=receipt['run_id'],
        kind='tool_result', payload=raw, summary='test', created_at='2026-10-20')
    finish(runtime, receipt, [event('compacted preview')])
    assert json.loads(read_all(service, context, receipt['run_id']))[0]['output']['content'] == raw['output']['content']
    with closing(runtime._conn()) as conn, conn:
        conn.execute('DELETE FROM agent_session_messages WHERE session_id=?', (receipt['session_id'],))
    runtime.agent_run_manager._update_run(receipt['run_id'], status=AgentRunStatus.FAILED, result_snapshot={})
    evidence = json.loads(read_all(service, context, receipt['run_id']))
    assert evidence[0]['source'] == 'tool_result_artifacts'
    assert evidence[0]['output']['content'] == raw['output']['content']


def test_schema_and_malicious_identifiers_do_not_read_paths(runtime, context, service, tmp_path):
    private = tmp_path / 'private.txt'
    private.write_text('SECRET_FILE')
    for run_id in (str(private), '../../etc/passwd', "' OR 1=1 --"):
        with pytest.raises(KeyError):
            service.read_task_result(context, run_id=run_id)
    tool = management_result_tools(service)[0]
    assert tool.spec.read_only is True and not tool.spec.requires_confirmation
    assert tool.spec.name == 'management.read_task_result'
    assert tool.spec.input_schema['additionalProperties'] is False
    assert 'path' not in tool.spec.input_schema['properties']
    receipt = task(runtime, context)
    result = tool.invoke(invocation=ToolInvocation(invocation_id='read', tool=tool.spec, session_id=context.session_id, context_id='test',
        input={'run_id': receipt['run_id']}), context=context)
    assert result.status == 'completed'


def test_snapshot_keeps_status_and_evidence_together(runtime, context, service, monkeypatch):
    receipt = task(runtime, context)
    finish(runtime, receipt, [event('original')])
    original = service._evidence

    def change_after_run_read(conn, record):
        assert conn.in_transaction
        runtime.session_service.append_message(session_id=receipt['session_id'], role='agent', content='new',
            payload={'run_id': receipt['run_id'], 'tool_events': [event('changed')]})
        return original(conn, record)

    monkeypatch.setattr(service, '_evidence', change_after_run_read)
    result = service.read_task_result(context, run_id=receipt['run_id'], section='evidence')
    assert json.loads(result['content'])[0]['output']['content'] == 'original'
