"""Registered-tool workflows with disposable data, without a model provider."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import venv

import pytest
from pypdf import PdfWriter

from app.core.config import Settings
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.tools import ToolContext
from app.platform.python_environment import MANIFEST_NAME, PYTHON_VERSION


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instance = LocalKnowledgeAgentRuntime(Settings(
        _env_file=None, LKA_DATA_DIR=tmp_path / "data", LKA_WORKSPACE_ROOTS=str(workspace),
        LKA_LOCAL_CONFIG=tmp_path / "missing.toml",
    ))
    yield instance, workspace
    instance.stop()


def execute(runtime, name, values, *, approved=False):
    result = runtime.tool_executor.execute(
        invocation_id=name, tool_name=name, tool_input=values,
        context=ToolContext(session_id="workflow", safety_review_approved=approved),
    )
    if result.status == "completed":
        assert runtime.tool_executor.validate_output(tool_name=name, result=result) == []
    return result


def test_pdf_registered_workflow_and_render_review(runtime):
    instance, workspace = runtime
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    source = workspace / "source.pdf"
    writer.write(source)
    opened = execute(instance, "documents.open", {"path": "source.pdf"})
    assert opened.status == "completed"
    selected = execute(instance, "documents.read_pages", {
        "path": "source.pdf", "expected_sha256": opened.output["sha256"],
    })
    assert selected.status == "completed"
    assert selected.output["pages"][0]["needs_visual"] is True
    values = {"path": "source.pdf", "expected_sha256": opened.output["sha256"],
              "page": 1, "output_path": "page.png"}
    denied = execute(instance, "documents.render_page", values)
    assert denied.status == "rejected" and not (workspace / "page.png").exists()
    rendered = execute(instance, "documents.render_page", values, approved=True)
    assert rendered.status == "completed"
    assert hashlib.sha256((workspace / "page.png").read_bytes()).hexdigest() == rendered.output["image_sha256"]
    unavailable = execute(instance, "vision.inspect_image", {
        "path": "page.png", "expected_sha256": rendered.output["image_sha256"],
        "question": "Read visible text",
    }, approved=True)
    assert unavailable.status == "failed" and "unavailable" in unavailable.error.lower()


def test_registered_python_script_execution_and_environment_info(runtime):
    instance, workspace = runtime
    environment = instance.python_environment
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment.venv)
    lock = environment.root / "requirements.lock"
    lock.write_text("", encoding="utf-8")
    (environment.root / MANIFEST_NAME).write_text(json.dumps({
        "schema_version": 1, "environment_root": str(environment.root),
        "python_version_request": PYTHON_VERSION, "python_version": platform.python_version(),
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    info = execute(instance, "bash.python_environment", {})
    assert info.status == "completed" and info.output["available"] is True
    assert info.output["interpreter"] != str(instance.settings.data_dir / ".venv/bin/python")
    created = execute(instance, "filesystem.create_file", {
        "path": "calculate.py", "content": "from pathlib import Path\nPath('answer.txt').write_text(str(sum(range(101))))\n",
    }, approved=True)
    assert created.status == "completed"
    command = '& $env:LKA_PYTHON calculate.py' if os.name == "nt" else '"$LKA_PYTHON" calculate.py'
    rejected = execute(instance, "bash.run", {"command": command, "python_environment": "default"})
    assert rejected.status == "rejected" and not (workspace / "answer.txt").exists()
    ran = execute(instance, "bash.run", {"command": command, "python_environment": "default"}, approved=True)
    assert ran.status == "completed" and ran.output["exit_code"] == 0
    assert (workspace / "answer.txt").read_text() == "5050"


def test_runtime_registry_includes_new_capabilities_without_provisioning(runtime):
    instance, _ = runtime
    assert not instance.python_environment.root.exists()
    packages = {package.name for package in instance.tool_registry.list_packages()}
    assert {"documents", "vision", "bash", "filesystem"} <= packages
    info = execute(instance, "bash.python_environment", {})
    assert info.status == "completed" and info.output["available"] is False
    assert not instance.python_environment.root.exists()
