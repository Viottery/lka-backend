from app.core.context_driver import ToolView
from app.core.multi_agent import MemoryReference, SideEffectLevel
from app.core.tools import ToolContext, ToolInvocation
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput
from app.tool_packages.memory import ReadMemoryTool, SearchMemoryTool


def test_memory_tools_restrict_project_scope_and_retraction(tmp_path):
    service = MemoryService(tmp_path / "db.sqlite3")
    service.ensure_schema()
    project_a = service.resolve_project(tmp_path / "a")
    project_b = service.resolve_project(tmp_path / "b")
    source = service.register_source(MemorySourceInput(
        source_type="user_message", source_ref="message", trusted_source=True,
    ))
    item = service.create(MemoryInput(
        content="Launch decision A", scope="project", project_id=project_a,
        source_id=source, sensitivity="normal",
    ))
    other = service.create(MemoryInput(
        content="Launch decision B", scope="project", project_id=project_b,
        source_id=source, sensitivity="normal",
    ))
    search = SearchMemoryTool(service)
    read = ReadMemoryTool(service)
    context = ToolContext(session_id="s", workspace_root=str(tmp_path / "a"))
    result = search.invoke(
        invocation=ToolInvocation(invocation_id="i", tool=search.spec,
                                  session_id="s", context_id="c", input={"query": "Launch"}),
        context=context,
    )
    assert [row["memory_id"] for row in result.output["memories"]] == [item.memory_id]
    denied = read.invoke(
        invocation=ToolInvocation(invocation_id="j", tool=read.spec,
                                  session_id="s", context_id="c", input={"memory_id": other.memory_id}),
        context=context,
    )
    assert denied.status == "rejected"
    service.retract(item.memory_id, expected_version=item.version)
    result = search.invoke(
        invocation=ToolInvocation(invocation_id="k", tool=search.spec,
                                  session_id="s", context_id="c", input={"query": "Launch"}),
        context=context,
    )
    assert result.output["memories"] == []


def _child_context(refs=()):
    view = ToolView(
        snapshot_id="snapshot-child", side_effect_level=SideEffectLevel.NONE,
        memory_refs=tuple(refs),
    )
    return ToolContext(session_id="child-session", tool_view=view)


def _reference(record, *, content=None, version=None):
    return MemoryReference(
        memory_id=record.memory_id,
        version=record.version if version is None else version,
        content=record.content if content is None else content,
        scope=record.scope, project_id=record.project_id,
        source_ids=tuple(record.source_ids), updated_at=record.updated_at,
    )


def _invoke(tool, context, tool_input, invocation_id="child-call"):
    return tool.invoke(
        invocation=ToolInvocation(
            invocation_id=invocation_id, tool=tool.spec, session_id=context.session_id,
            context_id="child-context", input=tool_input,
        ),
        context=context,
    )


def test_child_memory_tools_search_and_read_only_explicit_frozen_refs(tmp_path):
    service = MemoryService(tmp_path / "db.sqlite3")
    service.ensure_schema()
    source = service.register_source(MemorySourceInput(
        source_type="user_message", source_ref="frozen-source", trusted_source=True,
    ))
    selected = service.create(MemoryInput(
        content="Original frozen preference", source_id=source, sensitivity="normal",
    ))
    unassigned = service.create(MemoryInput(
        content="Unassigned matching preference", source_id=source, sensitivity="normal",
    ))
    ref = _reference(selected)
    context = _child_context((ref,))
    search = _invoke(SearchMemoryTool(service), context, {"query": "frozen preference"})
    assert [item["memory_id"] for item in search.output["memories"]] == [selected.memory_id]
    assert search.output["memories"][0]["content"] == ref.content
    assert unassigned.memory_id not in {item["memory_id"] for item in search.output["memories"]}

    # Same ID can be updated while this child continues to see its frozen version.
    conn = service._connect()
    try:
        conn.execute("UPDATE memory_entries SET content=?,version=version+1 WHERE memory_id=?",
                     ("New current content", selected.memory_id))
        conn.commit()
    finally:
        conn.close()
    read = _invoke(ReadMemoryTool(service), context, {"memory_id": selected.memory_id})
    assert read.status == "completed"
    assert read.output["memory"]["content"] == "Original frozen preference"
    assert read.output["memory"]["version"] == ref.version


def test_child_refs_without_assignment_or_after_retraction_are_rejected(tmp_path):
    service = MemoryService(tmp_path / "db.sqlite3")
    service.ensure_schema()
    source = service.register_source(MemorySourceInput(
        source_type="user_message", source_ref="revoked-source", trusted_source=True,
    ))
    record = service.create(MemoryInput(
        content="Frozen preference", source_id=source, sensitivity="normal",
    ))
    read_tool = ReadMemoryTool(service)
    denied = _invoke(read_tool, _child_context(), {"memory_id": record.memory_id})
    assert denied.status == "rejected"

    context = _child_context((_reference(record),))
    service.retract(record.memory_id, expected_version=record.version)
    revoked = _invoke(read_tool, context, {"memory_id": record.memory_id})
    assert revoked.status == "rejected"
    searched = _invoke(SearchMemoryTool(service), context, {"query": "Frozen preference"})
    assert searched.output["memories"] == []
