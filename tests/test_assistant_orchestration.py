"""Assistant role is frozen separately from ordinary context and tool authority."""
import json

import pytest

from app.core.safety import SafetyReviewMode
from app.core.tools import ToolContext
from app.domains.management_role import SUPERVISOR_INSTRUCTIONS
from tests.test_management_api import runtime as runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)



@pytest.mark.parametrize('graph', [False, True])
def test_supervisor_role_precedes_route_and_does_not_enter_task_policy(runtime, graph):
    runtime.agent_turn_loop.unified_entry_enabled = False
    sid = runtime.management_service.create()['session']['session_id']
    runtime.management_service.update(sid, expected_revision=1, status='active')
    client = ScriptedClient([
        json.dumps({'selected_package': 'management', 'reason': 'Delegate substantive work'}),
        operation('tool_call', tool_name='management.start_task', tool_input={
            'command_id': 'batch', 'purpose': 'Read current synthetic mail, extract required actions, deduplicate and save local Matters. Report source evidence and coverage.'}),
        operation('final_answer', reason='Dispatched'), '已下发整理任务。',
    ])
    runtime.agent_turn_loop.llm_client = client
    result = runtime.run_agent_turn(session_id=sid, user_input='从我的邮件整理必须事项，去重后保存。', resumable=graph,
                                    safety_review_mode=SafetyReviewMode.SKIP)
    assert result.tool_events[0].tool_name == 'management.start_task'
    assert all(SUPERVISOR_INSTRUCTIONS in call['system_prompt'] for call in client.calls)
    assert client.calls[0]['prompt_summary'].startswith('agent_turn_route')
    target = result.tool_events[0].result['output']
    task = runtime.agent_run_manager.get_run(target['run_id'])
    assert task.metadata['session_policy']['role_instructions'] is None
    assert task.metadata['session_policy']['allow_default_workspace'] is False
    assert task.metadata['session_policy']['allowed_paths'] == []
    assert 'mail.search' in runtime._session_run_policy(sid).allowed_tools
    assert 'matter.update' not in runtime._session_run_policy(sid).allowed_tools
    assert 'mail.search' in runtime._session_run_policy(sid).read_only_tools
    # User metadata cannot turn an ordinary session into the trusted supervisor.
    normal = runtime.session_service.create_session(metadata={'role_instructions': SUPERVISOR_INSTRUCTIONS}).session
    assert runtime._session_run_policy(normal.session_id).role_instructions is None


def test_conditional_supervisor_reader_does_not_grant_write_commands(runtime, tmp_path):
    sid = runtime.management_service.create()['session']['session_id']
    run = runtime.create_agent_run(session_id=sid, user_input='Simple search', resumable=True)
    view = runtime.agent_turn_loop._tool_view_for_run(run.run_id)
    visible = runtime.agent_turn_loop._tools_for_package('bash', tool_view=view)
    assert any(t.name == 'bash.run' for t in visible)
    result = runtime.tool_executor.execute(invocation_id='denied-write', tool_name='bash.run',
        tool_input={'command': 'touch must-not-exist.txt', 'cwd': str(tmp_path)},
        context=ToolContext(session_id=sid, run_id=run.run_id, tool_view=view, safety_review_approved=True))
    assert result.status == 'rejected' and result.execution_started is False
    assert result.output['authorization_denial']['code'] == 'invocation_not_read_only'
    assert not (tmp_path/'must-not-exist.txt').exists()
