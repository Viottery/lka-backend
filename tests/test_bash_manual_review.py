"""Explicit destructive shell commands stay human reviewed in every mode."""

import pytest

from app.tool_packages.bash_manual_review import command_manual_review_reason


@pytest.mark.parametrize("command", [
    "rm -rf /tmp/project",
    "rm -fr /tmp/project",
    "rm -R /tmp/project",
    "rm -r -f /tmp/project",
    "rm --recursive /tmp/project",
    "/bin/rm -rf /tmp/project",
    "command rm -rf /tmp/project",
    "env rm -rf /tmp/project",
    "env -- rm -rf /tmp/project",
    "sudo rm -rf /tmp/project",
    "sudo -u root rm -rf /tmp/project",
    "pwd; rm -rf /tmp/project",
    "pwd\nrm -rf /tmp/project",
    "pwd && rm -rf /tmp/project",
    "bash -lc 'rm -rf /tmp/project'",
    'bash -lc "rm -rf /tmp/project"',
    "bash -c $'rm -rf /tmp/project'",
    "echo $(rm -rf /tmp/project)",
    'echo "$(rm -rf /tmp/project)"',
    "echo `rm -rf /tmp/project`",
    'echo "`rm -rf /tmp/project`"',
    '''echo "$(printf ')'; rm -rf /tmp/project)"''',
    "trap 'rm -rf /tmp/project' EXIT",
    "cmd /c rd /s x",
    "pwsh -Command Remove-Item -Recurse x",
    "Remove-Item -Recurse C:\\temp\\project",
    "Remove-Item -r C:\\temp\\project",
    "rd /s /q C:\\temp\\project",
    "rmdir /s C:\\temp\\project",
    "del /s C:\\temp\\project",
    "mkfs.ext4 /dev/sda",
    "wipefs -a /dev/sda",
    "diskpart /s wipe-disk.txt",
    "format X:",
    "shred -u private.db",
    "dd if=/dev/zero of=/dev/sda",
    "git reset --hard",
    "git -C /tmp/repo reset --hard",
    "git -c advice.detachedHead=false reset --hard",
    "git clean -fdx",
    "git -C /tmp/repo clean -fdx",
    "git -c clean.requireForce=false clean -fdx",
])
def test_destructive_commands_are_forced_to_manual_review(command):
    reason = command_manual_review_reason(command)
    assert reason, command


@pytest.mark.parametrize("command", [
    "echo 'rm -rf /tmp/project'",
    'echo "rm -rf /tmp/project"',
    "echo rm '-rf' /tmp/project",
    "printf '%s\\n' 'Remove-Item -Recurse C:\\temp\\project'",
    "echo 'git reset --hard'",
    "echo '$(rm -rf /tmp/project)'",
    "echo '`rm -rf /tmp/project`'",
    "pwd",
    "ls -la",
    "find . -type f -maxdepth 3",
    "git status --short",
    "bash -lc 'printf hello'",
    "trap 'printf hello' EXIT",
])
def test_benign_literals_and_ordinary_commands_are_not_blacklisted(command):
    assert command_manual_review_reason(command) is None

