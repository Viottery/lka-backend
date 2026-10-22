"""Opt-in synthetic manager delegation evaluation; never starts a service.

Dry run is the default and does not load private configuration. All actual
requests, including safety reviews, share a durable bounded ledger.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from app.core.config import Settings
from app.core.local_config import LocalAppConfig, _dotenv_value, load_local_config
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.safety import SafetyReviewMode
from evals.lka_evals.live_budget import LiveBudget, MeteredClient, _protected_credential_values
from evals.lka_evals.management_cases import load_catalog, materialize_case
from evals.lka_evals.management_checks import result_status, trace_checks

READ_TOOLS = ("filesystem.read_file", "filesystem.list_directory")
DELEGATE_TOOLS = (
    "management.start_task",
    "management.send_instruction",
    "management.read_session",
    "management.read_task_result",
    "management.read_events",
    "management.list_sessions",
    "management.list_runs",
    "management.inspect_run",
    "management.control_run",
    "management.report_delegate",
    *READ_TOOLS,
)
ALLOWED_TOOLS = frozenset(
    (
        *DELEGATE_TOOLS,
        "management.delegate_work",
        "management.list_delegates",
        "management.read_delegate",
        "management.instruct_delegate",
        "management.decide_notification",
    )
)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class RecordedClient(MeteredClient):
    def __init__(self, *args, output, **kwargs):
        super().__init__(*args, **kwargs)
        self.output = output

    def record(self, value):
        with self.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _reserve(self, request):
        request = request.model_copy(
            update={
                "max_output_tokens": min(request.max_output_tokens or 8192, 8192),
            }
        )
        request, call_id = super()._reserve(request)
        self.record(
            {"event": "request", "call_id": call_id, "request": request.model_dump(mode="json")}
        )
        print(
            json.dumps({"event": "dispatch", "call_id": call_id, "stage": request.prompt_summary}),
            flush=True,
        )
        return request, call_id

    async def complete(self, request):
        request, call_id = self._reserve(request)
        started = time.monotonic()
        try:
            response = await self.client.complete(request)
        except BaseException as exc:
            self.budget.finish(call_id, None, status="failed_or_cancelled")
            self.record(
                {
                    "event": "error",
                    "call_id": call_id,
                    "error_type": type(exc).__name__,
                    "category": getattr(exc, "error_category", None),
                }
            )
            raise
        self.budget.finish(call_id, response.usage, status=response.status)
        self.record(
            {
                "event": "response",
                "call_id": call_id,
                "seconds": time.monotonic() - started,
                "response": response.model_dump(mode="json"),
            }
        )
        return response

    async def stream(self, request):
        raise RuntimeError("Live evaluation permits only recorded non-stream calls")
        yield


class EvalSettings(Settings):
    evaluation_config: object

    def load_local_config(self):
        return self.evaluation_config.model_copy(deep=True)


def json_objects(text):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start() :])
            if isinstance(value, dict):
                yield value
        except ValueError:
            continue


class Evaluation:
    def __init__(self, output, config, budget, protected, *, max_steps=80):
        self.max_steps = max_steps
        self.output, self.config, self.budget, self.protected = output, config, budget, protected
        self.report = {
            "synthetic_only": True,
            "production_modified": False,
            "provider": config.llm.default_client,
            "model": config.llm.model,
            "allowed_tools": sorted(ALLOWED_TOOLS),
            "max_output_tokens_ceiling": 8192,
            "tool_review_mode": config.safety.tool_review_mode,
            "decision_thinking_enabled": config.agent.decision_thinking_enabled,
            "cases": [],
            "semantic_review": "Structured fact/decision assertions plus trace provenance; independent review still required",
            "artifacts": {
                "ledger": str(budget.path),
                "model_calls": str(output / "model_calls.jsonl"),
            },
        }
        self.runtime = None

    def persist(self):
        self.report["budget"] = self.budget.snapshot()
        save(self.output / "report.json", self.report)

    def open_runtime(self, directory):
        workspace = directory / "workspace"
        runtime = LocalKnowledgeAgentRuntime(
            EvalSettings(
                _env_file=None,
                evaluation_config=self.config,
                LKA_DATA_DIR=directory / "runtime",
                LKA_WORKSPACE_ROOTS=str(workspace),
                LKA_LOCAL_CONFIG=directory / "missing.toml",
            )
        )
        original_policy = runtime._session_run_policy

        def scoped_policy(session_id):
            policy = original_policy(session_id)
            ceiling = (
                ALLOWED_TOOLS
                if policy.allowed_tools is None
                else ALLOWED_TOOLS.intersection(policy.allowed_tools)
            )
            return policy.model_copy(update={"allowed_tools": sorted(ceiling), "allow_fork": False})

        # Intersect the product policy on every read; never replace a child's
        # frozen task-only capabilities with the evaluator's broader ceiling.
        runtime._session_run_policy = scoped_policy
        runtime.session_service.run_policy_provider = scoped_policy
        runtime.managed_tasks.policy = scoped_policy
        for client in runtime.agent_llm_client.registry.list_clients():
            runtime.agent_llm_client.registry.register_client(
                RecordedClient(
                    client,
                    self.budget,
                    allowed_model=self.config.llm.model,
                    protected_values=self.protected,
                    output=self.output / "model_calls.jsonl",
                )
            )
        execute = runtime.tool_executor.execute

        def confined(*args, **kwargs):
            name, inputs = kwargs["tool_name"], kwargs["tool_input"]
            if name not in ALLOWED_TOOLS:
                self.current["denied_tools"].append(name)
                raise PermissionError("Evaluation tool is outside frozen allowlist")
            for key in ("path", "cwd", "workspace_path"):
                if inputs.get(key):
                    candidate = Path(inputs[key])
                    candidate = candidate if candidate.is_absolute() else workspace / candidate
                    if not candidate.resolve().is_relative_to(workspace.resolve()):
                        self.current["denied_tools"].append(name + ":outside_workspace")
                        raise PermissionError("Evaluation path leaves synthetic workspace")
            requested = inputs.get("allowed_tools")
            if requested is not None and not set(requested).issubset(READ_TOOLS):
                raise PermissionError("Delegation exceeds frozen tool ceiling")
            return execute(*args, **kwargs)

        runtime.tool_executor.execute = confined
        return runtime

    def snapshot(self):
        with closing(self.runtime._conn()) as conn:
            names = [
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ]
            selected = [
                n
                for n in names
                if re.fullmatch(r"[a-z_]+", n)
                and (
                    n.startswith(("manager_", "managed_", "agent_run", "agent_session"))
                    or n in {"background_jobs", "agent_tool_invocations"}
                )
            ]
            return {
                name: [dict(r) for r in conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
                for name in selected
            }

    def run_case(self, name):
        definition = materialize_case(load_catalog()["cases"][name])
        directory = self.output / name
        workspace = directory / "workspace"
        workspace.mkdir(parents=True)
        for filename, content in definition["files"].items():
            save(workspace / filename, content)
        save(directory / "oracle.json", definition["expected"])
        self.current = {
            "name": name,
            "artifact_directory": str(directory),
            "denied_tools": [],
            "steps": [],
        }
        self.report["cases"].append(self.current)
        try:
            self.runtime = self.open_runtime(directory)
            if not hasattr(self.runtime, "management_delegates"):
                raise RuntimeError("Delegate implementation is not integrated")
            parent = self.runtime.management_service.create(title="Synthetic " + name)["session"][
                "session_id"
            ]
            self.runtime.management_service.update(parent, expected_revision=1, status="active")
            self.current["parent_session_id"] = parent
            prompt = definition["prompt"].format(
                workspace=workspace, delegate_tools=json.dumps(READ_TOOLS)
            )
            result = self.runtime.run_agent_turn(
                session_id=parent,
                user_input=prompt,
                resumable=True,
                safety_review_mode=SafetyReviewMode(self.config.safety.tool_review_mode),
            )
            self.current["initial_result"] = result.model_dump(mode="json")
            self.current["initial_snapshot"] = self.snapshot()
            self.persist()
            # Acceptance must survive a process-equivalent reconstruction before workers execute.
            self.runtime.stop()
            self.runtime = self.open_runtime(directory)
            idle = 0
            for index in range(self.max_steps):
                before = self.budget.snapshot().get("groups", {}).get("llm", {}).get("calls", 0)
                tick = self.runtime.management_coordinator.tick()
                bound = self.runtime.management_coordinator.run_one()
                executed = self.runtime.agent_dispatch.run_one()
                self.current["steps"].append(
                    {"index": index, "tick": tick, "bound": bool(bound), "executed": bool(executed)}
                )
                save(directory / "durable_snapshot.json", self.snapshot())
                self.persist()
                after = self.budget.snapshot().get("groups", {}).get("llm", {}).get("calls", 0)
                if after >= self.budget.call_limit:
                    self.current["stopped"] = "call_budget_exhausted"
                    break
                idle = idle + 1 if not bound and not executed and before == after else 0
                if idle >= 2:
                    self.current["stopped"] = "no_eligible_work"
                    break
            else:
                self.current["stopped"] = "driver_step_limit"
            self.assess(definition, directory, parent, result.run_id)
        except BaseException as exc:
            self.current.update(
                passed=False,
                status="budget_exhausted"
                if getattr(exc, "error_category", None) == "live_budget_exceeded"
                else "error",
                error_type=type(exc).__name__,
                error_category=getattr(exc, "error_category", None),
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            if self.runtime is not None:
                save(directory / "durable_snapshot.json", self.snapshot())
                self.runtime.stop()
                self.runtime = None
            self.persist()
            print(
                json.dumps(
                    {
                        "event": "case_finished",
                        "name": name,
                        "passed": self.current.get("passed", False),
                        "checks": self.current.get("checks"),
                        "error_type": self.current.get("error_type"),
                    }
                ),
                flush=True,
            )

    def assess(self, definition, directory, parent, initial_run_id, *, recovery=False):
        snapshot = self.snapshot()
        sessions = snapshot.get("agent_sessions", [])
        managers = [r for r in sessions if r.get("kind") == "manager" and r["session_id"] != parent]
        tasks = [r for r in sessions if r.get("kind") == "ordinary"]
        deliveries = self.runtime.management_goals.list_deliveries(parent, limit=100)["deliveries"]
        parent_session = self.runtime.get_session(session_id=parent)
        published_run_ids = {d["run_id"] for d in deliveries if d["run_id"] != initial_run_id}
        answers = [
            m.content
            for m in parent_session.messages
            if m.role in {"agent", "assistant"} and m.payload.get("run_id") in published_run_ids
        ]
        save(directory / "parent_messages.json", parent_session.model_dump(mode="json"))
        parsed = [obj for answer in answers for obj in json_objects(answer) if "decision" in obj]
        expected = definition["expected"]
        comparisons = []
        for candidate in parsed:
            comparisons.append(
                {
                    key: (
                        set(candidate.get(key, [])) == set(value)
                        if key == "source_ids"
                        else type(candidate.get(key)) is type(value) and candidate.get(key) == value
                    )
                    for key, value in expected.items()
                }
            )
        matching = next(
            (
                candidate
                for candidate, comparison in zip(parsed, comparisons, strict=True)
                if all(comparison.values())
            ),
            None,
        )
        read_artifacts = {}
        for task in tasks:
            session_id = task["session_id"]
            read_artifacts[session_id] = self.runtime.get_session(session_id=session_id).model_dump(
                mode="json"
            )
        save(directory / "execution_sessions.json", read_artifacts)
        with closing(self.runtime._conn()) as conn:
            command_rows = [
                dict(r) for r in conn.execute("SELECT * FROM management_commands ORDER BY rowid")
            ]
        commands = []
        for row in command_rows:
            receipt = row.get("receipt")
            commands.append(
                {**row, "receipt": json.loads(receipt) if isinstance(receipt, str) else receipt}
            )
        save(directory / "commands.json", commands)
        reports = snapshot.get("manager_delegate_reports", [])
        task_starts = [r for r in commands if r.get("action") == "start_task"]
        starts_by_manager = {}
        for row in task_starts:
            sid = row.get("manager_session_id")
            starts_by_manager[sid] = starts_by_manager.get(sid, 0) + 1
        task_session_ids = {r["session_id"] for r in tasks}
        task_runs = [
            json.loads(r["record_payload"])
            for r in snapshot.get("agent_runs", [])
            if r["session_id"] in task_session_ids
        ]
        initial_tasks = [
            r
            for r in self.current["initial_snapshot"].get("agent_sessions", [])
            if r.get("kind") == "ordinary"
        ]
        historical_failures = [r["run_id"] for r in task_runs if r["status"] != "completed"]
        checked_task_runs = task_runs
        if recovery:
            latest = {}
            for run in sorted(task_runs, key=lambda r: (r["created_at"], r["run_id"])):
                latest[run["session_id"]] = run
            checked_task_runs = list(latest.values())
            self.current["historical_noncompleted_task_runs_preserved"] = historical_failures
        checks = {
            "independent_manager_count": len(managers) == definition["delegate_count"],
            "ordinary_execution_session_count": len(tasks) == definition["task_count"],
            "latest_tasks_completed_after_recovery"
            if recovery
            else "tasks_are_completed_independent_roots": len(checked_task_runs)
            == definition["task_count"]
            and all(
                r["status"] == "completed" and r.get("parent_run_id") is None
                for r in checked_task_runs
            ),
            "parent_returned_before_execution_tasks": not initial_tasks,
            "each_delegate_started_expected_tasks": all(
                starts_by_manager.get(r["session_id"], 0)
                == definition["task_count"] // definition["delegate_count"]
                for r in managers
            )
            and bool(managers),
            "parent_did_not_start_execution_tasks": starts_by_manager.get(parent, 0) == 0,
            "parent_new_turn_published": any(d["run_id"] != initial_run_id for d in deliveries),
            "reports_persisted": len(reports) >= definition["delegate_count"],
            "reports_have_task_evidence": bool(reports)
            and all(json.loads(r["evidence"]) for r in reports),
            "needs_decision_report": not definition["needs_decision"]
            or any(r["kind"] == "needs_decision" for r in reports),
            "grounded_decision_and_arithmetic": matching is not None,
            "no_denied_tools": not self.current["denied_tools"],
            "synthetic_documents_unchanged": all(
                json.loads((directory / "workspace" / filename).read_text()) == value
                for filename, value in definition["files"].items()
            ),
        }
        # Store exact underlying evidence for review; a fact token in an answer alone is insufficient.
        read_results = []
        for row in snapshot.get("agent_runs", []):
            run_id = row.get("run_id")
            if run_id:
                read_results += self.runtime.agent_run_store.list_completed_tool_results(
                    run_id=run_id, tool_names=("filesystem.read_file",)
                )
        save(directory / "filesystem_read_results.json", read_results)
        serialized_reads = json.dumps(read_results, ensure_ascii=False, default=str)
        checks["all_required_files_actually_read"] = all(
            filename in serialized_reads for filename in definition["files"]
        )
        checks.update(
            trace_checks(
                snapshot, parent=parent, definition=definition, initial_run_id=initial_run_id
            )
        )
        complete = all(checks.values())
        status = result_status(checks, stop=self.current.get("stopped"))
        self.current.update(
            status=status,
            checks=checks,
            passed=complete and status == "passed",
            parent_delivery_count=len(deliveries),
            delegate_count=len(managers),
            execution_session_count=len(tasks),
            report_count=len(reports),
            extracted_decisions=parsed,
            decision_comparisons=comparisons,
            semantic_review_required=True,
        )


def configured_evaluation(config_path, env_path):
    source = load_local_config(config_path)
    clients = source.llm.client_configs()
    selected = next(
        (c for c in clients if c.name == source.llm.default_client), clients[0] if clients else None
    )
    if (
        selected is None
        or selected.provider != "openai_compatible"
        or selected.default_model != "deepseek-flash"
    ):
        raise ValueError(
            "Default configured model is unavailable or lacks the existing evaluation pricing"
        )
    secret = selected.resolved_api_key() or _dotenv_value(selected.api_key_env, env_path)
    if not secret:
        raise ValueError("Configured provider credential unavailable")
    protected = tuple({*_protected_credential_values(env_path), secret})
    selected = selected.model_copy(deep=True)
    selected.timeout_seconds = 180
    os.environ[selected.api_key_env] = secret
    config = LocalAppConfig()
    config.llm = source.llm.model_copy(deep=True)
    config.llm.clients = [selected]
    config.llm.default_client = selected.name
    config.llm.model = selected.default_model
    config.llm.fallback_client = None
    config.llm.max_attempts = 1
    config.agent.orchestrator = "langgraph"
    config.agent.checkpoint_backend = "sqlite"
    config.agent.unified_entry_enabled = True
    config.agent.decision_thinking_enabled = source.agent.decision_thinking_enabled
    config.agent.multi_agent_planning_enabled = False
    config.management.enabled = True
    config.management.autonomous_enabled = False
    config.management.min_wake_interval_seconds = 0
    config.memory.enabled = False
    config.message_history.enabled = False
    config.embedding.enabled = False
    config.reranker.enabled = False
    config.safety.tool_review_mode = source.safety.tool_review_mode
    return config, selected, protected


def run_live(args, names):
    """Private configuration is read only behind both CLI dispatch gates."""
    import urllib.request
    from urllib.parse import urlsplit

    previous_env = dict(os.environ)
    original_urlopen = urllib.request.urlopen
    evaluation = None
    try:
        for key in tuple(os.environ):
            if key.startswith("LKA_"):
                os.environ.pop(key)
        config, client, protected = configured_evaluation(args.local_config, args.env)
        endpoint = urlsplit(client.base_url)
        if endpoint.scheme != "https":
            raise ValueError("Live evaluation requires an HTTPS model endpoint")

        def model_only(request, *positional, **keywords):
            parsed = urlsplit(request.full_url if hasattr(request, "full_url") else str(request))
            if (
                parsed.scheme != "https"
                or parsed.netloc != endpoint.netloc
                or parsed.path != endpoint.path.rstrip("/") + "/chat/completions"
            ):
                raise PermissionError("Only the configured model endpoint is permitted")
            return original_urlopen(request, *positional, **keywords)

        args.output.mkdir(parents=True)
        budget = bounded_budget(
            args.ledger or args.output / "budget.sqlite3",
            usd_limit=args.usd_limit,
            call_limit=args.call_limit,
        )
        evaluation = Evaluation(
            args.output.resolve(), config, budget, protected, max_steps=args.max_steps
        )
        urllib.request.urlopen = model_only
        for name in names:
            evaluation.run_case(name)
            if not evaluation.report["cases"][-1].get("passed", False):
                break
        evaluation.report["passed"] = len(evaluation.report["cases"]) == len(names) and all(
            c.get("passed", False) for c in evaluation.report["cases"]
        )
        evaluation.persist()
        print(
            json.dumps(
                {
                    "passed": evaluation.report["passed"],
                    "budget": budget.snapshot(),
                    "report": str(args.output / "report.json"),
                }
            )
        )
        return 0 if evaluation.report["passed"] else 1
    finally:
        urllib.request.urlopen = original_urlopen
        os.environ.clear()
        os.environ.update(previous_env)


def bounded_budget(path, *, usd_limit, call_limit):
    """Persist ceilings in addition to charges; retries cannot silently raise them."""
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables - {"calls", "management_eval_limits"}:
                raise ValueError("Ledger must be a dedicated evaluation database")
    budget = LiveBudget(path, usd_limit=usd_limit, call_limit=call_limit, search_limit=0)
    with budget.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS management_eval_limits(usd REAL, calls INTEGER)")
        prior = conn.execute("SELECT usd,calls FROM management_eval_limits").fetchone()
        if prior and (usd_limit > prior[0] or call_limit > prior[1]):
            raise ValueError(
                "Shared ledger ceilings cannot be raised; preserve the authorized budget"
            )
        if prior:
            conn.execute("UPDATE management_eval_limits SET usd=?,calls=?", (usd_limit, call_limit))
        else:
            conn.execute("INSERT INTO management_eval_limits VALUES(?,?)", (usd_limit, call_limit))
    return budget
