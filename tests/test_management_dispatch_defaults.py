"""Nullable task workspace means the reviewed default, never broader authority."""
from pathlib import Path

import pytest

from app.core.safety import SafetyReviewMode
from app.core.tools import ToolContext
from tests.test_management_api import runtime as runtime_fixture
from tests.test_management_tasks import counts
from tests.test_management_tasks import manager_context as context_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize('resumable', [False, True])
@pytest.mark.parametrize('selection', ['omitted', 'null', 'explicit'])
def test_task_workspace_defaults_pass_executor_without_repair(runtime, tmp_path, resumable, selection):
    default = tmp_path / 'default'
    explicit = tmp_path / 'project'
    default.mkdir()
    explicit.mkdir()
    sid = runtime.management_service.create(default_execution_workspace={
        'path': str(default), 'platform': 'linux',
    })['session']['session_id']
    runtime.management_service.update(sid, expected_revision=1, status='active')
    inputs = {'command_id': 'default-task', 'purpose': 'Inspect the synthetic task workspace.'}
    if selection == 'null':
        inputs['workspace_path'] = None
    elif selection == 'explicit':
        inputs['workspace_path'] = str(explicit)
    client = ScriptedClient([
        operation('expand_package', package_name='management'),
        operation('tool_call', tool_name='management.start_task', tool_input=inputs),
        operation('final_answer', reason='Task accepted.'), '已安排工作。',
    ])
    runtime.agent_turn_loop.llm_client = client
    result = runtime.run_agent_turn(session_id=sid, user_input='安排一个执行任务。',
        resumable=resumable, safety_review_mode=SafetyReviewMode.SKIP)
    assert len(client.calls) == 4
    [event] = result.tool_events
    assert event.tool_name == 'management.start_task'
    assert event.result['status'] == 'completed' and event.result['execution_started'] is True
    queued = runtime.agent_run_manager.get_run(event.result['output']['run_id'])
    actual = Path(queued.metadata['execution_workspace']['backend_path'])
    assert actual == explicit if selection == 'explicit' else actual.parent == default
    assert actual.is_dir()
    assert queued.metadata['session_policy']['allow_default_workspace'] is False
    [review] = runtime.agent_run_manager.list_safety_reviews(result.run_id)
    assert review.status.value == 'approved' and review.mode == SafetyReviewMode.SKIP


@pytest.mark.parametrize('bad_path', [17, False, {}, ['a']])
def test_nullable_workspace_still_rejects_invalid_types(runtime, bad_path):
    tool = runtime.tool_registry.get_tool('management.start_task')
    errors = runtime.tool_executor._validate_input(schema=tool.spec.input_schema,
        value={'command_id': 'invalid', 'purpose': 'Inspect', 'workspace_path': bad_path}, path='$')
    assert errors


def test_nullable_default_does_not_skip_safety_gate(runtime):
    result = runtime.tool_executor.execute(invocation_id='unreviewed-task',
        tool_name='management.start_task',
        tool_input={'command_id': 'unreviewed', 'purpose': 'Inspect', 'workspace_path': None},
        context=ToolContext(session_id='unreviewed-manager'))
    assert result.status == 'rejected' and result.execution_started is False
    assert result.output['safety_review_required'] is True


def test_empty_workspace_does_not_fall_back_to_backend_or_create_task(runtime):
    context = context_fixture.__wrapped__(runtime)
    before = counts(runtime)
    with pytest.raises(ValueError, match='absolute'):
        runtime.managed_tasks.start_task(context, command_id='empty-workspace',
            purpose='Inspect', workspace_path='')
    assert counts(runtime) == before
