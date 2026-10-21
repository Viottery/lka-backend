"""Offline recovery indexes and revocable cross-task raw-result imports."""
import json
from contextlib import closing

import pytest

from app.core.agent_runs import AgentRunStatus
from app.domains.managed_tasks import ManagedTaskRunMismatchError
from app.domains.management import ManagementConflictError
from app.domains.management_results import ManagementResultService
from tests import test_management_results as helpers
from tests.test_management_results import event, task
from tests.test_management_tasks import counts, new_target


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from helpers.runtime.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def context(runtime):
    return helpers.context.__wrapped__(runtime)


@pytest.fixture
def service(runtime):
    return helpers.service.__wrapped__(runtime)


def persist(runtime, receipt, invocation='read', *, tool_name='mail.load_messages', output=None):
    raw = {'tool_name': tool_name, 'invocation_id': invocation, 'status': 'completed',
           'execution_started': True, 'input': {'raw_prompt': 'PRIVATE_INPUT'},
           'output': output or {'messages': [{'body_text': 'Original mail body', 'subject': 'Mail subject'}],
                                'stdout': 'PRIVATE_STDOUT', 'raw_prompt': 'PRIVATE_PROMPT'}}
    runtime.agent_run_store.put_artifact(artifact_id='tool_result_' + invocation, run_id=receipt['run_id'],
        kind='tool_result', payload=raw, summary='synthetic result', created_at='2026-10-20T00:00:00Z')
    return raw


def test_empty_failed_answer_has_bounded_evidence_index_and_real_failure(runtime, context, service):
    receipt = task(runtime, context)
    for index in range(23):
        persist(runtime, receipt, f'read-{index:02d}')
    runtime.agent_run_manager._update_run(receipt['run_id'], status=AgentRunStatus.FAILED,
        error_type='budget_deferred', error='PRIVATE_ERROR',
        result_snapshot={'answer': '', 'task_outcome': 'incomplete', 'stop_reason': 'budget_deferred'})
    page = service.read_task_result(context, run_id=receipt['run_id'])
    assert page['content'] == ''
    assert page['successful_operation_count'] == 23
    assert page['successful_operation_count_source'] == 'tool_result_artifacts'
    assert page['task_outcome'] == 'incomplete' and page['stop_reason'] == 'budget_deferred'
    assert page['error_type'] == 'budget_deferred'
    assert page['failure_category'] == 'temporary_resource_deferral'
    assert 'Load prior evidence' in page['recovery_hint']
    assert len(page['evidence_index']['items']) == 20
    assert page['evidence_index']['next_offset'] == 20
    assert 'PRIVATE' not in json.dumps(page)
    item = page['evidence_index']['items'][0]
    assert item['saved_at'] == '2026-10-20T00:00:00Z'
    assert item['source'] == 'tool_result_artifacts' and item['tool_name'] == 'mail.load_messages'
    evidence_page = service.read_task_result(context, **item['read_result'])
    assert json.loads(evidence_page['content'])['invocation_id'] == item['invocation_id']
    final = service.read_task_result(context, run_id=receipt['run_id'], evidence_offset=20)
    assert len(final['evidence_index']['items']) == 3
    assert final['evidence_index']['next_offset'] is None


def test_successful_final_text_without_artifact_does_not_inflate_operation_count(runtime, context, service):
    receipt = task(runtime, context)
    runtime.agent_run_manager._update_run(receipt['run_id'], status=AgentRunStatus.COMPLETED,
        result_snapshot={'answer': 'Done', 'tool_events': [event()]})
    page = service.read_task_result(context, run_id=receipt['run_id'])
    assert page['successful_operation_count'] == 0
    assert page['task_outcome'] == 'unknown' and page['business_completion'] == 'not_determined'


@pytest.mark.parametrize('revocation', ['deleted', 'permission', 'hash', 'copy_hash', 'ownership'])
def test_mail_raw_import_keeps_fields_and_revalidates_origin(runtime, context, monkeypatch, revocation):
    service = ManagementResultService(tasks=runtime.managed_tasks, registry=runtime.tool_registry)
    receipt = task(runtime, context)
    runtime.import_mail(account=helpers.MailAccountInput(email_address='history@example.test'),
        messages=[helpers.MailMessageInput(external_id='history-message', folder='Inbox',
            received_at='2026-10-20T08:00:00+00:00', subject='Mail subject',
            sender='sender@example.test', body_text='Original mail body')])
    message_id = runtime.mail_service.list_messages(
        received_from='2026-10-20T00:00:00+00:00', received_before='2026-10-21T00:00:00+00:00',
        folder='Inbox', start_rank=1, end_rank=1,
    ).messages[0].message_id
    helpers.persist_registered_mail_operation(runtime, receipt, tool_name='mail.load_messages',
        tool_input={'message_ids': [message_id]}, invocation_id='read')
    page = service.read_task_result(context, run_id=receipt['run_id'], section='observation', invocation_id='read')
    route = page['observation']
    assert route['source_run_id'] == receipt['run_id'] and route['historical_only']
    imported = route['artifact_id']
    store = runtime.agent_run_store
    store.progress_import_validator = service.can_read_import
    assert store.load_tool_result_artifact('tool_result_read', context.run_id) is None
    raw = store.load_tool_result_artifact(imported, context.run_id)
    assert raw['output']['messages'][0]['body_text'] == 'Original mail body'
    assert raw['invocation_id'] == 'read' and raw['tool_name'] == 'mail.load_messages'
    assert 'PRIVATE' not in json.dumps(raw) and 'input' not in raw
    assert store.load_tool_result_artifact(imported, receipt['run_id']) is None
    if revocation == 'permission':
        original = runtime.managed_tasks.policy
        monkeypatch.setattr(runtime.managed_tasks, 'policy', lambda sid: original(sid).model_copy(
            update={'allowed_tools': []}) if sid == receipt['session_id'] else original(sid))
    else:
        with closing(runtime._conn()) as conn, conn:
            if revocation == 'deleted':
                conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id=?", (receipt['session_id'],))
            elif revocation == 'ownership':
                conn.execute('DELETE FROM managed_session_links WHERE task_session_id=?', (receipt['session_id'],))
            else:
                conn.execute('UPDATE agent_run_artifacts SET payload=? WHERE artifact_id=?',
                             ('{}', imported if revocation == 'copy_hash' else 'tool_result_read'))
    assert store.load_tool_result_artifact(imported, context.run_id) is None


@pytest.mark.parametrize('tool_name', ['filesystem.create_file', 'bash.run', 'bash.read_session'])
def test_raw_import_denies_write_and_command_streams(runtime, context, tool_name):
    service = ManagementResultService(tasks=runtime.managed_tasks, registry=runtime.tool_registry)
    receipt = task(runtime, context)
    persist(runtime, receipt, tool_name=tool_name)
    with pytest.raises(ManagementConflictError, match='use section=evidence'):
        service.read_task_result(context, run_id=receipt['run_id'], section='observation', invocation_id='read')


def test_owned_task_reader_cannot_proxy_raw_source_export(runtime, context):
    from app.core.tools import ToolContext, ToolInvocation

    service = ManagementResultService(tasks=runtime.managed_tasks, registry=runtime.tool_registry)
    receipt = task(runtime, context)
    persist(runtime, receipt)
    tool = runtime.tool_registry.get_tool('observation.read')
    arguments = {'artifact_id': 'tool_result_read', 'path': '/output/messages/0/body_text'}
    derived = tool.invoke(invocation=ToolInvocation(invocation_id='proxy', tool=tool.spec,
        session_id=receipt['session_id'], context_id='test', input=arguments),
        context=ToolContext(session_id=receipt['session_id'], run_id=receipt['run_id']))
    assert derived.status == 'completed'
    runtime.agent_run_store.put_artifact(artifact_id='tool_result_proxy', run_id=receipt['run_id'],
        kind='tool_result', payload=derived.model_dump(mode='json'), summary='synthetic proxy',
        created_at='2026-10-21T00:00:00Z',
        source_artifact=tool.result_artifact_source(tool_input=arguments, result=derived))
    runtime.agent_run_manager.complete_run(receipt['run_id'])
    page = service.read_task_result(context, run_id=receipt['run_id'])
    proxy = next(item for item in page['evidence_index']['items'] if item['invocation_id'] == 'proxy')
    assert 'read_observation' not in proxy
    evidence = service.read_task_result(context, run_id=receipt['run_id'], section='evidence', max_chars=8000)
    receipt_evidence = next(item for item in json.loads(evidence['content']) if item['invocation_id'] == 'proxy')
    assert receipt_evidence['status'] == 'completed'
    assert receipt_evidence['output']['body_withheld'] is True
    assert 'Original mail body' not in evidence['content']
    with pytest.raises(ManagementConflictError, match='original producer'):
        service.read_task_result(context, run_id=receipt['run_id'], section='observation', invocation_id='proxy')
    # Missing legacy provenance also fails closed, rather than exporting its body.
    persist(runtime, receipt, 'legacy-reader', tool_name='observation.read', output={'text': 'Old source'})
    with pytest.raises(ManagementConflictError, match='no longer readable'):
        service.read_task_result(context, run_id=receipt['run_id'], section='observation', invocation_id='legacy-reader')
    # Receipt-only service instances also use trusted store producer registration.
    receipt_only = ManagementResultService(tasks=runtime.managed_tasks)
    legacy = receipt_only.read_task_result(context, run_id=receipt['run_id'], section='evidence', max_chars=8000)
    assert 'Old source' not in legacy['content']


def test_mismatched_run_returns_safe_current_view_without_acceptance(runtime, context):
    target = new_target(runtime, status='paused')
    runtime.agent_run_manager._update_run(target.run_id, status=AgentRunStatus.PAUSED, metadata_patch={'raw_prompt': 'PRIVATE_PROMPT'})
    before = counts(runtime)
    with pytest.raises(ManagedTaskRunMismatchError) as failure:
        runtime.managed_tasks.send_instruction(context, command_id='wrong-reference', session_id=target.session_id,
                                               expected_run_id='mistyped-id', purpose='Continue the task')
    exc = failure.value
    assert exc.code == 'expected_run_mismatch' and exc.expected_run_id == 'mistyped-id'
    assert exc.current_run['run_id'] == target.run_id and exc.current_run['status'] == 'paused'
    assert exc.observed_at and 'PRIVATE' not in json.dumps(exc.current_run)
    assert counts(runtime) == before


def test_run_mismatch_tool_result_exposes_exact_reference_without_retry(runtime, context):
    from app.core.tools import ToolInvocation

    target = new_target(runtime, status='paused')
    tool = runtime.tool_registry.get_tool('management.send_instruction')
    before = counts(runtime)
    result = tool.invoke(context=context, invocation=ToolInvocation(
        invocation_id='wrong-run-ref', tool=tool.spec, session_id=context.session_id, context_id='test',
        input={'command_id': 'wrong-ref', 'session_id': target.session_id,
               'expected_run_id': 'mistyped-id', 'purpose': 'Continue the task'},
    ))
    assert result.status == 'rejected'
    assert result.output['failure']['code'] == 'expected_run_mismatch'
    assert result.output['failure']['retryable'] is False
    assert result.output['task_accepted'] is False
    assert result.output['current_run']['run_id'] == target.run_id
    assert counts(runtime) == before
