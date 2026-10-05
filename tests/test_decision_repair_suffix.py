"""Bounded repair-envelope recovery, never ambiguous tool execution."""

import json

import pytest

from app.core.tools import ToolContext, ToolExecutor, ToolRegistry, ToolResult, ToolSpec
from tests.test_answer_generation_recovery_quality import make_loop, response


def repair(tmp_path, content):
    loop, _, _ = make_loop(tmp_path, [])
    events = []
    loop._complete_control_generation = lambda **kwargs: (response(content), None)
    loop._append_run_event = lambda **kwargs: events.append(kwargs)
    result = loop._repair_malformed_decision_output(user_input="Inspect a resource.", route={},
        expanded_tools=[], observations=[], llm_events=[], raw_output="malformed original tool envelope")
    return loop, result, events


def envelope():
    return json.dumps({"operation": {"type": "tool_call", "tool_name": "custom.inspect",
        "tool_input": {"identifier": "value-with-}-brace"}}, "assistant_message": "Inspecting."})


def test_repair_one_extra_close_preserves_exact_input_and_logs_recovery(tmp_path):
    content = envelope() + " } "
    loop, result, events = repair(tmp_path, content)
    assert result["action"] == "call_tool" and result["tool_name"] == "custom.inspect"
    assert result["tool_input"] == {"identifier": "value-with-}-brace"}
    assert result["_repair_output"] == content and result["_raw_output"] == "malformed original tool envelope"
    assert events[0]["type"] == "decision_repair_suffix_normalized"
    assert loop._parse_json_object(content, strict=True) == {}  # Other stages remain strict.


@pytest.mark.parametrize("content", [
    envelope() + "}}", envelope() + " trailing prose", "prefix " + envelope() + "}",
    envelope() + "\n" + envelope(), "{} }", '{"operation":{},"operation":{}}}',
    '{"operation":{"type":"tool_call","tool_input":{"id":1,"id":2}}}}',
])
def test_repair_ambiguous_or_nonminimal_suffix_stays_rejected(tmp_path, content):
    _, result, _ = repair(tmp_path, content)
    assert result is None


def test_recovered_repair_still_passes_mandatory_tool_safety_gate(tmp_path):
    _, decision, _ = repair(tmp_path, envelope() + "}")
    class WriteTool:
        spec = ToolSpec(name="custom.inspect", type="local_tool", description="Write-capable test.",
            read_only=False, input_schema={"identifier": "string"})
        def invoke(self, *, invocation, context):
            pytest.fail("Repair must never bypass mandatory safety review")
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="completed")
    registry = ToolRegistry()
    registry.register_tool(WriteTool())
    result = ToolExecutor(registry).execute(invocation_id="recovered", tool_name=decision["tool_name"],
        tool_input=decision["tool_input"], context=ToolContext(session_id="test", run_id="test"))
    assert result.status == "rejected" and result.execution_started is False
