from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest

from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tools import ToolContext, ToolExecutor, ToolInvocation, ToolRegistry
from app.platform.commands import shell_argv
from app.platform.python_environment import (
    MANIFEST_NAME,
    PREPARATION_NAME,
    ManagedPythonEnvironment,
)
from app.tool_packages.bash import BashAccessPolicy, BashRunTool, BashSessionManager
from app.tool_packages.filesystem import CreateFileTool, FileAccessPolicy
from scripts.manage_agent_python import preparation_lock, prepare


def call(tool, values, context=None):
    context = context or ToolContext(session_id="python-test")
    return tool.invoke(invocation=ToolInvocation(invocation_id="inv", tool=tool.spec,
                       session_id=context.session_id, context_id="ctx", input=values), context=context)


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "managed-python"
    environment = ManagedPythonEnvironment(root)
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment.venv)
    lock = root / "requirements.lock"
    lock.write_text("# test environment has no dependencies\n")
    (root / MANIFEST_NAME).write_text(json.dumps({"schema_version": 1, "environment_root": str(root),
        "python_version_request": "3.12", "python_version": platform.python_version(),
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest()}))
    return environment


def test_status_distinguishes_actual_installed_environment(tmp_path, managed):
    missing = ManagedPythonEnvironment(tmp_path / "missing").status(tmp_path)
    assert not missing["available"] and missing["packages"] == {} and missing["python_version"] is None
    actual = managed.status(tmp_path)
    assert actual["available"] and actual["python_version"].startswith("3.")
    assert actual["packages"] == {}  # Backend installed libraries are not inherited.
    assert actual["interpreter"] == str(managed.interpreter)


def test_backend_and_wrong_environment_directories_rejected(tmp_path, managed):
    with pytest.raises(ValueError):
        ManagedPythonEnvironment(Path(sys.prefix))
    with pytest.raises(ValueError):
        ManagedPythonEnvironment(tmp_path / ".venv")
    (managed.root / MANIFEST_NAME).write_text(json.dumps({"schema_version": 1, "environment_root": "/other"}))
    with pytest.raises(ValueError, match="different directory"):
        managed.require_available()


def test_manifest_rejects_modified_lock_and_python_version(managed):
    (managed.root / "requirements.lock").write_text("altered dependencies")
    with pytest.raises(ValueError, match="dependency lock"):
        managed.require_available()


def test_explicit_preparation_resumes_failed_install_without_recreating_venv(tmp_path, monkeypatch):
    environment = ManagedPythonEnvironment(tmp_path / "prepared")
    monkeypatch.setattr("scripts.manage_agent_python.shutil.which", lambda name: "uv")
    actual_run = subprocess.run
    operations = []
    failed = False

    def controlled_run(argv, **kwargs):
        nonlocal failed
        if argv[0] != "uv":
            return actual_run(argv, **kwargs)
        operations.append(argv)
        assert "--no-config" in argv
        assert "OPENAI_API_KEY" not in kwargs["env"]
        if "compile" in argv:
            Path(argv[argv.index("--output-file") + 1]).write_text("# synthetic test dependency lock\n")
        elif "venv" in argv:
            venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(Path(argv[-1]))
        elif "sync" in argv and not failed:
            failed = True
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setattr("scripts.manage_agent_python.subprocess.run", controlled_run)
    with pytest.raises(subprocess.CalledProcessError):
        prepare(environment)
    assert not environment.status(tmp_path)["available"]
    assert prepare(environment)["available"]
    assert prepare(environment)["available"]
    assert sum("compile" in argv for argv in operations) == 1
    assert sum("venv" in argv for argv in operations) == 1
    assert sum("sync" in argv for argv in operations) == 3


def test_preparation_refuses_unrelated_nonempty_directory(tmp_path, monkeypatch):
    root = tmp_path / "unrelated"
    root.mkdir()
    marker = root / "user-data.txt"
    marker.write_text("preserve")
    monkeypatch.setattr("scripts.manage_agent_python.shutil.which", lambda name: "uv")
    with pytest.raises(ValueError, match="unrelated"):
        prepare(ManagedPythonEnvironment(root))
    assert marker.read_text() == "preserve" and len(list(root.iterdir())) == 1


def test_preparation_lock_rejects_concurrent_writer_and_releases_after_failure(tmp_path):
    root = tmp_path / "managed"
    with pytest.raises(ValueError, match="synthetic failure"), preparation_lock(root):
        with pytest.raises(RuntimeError, match="already running"), preparation_lock(root):
            pytest.fail("Concurrent preparation acquired the lock")
        raise ValueError("synthetic failure")
    with preparation_lock(root):
        pass


@pytest.mark.parametrize("stage", ["compile", "venv"])
def test_failed_provisioning_retries_partial_lock_or_venv(tmp_path, monkeypatch, stage):
    environment = ManagedPythonEnvironment(tmp_path / "prepared")
    monkeypatch.setattr("scripts.manage_agent_python.shutil.which", lambda name: "uv")
    actual_run = subprocess.run
    failed = False
    operations = []

    def controlled_run(argv, **kwargs):
        nonlocal failed
        if argv[0] != "uv":
            return actual_run(argv, **kwargs)
        operations.append(argv)
        if "compile" in argv:
            Path(argv[argv.index("--output-file") + 1]).write_text("# synthetic lock\n")
        elif "venv" in argv:
            if stage == "venv" and not failed:
                Path(argv[-1]).mkdir()
            else:
                venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(Path(argv[-1]))
        if stage in argv and not failed:
            failed = True
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("scripts.manage_agent_python.subprocess.run", controlled_run)
    with pytest.raises(subprocess.CalledProcessError):
        prepare(environment)
    if stage == "compile":
        assert not (environment.root / "requirements.lock").exists()
    assert prepare(environment)["available"]
    assert sum(stage in argv for argv in operations) == 2


def test_registered_environment_can_repair_missing_interpreter(managed, monkeypatch):
    managed.interpreter.unlink()
    monkeypatch.setattr("scripts.manage_agent_python.shutil.which", lambda name: "uv")
    actual_run = subprocess.run

    def controlled_run(argv, **kwargs):
        if argv[0] != "uv":
            return actual_run(argv, **kwargs)
        if "venv" in argv:
            venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(Path(argv[-1]))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("scripts.manage_agent_python.subprocess.run", controlled_run)
    assert prepare(managed)["available"]


def test_environment_inspection_blocks_missing_locked_dependency(managed, tmp_path):
    lock = managed.root / "requirements.lock"
    lock.write_text("numpy==2.3.4 \\\n    --hash=sha256:synthetic\n")
    path = managed.root / MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest["lock_sha256"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))
    status = managed.status(tmp_path)
    assert not status["available"] and "numpy==2.3.4" in status["error"]
    with pytest.raises(ValueError, match="Installed dependency"):
        managed.environment(tmp_path)


def test_environment_inspection_blocks_system_packages_and_incomplete_prepare(managed, tmp_path):
    config = managed.venv / "pyvenv.cfg"
    config.write_text(config.read_text().replace("include-system-site-packages = false",
                                              "include-system-site-packages = true"))
    with pytest.raises(ValueError, match="system site packages"):
        managed.environment(tmp_path)
    (managed.root / PREPARATION_NAME).write_text("{}")
    assert "incomplete" in managed.status(tmp_path)["error"]


def test_execution_blocks_version_drift_and_inspection_process_failure(managed, tmp_path, monkeypatch):
    path = managed.root / MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest["python_version"] = platform.python_version() + ".changed"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Python version differs"):
        managed.environment(tmp_path)

    def failing_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10)

    monkeypatch.setattr("app.platform.python_environment.subprocess.run", failing_run)
    with pytest.raises(RuntimeError, match="inspection failed"):
        managed.environment(tmp_path)


def test_inspection_rejects_backend_interpreter_prefix(managed, tmp_path, monkeypatch):
    def substituted_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            "python_version": platform.python_version(), "prefix": sys.prefix,
            "base_prefix": sys.base_prefix, "packages": {},
        }))

    monkeypatch.setattr("app.platform.python_environment.subprocess.run", substituted_run)
    assert "dedicated virtual environment" in managed.status(tmp_path)["error"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell startup profiles")
def test_managed_shell_disables_login_profiles():
    assert shell_argv("pwd") == ["/bin/bash", "-lc", "pwd"]
    assert shell_argv("pwd", load_profile=False) == ["/bin/bash", "--noprofile", "--norc", "-c", "pwd"]


def test_filtered_environment_drops_service_secrets_and_python_hooks(managed, tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "LKA_MESSAGES_CONTROL_TOKEN",
                 "MAIL_PASSWORD", "HTTPS_PROXY", "UV_INDEX_URL", "PYTHONPATH", "BASH_ENV"):
        monkeypatch.setenv(name, "secret")
    env = managed.environment(tmp_path)
    assert not any("secret" == value for value in env.values())
    assert env["LKA_PYTHON"] == str(managed.interpreter)
    assert env["VIRTUAL_ENV"] == str(managed.venv)


def test_python_script_execution_reports_artifact_and_exit_status(tmp_path, managed, monkeypatch):
    create = CreateFileTool(FileAccessPolicy([tmp_path]))
    script = "import os,sys,json\nfrom pathlib import Path\n" \
             "Path('result.json').write_text(json.dumps({'python':sys.executable, 'secret':os.getenv('OPENAI_API_KEY')}))\n" \
             "print('artifact ready')\n"
    assert call(create, {"path": "calculate.py", "content": script}).status == "completed"
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
    manager = BashSessionManager()
    try:
        tool = BashRunTool(policy=BashAccessPolicy([tmp_path]), session_manager=manager, python_environment=managed)
        command = '& $env:LKA_PYTHON calculate.py' if os.name == "nt" else '"$LKA_PYTHON" calculate.py'
        result = call(tool, {"command": command, "python_environment": "default"})
        assert result.status == "completed" and result.output["exit_code"] == 0
        assert result.output["read_only"] is False
        artifact = json.loads((tmp_path / "result.json").read_text())
        assert artifact["python"] == str(managed.interpreter) and artifact["secret"] is None
        failing = '& $env:LKA_PYTHON -c "raise SystemExit(7)"' if os.name == "nt" else '"$LKA_PYTHON" -c "raise SystemExit(7)"'
        assert call(tool, {"command": failing, "python_environment": "default"}).output["exit_code"] == 7
    finally:
        manager.close()


def test_managed_selection_requires_review_and_rejects_other_environment(tmp_path, managed):
    manager = BashSessionManager()
    try:
        tool = BashRunTool(policy=BashAccessPolicy([tmp_path]), session_manager=manager, python_environment=managed)
        assert tool.is_read_only_invocation({"command": "pwd", "python_environment": "default"}) is False
        registry = ToolRegistry()
        registry.register_tool(tool)
        result = ToolExecutor(registry).execute(invocation_id="inv", tool_name="bash.run",
            tool_input={"command": "pwd", "python_environment": "default"}, context=ToolContext(session_id="test"))
        assert result.status == "rejected" and not result.execution_started
        assert call(tool, {"command": "pwd", "python_environment": str(managed.venv)}).status == "failed"
        unavailable = BashRunTool(policy=tool.policy, session_manager=manager,
                                 python_environment=ManagedPythonEnvironment(tmp_path / "unavailable"))
        assert call(unavailable, {"command": "pwd", "python_environment": "default"}).status == "failed"
    finally:
        manager.close()


def test_managed_background_and_timeout_reuse_process_controls(tmp_path, managed):
    (tmp_path / "sleep.py").write_text("import os,time\nprint(os.getenv('OPENAI_API_KEY'), flush=True)\ntime.sleep(30)\n")
    command = '& $env:LKA_PYTHON sleep.py' if os.name == "nt" else '"$LKA_PYTHON" sleep.py'
    manager = BashSessionManager()
    try:
        tool = BashRunTool(policy=BashAccessPolicy([tmp_path]), session_manager=manager, python_environment=managed)
        result = call(tool, {"command": command, "python_environment": "default", "mode": "background"})
        assert result.status == "completed" and result.output["running"]
        session_id = result.output["session_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "None" in manager.read(session_id=session_id)["output"]:
                break
            time.sleep(.02)
        assert "None" in manager.read(session_id=session_id)["output"]
        manager.terminate(session_id=session_id)
        assert not manager.read(session_id=session_id)["running"]
        timeout = call(tool, {"command": command, "python_environment": "default", "timeout_seconds": 1})
        assert timeout.output["timed_out"] and timeout.output["exit_code"] is None
    finally:
        manager.close()


def test_create_file_preserves_existing_files_and_requires_parent(tmp_path):
    tool = CreateFileTool(FileAccessPolicy([tmp_path]))
    assert call(tool, {"path": "script.py", "content": "print('你好')\n"}).status == "completed"
    assert call(tool, {"path": "script.py", "content": "overwrite"}).status == "failed"
    assert (tmp_path / "script.py").read_text() == "print('你好')\n"
    assert call(tool, {"path": "missing/script.py", "content": "x"}).status == "failed"
    assert call(tool, {"path": "binary", "content": "\x00"}).status == "failed"


def test_create_external_file_exact_approval_and_child_ceiling(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = CreateFileTool(FileAccessPolicy([workspace]))
    outside = tmp_path / "outside.py"
    values = {"path": str(outside), "content": "x"}
    assert call(tool, values).status == "failed"
    approved = ToolContext(session_id="test", approved_paths=[str(outside)], safety_review_approved=True)
    assert call(tool, values, approved).status == "completed"
    forbidden = tmp_path / "child-outside.py"
    view = ToolView(snapshot_id="child", allowed_tools=("filesystem.create_file",),
                    allowed_packages=("filesystem",), allowed_paths=(str(workspace),),
                    side_effect_level=SideEffectLevel.WRITE, child_run_id="child")
    child = ToolContext(session_id="child", approved_paths=[str(forbidden)], tool_view=view, safety_review_approved=True)
    assert call(tool, {"path": str(forbidden), "content": "x"}, child).status == "failed"
    assert not forbidden.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_create_file_rejects_symlink_escape_without_exact_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    tool = CreateFileTool(FileAccessPolicy([workspace]))
    assert call(tool, {"path": "link/script.py", "content": "x"}).status == "failed"
    assert not (outside / "script.py").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_create_file_preserves_dangling_symlink(tmp_path):
    target = tmp_path / "missing.py"
    link = tmp_path / "existing.py"
    link.symlink_to(target)
    tool = CreateFileTool(FileAccessPolicy([tmp_path]))
    result = call(tool, {"path": "existing.py", "content": "print('new')"})
    assert result.status == "failed" and "symlink" in result.error
    assert link.is_symlink() and not target.exists()
