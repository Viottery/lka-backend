"""Private, metered LKA worker. Invoked only by the explicit paid SWE runner."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.config import Settings
from app.core.llm.models import LLMResponseMode
from app.core.local_config import LocalAppConfig, _dotenv_value, load_local_config
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.safety import SafetyReviewMode
from app.core.tools import ToolResult
from evals.lka_evals.live_budget import MeteredClient, _protected_credential_values
from evals.lka_evals.management_runtime import bounded_budget
from evals.lka_evals.swe_bench import (
    NATIVE_PROFILE_VERSION,
    environment_identity,
    object_digest,
    save,
    verify_source,
)
from evals.lka_evals.swe_sandbox import Sandbox

ALLOWED_TOOLS = frozenset(
    {
        "filesystem.read_file",
        "filesystem.list_directory",
        "filesystem.edit_file",
        "filesystem.create_file",
        "bash.run",
        "observation.read",
        "observation.search",
        "observation.group",
    }
)


def configuration_identity(config) -> str:
    """Hash effective settings after removing credentials, including URL secrets."""
    sensitive = {
        "api_key",
        "key",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
    }

    def sanitized(value):
        if isinstance(value, dict):
            return {
                key: ("<redacted>" if key.lower() in sensitive else sanitized(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [sanitized(item) for item in value]
        if isinstance(value, str) and "://" in value:
            parsed = urlsplit(value)
            query = urlencode(
                [
                    (key, "<redacted>" if key.lower() in sensitive else item)
                    for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                ]
            )
            return urlunsplit(
                (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, query, "")
            )
        return value

    return object_digest(sanitized(config.model_dump(mode="json")))


def test_guidance(task: dict) -> str:
    """Versioned public environment guidance; never inspect heldout identities."""
    runner = (
        "This historical SymPy checkout uses its native runner: "
        "python bin/test -C --verbose --no-colors <relevant test file>. "
        "Modern pytest may not collect these historical tests."
        if task.get("repo") == "sympy/sympy"
        else "Run relevant available tests with python -m pytest <test file or node>."
    )
    return (
        f"Environment profile: {NATIVE_PROFILE_VERSION}. {runner} "
        "Python and installed dependencies are preconfigured and network access is disabled. "
        "Shell pipelines preserve non-SIGPIPE failures; use the actual test exit status, "
        "not successful tail/head output, as validation evidence.\n\n"
    )


class EvalSettings(Settings):
    evaluation_config: object

    def load_local_config(self):
        return self.evaluation_config.model_copy(deep=True)


class RecordedClient(MeteredClient):
    def __init__(self, *args, output: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.output = output

    async def complete(self, request):
        request, call_id = self._reserve(request)
        started = time.monotonic()
        event = {
            "call_id": call_id,
            "stage": request.prompt_summary,
            "model": request.model,
            "status": "failed_or_cancelled",
        }
        try:
            response = await self.client.complete(request)
            self.budget.finish(call_id, response.usage, status=response.status)
            event.update(status=response.status, usage=response.usage)
            return response
        except BaseException:
            self.budget.finish(call_id, None, status="failed_or_cancelled")
            raise
        finally:
            event["elapsed_seconds"] = round(time.monotonic() - started, 3)
            with self.output.open("a", encoding="utf-8") as log:
                log.write(json.dumps(event, ensure_ascii=False) + "\n")


def configuration(job: dict, *, resolve_credentials: bool = True):
    """Import model settings only; never import personal domains or memory."""
    source = load_local_config(Path(job["local_config"]))
    clients = source.llm.client_configs()
    client = next((c for c in clients if c.name == source.llm.default_client), None)
    if source.llm.default_client is None and clients:
        client = clients[0]
    # LiveBudget uses this model's existing evaluation tariff. Unknown tariffs fail closed.
    if (
        client is None
        or client.provider != "openai_compatible"
        or client.default_model != "deepseek-flash"
    ):
        raise ValueError("Current ledger supports only configured deepseek-flash pricing")
    protected = ()
    if resolve_credentials:
        secret = client.resolved_api_key() or _dotenv_value(client.api_key_env, Path(job["env"]))
        if not secret:
            raise ValueError("Configured provider credential unavailable")
        os.environ[client.api_key_env] = secret
        protected = tuple({*_protected_credential_values(Path(job["env"])), secret})
    config = LocalAppConfig()
    config.llm = source.llm.model_copy(deep=True)
    config.llm.clients = [client.model_copy(deep=True)]
    config.llm.default_client = client.name
    config.llm.model = client.default_model
    config.llm.fallback_client = None
    config.llm.max_attempts = 1
    # Keep the configured Agent mode; only explicit experiment controls differ.
    config.agent = source.agent.model_copy(deep=True)
    config.agent.max_decision_steps = job["max_steps"]
    config.agent.multi_agent_planning_enabled = job["multi_agent"]
    config.agent.mail_expert_enabled = False
    config.agent.codex_expert_enabled = False
    config.agent.mock_workflow_agent_enabled = False
    config.agent.inference_profiles = ()
    config.agent.allowed_child_inference_profile_ids = ()
    config.memory.enabled = config.memory.background_enabled = False
    config.message_history.enabled = config.message_history.background_enabled = False
    config.embedding.enabled = config.reranker.enabled = False
    config.safety.tool_review_mode = job.get("safety_review_mode") or source.safety.tool_review_mode
    return config, client, protected


def confine(runtime, workspace: Path, sandbox: Sandbox, *, deadline: float | None = None) -> None:
    """Keep normal schema/safety handling; replace command execution at its OS boundary."""
    policy_provider = runtime._session_run_policy

    def policy(session_id):
        value = policy_provider(session_id)
        tools = (
            ALLOWED_TOOLS
            if value.allowed_tools is None
            else ALLOWED_TOOLS.intersection(value.allowed_tools)
        )
        return value.model_copy(update={"allowed_tools": sorted(tools)})

    runtime._session_run_policy = policy
    runtime.session_service.run_policy_provider = policy
    execute = runtime.tool_executor.execute
    tool_lock = threading.RLock()

    def checked_execute(*args, **kwargs):
        name, inputs = kwargs["tool_name"], kwargs["tool_input"]
        if name not in ALLOWED_TOOLS:
            return ToolResult(
                invocation_id=kwargs["invocation_id"],
                tool_name=name,
                status="rejected",
                error="Outside evaluation tool scope",
            )
        for key in ("path", "cwd"):
            if not inputs.get(key):
                continue
            value = str(inputs[key])
            for variable in ("workspace_root", "WORKSPACE_ROOT", "LKA_WORKSPACE_ROOT"):
                value = value.replace("${" + variable + "}", str(workspace)).replace(
                    "$" + variable, str(workspace)
                )
            target = Path(value)
            target = target if target.is_absolute() else workspace / target
            if not target.resolve().is_relative_to(workspace):
                return ToolResult(
                    invocation_id=kwargs["invocation_id"],
                    tool_name=name,
                    status="rejected",
                    error="Outside disposable workspace",
                )
        if name == "bash.run" and (
            inputs.get("mode", "sync") != "sync" or inputs.get("python_environment") is not None
        ):
            return ToolResult(
                invocation_id=kwargs["invocation_id"],
                tool_name=name,
                status="rejected",
                error="Benchmark supports sync sandbox commands only; use python from PATH",
            )
        return execute(*args, **kwargs)

    def confined_execute(*args, **kwargs):
        # Filesystem tools execute in the host process. Serialize path checks and
        # I/O with sandbox commands so sibling children cannot race symlink changes.
        # Sandbox PID namespaces ensure no command leaves a mutator behind.
        with tool_lock:
            return checked_execute(*args, **kwargs)

    runtime.tool_executor.execute = confined_execute
    bash = runtime.tool_registry.get_tool("bash.run")

    def run_sync(
        *, command, cwd, read_only, policy, timeout_seconds, max_output_bytes, execution_env=None
    ):
        timeout = min(max(timeout_seconds, 1), 300)
        if deadline is not None:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("Evaluation worker deadline reached before command dispatch")
            timeout = min(timeout, remaining)
        outcome = sandbox.run(command, cwd=cwd, timeout=timeout)
        size = min(max(max_output_bytes, 1), 131072)
        stdout, stderr = outcome["stdout"][:size], outcome["stderr"][:size]
        return {
            "mode": "sync",
            "command": command,
            "cwd": str(cwd),
            "workspace_root": str(workspace),
            "read_only": read_only,
            "status": "timed_out" if outcome["timed_out"] else "exited",
            "running": False,
            **outcome,
            "stdout": stdout,
            "stderr": stderr,
            "output": stdout + stderr,
            "session_id": None,
            "next_offset": None,
        }

    bash._run_sync = run_sync


def run(job: dict) -> int:
    output, workspace = Path(job["output"]), Path(job["workspace"]).resolve()
    source_root = Path(job["source_root"]).resolve()
    if Path(__file__).resolve().parents[2] != source_root:
        raise ValueError("Worker must import the isolated evaluation source snapshot")
    verify_source(source_root, job["source_identity"])
    if environment_identity(Path(job["python"])) != job["benchmark_environment"]:
        raise ValueError("Benchmark environment fingerprint changed")
    sandbox = Sandbox(workspace, Path(job["python"]))
    status = sandbox.check()
    if not status["available"]:
        raise RuntimeError(status["error"])
    # A worker is a separate process, so clearing inherited app variables cannot affect daily runtime.
    for key in tuple(os.environ):
        if key.startswith("LKA_"):
            os.environ.pop(key)
    config, _client, protected = configuration(job)
    if configuration_identity(config) != job["effective_config_sha256"]:
        raise ValueError("Effective configuration fingerprint changed")
    save(
        output / "worker_identity.json",
        {
            "source_sha256": job["source_identity"]["source_sha256"],
            "worker_dependencies": job["source_identity"]["worker_dependencies"],
            "effective_config_sha256": job["effective_config_sha256"],
            "benchmark_environment": job["benchmark_environment"],
            "deadline_unix_seconds": job["deadline_unix_seconds"],
            "verified_at_unix_seconds": time.time(),
        },
    )
    if time.time() >= job["deadline_unix_seconds"]:
        raise TimeoutError("Evaluation deadline reached before runtime initialization")
    budget = bounded_budget(
        Path(job["ledger"]), usd_limit=job["usd_limit"], call_limit=job["call_limit"]
    )
    report = {
        "model": config.llm.model,
        "provider": config.llm.default_client,
        "status": "runtime_error",
        "multi_agent": job["multi_agent"],
        "agent_config": config.agent.model_dump(mode="json"),
        "safety_review_mode": config.safety.tool_review_mode,
        "safety_review_record_required": True,
        "allowed_tools": sorted(ALLOWED_TOOLS),
        "sandbox": "bubblewrap_no_network",
    }
    report["tool_execution_serialized"] = True
    runtime = None
    try:
        runtime = LocalKnowledgeAgentRuntime(
            EvalSettings(
                _env_file=None,
                evaluation_config=config,
                LKA_DATA_DIR=output / "runtime",
                LKA_WORKSPACE_ROOTS=str(workspace),
                LKA_LOCAL_CONFIG=output / "missing.toml",
            )
        )
        confine(runtime, workspace, sandbox, deadline=job["deadline_unix_seconds"])
        for registered in runtime.agent_llm_client.registry.list_clients():
            runtime.agent_llm_client.registry.register_client(
                RecordedClient(
                    registered,
                    budget,
                    allowed_model=config.llm.model,
                    protected_values=protected,
                    output=output / "model_calls.jsonl",
                )
            )
        session = runtime.create_session(
            title=job["task"]["instance_id"],
            metadata={"workspace": {"path": str(workspace), "platform": "linux"}},
        )
        prompt = (
            "Resolve the following issue in this repository. Inspect the code, implement "
            "the fix, and run relevant available tests. Leave your changes in the workspace. "
            "Report what changed and the validation performed. The checkout is disposable; "
            "commands run without network access.\n\n"
            + test_guidance(job["task"])
            + job["task"]["problem_statement"]
        )
        run_record = runtime.agent_turn_loop.create_run_for_turn(
            session_id=session.session.session_id,
            user_input=prompt,
        )
        runtime.agent_run_manager._update_run(
            run_record.run_id,
            status=run_record.status,
            metadata_patch={"execution_deadline_unix_seconds": job["deadline_unix_seconds"]},
        )
        result = runtime.run_agent_turn(
            session_id=session.session.session_id,
            user_input=prompt,
            llm_client_name=config.llm.default_client,
            llm_model=config.llm.model,
            llm_response_mode=LLMResponseMode.TEXT,
            safety_review_mode=SafetyReviewMode(config.safety.tool_review_mode),
            existing_run_id=run_record.run_id,
        )
        save(output / "result.json", result.model_dump(mode="json"))
        manager = runtime.agent_run_manager
        root = manager.get_run(result.run_id)
        runs = [root, *manager.child_tree(result.run_id)] if root else []
        trace = [
            {
                "run": r.model_dump(mode="json"),
                "events": [e.model_dump(mode="json") for e in manager.list_events(r.run_id)],
                "safety_reviews": [
                    s.model_dump(mode="json") for s in manager.list_safety_reviews(r.run_id)
                ],
                "tool_invocations": runtime.agent_run_store.list_tool_invocations(r.run_id),
            }
            for r in runs
        ]
        save(output / "trace.json", trace)
        report.update(
            status=root.status.value if root else "unknown",
            run_id=result.run_id,
            task_outcome=getattr(result, "task_outcome", "unknown"),
            stop_reason=getattr(result, "stop_reason", None),
            root_tool_calls=len(result.tool_events),
            child_runs=max(len(runs) - 1, 0),
            tool_invocation_count=sum(len(item["tool_invocations"]) for item in trace),
        )
    except Exception as exc:  # noqa: BLE001 - persist a failed experiment before process exit
        (output / "runtime_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        report.update(
            status="runtime_error",
            error_type=type(exc).__name__,
            error_category=getattr(exc, "error_category", None),
        )
    finally:
        report["budget"] = budget.snapshot()
        save(output / "runtime.json", report)
        if runtime:
            runtime.stop()
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))))
