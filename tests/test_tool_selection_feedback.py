"""Capability projections and invalid tool proposals preserve authorization."""
import json

import pytest

from app.core.agent_turn import AgentTurnLoop
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.safety import SafetyReviewMode
from app.core.tools import ToolExecutor, ToolPackageSpec, ToolRegistry, ToolSpec
from tests.test_management_api import runtime as runtime_fixture
from tests.test_unified_entry_quality import ScriptedClient, operation


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)



class Tool:
    def __init__(self, name, readonly):
        self.spec = ToolSpec(name=name, package='records', type='local_tool', description=name, read_only=readonly)


def loop_and_view():
    registry = ToolRegistry()
    registry.register_package(ToolPackageSpec(name='records', description='Read and write records',
        decision_hints=['Use records.write'], capability_hints=[
            {'required_tools':['records.read'], 'decision_hints':['Read records using records.read']},
            {'required_tools':['records.write'], 'decision_hints':['Write using records.write']}]))
    registry.register_tool(Tool('records.read',True)); registry.register_tool(Tool('records.write',False))
    loop=AgentTurnLoop.__new__(AgentTurnLoop);loop.tool_executor=ToolExecutor(registry)
    view=ToolView(snapshot_id='test',allowed_tools=('records.read',),side_effect_level=SideEffectLevel.READ)
    return loop, view


def test_partial_catalog_has_no_ungranted_operation_promises():
    loop,view=loop_and_view(); catalog=loop._package_catalog(view)
    assert 'records.write' not in json.dumps(catalog)
    assert 'records.read' in json.dumps(catalog)
    assert 'capability_hints' not in catalog[0]


@pytest.mark.parametrize(('name','code','package'),[
    ('records.missing','tool_not_registered',None),
    ('records.write','tool_not_authorized',None),
    ('records.read','tool_not_expanded','records'),
])
def test_selection_feedback_distinguishes_registration_grant_and_expansion(name,code,package):
    loop,view=loop_and_view()
    feedback=loop._tool_selection_feedback(name,expanded_tools=[],tool_view=view,observations=[])
    assert feedback['tool_selection_error']==code
    assert feedback.get('package_name')==package
    assert feedback['execution_started'] is False
    denied=loop._tool_selection_feedback(name,expanded_tools=[],tool_view=view,observations=[feedback,feedback])
    assert denied['retry_allowed'] is False


@pytest.mark.parametrize('graph',[False,True])
def test_illegal_writer_is_rejected_then_authorized_writer_can_recover(runtime,graph):
    sid=runtime.management_service.create()['session']['session_id']
    runtime.management_service.update(sid,expected_revision=1,status='active')
    client=ScriptedClient([
        operation('expand_package',package_name='matter'),
        operation('tool_call',tool_name='matter.create',tool_input={'title':'must not execute'}),
        operation('tool_call',tool_name='management.create_matter',tool_input={'command_id':'valid','title':'Recovered'}),
        operation('final_answer',reason='Saved'),'已保存。'])
    runtime.agent_turn_loop.llm_client=client
    result=runtime.run_agent_turn(session_id=sid,user_input='记下一条明确事项。',resumable=graph,safety_review_mode=SafetyReviewMode.SKIP)
    assert [e.tool_name for e in result.tool_events]==['management.create_matter']
    assert runtime.matter_service.list_matters().total_count==1
    rejection=next(e for e in result.progress_events if e.type=='tool_rejected')
    assert rejection.metadata['tool_selection_error']=='tool_not_authorized'
    assert any('tool_not_authorized' in call['user_prompt'] for call in client.calls)


@pytest.mark.parametrize('graph', [False, True])
def test_native_unknown_function_recovers_without_execution(runtime, graph):
    from app.core.llm import LLMResponse

    class Native:
        supports_function_calling = True
        def __init__(self):
            self.index = 0
        def complete_text(self, *, tools=None, prompt_summary='', **kwargs):
            calls = []
            text = '已保存。'
            if tools:
                options = [t.name for t in tools]
                if self.index == 0:
                    name, args = 'agent_expand_package', {'package_name':'matter'}
                elif self.index == 1:
                    # A raw registry name must not act as a native function alias.
                    name, args = 'management.create_matter', {'command_id':'invalid','title':'Wrong'}
                elif self.index == 2:
                    name = next(n for n in options if n.endswith('_management_create_matter'))
                    args = {'command_id':'valid','title':'Recovered native'}
                    assert 'unknown_function' in kwargs['user_prompt']
                else:
                    name, args = 'agent_final_answer', {'reason':'Saved'}
                self.index += 1
                calls = [{'id':str(self.index),'name':name,'arguments':args}]
                text = ''
            return LLMResponse(provider='synthetic',status='completed',content=text,prompt_summary=prompt_summary,tool_calls=calls)

    sid=runtime.management_service.create()['session']['session_id']
    runtime.management_service.update(sid,expected_revision=1,status='active')
    runtime.agent_turn_loop.llm_client=Native()
    result=runtime.run_agent_turn(session_id=sid,user_input='记下一条明确事项。',resumable=graph,safety_review_mode=SafetyReviewMode.SKIP)
    assert [e.tool_name for e in result.tool_events] == ['management.create_matter']
    assert runtime.matter_service.list_matters().total_count == 1
    assert next(e for e in result.progress_events if e.type=='tool_rejected').metadata['tool_selection_error']=='unknown_function'
