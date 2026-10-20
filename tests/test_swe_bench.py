"""Offline CLI, patch capture, provenance and worker boundary contracts."""

import asyncio
import json
import shutil
import sys
from types import SimpleNamespace

import pytest

from evals.lka_evals import swe_bench as bench


def record():
    return {
        "instance_id": "owner__project-1",
        "repo": "owner/project",
        "base_commit": "a" * 40,
        "problem_statement": "Repair behavior",
        "version": "1",
        "patch": "gold",
        "test_patch": "test",
        "FAIL_TO_PASS": ["tests/test.py::test_fix"],
        "PASS_TO_PASS": [],
    }


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "dataset.jsonl"
    path.write_text(json.dumps(record()) + "\n", encoding="utf-8")
    return path


def test_listing_and_unbounded_prepare_never_import_private_worker(
    dataset, tmp_path, monkeypatch, capsys
):
    previous = sys.modules.pop("evals.lka_evals.swe_runtime", None)
    try:
        assert bench.main(["list", "--dataset", str(dataset)]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["provider_calls"] == 0
        assert result["cases"][0]["instance_id"] == record()["instance_id"]
        assert "evals.lka_evals.swe_runtime" not in sys.modules
        monkeypatch.setattr(
            bench, "prepare", lambda *args: pytest.fail("Unbounded prepare dispatched")
        )
        with pytest.raises(SystemExit):
            bench.main(["prepare", "--dataset", str(dataset), "--output", str(tmp_path / "unused")])
        assert not (tmp_path / "unused").exists()
    finally:
        if previous is not None:
            sys.modules["evals.lka_evals.swe_runtime"] = previous


def test_paid_dispatch_gate_precedes_config_and_bundle_access(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "run_cases", lambda *args: pytest.fail("Paid dispatch without gate"))
    with pytest.raises(SystemExit):
        bench.main(
            [
                "run",
                "--prepared",
                str(tmp_path / "missing"),
                "--output",
                str(tmp_path / "out"),
                "--python",
                "/missing/python",
                "--calibration",
                "/missing/control.json",
                "--local-config",
                "/missing/private.toml",
            ]
        )
    assert not (tmp_path / "out").exists()


def test_outputs_must_be_ignored_and_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        bench.ignored_output(tmp_path / "published")
    path = tmp_path / "data/run"
    assert bench.ignored_output(path) == path
    path.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        bench.ignored_output(path)
    assert bench.ignored_output(path, fresh=False) == path
    (tmp_path / "data/escape").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        bench.ignored_output(tmp_path / "data/escape/leak")


def test_patch_capture_includes_ignored_new_and_deleted_files_and_ignores_candidate_git(tmp_path):
    pristine = tmp_path / "data/pristine"
    pristine.mkdir(parents=True)
    (pristine / "existing.py").write_text("before\n")
    (pristine / "deleted.py").write_text("delete me\n")
    (pristine / ".gitignore").write_text("*.ignored\n")
    (pristine / "original.ignored").write_text("unchanged base file\n")
    workspace = tmp_path / "data/workspace"
    shutil.copytree(pristine, workspace)
    bench.initialize_git(workspace)
    (workspace / "existing.py").write_text("after\n")
    (workspace / "deleted.py").unlink()
    (workspace / "new.py").write_text("new\n")
    (workspace / "new.ignored").write_text("intentionally ignored\n")
    marker = tmp_path / "malicious-command-ran"
    (workspace / ".git/config").write_text(
        f"[core]\n\thooksPath = /does/not/exist\n[diff]\n\texternal = touch {marker}\n"
    )
    output = tmp_path / "data/capture"
    output.mkdir()
    patch = bench.capture_patch(pristine, workspace, output)
    for name in ("existing.py", "deleted.py", "new.py", "new.ignored"):
        assert f"diff --git a/{name} b/{name}" in patch
    assert "-before" in patch and "+after" in patch
    assert "deleted file mode" in patch
    assert not marker.exists()
    assert ".git/config" not in patch
    assert "original.ignored" not in patch


def test_bundle_rejects_dataset_and_task_tampering(dataset, tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "ROOT", tmp_path)

    def download(row, archive, pristine):
        archive.write_bytes(b"synthetic archive")
        pristine.mkdir()
        (pristine / "module.py").write_text("base\n")
        return bench.digest(archive)

    monkeypatch.setattr(bench, "download_repository", download)
    output = tmp_path / "data/prepared"
    bench.prepare(SimpleNamespace(output=output, dataset=dataset), [record()])
    assert bench.load_bundle(output)[2][record()["instance_id"]] == record()
    task = output / record()["instance_id"] / "task.json"
    original = task.read_text()
    task.write_text("{}")
    with pytest.raises(ValueError, match="task"):
        bench.load_bundle(output)
    task.write_text(original)
    module = output / record()["instance_id"] / "pristine/module.py"
    module.write_text("tampered\n")
    with pytest.raises(ValueError, match="[Pp]ristine"):
        bench.load_bundle(output)
    module.write_text("base\n")
    dataset.write_text(dataset.read_text() + "\n")
    with pytest.raises(ValueError, match="dataset hash"):
        bench.load_bundle(output)


def test_pristine_hash_covers_internal_directory_symlink_targets(tmp_path):
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "same.py").write_text("same\n")
    link = tmp_path / "selected"
    link.symlink_to("one", target_is_directory=True)
    original = bench.tree_digest(tmp_path)
    link.unlink()
    link.symlink_to("two", target_is_directory=True)
    assert bench.tree_digest(tmp_path) != original


def test_exact_execution_subset_keeps_frozen_prepared_manifest(tmp_path, monkeypatch):
    a, b = record(), {**record(), "instance_id": "owner__project-2"}
    manifest = {"selection": [a["instance_id"], b["instance_id"]], "cases": []}
    monkeypatch.setattr(
        bench,
        "load_bundle",
        lambda path: (
            tmp_path,
            manifest,
            {a["instance_id"]: a, b["instance_id"]: b},
        ),
    )
    _, selected, rows = bench.selected_bundle(
        SimpleNamespace(prepared=tmp_path, case=[b["instance_id"]])
    )
    assert selected["selection"] == [b["instance_id"]]
    assert selected["prepared_selection"] == manifest["selection"]
    assert manifest["selection"] == [a["instance_id"], b["instance_id"]]
    assert list(rows) == [b["instance_id"]]
    with pytest.raises(ValueError):
        bench.selected_bundle(SimpleNamespace(prepared=tmp_path, case=["missing__task-1"]))


def fake_runtime():
    from app.core.sessions import SessionRunPolicy

    calls = []
    bash = SimpleNamespace()
    executor = SimpleNamespace(
        execute=lambda **kwargs: calls.append(kwargs) or "validated execution"
    )
    runtime = SimpleNamespace(
        _session_run_policy=lambda session_id: SessionRunPolicy(),
        session_service=SimpleNamespace(),
        tool_executor=executor,
        tool_registry=SimpleNamespace(get_tool=lambda name: bash),
    )
    return runtime, calls, bash


def test_configuration_uses_first_client_when_default_name_is_omitted(tmp_path, monkeypatch):
    from app.core.local_config import LLMClientConfig, LocalAppConfig
    from evals.lka_evals import swe_runtime

    source = LocalAppConfig()
    source.llm.model = "deepseek-flash"
    source.llm.clients = [
        LLMClientConfig(
            name="evaluation",
            provider="openai_compatible",
            default_model="deepseek-flash",
            api_key_env="SWE_TEST_API_KEY",
        )
    ]
    monkeypatch.setenv("SWE_TEST_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(swe_runtime, "load_local_config", lambda path: source)
    config, client, _ = swe_runtime.configuration(
        {
            "local_config": str(tmp_path / "test.toml"),
            "env": str(tmp_path / "missing.env"),
            "max_steps": 40,
            "multi_agent": False,
        }
    )
    assert client.name == config.llm.default_client == "evaluation"
    assert config.agent.orchestrator == source.agent.orchestrator
    assert config.agent.max_decision_steps == 40
    source.llm.default_client = "missing"
    with pytest.raises(ValueError):
        swe_runtime.configuration({"local_config": "unused"})


def test_worker_rejects_scope_escapes_but_keeps_existing_executor(tmp_path):
    from evals.lka_evals.swe_runtime import confine

    runtime, calls, _ = fake_runtime()
    confine(runtime, tmp_path, SimpleNamespace())
    for name, payload in [
        ("mail.search", {}),
        ("filesystem.read_file", {"path": "../secret"}),
        ("filesystem.read_file", {"path": "/etc/passwd"}),
        ("bash.run", {"mode": "background", "command": "echo ok"}),
        ("bash.run", {"python_environment": "default", "command": "echo ok"}),
    ]:
        result = runtime.tool_executor.execute(
            tool_name=name, tool_input=payload, invocation_id="test"
        )
        assert result.status == "rejected"
    assert calls == []
    result = runtime.tool_executor.execute(
        tool_name="filesystem.read_file",
        tool_input={"path": "$WORKSPACE_ROOT/source.py"},
        invocation_id="ok",
    )
    assert result == "validated execution"
    assert len(calls) == 1
    assert "mail.search" not in runtime._session_run_policy("s").allowed_tools


def test_worker_bash_boundary_uses_sandbox_and_bounds_timeout_and_output(tmp_path):
    from evals.lka_evals.swe_runtime import confine

    runtime, _, bash = fake_runtime()
    calls = []

    def run(command, cwd, timeout):
        calls.append((command, cwd, timeout))
        return {"stdout": "abcdef", "stderr": "error", "exit_code": 0, "timed_out": False}

    confine(runtime, tmp_path, SimpleNamespace(run=run))
    result = bash._run_sync(
        command="python -m pytest",
        cwd=tmp_path,
        read_only=False,
        policy=None,
        timeout_seconds=900,
        max_output_bytes=3,
    )
    assert calls == [("python -m pytest", tmp_path, 300)]
    assert result["stdout"] == "abc" and result["stderr"] == "err"
    assert result["running"] is False and result["exit_code"] == 0


def test_worker_meter_records_and_refuses_repeat_without_provider_dispatch(tmp_path):
    from app.core.llm.models import LLMMessage, LLMRequest, LLMResponse
    from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded
    from evals.lka_evals.swe_runtime import RecordedClient

    class Client:
        default_model = "priced"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            return LLMResponse(
                provider="fake",
                status="completed",
                content="ok",
                prompt_summary="test",
                usage={"prompt_tokens": 10, "completion_tokens": 2},
            )

    budget = LiveBudget(tmp_path / "ledger.sqlite3", call_limit=1)
    client = Client()
    wrapped = RecordedClient(
        client, budget, allowed_model="priced", protected_values=(), output=tmp_path / "calls.jsonl"
    )
    request = LLMRequest(
        messages=[LLMMessage(role="user", content="synthetic")],
        model="priced",
        prompt_summary="test",
    )
    asyncio.run(wrapped.complete(request))
    with pytest.raises(LiveBudgetExceeded):
        asyncio.run(wrapped.complete(request))
    assert client.calls == 1
    event = json.loads((tmp_path / "calls.jsonl").read_text())
    assert event["status"] == "completed"
    assert "synthetic" not in json.dumps(event)
