"""A persisted invocation is not permission to repeat an uncertain side effect."""

from app.core.agent_storage import SqliteAgentRunStore
from app.core.agent_tool_graph import AgentToolLifecycleGraph
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry, ToolSpec


def test_restarted_lifecycle_refuses_uncertain_write_even_after_user_approval(tmp_path):
    class WriteTool:
        spec = ToolSpec(name="test.write", type="test", description="Write", read_only=False)

        def __init__(self):
            self.calls = 0

        def invoke(self, **kwargs):
            self.calls += 1
            raise AssertionError("An uncertain prior external write must not be replayed")

    db = tmp_path / "runs.sqlite3"
    store = SqliteAgentRunStore(db)
    store.claim_tool_invocation(invocation_id="uncertain-write", run_id="run", tool_name="test.write",
                                tool_input={}, claimed_at="2026-10-02T00:00:00+00:00")
    # No completion is stored: the process could have exited after an external
    # service accepted the write, before our local result transaction committed.
    registry = ToolRegistry()
    tool = WriteTool()
    registry.register_tool(tool)
    progress = []
    graph = AgentToolLifecycleGraph(
        tool_executor=ToolExecutor(registry), review_tool_call=lambda *_args: (None, None),
        append_progress=lambda event, *_args: progress.append(event),
        raise_if_cancel_requested=lambda: None, artifact_store=SqliteAgentRunStore(db),
    )
    result = graph.run(invocation_id="uncertain-write", run_id="run", tool_name="test.write",
                       tool_input={}, context=ToolContext(session_id="session", safety_review_approved=True))
    assert result.status == "failed" and result.output["invocation_claim"] == "uncertain"
    assert tool.calls == 0
    assert "tool_execution_uncertain" in progress
