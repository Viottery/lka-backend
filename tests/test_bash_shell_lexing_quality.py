"""Real READ-scope effects must not hide behind shell quoting/comments/config."""

import shlex
import sys
import time
from types import SimpleNamespace

import pytest

from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry
from app.tool_packages.bash import (
    BashAccessPolicy,
    BashRunTool,
    BashSessionManager,
    is_read_only_command,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX lexing; native PowerShell policy is unchanged")


@pytest.fixture
def shell(tmp_path):
    manager = BashSessionManager()
    policy = BashAccessPolicy([tmp_path])
    tool = BashRunTool(policy=policy, session_manager=manager)
    registry = ToolRegistry()
    registry.register_tool(tool)
    view = ToolView(snapshot_id="frozen-read", allowed_tools=("bash.run",),
                    allowed_packages=("bash",), side_effect_level=SideEffectLevel.READ,
                    full_workspace_authority=True, child_run_id="child")

    def execute(command, *, approved=False, read=True, mode="sync"):
        return ToolExecutor(registry).execute(invocation_id="inv", tool_name="bash.run",
            tool_input={"command": command, "cwd": str(tmp_path), "timeout_seconds": 5, "mode": mode},
            context=ToolContext(session_id="child" if read else "parent", tool_view=view if read else None,
                                safety_review_approved=approved, safety_review_id="review" if approved else None))

    yield tmp_path, tool, execute
    close = getattr(manager, "close", None)
    if callable(close):
        close()


@pytest.mark.parametrize("command,output", [
    ("sort input#literal -o marker", "marker"),
    ("uniq input ';'", ";"),
    ("uniq input '|'", "|"),
    ("sort input\u00a0#literal -o marker", "marker"),
])
def test_real_read_scope_cannot_overwrite_through_comment_or_quoted_separator(shell, command, output):
    workspace, _, execute = shell
    (workspace / "input#literal").write_text("b\na\n", encoding="utf-8")
    (workspace / "input\u00a0#literal").write_text("b\na\n", encoding="utf-8")
    (workspace / "input").write_text("a\na\n", encoding="utf-8")
    marker = workspace / output
    marker.write_text("KEEP\n", encoding="utf-8")
    result = execute(command)
    assert marker.read_text(encoding="utf-8") == "KEEP\n"  # Fails with actual effect before fix.
    assert result.status == "rejected" and result.execution_started is False


@pytest.mark.parametrize("command", [
    "uniq input ';' && pwd", "uniq input '|' || pwd", "uniq input \"&&\"",
    "uniq input \\;", "uniq input \\|", "uniq input '# output'",
    "sort input#literal -o marker", "sort input#literal --out=marker",
    "sort 'input#literal' -o marker", "sort input\\#literal -o marker",
    "pwd # comment\nsort input -o marker", "pwd\n# ignore\nuniq input marker",
    "pwd\nls\nsort input -o marker", "pwd\n# ignore\ntouch marker",
    "pwd |", "pwd &&", "pwd;; ls", "pwd & ls",
    "sort input\u00a0#literal -o marker", "sort input\v#literal -o marker",
    "sort input''#literal -o marker", "pwd # comment\n# another\nsort input -o marker",
    "pwd ||", "pwd \\\nls", "cat 'unterminated", "pwd &&; ls",
])
def test_every_real_segment_and_literal_output_operand_is_checked(command):
    assert is_read_only_command(command) is False


@pytest.mark.parametrize("command", [
    "cat ';'", "cat '|'", "cat 'input#literal'", "cat input#literal", "cat '#literal'",
    "cat \\#literal", "cat 'semi;pipe|hash#'", "rg ';|#' input",
    "pwd # touch marker", "pwd # && rm arbitrary", "pwd # 'unterminated quote",
    "pwd # $UNKNOWN > ignored\nls", "pwd\nls", "pwd\n# comment\nls",
    "# initial comment\npwd\n", "pwd; ls", "ls | head -5", "pwd && ls || pwd",
    "sed -n '1,$p' input", "find . -name '*.txt'",
    "pwd;# ignore\nls", "pwd && # ignore\nls", "cat ''#literal",
    "cat 'double\"quote;#'", "cat \"escaped\\\"quote;#\"",
])
def test_quoted_literals_comments_and_readonly_multiline_commands_remain_readonly(command):
    assert is_read_only_command(command) is True


def test_real_comment_does_not_hide_next_line_and_quoted_arguments_are_read(shell):
    workspace, _, execute = shell
    (workspace / "input").write_text("a\na\n", encoding="utf-8")
    (workspace / ";").write_text("literal separator\n", encoding="utf-8")
    result = execute("pwd # ignored command text\ncat ';'")
    assert result.status == "completed" and result.output["exit_code"] == 0
    assert "literal separator" in result.output["stdout"]
    marker = workspace / "marker"
    result = execute("pwd # ignored\nsort input -o marker")
    assert result.status == "rejected" and result.execution_started is False and not marker.exists()


def config_hook(workspace, monkeypatch):
    marker = workspace / "hook-marker"
    script = workspace / "preprocessor.sh"
    script.write_text("#!/bin/sh\nprintf effect > " + shlex.quote(str(marker)) + '\ncat "$1"\n',
                      encoding="utf-8")
    script.chmod(0o700)
    config = workspace / "rg-config"
    config.write_text("--pre=" + str(script) + "\n", encoding="utf-8")
    (workspace / "input").write_text("needle\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    return marker, config


def test_host_ripgrep_config_cannot_run_preprocessor_in_real_read_scope(shell, monkeypatch):
    workspace, _, execute = shell
    marker, _ = config_hook(workspace, monkeypatch)
    result = execute("rg needle input")
    assert not marker.exists()  # Actual implicit --pre effect before fix.
    assert result.status == "completed" and result.output["exit_code"] == 0
    assert result.output["read_only"] is True and "needle" in result.output["stdout"]


@pytest.mark.parametrize("explicit_assignment", [False, True])
def test_approved_nonreadonly_config_semantics_are_preserved(shell, monkeypatch, explicit_assignment):
    workspace, tool, execute = shell
    marker, config = config_hook(workspace, monkeypatch)
    command = ("env RIPGREP_CONFIG_PATH=" + shlex.quote(str(config)) + " rg needle input"
               if explicit_assignment else "env rg needle input")
    assert is_read_only_command(command) is False
    rejected = execute(command, read=False)
    assert rejected.status == "rejected" and not marker.exists()
    assert rejected.output["safety_review_required"] is True
    assert tool._env(tool.policy, read_only=False)["RIPGREP_CONFIG_PATH"] == str(config)
    approved = execute(command, read=False, approved=True)
    assert approved.status == "completed" and approved.output["exit_code"] == 0
    assert approved.output["read_only"] is False and marker.read_text() == "effect"


def test_background_read_scope_also_receives_config_free_env(shell, monkeypatch):
    workspace, tool, _ = shell
    _, _config = config_hook(workspace, monkeypatch)
    captured = []

    def start(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(session_id="fake-session")

    monkeypatch.setattr(tool.session_manager, "start", start)
    result = tool._run_background(command="rg needle input", cwd=workspace, read_only=True,
                                  policy=tool.policy)
    assert result["read_only"] is True
    assert "RIPGREP_CONFIG_PATH" not in captured[0]["env"]


@pytest.mark.parametrize("mode", ["sync", "background"])
@pytest.mark.parametrize("hook_kind", ["startup_file", "exported_function"])
def test_read_scope_does_not_execute_inherited_shell_code(shell, monkeypatch, mode, hook_kind):
    workspace, tool, execute = shell
    marker = workspace / "shell-hook-effect"
    action = "printf effect > " + shlex.quote(str(marker))
    if hook_kind == "startup_file":
        hook = workspace / "startup.sh"
        hook.write_text(action + "\n", encoding="utf-8")
        monkeypatch.setenv("BASH_ENV", str(hook))
        monkeypatch.setenv("ENV", str(hook))
    else:
        monkeypatch.setenv("BASH_FUNC_pwd%%", "() { " + action + "; }")
    result = execute("pwd", mode=mode)
    assert result.status == "completed" and result.output["read_only"] is True
    if mode == "background":
        deadline = time.monotonic() + 5
        session = tool.session_manager.get(result.output["session_id"], owner_run_id="child")
        while session.process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert session.process.poll() == 0
    assert not marker.exists()  # Genuine shell startup/function execution before fix.


def test_reviewed_nonreadonly_retains_explicit_shell_startup_semantics(shell, monkeypatch):
    workspace, _, execute = shell
    marker = workspace / "reviewed-hook-effect"
    hook = workspace / "startup.sh"
    hook.write_text("printf effect > " + shlex.quote(str(marker)) + "\n", encoding="utf-8")
    monkeypatch.setenv("BASH_ENV", str(hook))
    result = execute("env pwd", read=False, approved=True)
    assert result.status == "completed" and result.output["read_only"] is False
    assert marker.read_text() == "effect"
