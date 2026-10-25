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
    # Session responsibility is not a machine capability restriction: the
    # supervisor cannot update a Matter directly, while its same-backend task can.
    assert task.metadata.get('remote_execution') is None
    assert runtime.tool_registry.get_tool('mail.search') is not None
    task_tools = task.metadata['session_policy']['allowed_tools']
    assert task_tools is None or 'matter.update' in task_tools
    assert runtime.tool_registry.get_tool('matter.update') is not None
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


@pytest.mark.parametrize('enabled,autonomous', [(True, False), (True, True), (False, True)])
def test_supervisor_sees_effective_followup_capability_without_enabling_it(runtime, enabled, autonomous):
    runtime.local_app_config.management.enabled = enabled
    runtime.local_app_config.management.autonomous_enabled = autonomous
    sid = runtime.management_service.create()['session']['session_id']
    # Capability information cannot override the paused controller's write restriction.
    paused = runtime._session_run_policy(sid)
    assert '当前管理状态为 paused' in paused.role_instructions
    assert 'management.set_goal' not in paused.allowed_tools
    expected = '自动跟进已启用' if enabled and autonomous else '自动跟进未启用'
    assert expected in paused.role_instructions
    runtime.management_service.update(sid, expected_revision=1, status='active')
    run = runtime.create_agent_run(session_id=sid, user_input='负责跟进这项工作', resumable=True)
    policy = run.metadata['session_policy']
    assert expected in policy['role_instructions']
    assert 'management.set_goal' in policy['allowed_tools']
    assert runtime.local_app_config.management.enabled is enabled
    assert runtime.local_app_config.management.autonomous_enabled is autonomous


def test_supervisor_reviews_report_gaps_without_rescanning_sources():
    assert "只针对缺口给出补充指令" in SUPERVISOR_INSTRUCTIONS
    assert "复用返回的证据和结果，不为补齐报告而重扫原资料" in SUPERVISOR_INSTRUCTIONS
    assert "日期精度或来源冲突" in SUPERVISOR_INSTRUCTIONS
    assert "区分必需条件和可选改进，不自行加严要求" in SUPERVISOR_INSTRUCTIONS
    assert "实质影响原要求是否满足时才构成阻塞" in SUPERVISOR_INSTRUCTIONS
    assert "next_char" in SUPERVISOR_INSTRUCTIONS and "source_envelope" in SUPERVISOR_INSTRUCTIONS
