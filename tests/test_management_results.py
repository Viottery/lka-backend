"""Offline evidence reads preserve management scope and bounded continuation."""
import json
from contextlib import closing
from hashlib import sha256

import pytest

from app.core.agent_runs import AgentRunStatus
from app.core.tools import ToolContext, ToolInvocation, ToolSpec
from app.domains.mail import MailAccountInput, MailMessageInput
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
    return ManagementResultService(tasks=runtime.managed_tasks, registry=runtime.tool_registry)


def task(runtime, context, command='task'):
    return runtime.managed_tasks.start_task(context, command_id=command, purpose='Produce bounded evidence')


def persist_registered_mail_operation(runtime, receipt, *, tool_name, tool_input, invocation_id):
    tool = runtime.tool_registry.get_tool(tool_name)
    tool_context = runtime.session_progress_cache._context_for_run(receipt['run_id'])
    invocation = ToolInvocation(invocation_id=invocation_id, tool=tool.spec,
        session_id=receipt['session_id'], context_id='test', input=tool_input)
    result = tool.invoke(invocation=invocation, context=tool_context)
    raw = result.model_copy(update={'execution_started': True}).model_dump(mode='json')
    runtime.agent_run_store.put_artifact(artifact_id='tool_result_' + invocation_id,
        run_id=receipt['run_id'], kind='tool_result', payload=raw,
        summary='Synthetic registered mail tool result', created_at='2026-10-21T00:00:00Z')
    event = {'tool_name': tool_name, 'input': tool_input, 'result': raw,
             'completed_at': '2026-10-21T00:00:00Z'}
    runtime.session_progress_cache.record(context=tool_context, tool_events=[event])
    return event


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


def test_answer_includes_bounded_matter_write_receipt_without_sensitive_output(runtime, context, service):
    receipt = task(runtime, context)
    matter = {
        'matter_id': 'matter_123', 'revision': 4, 'title': 'Submit application',
        'status': 'open', 'due_at': '2026-10-27', 'scheduled_on': None,
        'scheduled_start': None, 'scheduled_end': None,
        'summary': 'PRIVATE_SUMMARY', 'metadata': {'private': 'PRIVATE_METADATA'},
        'source_links': [{'source_id': 'PRIVATE_SOURCE'}],
    }
    result_event = {
        'tool_name': 'matter.create',
        'result': {'invocation_id': 'matter-create', 'tool_name': 'matter.create',
                   'status': 'completed', 'execution_started': True,
                   'output': {'matter': matter, 'raw_prompt': 'PRIVATE_PROMPT'}},
    }
    finish(runtime, receipt, [result_event], answer='Matter created.')

    answer = service.read_task_result(context, run_id=receipt['run_id'], section='answer')
    assert answer['content'] == 'Matter created.'
    assert answer['execution_receipts']['total'] == 1
    projected = answer['execution_receipts']['items'][0]
    assert projected['tool_name'] == 'matter.create'
    assert projected['execution_status'] == 'completed'
    assert projected['execution_started'] is True
    assert projected['records_complete'] is True
    assert projected['read_evidence'] == answer['evidence_index']['items'][0]['read_result']
    assert projected['evidence_chars'] == answer['evidence_index']['items'][0]['evidence_chars']
    assert projected['records'] == [{
        'matter_id': 'matter_123', 'revision': 4, 'title': 'Submit application',
        'status': 'open', 'due_at': '2026-10-27', 'scheduled_on': None,
        'scheduled_start': None, 'scheduled_end': None,
        'missing_fields': [], 'truncated_fields': [],
    }]
    assert projected['receipt_scope'] == 'historical_tool_output_not_current_state_verification'
    serialized = json.dumps(answer, ensure_ascii=False)
    assert 'PRIVATE_' not in serialized and 'summary' not in serialized and 'source_links' not in serialized


def test_matter_receipt_reports_missing_and_truncated_output_truthfully(service):
    missing = service._matter_write_receipt('matter.update', 'update-missing', 'completed', True,
                                             {'unexpected': 'private'})
    assert missing['records_available'] is False
    assert missing['records'] == []
    assert missing['execution_status'] == 'completed'

    oversized = service._matter_write_receipt('matter.create_many', 'batch', 'completed', True, {
        'matters_created': 21,
        'matters': [{'matter_id': 'm' * 200, 'revision': 1, 'title': 'T' * 300,
                     'status': 'open', 'due_at': None, 'scheduled_on': None,
                     'scheduled_start': None, 'scheduled_end': None} for _ in range(21)],
    })
    assert len(oversized['records']) == 20
    assert oversized['records_truncated'] is True
    assert oversized['tool_reported_count'] == 21
    assert oversized['records'][0]['truncated_fields'] == ['matter_id', 'title']


@pytest.mark.parametrize(('status', 'started'), [('failed', True), ('rejected', False), ('completed', False)])
def test_matter_receipt_never_reports_records_for_unexecuted_or_failed_write(service, status, started):
    result = service._matter_write_receipt('matter.update', 'write', status, started,
        {'matter': {'matter_id': 'm1', 'revision': 2, 'title': 'x', 'status': 'open'}})
    assert result['records'] == []
    assert result['records_available'] is False
    assert result['records_withheld_reason'] == 'write_not_confirmed_completed'


@pytest.mark.parametrize('tamper', ['invocation_id', 'tool_name', 'content_hash'])
def test_mismatched_persisted_artifact_cannot_fall_back_to_positive_preview(runtime, context, service, tamper):
    receipt = task(runtime, context)
    matter = {'matter_id': 'm1', 'revision': 1, 'title': 'Expected', 'status': 'open',
              'due_at': None, 'scheduled_on': None, 'scheduled_start': None, 'scheduled_end': None}
    result = {'invocation_id': 'matter-write', 'tool_name': 'matter.create', 'status': 'completed',
              'execution_started': True, 'output': {'matter': matter}}
    event = {'tool_name': 'matter.create', 'result': result}
    runtime.agent_run_store.put_artifact(
        artifact_id='tool_result_matter-write', run_id=receipt['run_id'], kind='tool_result',
        payload=result, summary='Persisted Matter tool result', created_at='2026-10-21T00:00:00Z')
    finish(runtime, receipt, [event], answer='Matter created.')

    with closing(runtime._conn()) as conn, conn:
        row = conn.execute("SELECT payload,content_hash FROM agent_run_artifacts WHERE artifact_id=?",
                           ('tool_result_matter-write',)).fetchone()
        payload = json.loads(row['payload'])
        if tamper == 'invocation_id':
            payload['invocation_id'] = 'different-invocation'
        elif tamper == 'tool_name':
            payload['tool_name'] = 'matter.update'
        else:
            payload['output']['matter']['title'] = 'Tampered'
        payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        content_hash = row['content_hash'] if tamper == 'content_hash' else sha256(payload_text.encode()).hexdigest()
        conn.execute('UPDATE agent_run_artifacts SET payload=?,content_hash=? WHERE artifact_id=?',
                     (payload_text, content_hash, 'tool_result_matter-write'))

    page = service.read_task_result(context, run_id=receipt['run_id'])
    assert page['execution_receipts']['total'] == 0
    assert page['successful_operation_count'] == 0
    evidence = json.loads(service.read_task_result(
        context, run_id=receipt['run_id'], section='evidence')['content'])
    assert evidence[0]['status'] == 'unavailable'
    assert evidence[0]['source'] == 'artifact_integrity_mismatch'
    assert 'execution_receipt' not in evidence[0]
    assert evidence[0]['output']['result_withheld'] is True


@pytest.mark.parametrize('failure', ['error', 'output_success_false'])
def test_matter_receipt_is_withheld_for_tool_error_or_explicit_failure(runtime, service, failure):
    tool = runtime.tool_registry.get_tool('matter.create')
    result = {'invocation_id': 'write', 'tool_name': 'matter.create', 'status': 'completed',
              'execution_started': True,
              'output': {'matter': {'matter_id': 'm1', 'revision': 1, 'title': 'T', 'status': 'open'}}}
    if failure == 'error':
        result['error'] = 'operation reported an error'
    else:
        result['output']['success'] = False
    assert 'execution_receipt' not in service._event(result, tool.spec.name, 'test', None)


def test_matter_receipt_marks_empty_identity_fields_and_nonpositive_revision_missing(service):
    receipt = service._matter_write_receipt('matter.create', 'write', 'completed', True, {'matter': {
        'matter_id': ' ', 'revision': 0, 'title': '', 'status': None,
        'due_at': None, 'scheduled_on': None, 'scheduled_start': None, 'scheduled_end': None,
    }})
    assert receipt['records_complete'] is False
    assert receipt['records'][0]['missing_fields'] == ['matter_id', 'revision', 'title', 'status']


def test_matter_receipt_requires_registered_matter_write_producer(runtime, service):
    output = {'matter': {'matter_id': 'm1', 'revision': 2, 'title': 'Example', 'status': 'open'}}
    raw = {'invocation_id': 'write', 'tool_name': 'matter.create', 'status': 'completed',
           'execution_started': True, 'output': output}
    without_registry = ManagementResultService(tasks=runtime.managed_tasks)
    assert 'execution_receipt' not in without_registry._event(raw, 'matter.create', 'test', None)

    registry = runtime.tool_registry
    original = registry.get_tool('matter.create')
    for package, read_only, cached_reader in (
        ('other', False, False), ('matter', True, False), ('matter', False, True),
    ):
        replacement = type('ReplacementTool', (), {})()
        replacement.spec = ToolSpec(name='matter.create', type='local_tool', description='test replacement',
                                    package=package, read_only=read_only)
        if cached_reader:
            replacement.result_artifact_source = lambda **_: {}
        registry.register_tool(replacement)
        assert 'execution_receipt' not in service._event(raw, 'matter.create', 'test', None)
        registry.register_tool(original)


def test_failed_run_answer_recovers_receipt_from_persisted_matter_write(runtime, context, service):
    receipt = task(runtime, context)
    tool = runtime.tool_registry.get_tool('matter.create')
    invocation = ToolInvocation(
        invocation_id='persisted-matter-write', tool=tool.spec,
        session_id=receipt['session_id'], context_id='test',
        input={'title': 'Recovered matter', 'summary': 'Synthetic test matter', 'due_at': '2026-10-27'},
    )
    tool_context = ToolContext(session_id=receipt['session_id'], run_id=receipt['run_id'])
    actual_result = tool.invoke(invocation=invocation, context=tool_context)
    raw_result = actual_result.model_copy(update={'execution_started': True}).model_dump(mode='json')
    runtime.agent_run_store.put_artifact(
        artifact_id='tool_result_persisted-matter-write', run_id=receipt['run_id'],
        kind='tool_result', payload=raw_result, summary='Persisted Matter write result',
        created_at='2026-10-21T00:00:00Z',
    )
    runtime.agent_run_manager._update_run(receipt['run_id'], status=AgentRunStatus.FAILED,
        error_type='budget_deferred', result_snapshot={'answer': '', 'task_outcome': 'incomplete'})

    page = service.read_task_result(context, run_id=receipt['run_id'], section='answer')
    assert page['run']['status'] == 'failed'
    assert page['execution_receipts']['total'] == 1
    recovered = page['execution_receipts']['items'][0]
    matter = actual_result.output['matter']
    assert recovered['invocation_id'] == 'persisted-matter-write'
    assert recovered['execution_status'] == 'completed' and recovered['execution_started'] is True
    assert recovered['records'][0]['matter_id'] == matter['matter_id']
    assert recovered['records'][0]['revision'] == matter['revision']
    assert recovered['records'][0]['title'] == 'Recovered matter'
    assert recovered['records'][0]['due_at'] == '2026-10-27'
    assert recovered['read_evidence']['section'] == 'evidence'


def test_large_matter_batch_answer_is_bounded_and_evidence_pages_recover_all_rows(runtime, context, service):
    receipt = task(runtime, context)
    matters = [{
        'matter_id': f'matter_{index:02d}', 'revision': 1, 'title': f'T{index:02d} ' + ('x' * 230),
        'status': 'open', 'due_at': None, 'scheduled_on': None,
        'scheduled_start': None, 'scheduled_end': None,
        'metadata': {'secret': 'DO_NOT_EXPOSE'},
    } for index in range(25)]
    raw = {'invocation_id': 'large-batch', 'tool_name': 'matter.create_many',
           'status': 'completed', 'execution_started': True,
           'output': {'matters_created': 25, 'matters': matters, 'raw_prompt': 'DO_NOT_EXPOSE'}}
    runtime.agent_run_store.put_artifact(
        artifact_id='tool_result_large-batch', run_id=receipt['run_id'], kind='tool_result',
        payload=raw, summary='Synthetic Matter batch result', created_at='2026-10-21T00:00:00Z')
    runtime.agent_run_manager._update_run(receipt['run_id'], status=AgentRunStatus.COMPLETED,
        result_snapshot={'answer': 'Batch processed.'})

    answer = service.read_task_result(context, run_id=receipt['run_id'], section='answer')
    assert len(json.dumps(answer['execution_receipts'], ensure_ascii=False, separators=(',', ':'))) <= 12_000
    preview = answer['execution_receipts']['items'][0]
    assert answer['execution_receipts']['truncated'] is True
    assert preview['records_truncated'] is True and preview['records_complete'] is False
    assert len(preview['records']) <= 4
    assert preview['tool_reported_count'] == 25
    assert preview['evidence_chars'] > preview['read_evidence']['max_chars']
    assert preview['read_evidence']['max_chars'] == 8000

    full_evidence = json.loads(read_all(service, context, receipt['run_id'], max_chars=7999))
    full_receipt = full_evidence[0]['execution_receipt']
    assert len(full_receipt['records']) == 25
    assert full_receipt['records'][-1]['matter_id'] == 'matter_24'
    serialized = json.dumps(full_evidence, ensure_ascii=False)
    assert 'DO_NOT_EXPOSE' not in serialized and 'metadata' not in serialized


def test_mail_coverage_receipts_require_validated_source_and_cache_and_bound_private_data(
    runtime, context, service, monkeypatch,
):
    runtime.import_mail(
        account=MailAccountInput(email_address='synthetic@example.test'),
        messages=[MailMessageInput(
            external_id='synthetic-mail-1', folder='Inbox',
            subject='SUBJECT_PRIVATE_MARKER', sender='SENDER_PRIVATE_MARKER',
            received_at='2026-10-20T08:00:00+08:00',
            body_text='BODY_PRIVATE_MARKER ' + ('x' * 120),
        )],
    )
    receipt = task(runtime, context)
    list_input = {
        'received_from': '2026-10-20T00:00:00+08:00',
        'received_before': '2026-10-21T00:00:00+08:00',
        'folder': 'Inbox', 'private_prompt': 'INPUT_PRIVATE_MARKER',
    }
    listed = persist_registered_mail_operation(runtime, receipt, tool_name='mail.list',
        tool_input=list_input, invocation_id='mail-list-coverage')
    message_id = listed['result']['output']['messages'][0]['message_id']
    loaded = persist_registered_mail_operation(runtime, receipt, tool_name='mail.load_messages',
        tool_input={'message_ids': [message_id], 'max_chars_per_message': 80,
                    'private_prompt': 'INPUT_PRIVATE_MARKER'},
        invocation_id='mail-load-coverage')
    finish(runtime, receipt, [listed, loaded], answer='Synthetic mail processed.')

    page = service.read_task_result(context, run_id=receipt['run_id'])
    mail_receipts = page['mail_coverage_receipts']
    assert len(mail_receipts['items']) == 2
    list_receipt, load_receipt = mail_receipts['items']
    assert list_receipt['available'] is True, list_receipt.get('unavailable_reason')
    assert list_receipt['total_matches'] == 1 and list_receipt['returned_count'] == 1
    assert list_receipt['has_more'] is False and list_receipt['window_enumeration_complete'] is True
    assert list_receipt['received_from'] == list_input['received_from']
    assert list_receipt['received_before'] == list_input['received_before']
    assert list_receipt['folder'] == 'Inbox'
    assert load_receipt['available'] is True
    assert load_receipt['delivered_messages'][0]['message_id'] == message_id
    assert load_receipt['delivered_messages'][0]['body_truncated'] is True
    assert load_receipt['delivered_messages'][0]['delivered_body_chars'] == 80
    assert load_receipt['delivered_messages'][0]['source_body_chars'] is None
    assert load_receipt['delivered_messages'][0]['source_body_chars_known'] is False
    assert load_receipt['delivered_messages'][0]['missing_fields'] == []
    assert load_receipt['delivered_messages'][0]['body_complete'] is False
    assert load_receipt['delivery_complete'] is True
    assert load_receipt['all_delivered_bodies_complete'] is False
    serialized = json.dumps(page, ensure_ascii=False)
    for private in ('BODY_PRIVATE_MARKER', 'SUBJECT_PRIVATE_MARKER', 'SENDER_PRIVATE_MARKER', 'INPUT_PRIVATE_MARKER'):
        assert private not in serialized
    assert len(json.dumps(page['execution_receipts'], ensure_ascii=False, separators=(',', ':'))) <= 8_500
    assert len(json.dumps(page['mail_coverage_receipts'], ensure_ascii=False, separators=(',', ':'))) <= 3_000
    assert len(json.dumps({
        'execution_receipts': page['execution_receipts'],
        'mail_coverage_receipts': page['mail_coverage_receipts'],
    }, ensure_ascii=False, separators=(',', ':'))) <= 12_000
    assert page['evidence_index']['items'][0]['read_observation']['section'] == 'observation'
    imported_observation = service.read_task_result(
        context, run_id=receipt['run_id'], section='observation', invocation_id='mail-load-coverage',
    )['observation']
    imported_artifact_id = imported_observation['artifact_id']
    assert service.can_read_import(imported_artifact_id, context.run_id) is True

    runtime.import_mail(account=MailAccountInput(
        account_id='synthetic-mail', provider='local', email_address='synthetic@example.test',
    ), messages=[MailMessageInput(
        external_id='full-body-message', folder='Inbox', received_at='2026-10-20T08:00:00+00:00',
        subject='FULL_SUBJECT_PRIVATE', sender='full-sender@example.test',
        body_text='full untruncated body', is_unread=False, is_important=False,
    )])
    after_new_mail = service.read_task_result(context, run_id=receipt['run_id'])['mail_coverage_receipts']['items']
    assert all(item['available'] is True for item in after_new_mail)
    full_message_id = runtime.mail_service.list_messages(
        received_from='2026-10-20T00:00:00+00:00',
        received_before='2026-10-21T00:00:00+00:00', folder='Inbox', start_rank=1,
        end_rank=1,
    ).messages[0].message_id
    full_read = persist_registered_mail_operation(runtime, receipt, tool_name='mail.load_messages',
        tool_input={'message_ids': [full_message_id], 'max_chars_per_message': 256},
        invocation_id='mail-load-full-body')
    finish(runtime, receipt, [full_read], answer='Second synthetic mail read.')
    full_page = service.read_task_result(context, run_id=receipt['run_id'])
    full_receipt = full_page['mail_coverage_receipts']['items'][0]
    assert full_receipt['delivered_messages'][0]['body_truncated'] is False
    assert full_receipt['delivered_messages'][0]['source_body_chars'] is None
    assert full_receipt['delivered_messages'][0]['source_body_chars_known'] is False
    assert full_receipt['delivered_messages'][0]['missing_fields'] == []
    assert full_receipt['delivery_complete'] is True
    assert full_receipt['all_delivered_bodies_complete'] is True
    assert 'full untruncated body' not in json.dumps(full_page, ensure_ascii=False)
    assert service.can_read_import(imported_artifact_id, context.run_id) is True

    source_session_id = receipt['session_id']
    original_policy = runtime.managed_tasks.policy
    monkeypatch.setattr(runtime.managed_tasks, 'policy', lambda session_id: (
        original_policy(session_id).model_copy(update={'allowed_tools': []})
        if session_id == source_session_id else original_policy(session_id)
    ))
    revoked = service.read_task_result(context, run_id=receipt['run_id'])['mail_coverage_receipts']['items']
    assert len(revoked) == 3 and all(item['available'] is False for item in revoked)
    assert all('received_from' not in item and 'delivered_messages' not in item for item in revoked)
    assert all('read_observation' in item for item in revoked)

    monkeypatch.setattr(runtime.managed_tasks, 'policy', original_policy)
    with runtime._conn() as conn:
        conn.execute("UPDATE knowledge_sources SET status='inactive' WHERE source_type='mail_message'")
        conn.commit()
    source_revoked = service.read_task_result(context, run_id=receipt['run_id'])['mail_coverage_receipts']['items']
    assert len(source_revoked) == 3 and all(item['available'] is False for item in source_revoked)
    assert all('received_from' not in item and 'delivered_messages' not in item for item in source_revoked)
    with pytest.raises(ManagementConflictError, match='source access cannot be revalidated'):
        service.read_task_result(context, run_id=receipt['run_id'], section='observation',
                                 invocation_id='mail-load-coverage')
    assert service.can_read_import(imported_artifact_id, context.run_id) is False
