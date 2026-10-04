"""A read-command name alone must not bypass review for writing arguments."""

import pytest

from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolContext
from app.tool_packages.bash import is_read_only_command
from tests.test_bash_tools import _runtime


@pytest.mark.parametrize("command", [
    "sed -i s/alpha/beta/ notes.txt", "sed -ni 1p notes.txt",
    "sed '1e touch marker' notes.txt", "sed -f script.sed notes.txt",
    "find . -delete", "find . -exec touch marker +", "find . -execdir touch marker +",
    "find . -fprintf marker %p", "rg --pre=touch needle .", "rg --hostname-bin touch needle .",
    "sort -o marker notes.txt", "sort -ro marker notes.txt", "sort --compress-program=touch notes.txt",
    "uniq notes.txt marker", "file -C -m magic", "git show --output=marker HEAD",
    "git diff --ext-diff", "git show --textconv HEAD", "git branch new-name",
    "git branch -D old-name", "./cat notes.txt", "awk 'BEGIN {print 1}'",
    "find . $UNTRUSTED_FLAG", "ls $UNTRUSTED_PATH", "find . *", "find . {-delete,-print}",
    "sort --out=marker notes.txt", "file --comp -m magic", "git show --out=marker HEAD",
])
def test_writing_programmable_or_unknown_invocations_are_not_proven_readonly(command):
    assert is_read_only_command(command) is False


@pytest.mark.parametrize("command", [
    "pwd", "ls -la | head -5", "find . -type f -maxdepth 3", "rg -n needle .",
    "grep -n needle notes.txt", "sed -n 1,20p notes.txt", "sort -r notes.txt",
    "uniq -c notes.txt", "uniq -f 2 -s 1 notes.txt", "git branch --show-current",
    "git branch --list 'feature-*'", "git show HEAD", "git diff --stat",
    'ls "$WORKSPACE_ROOT/nested"', "find . -name '*.txt'", "rg --pretty needle .",
    "sed -n '1,$p' notes.txt",
])
def test_ordinary_read_arguments_remain_available(command):
    assert is_read_only_command(command) is True


def test_read_scope_cannot_edit_through_sed_but_approved_parent_can(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    path = workspace / "notes.txt"
    path.write_text("alpha\n", encoding="utf-8")
    frozen = ToolView(snapshot_id="readonly", allowed_tools=("bash.run",),
                      allowed_packages=("bash",), side_effect_level=SideEffectLevel.READ,
                      full_workspace_authority=True)
    rejected = runtime.tool_executor.execute(
        invocation_id="read-cannot-edit", tool_name="bash.run",
        tool_input={"command": "sed -i s/alpha/beta/ notes.txt", "cwd": str(workspace)},
        context=ToolContext(session_id="read", tool_view=frozen, safety_review_approved=True),
    )
    assert rejected.status == "rejected" and rejected.execution_started is False
    assert path.read_text(encoding="utf-8") == "alpha\n"
    assert frozen.side_effect_level == SideEffectLevel.READ
    pending = runtime.tool_executor.execute(
        invocation_id="parent-needs-review", tool_name="bash.run",
        tool_input={"command": "sed -i s/alpha/beta/ notes.txt", "cwd": str(workspace)},
        context=ToolContext(session_id="parent"),
    )
    assert pending.status == "rejected" and pending.output["safety_review_required"] is True
    approved = runtime.tool_executor.execute(
        invocation_id="parent-approved", tool_name="bash.run",
        tool_input={"command": "sed -i s/alpha/beta/ notes.txt", "cwd": str(workspace)},
        context=ToolContext(session_id="parent", safety_review_approved=True, safety_review_id="review"),
    )
    assert approved.status == "completed" and approved.output["read_only"] is False
    assert path.read_text(encoding="utf-8") == "beta\n"
