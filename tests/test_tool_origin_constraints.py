from types import SimpleNamespace

from app.core.tool_constraints import ToolConstraintStore
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry, ToolResult, ToolSpec
from app.tool_packages.messages import MESSAGE_ORIGIN_CONSTRAINT, register_message_constraints


class FakeTool:
    def __init__(self, spec):
        self.spec = spec
        self.called = 0

    def invoke(self, *, invocation, context):
        self.called += 1
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="completed")


def registry():
    value = ToolRegistry()
    register_message_constraints(value)
    for name, options in [
        ("source", {"read_only": True, "origin_constraints": (MESSAGE_ORIGIN_CONSTRAINT,)}),
        ("mutation", {"read_only": False, "effect_domains": ("matter",)}),
        ("process", {"read_only": True, "unrestricted_execution": True}),
        ("other", {"read_only": False, "effect_domains": ("other",)}),
    ]:
        value.register_tool(FakeTool(ToolSpec(name=name, type="local_tool", description=name, **options)))
    return value


def invoke(executor, name, session="s", run=None):
    return executor.execute(invocation_id="i", tool_name=name, tool_input={},
                            context=ToolContext(session_id=session, run_id=run, safety_review_approved=True))


def test_source_constraints_block_even_approved_unattributed_mutations_and_process():
    executor = ToolExecutor(registry())
    assert invoke(executor, "mutation").status == "completed"
    assert invoke(executor, "source").status == "completed"
    assert invoke(executor, "mutation").status == "rejected"
    assert invoke(executor, "process").status == "rejected"
    assert invoke(executor, "other").status == "completed"
    assert invoke(executor, "mutation", session="unrelated").status == "completed"


def test_constraints_survive_executor_and_model_context_replacement(tmp_path):
    import sqlite3
    factory = lambda: sqlite3.connect(tmp_path / "test.sqlite")
    first = ToolExecutor(registry())
    first.constraint_store = ToolConstraintStore(factory)
    invoke(first, "source")
    second = ToolExecutor(registry())
    second.constraint_store = ToolConstraintStore(factory)
    assert invoke(second, "mutation").status == "rejected"


def test_child_read_taints_parent_session_and_siblings():
    executor = ToolExecutor(registry())
    runs = {
        "parent": SimpleNamespace(session_id="root-session", parent_run_id=None),
        "child": SimpleNamespace(session_id="child-session", parent_run_id="parent"),
        "sibling": SimpleNamespace(session_id="sibling-session", parent_run_id="parent"),
    }
    executor.run_manager = SimpleNamespace(get_run=lambda key: runs.get(key))
    executor._run_guard = lambda **kwargs: None
    assert invoke(executor, "source", session="child-session", run="child").status == "completed"
    assert invoke(executor, "mutation", session="root-session", run="parent").status == "rejected"
    assert invoke(executor, "process", session="sibling-session", run="sibling").status == "rejected"


def test_bash_environment_does_not_expose_control_tokens(monkeypatch):
    from pathlib import Path

    from app.tool_packages.bash import BashAccessPolicy, BashRunTool
    for name in ("LKA_MESSAGES_CONTROL_TOKEN", "LKA_MESSAGES_API_TOKEN", "LKA_MESSAGES_IMPORT_TOKEN"):
        monkeypatch.setenv(name, "secret")
    tool = BashRunTool(policy=BashAccessPolicy(roots=[Path("/tmp")]), session_manager=None)
    assert not any(key.startswith("LKA_MESSAGES_") for key in tool._env(tool.policy))


def test_trusted_resolver_commits_before_execution_even_on_failure():
    registered = registry()
    executor = ToolExecutor(registered)

    class DynamicSource(FakeTool):
        def resolve_origin_constraints(self, *, invocation, context):
            return [MESSAGE_ORIGIN_CONSTRAINT]

        def invoke(self, *, invocation, context):
            assert invoke(executor, "mutation").status == "rejected"
            raise ValueError("source failed after beginning")

    registered.register_tool(DynamicSource(ToolSpec(name="dynamic", type="local_tool", description="source", read_only=True)))
    assert invoke(executor, "dynamic").status == "failed"
    assert invoke(executor, "mutation").status == "rejected"


def test_malformed_registered_resolver_fails_before_data_exposure():
    registered = registry()

    class BadSource(FakeTool):
        def resolve_origin_constraints(self, *, invocation, context):
            return "not a list"

    source = BadSource(ToolSpec(name="bad", type="local_tool", description="source", read_only=True))
    registered.register_tool(source)
    assert invoke(ToolExecutor(registered), "bad").status == "failed"
    assert source.called == 0
