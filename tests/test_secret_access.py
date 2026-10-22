"""Agent shells never inherit credentials; credential reads always need a human."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.tools import tool_manual_review_reason
from app.platform.sensitive_paths import is_secret_env_name, scrub_secret_env, sensitive_path_reason
from app.tool_packages.bash import BashAccessPolicy, BashRunTool, BashSessionManager
from app.tool_packages.filesystem import FileAccessPolicy, ListDirectoryTool, ReadFileTool


@pytest.fixture
def bash(tmp_path):
    return BashRunTool(policy=BashAccessPolicy.from_workspace_roots([tmp_path]), session_manager=BashSessionManager())


def test_agent_shell_env_drops_provider_keys_and_agent_sockets(bash, monkeypatch, tmp_path):
    for name in ("PACKY_API_KEY", "BRAVE_SEARCH_API_KEY", "LKA_MESSAGES_CONTROL_TOKEN",
                 "MAIL_IMAP_PASSWORD", "SSH_AUTH_SOCK", "GITHUB_TOKEN"):
        monkeypatch.setenv(name, "secret-value")
    monkeypatch.setenv("LANG", "C.UTF-8")
    for read_only in (True, False):
        env = bash._env(bash.policy, read_only=read_only)
        assert not any(value == "secret-value" for value in env.values())
        assert env["LANG"] == "C.UTF-8" and env["WORKSPACE_ROOT"] == tmp_path.resolve().as_posix()


def test_real_shell_run_cannot_print_a_provider_key(bash, monkeypatch, tmp_path):
    monkeypatch.setenv("PACKY_API_KEY", "sk-should-not-leak")
    payload = bash._run_sync(command="echo \"[$PACKY_API_KEY]\"", cwd=tmp_path, read_only=True,
                             policy=bash.policy, timeout_seconds=10, max_output_bytes=4096)
    assert payload["stdout"].strip() == "[]"


@pytest.mark.parametrize("command", [
    "cat .env", "cat ./sub/.env.local", "cat ~/.ssh/id_rsa", "head -c 9 ~/.aws/credentials",
    "cat config/local.toml", "cat certs/server.pem", "printenv", "printenv PACKY_API_KEY", "env",
    "head /proc/self/environ", "rg KEY .env",
])
def test_credential_reads_force_manual_review(bash, command):
    assert tool_manual_review_reason(bash, {"command": command})


@pytest.mark.parametrize("command", [
    "cat README.md", "cat .env.example", "rg TODO src", "ls -la", "git status", "env FOO=1 pytest -q",
])
def test_ordinary_commands_keep_their_review_mode(bash, command):
    assert tool_manual_review_reason(bash, {"command": command}) is None


def test_read_file_on_credentials_forces_manual_review(tmp_path):
    policy = FileAccessPolicy.from_workspace_roots([tmp_path])
    reader = ReadFileTool(policy)
    assert tool_manual_review_reason(reader, {"path": ".env"})
    assert tool_manual_review_reason(reader, {"path": "~/.ssh/id_ed25519"})
    assert tool_manual_review_reason(reader, {"path": "notes/plan.md"}) is None
    # Listing names never reads contents, so it keeps the normal review mode.
    assert tool_manual_review_reason(ListDirectoryTool(policy), {"path": "~/.ssh"}) is None


def test_helpers():
    assert is_secret_env_name("OPENAI_API_KEY") and not is_secret_env_name("PATH")
    assert scrub_secret_env({"A_TOKEN": "x", "HOME": "/h"}) == {"HOME": "/h"}
    assert sensitive_path_reason(str(Path("project") / ".env")) and not sensitive_path_reason("src/env.py")
