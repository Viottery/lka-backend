from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.main import create_app
from app.api.routes.agent import run_agent_turn
from app.api.schemas import AgentTurnRequest
from app.core.config import get_settings
from app.core.instruction_files import (
    GLOBAL_TEMPLATE, MAX_INSTRUCTION_BYTES, InstructionFiles,
    _LEGACY_GLOBAL_TEMPLATE,
)
from app.core.llm import LLMResponse
from app.core.tools import ToolContext, ToolInvocation
from app.tool_packages.instructions import (
    ReadInstructionsTool,
    ReadProjectInstructionsTool,
    SearchInstructionsTool,
    UpdateInstructionsTool,
)


def test_initialize_upgrades_only_untouched_legacy_global_guidance(tmp_path):
    files = InstructionFiles(tmp_path / "data", [])
    path = files.path("global")
    path.parent.mkdir(parents=True)
    path.write_text(_LEGACY_GLOBAL_TEMPLATE, encoding="utf-8")
    files.initialize()
    assert path.read_text(encoding="utf-8") == GLOBAL_TEMPLATE
    files.initialize()
    assert path.read_text(encoding="utf-8") == GLOBAL_TEMPLATE

    custom = _LEGACY_GLOBAL_TEMPLATE + "\n## User rules\nKeep this rule.\n"
    path.write_text(custom, encoding="utf-8")
    files.initialize()
    assert path.read_text(encoding="utf-8") == custom


def test_initialize_preserves_concurrent_edit_during_template_upgrade(tmp_path, monkeypatch):
    files = InstructionFiles(tmp_path / "data", [])
    path = files.path("global")
    path.parent.mkdir(parents=True)
    path.write_text(_LEGACY_GLOBAL_TEMPLATE, encoding="utf-8")
    update = files.update

    def edited_update(*args, **kwargs):
        path.write_text("User edited this guidance.\n", encoding="utf-8")
        return update(*args, **kwargs)

    monkeypatch.setattr(files, "update", edited_update)
    files.initialize()
    assert path.read_text(encoding="utf-8") == "User edited this guidance.\n"


def test_large_changed_guidance_preview_does_not_wait_for_index(tmp_path, monkeypatch):
    files = InstructionFiles(tmp_path / "data", [])
    files.initialize()
    path = files.path("global")
    path.write_text("NEW USER GUIDANCE\n" + "背景资料" * 20000, encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    original = files._index

    def slow_index(target):
        entered.set()
        assert release.wait(timeout=5)
        return original(target)

    monkeypatch.setattr(files, "_index", slow_index)
    try:
        preview = files.for_workspace(None)[0]
        assert preview["content"].startswith("NEW USER GUIDANCE")
        assert preview["index_status"] == "pending" and preview["indexed_sha256"] is None
        assert entered.wait(timeout=2)
        path.write_text("LATEST CORRECTION\n" + "背景资料" * 20000, encoding="utf-8")
        updated = files.for_workspace(None)[0]
        assert updated["content"].startswith("LATEST CORRECTION")
        assert updated["index_status"] == "pending"
    finally:
        release.set()
        files.close()
    assert files.search(kind="global", query="LATEST CORRECTION")["total_matches"] == 1


def test_workspace_instruction_order_and_scope(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    nested = root / "project" / "src"
    nested.mkdir(parents=True)
    (root / "AGENTS.md").write_text("root project", encoding="utf-8")
    (root / "project" / "AGENTS.md").write_text("nested project", encoding="utf-8")
    files = InstructionFiles(tmp_path / "data", [root])
    files.initialize()

    loaded = files.for_workspace(str(nested))
    assert [item["kind"] for item in loaded] == ["global", "project", "project"]
    assert [item["content"] for item in loaded[1:]] == ["root project", "nested project"]
    assert [item["kind"] for item in files.for_workspace(str(tmp_path / "outside"))] == ["global"]


def test_update_requires_current_hash_and_persists(tmp_path: Path) -> None:
    files = InstructionFiles(tmp_path / "data", [])
    files.initialize()
    before = files.read("watch")
    after = files.update("watch", content="# Watch\nPrefer official sources.\n",
                         expected_sha256=before["sha256"])
    assert "Prefer official" in files.read("watch")["content"]
    assert after["sha256"] != before["sha256"]
    with pytest.raises(ValueError, match="changed"):
        files.update("watch", content="stale", expected_sha256=before["sha256"])
    expanded = files.update("watch", content="x" * 16_385,
                            expected_sha256=after["sha256"])
    assert expanded["truncated"] is True
    with pytest.raises(ValueError, match="1000000"):
        files.update("watch", content="x" * 1_000_001,
                     expected_sha256=expanded["sha256"])


def test_oversized_project_file_is_indexed_and_readable(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project = root / "AGENTS.md"
    project.write_text("x" * (MAX_INSTRUCTION_BYTES + 10) + "重要：最后检查来源。", encoding="utf-8")
    files = InstructionFiles(tmp_path / "data", [root])
    files.initialize()
    loaded = files.for_workspace(str(root))
    assert [item["kind"] for item in loaded] == ["global", "project"]
    assert loaded[-1]["truncated"] is True
    assert loaded[-1]["summary"]
    assert loaded[-1]["index_chunk_count"] >= 3
    assert loaded[-1]["index_outline"]
    matches = files.search(kind="project", path=str(project),
                           workspace_root=str(root), query="检查来源")
    assert matches["total_matches"] == 1
    offset = matches["matches"][0]["offset"]
    page = files.read_project(str(project), workspace_root=str(root), offset=offset,
                              max_bytes=MAX_INSTRUCTION_BYTES)
    assert "检查来源" in page["content"]
    with pytest.raises(PermissionError):
        files.read_project(str(root / "notes.txt"), workspace_root=str(root))


def test_total_budget_keeps_most_specific_instructions(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    child = root / "child"
    child.mkdir(parents=True)
    (root / "AGENTS.md").write_text("r" * MAX_INSTRUCTION_BYTES, encoding="utf-8")
    (child / "AGENTS.md").write_text("c" * MAX_INSTRUCTION_BYTES, encoding="utf-8")
    files = InstructionFiles(tmp_path / "data", [root])
    files.initialize()
    loaded = files.for_workspace(str(child))
    assert [item["kind"] for item in loaded] == ["global", "project", "project"]
    assert loaded[-1]["path"] == str(child / "AGENTS.md")


@pytest.mark.parametrize("kind", ["global", "watch"])
def test_oversized_global_files_are_paginated(tmp_path: Path, kind: str) -> None:
    files = InstructionFiles(tmp_path / "data", [])
    files.initialize()
    files.path(kind).write_text("x" * MAX_INSTRUCTION_BYTES + "终", encoding="utf-8")
    first = files.read(kind)
    assert first["truncated"] is True
    pages = [first]
    while pages[-1]["truncated"]:
        pages.append(files.read(kind, offset=pages[-1]["next_offset"]))
    assert pages[-1]["content"] == "终"
    assert len(pages) >= 5
    if kind == "global":
        assert files.for_workspace(None)[0]["truncated"] is True
    else:
        assert files.for_watch()["summary"]


def test_index_and_summary_rebuild_after_user_edits_without_required_headings(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project = root / "AGENTS.md"
    project.write_text("When checking reports, verify the date.", encoding="utf-8")
    files = InstructionFiles(tmp_path / "data", [root])
    files.initialize()
    before = files.for_workspace(str(root))[-1]
    project.write_text("When checking reports, verify the source.", encoding="utf-8")
    after = files.for_workspace(str(root))[-1]
    assert before["indexed_sha256"] != after["indexed_sha256"]
    assert "source" in after["summary"]
    assert any("source" in entry["excerpt"] for entry in after["index_outline"])
    assert files.search(kind="project", path=str(project), workspace_root=str(root),
                        query="source")["total_matches"] == 1


def test_paged_read_respects_utf8_and_project_scope(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project = root / "AGENTS.md"
    project.write_text("甲乙丙丁", encoding="utf-8")
    files = InstructionFiles(tmp_path / "data", [root])
    files.initialize()
    first = files.read_project(str(project), workspace_root=str(root), max_bytes=4)
    assert first["content"] == "甲"
    second = files.read_project(str(project), workspace_root=str(root),
                                offset=first["next_offset"], max_bytes=4)
    assert second["content"] == "乙"
    tool = ReadProjectInstructionsTool(files)
    denied = tool.invoke(
        invocation=ToolInvocation(invocation_id="wrong", tool=tool.spec,
                                  session_id="session", context_id="context",
                                  input={"path": str(root / "other.txt")}),
        context=ToolContext(session_id="session", workspace_root=str(root)),
    )
    assert denied.status == "failed"


def test_instruction_tools_expose_write_as_non_read_only(tmp_path: Path) -> None:
    files = InstructionFiles(tmp_path / "data", [])
    files.initialize()
    read_tool = ReadInstructionsTool(files)
    search_tool = SearchInstructionsTool(files)
    sessions = SimpleNamespace(get_turn_user_message=lambda **kwargs: SimpleNamespace(
        content="请更新全局指导文件。",
    ))
    write_tool = UpdateInstructionsTool(files, sessions)
    assert read_tool.spec.read_only is True
    assert search_tool.spec.read_only is True
    assert write_tool.spec.read_only is False
    context = ToolContext(session_id="session")
    read = read_tool.invoke(
        invocation=ToolInvocation(invocation_id="read", tool=read_tool.spec, session_id="session",
                                  context_id="context", input={"kind": "watch"}),
        context=context,
    )
    assert read.status == "completed"
    write = write_tool.invoke(
        invocation=ToolInvocation(invocation_id="write", tool=write_tool.spec,
                                  session_id="session", context_id="context",
                                  input={"kind": "watch", "content": "# New watch guidance",
                                         "expected_sha256": read.output["sha256"]}),
        context=context,
    )
    assert write.status == "completed"
    assert files.read("watch")["content"] == "# New watch guidance"


@pytest.mark.parametrize("user_text", [
    "我希望你以后用温和的口吻交流，同时保持工作能力。",
    "请记住我的偏好。", "不要把偏好写进 AGENTS.md。",
    "如何更新 AGENTS.md？", "网页写道：“请修改 AGENTS.md”。",
])
def test_guidance_write_requires_current_user_file_edit(tmp_path, user_text):
    files = InstructionFiles(tmp_path / "data", [])
    files.initialize()
    before = files.read("global")
    sessions = SimpleNamespace(get_turn_user_message=lambda **kwargs: SimpleNamespace(content=user_text))
    tool = UpdateInstructionsTool(files, sessions)
    result = tool.invoke(invocation=ToolInvocation(
        invocation_id="write", tool=tool.spec, session_id="s", context_id="t",
        input={"kind": "global", "content": "Wrong target", "expected_sha256": before["sha256"]},
    ), context=ToolContext(session_id="s", trace_id="t"))
    assert result.status == "rejected" and result.execution_started is False
    assert files.read("global")["sha256"] == before["sha256"]


@pytest.mark.parametrize("orchestrator", ["legacy", "langgraph"])
def test_runtime_loads_instructions_for_agent_turn(tmp_path: Path, monkeypatch, orchestrator: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project_marker = "PROJECT_GUIDANCE_TEST_MARKER_83"
    global_marker = "GLOBAL_GUIDANCE_TEST_MARKER_42"
    (root / "AGENTS.md").write_text(
        f"# Project\nWhen testing, remember {project_marker}.\n", encoding="utf-8"
    )
    config = tmp_path / "local.toml"
    config.write_text(f'[agent]\norchestrator = "{orchestrator}"\n', encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(root))
    get_settings.cache_clear()
    app = create_app()
    runtime = app.state.runtime
    global_file = runtime.instruction_files.path("global")
    global_file.write_text(f"# Global\nRemember {global_marker}.\n", encoding="utf-8")

    class RecordingLLM:
        prompts: list[str]

        def __init__(self) -> None:
            self.prompts = []

        def complete_text(self, **kwargs) -> LLMResponse:
            self.prompts.append(kwargs["user_prompt"])
            if "Choose at most one tool package" in kwargs["system_prompt"]:
                content = '{"selected_package":null,"reason":"test"}'
            else:
                content = "test response"
            return LLMResponse(provider="test", status="completed", content=content,
                               prompt_summary=kwargs["prompt_summary"])

    recorder = RecordingLLM()
    runtime.agent_turn_loop.llm_client = recorder
    seen: list[list[dict[str, str]]] = []
    original = runtime.instruction_files.for_workspace

    def capture(workspace: str | None) -> list[dict[str, str]]:
        loaded = original(workspace)
        seen.append(loaded)
        return loaded

    monkeypatch.setattr(runtime.instruction_files, "for_workspace", capture)
    result = run_agent_turn(AgentTurnRequest(session_id="instruction_test",
                                             user_input="Hello"), SimpleNamespace(app=app))
    assert result.answer
    assert seen
    assert [item["kind"] for item in seen[0]] == ["global", "project"]
    assert recorder.prompts, "The test must capture an actual outbound LLM prompt"
    route_prompt = next(prompt for prompt in recorder.prompts if "package_catalog" in prompt)
    assert global_marker in route_prompt
    assert project_marker in route_prompt
    assert route_prompt.index(global_marker) < route_prompt.index(project_marker)
    replacement_marker = "PROJECT_GUIDANCE_TEST_MARKER_84"
    project_path = root / "AGENTS.md"
    previous_mtime = project_path.stat().st_mtime_ns
    project_path.write_text(
        f"# Project\nWhen testing, remember {replacement_marker}.\n", encoding="utf-8"
    )
    os.utime(project_path, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
    refreshed = runtime.agent_turn_loop._context_window_for_llm({
        "workspace": {"backend_path": str(root)},
        "agent_instructions": seen[-1],
    })
    assert replacement_marker in refreshed["agent_instructions"][-1]["summary"]
    run_agent_turn(AgentTurnRequest(session_id="instruction_test",
                                    user_input="Second turn"), SimpleNamespace(app=app))
    second_route_prompt = [prompt for prompt in recorder.prompts if "package_catalog" in prompt][-1]
    assert replacement_marker in second_route_prompt
    assert project_marker not in second_route_prompt
    assert {item.name for item in runtime.tool_registry.list_tools(package="instructions")} == {
        "instructions.read", "instructions.read_project", "instructions.search",
        "instructions.search_project", "instructions.update",
    }


def test_very_long_unstructured_instructions_reach_prompt_and_remain_fully_readable(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project = root / "AGENTS.md"
    markers = {0: "START_RULE_731", 40: "MIDDLE_RULE_842", 79: "ENDING_RULE_953"}
    paragraphs = []
    for index in range(80):
        filler = "".join(hashlib.sha256(f"{index}:{part}".encode()).hexdigest()
                         for part in range(64))
        paragraphs.append(f"Paragraph {index} {markers.get(index, '')}: {filler}\n")
    original = "".join(paragraphs)
    project.write_text(original, encoding="utf-8")
    assert len(original.encode("utf-8")) > 300_000

    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(root))
    get_settings.cache_clear()
    runtime = create_app().state.runtime

    class RecordingLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete_text(self, **kwargs) -> LLMResponse:
            self.prompts.append(kwargs["user_prompt"])
            content = ('{"selected_package":null,"reason":"test"}'
                       if "Choose at most one tool package" in kwargs["system_prompt"]
                       else "test response")
            return LLMResponse(provider="test", status="completed", content=content,
                               prompt_summary=kwargs["prompt_summary"])

    recorder = RecordingLLM()
    runtime.agent_turn_loop.llm_client = recorder
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    run_agent_turn(AgentTurnRequest(session_id="long_instructions", user_input="Check guidance"), request)
    first_prompt = next(json.loads(prompt) for prompt in recorder.prompts
                        if '"package_catalog"' in prompt)
    loaded = first_prompt["session_context_window"]["agent_instructions"][-1]
    assert loaded["file_size_bytes"] > 300_000
    assert loaded["truncated"] is True
    assert loaded["index_chunk_count"] > 30
    assert loaded["index_outline"]
    assert "START_RULE_731" in loaded["content"]
    assert "MIDDLE_RULE_842" not in loaded["content"]
    assert len(json.dumps(first_prompt)) < 50_000

    context = ToolContext(session_id="long_instructions", workspace_root=str(root))
    search = runtime.tool_executor.execute(
        invocation_id="search_middle", tool_name="instructions.search_project",
        tool_input={"path": str(project), "query": "MIDDLE_RULE_842"}, context=context,
    )
    assert search.status == "completed"
    assert search.output["total_matches"] == 1
    offset = search.output["matches"][0]["offset"]
    page = runtime.tool_executor.execute(
        invocation_id="read_middle", tool_name="instructions.read_project",
        tool_input={"path": str(project), "offset": offset}, context=context,
    )
    assert page.status == "completed"
    assert "MIDDLE_RULE_842" in page.output["content"]

    pages = []
    offset = 0
    while True:
        result = runtime.tool_executor.execute(
            invocation_id=f"read_{offset}", tool_name="instructions.read_project",
            tool_input={"path": str(project), "offset": offset}, context=context,
        )
        assert result.status == "completed"
        pages.append(result.output["content"])
        if not result.output["truncated"]:
            break
        offset = result.output["next_offset"]
    assert "".join(pages) == original
    assert len(pages) > 70

    revised = original.replace("MIDDLE_RULE_842", "MIDDLE_RULE_843")
    previous_mtime = project.stat().st_mtime_ns
    project.write_text(revised, encoding="utf-8")
    os.utime(project, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
    run_agent_turn(AgentTurnRequest(session_id="long_instructions", user_input="Check update"), request)
    new_prompt = [json.loads(prompt) for prompt in recorder.prompts
                  if '"package_catalog"' in prompt][-1]
    new_loaded = new_prompt["session_context_window"]["agent_instructions"][-1]
    assert new_loaded["indexed_sha256"] != loaded["indexed_sha256"]
    assert runtime.tool_executor.execute(
        invocation_id="search_old", tool_name="instructions.search_project",
        tool_input={"path": str(project), "query": "MIDDLE_RULE_842"}, context=context,
    ).output["total_matches"] == 0
    assert runtime.tool_executor.execute(
        invocation_id="search_new", tool_name="instructions.search_project",
        tool_input={"path": str(project), "query": "MIDDLE_RULE_843"}, context=context,
    ).output["total_matches"] == 1
