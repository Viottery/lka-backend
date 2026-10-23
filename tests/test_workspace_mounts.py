"""Mount contracts use synthetic mount tables and disposable local descriptors."""
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domains.device_connections import DeviceConnectionConfig
from app.domains.node_workspaces import NodeWorkspaceService
from app.domains.workspace_mounts import WorkspaceMountConfig, WorkspaceMountGuard, _probe


@pytest.fixture
def mounted(tmp_path):
    if sys.platform != "linux":
        pytest.skip("native descriptor mount identity is Linux-only")
    root = tmp_path / "mounted workspace"
    root.mkdir()
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        device = os.fstat(descriptor).st_dev
        info = Path(f"/proc/self/fdinfo/{descriptor}").read_text()
        mount_id = next(int(line.split(":")[1]) for line in info.splitlines() if line.startswith("mnt_id:"))
    finally:
        os.close(descriptor)
    escaped = str(root).replace(" ", r"\040")
    table = [f"{mount_id} 1 {os.major(device)}:{os.minor(device)} / {escaped} rw - fuse.sshfs synthetic:/workspace rw\n"]
    config = WorkspaceMountConfig(expected_filesystem="fuse.sshfs", expected_source="synthetic:/workspace")
    service = NodeWorkspaceService({"remote": str(root)}, {"remote": config})
    service.mount_guard = WorkspaceMountGuard(service.mappings, {"remote": config},
                                            mountinfo_reader=lambda: table[0], probe=lambda *_: True)
    return SimpleNamespace(root=root, table=table, config=config, service=service)


def test_declared_mount_identity_is_required_even_when_directory_exists(mounted):
    f = mounted
    assert f.service.root("remote") == f.root
    assert f.service.status("remote") == {"kind": "mounted", "available": True, "read_only": False}
    f.table[0] = ""
    assert f.root.is_dir()
    assert f.service.is_mounted("remote")
    assert f.service.status("remote")["available"] is False
    with pytest.raises(PermissionError, match="absent"):
        f.service.create_file("remote", "should-not-exist", b"content")
    assert not (f.root / "should-not-exist").exists()


@pytest.mark.parametrize("replacement", ["wrong-source", "wrong-filesystem", "wrong-device", "wrong-id", "stacked"])
def test_replaced_mount_or_descriptor_identity_is_rejected(mounted, replacement):
    f = mounted
    if replacement == "wrong-source":
        f.table[0] = f.table[0].replace("synthetic:/workspace", "unapproved:/workspace")
    elif replacement == "wrong-filesystem":
        f.table[0] = f.table[0].replace("fuse.sshfs", "ext4")
    elif replacement == "wrong-device":
        fields = f.table[0].split()
        fields[2] = "0:0"
        f.table[0] = " ".join(fields)
    elif replacement == "wrong-id":
        fields = f.table[0].split()
        fields[0] = str(int(fields[0]) + 1)
        f.table[0] = " ".join(fields)
    else:
        f.table[0] += f.table[0]
    with pytest.raises(PermissionError):
        f.service.create_directory("remote", "nope")
    assert not (f.root / "nope").exists()


def test_mounted_file_operations_use_pinned_descriptors(mounted):
    service = mounted.service
    service.create_directory("remote", "sub")
    service.create_file("remote", "sub/result.txt", b"verified")
    assert service.read("remote", "sub/result.txt") == b"verified"
    assert service.entries("remote", "sub")["items"] == [
        {"name": "result.txt", "is_directory": False, "size": 8}]
    with pytest.raises(FileExistsError):
        service.create_file("remote", "sub/result.txt", b"overwrite")


def test_unmount_during_create_never_writes_empty_backing_directory(mounted, monkeypatch):
    f = mounted
    original = os.open
    detached = f.root.with_name("detached")
    def open_after_unmount(path, flags, *args, **kwargs):
        if path == "result.txt" and flags & os.O_CREAT:
            f.root.rename(detached)
            f.root.mkdir()
            f.table[0] = ""
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", open_after_unmount)
    with pytest.raises(PermissionError):
        f.service.create_file("remote", "result.txt", b"must not reach local disk")
    assert list(f.root.iterdir()) == []
    assert list(detached.iterdir()) == []


@pytest.mark.parametrize("native_read_only", [False, True])
def test_read_only_declaration_or_native_mount_blocks_writes(mounted, native_read_only):
    f = mounted
    if native_read_only:
        f.table[0] = f.table[0].replace(" rw", " ro")
    else:
        f.service.mount_guard.mounts["remote"] = f.config.model_copy(update={"read_only": True})
    (f.root / "read.txt").write_bytes(b"read permitted")
    assert f.service.read("remote", "read.txt") == b"read permitted"
    assert f.service.status("remote")["read_only"]
    with pytest.raises(PermissionError, match="read-only"):
        f.service.create_file("remote", "blocked", b"no")


def test_probe_failure_unsupported_platform_and_changed_mount_fail_closed(mounted):
    f = mounted
    f.service.mount_guard._probe = lambda *_: False
    assert not f.service.status("remote")["available"]
    with pytest.raises(PermissionError, match="probe"):
        f.service.root("remote")
    f.service.mount_guard.platform = "win32"
    with pytest.raises(PermissionError, match="Linux"):
        f.service.root("remote")
    f.service.mount_guard.platform = "linux"
    def unmount_during_probe(*_):
        f.table[0] = ""
        return True
    f.service.mount_guard._probe = unmount_during_probe
    with pytest.raises(PermissionError):
        f.service.root("remote")


def test_mount_path_guard_cannot_be_bypassed_by_nested_local_mapping(mounted):
    f = mounted
    child = f.root / "project"
    child.mkdir()
    f.service.mappings["alias"] = str(child)
    assert f.service.mounted_workspace_for_path(child / "result") == "remote"
    assert f.service.is_mounted("alias")
    f.table[0] = ""
    with pytest.raises(PermissionError):
        f.service.root("alias")
    with pytest.raises(PermissionError):
        f.service.check_path(child / "result", write=True)


def test_mount_configuration_is_explicit_and_writeback_is_not_promised(tmp_path):
    with pytest.raises(ValidationError, match="mapping"):
        DeviceConnectionConfig(workspace_mounts={"missing": {
            "expected_filesystem": "fuse.sshfs", "expected_source": "synthetic:/workspace"}})
    with pytest.raises(ValidationError, match="writeback"):
        WorkspaceMountConfig(expected_filesystem="fuse.rclone", expected_source="remote:")
    service = NodeWorkspaceService({"local": str(tmp_path)})
    assert service.status("local") == {"kind": "local", "available": True}
    service.create_file("local", "normal.txt", b"ordinary local behavior")
    assert service.read("local", "normal.txt") == b"ordinary local behavior"
    assert service.mounted_workspace_for_path(tmp_path / "normal.txt") is None


def test_native_probe_timeout_never_waits_indefinitely_for_blocked_io(monkeypatch, tmp_path):
    waits = []
    class BlockedProcess:
        killed = False
        def wait(self, *, timeout):
            waits.append(timeout)
            raise subprocess.TimeoutExpired("synthetic probe", timeout)
        def kill(self):
            self.killed = True
    process = BlockedProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    assert _probe(tmp_path, 0.2) is False
    assert process.killed and waits == [0.2, 0.1]


@pytest.mark.parametrize("mount_inside_data", [False, True])
def test_runtime_rejects_data_directory_mount_overlap_before_database_creation(tmp_path, monkeypatch, mount_inside_data):
    from app.core.runtime import LocalKnowledgeAgentRuntime

    data = tmp_path / "private-data"
    root = data / "remote" if mount_inside_data else tmp_path
    config = SimpleNamespace(connections=DeviceConnectionConfig(
        workspace_roots={"remote": str(root)}, workspace_mounts={"remote": {
            "expected_filesystem": "fuse.sshfs", "expected_source": "synthetic:/workspace"}}))
    settings = SimpleNamespace(data_dir=data, load_local_config=lambda: config)
    with pytest.raises(ValueError, match="overlap"):
        LocalKnowledgeAgentRuntime(settings)
    assert not data.exists()


def test_generic_tool_revalidates_mount_after_admission_and_denies_unpinned_writes(mounted):
    from app.core.runtime import LocalKnowledgeAgentRuntime
    from app.core.tools import ToolContext, ToolExecutor, ToolRegistry, ToolResult, ToolSpec

    f = mounted
    original_table = f.table[0]
    runtime = LocalKnowledgeAgentRuntime.__new__(LocalKnowledgeAgentRuntime)
    runtime.node_workspaces = f.service
    invoked = []
    class ReadTool:
        spec = ToolSpec(name="filesystem.read_file", description="Synthetic reader", type="local_tool",
                        read_only=True, scope_path_fields=("path",), input_schema={"type": "object"})
        def invoke(self, *, invocation, context):
            invoked.append(True)
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="completed")
    registry = ToolRegistry()
    registry.register_tool(ReadTool())
    executor = ToolExecutor(registry)
    checks = []
    def guard(*args):
        checks.append(True)
        if len(checks) == 2:
            f.table[0] = ""
        return runtime._check_node_tool_workspace(*args)
    executor.workspace_access_guard = guard
    result = executor.execute(invocation_id="read", tool_name="filesystem.read_file",
                              tool_input={"path": str(f.root / "result")}, context=ToolContext(session_id="session"))
    assert result.status == "rejected" and len(checks) == 2 and not invoked
    # Generic tools never receive mounted write authority, even with live identity.
    f.table[0] = original_table
    assert "pinned" in runtime._check_node_tool_workspace(
        ReadTool.spec, {"path": str(f.root / "result")}, ToolContext(session_id="session"), False)


def test_reserved_workspace_materialization_cannot_fall_back_after_unmount(mounted):
    from app.core.runtime import LocalKnowledgeAgentRuntime
    from app.core.sessions import SessionWorkspace

    f = mounted
    target = f.root / "reserved-task"
    workspace = SessionWorkspace(path=str(target), backend_path=str(target), platform="linux", state="reserved")
    runtime = LocalKnowledgeAgentRuntime.__new__(LocalKnowledgeAgentRuntime)
    runtime._workspace_materialize_lock = threading.Lock()
    runtime.node_workspaces = f.service
    runtime.session_service = SimpleNamespace(get_session_or_none=lambda **_: SimpleNamespace(workspace=workspace),
                                               set_workspace=lambda **_: None)
    runtime.path_resolver = SimpleNamespace(resolve_workspace=lambda _: object(), is_allowed_workspace=lambda _: True)
    runtime.project_service = SimpleNamespace(register=lambda *_: {"project_id": "project"})
    f.table[0] = ""
    with pytest.raises((PermissionError, ValueError)):
        runtime.materialize_session_workspace(session_id="session")
    assert not target.exists()
