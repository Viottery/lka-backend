"""Opt-in real-model supervision of an implicitly assumed release responsibility.

Dry planning imports no runtime/configuration and never dispatches providers.
An ongoing user mandate authorizes this responsibility, not arbitrary history scans.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path

CASES = ("implicit_followup", "cancel_followup", "central_sweep")
READ_TOOLS = frozenset(("filesystem.read_file", "filesystem.list_directory"))
ALLOWED_TOOLS = READ_TOOLS | {"management." + name for name in (
    "start_task", "send_instruction", "read_task_result", "list_goals", "read_goal",
    "set_goal", "read_events", "decide_notification", "inspect_run", "read_session", "control_run",
)}
TERMINAL = {"completed", "failed", "cancelled", "timed_out"}
LIVE = {"queued", "running", "paused", "waiting_confirmation", "waiting_user"}


def live_continuation(goal, runs, *, now=None):
    """A finished run is not an actionable continuation of unfinished business."""
    if goal.get("status") not in {"active", "waiting"}:
        return False
    waiting = goal.get("wait_run_id")
    if waiting and any(r.get("run_id") == waiting and r.get("status") in LIVE for r in runs):
        return True
    try:
        wake = datetime.fromisoformat(goal["next_wake_at"])
        return wake.tzinfo is not None and wake > (now or datetime.now(UTC))
    except (KeyError, ValueError, TypeError):
        return False


def owned_runs(state, parent):
    ids = {r["run_id"] for r in state.get("managed_session_links", []) if r["manager_session_id"] == parent}
    return [r for r in state.get("agent_runs", []) if r["run_id"] in ids]


def reviewed_run(event, run_id):
    result = event.get("result", {})
    return (isinstance(run_id, str) and bool(run_id)
            and event.get("tool_name") == "management.read_task_result"
            and event.get("input", {}).get("run_id") == run_id
            and event.get("input", {}).get("section", "answer") == "answer"
            and result.get("status") == "completed"
            and result.get("output", {}).get("run", {}).get("run_id") == run_id)


def background_reviewed(state, parent, tool_results):
    """Silent publication is a completed execution, independent of user delivery."""
    executions = {e["execution_id"]: e for e in state.get("manager_executions", [])
                  if e.get("session_id") == parent and e.get("status") == "completed"}
    run_ids = {r["run_id"] for r in owned_runs(state, parent)}
    for event in state.get("manager_inbox", []):
        execution = executions.get(event.get("consumed_execution_id"))
        payload = event.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        if (execution and event.get("session_id") == parent and event.get("kind") == "task_terminal"
                and payload.get("run_id") in run_ids and any(
                    r.get("run_id") == execution["run_id"] and r.get("session_id") == parent
                    and reviewed_run(r, payload["run_id"]) for r in tool_results)):
            return True
    return False


def central_executions(state, parent):
    """Completed publications consuming genuine central pulse events, including silent ones."""
    events = {}
    for event in state.get("manager_inbox", []):
        payload = event.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        if (event.get("session_id") == parent and event.get("kind") == "timer"
                and payload.get("attention_sweep") is True and event.get("goal_id")):
            events[event["event_id"]] = event
    result = []
    for execution in state.get("manager_executions", []):
        ids = execution.get("event_ids", [])
        if isinstance(ids, str):
            ids = json.loads(ids)
        if (execution.get("session_id") == parent and execution.get("status") == "completed"
                and any(event_id in events and events[event_id].get("consumed_execution_id") == execution["execution_id"]
                        for event_id in ids)):
            result.append(execution)
    return result


def sweep_acquired(execution, parent, events, source, path):
    """Evidence belongs to this pulse's manager or an owned child it actually reviewed."""
    reviews = [e for e in events if e.get("session_id") == parent and e.get("run_id") == execution["run_id"]]
    return any(source_read(e, source, path) and (e.get("run_id") == execution["run_id"]
        and e.get("session_id") == parent or any(reviewed_run(r, e.get("run_id")) for r in reviews))
        for e in events)


def objective_covers(goals, references):
    text = "\n".join(g.get("objective", "") for g in goals)
    return all(any(reference in text for reference in alternatives) for alternatives in references)


def closed_goal(goal, *, cancelled=False):
    return (goal.get("status") in ({"cancelled", "paused"} if cancelled else {"completed"})
            and goal.get("next_wake_at") is None and goal.get("wait_run_id") is None)


def source_read(event, source, source_path):
    """Require exact acquired source contents; filenames/answers are not evidence."""
    result = event.get("result", {})
    output = result.get("output", {})
    if (event.get("tool_name") != "filesystem.read_file" or result.get("status") != "completed"
            or output.get("truncated") is not False
            or output.get("resolved_path") != str(source_path)):
        return False
    try:
        return json.loads(output["content"]) == source
    except (KeyError, TypeError, ValueError):
        return False


def grounded_completion(events, parent, goal_id, source, path):
    """Closure must follow fresh acquisition and the manager's review of child evidence."""
    for index, event in enumerate(events):
        if (event.get("session_id") != parent or event.get("tool_name") != "management.set_goal"
                or event.get("result", {}).get("status") != "completed"
                or event.get("input", {}).get("goal_id") != goal_id
                or event.get("input", {}).get("status") != "completed"):
            continue
        for offset, read in enumerate(events[:index]):
            if source_read(read, source, path) and (read.get("session_id") == parent or any(
                    review.get("session_id") == parent and reviewed_run(review, read.get("run_id"))
                    for review in events[offset + 1:index])):
                return True
    return False


def ready(source):
    return (source.get("package_ready") is True and source.get("validation_passed") is True
            and type(source.get("revision")) is int
            and type(source.get("approved_revision")) is int
            and source["approved_revision"] == source["revision"])


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class Evaluation:
    def __init__(self, output, config, budget, protected, *, max_steps):
        self.output, self.config, self.budget, self.protected = output, config, budget, protected
        self.max_steps, self.runtime = max_steps, None
        self.report = {"synthetic_only": True, "production_modified": False, "cases": [],
                       "allowed_tools": sorted(ALLOWED_TOOLS), "max_output_tokens_ceiling": 8192,
                       "transport_timeout_seconds": 60, "driver_iterations_per_case": max_steps,
                       "provider": config.llm.default_client, "model": config.llm.model,
                       "agent_configuration": config.agent.model_dump(mode="json"),
                       "tool_review_mode": config.safety.tool_review_mode,
                       "authority": "Current ordinary user mandate only; no arbitrary history scanning",
                       "scope": "User delegates verification and ongoing ownership, not goals/timers; spontaneous decomposition is not scored",
                       "semantic_review_required": True}

    def persist(self):
        self.report["budget"] = self.budget.snapshot()
        save(self.output / "report.json", self.report)

    def open_runtime(self, directory):
        from app.core.runtime import LocalKnowledgeAgentRuntime
        from evals.lka_evals.management_runtime import EvalSettings, RecordedClient

        workspace = directory / "workspace"
        runtime = LocalKnowledgeAgentRuntime(EvalSettings(
            _env_file=None, evaluation_config=self.config, LKA_DATA_DIR=directory / "runtime",
            LKA_WORKSPACE_ROOTS=str(workspace), LKA_LOCAL_CONFIG=directory / "missing.toml"))
        self.runtime = runtime  # Ensure cleanup also covers a confinement setup failure.
        original_policy = runtime._session_run_policy

        def scoped_policy(session_id):
            policy = original_policy(session_id)
            ceiling = ALLOWED_TOOLS if policy.allowed_tools is None else ALLOWED_TOOLS.intersection(policy.allowed_tools)
            return policy.model_copy(update={"allowed_tools": sorted(ceiling), "allow_fork": False})

        runtime._session_run_policy = scoped_policy
        runtime.session_service.run_policy_provider = scoped_policy
        runtime.managed_tasks.policy = scoped_policy
        for client in runtime.agent_llm_client.registry.list_clients():
            runtime.agent_llm_client.registry.register_client(RecordedClient(
                client, self.budget, allowed_model=self.config.llm.model,
                protected_values=self.protected, output=self.output / "model_calls.jsonl"))
        execute = runtime.tool_executor.execute

        def confined(*args, **kwargs):
            name, inputs, context = kwargs["tool_name"], kwargs["tool_input"], kwargs["context"]
            if name not in ALLOWED_TOOLS:
                self.current["denied_tools"].append(name)
                raise PermissionError("Evaluation tool ceiling exceeded")
            for key in ("path", "cwd", "workspace_path"):
                if inputs.get(key):
                    value = str(inputs[key])
                    for variable in ("${workspace_root}", "$workspace_root", "${WORKSPACE_ROOT}", "$WORKSPACE_ROOT", "${LKA_WORKSPACE_ROOT}", "$LKA_WORKSPACE_ROOT"):
                        value = value.replace(variable, str(workspace))
                    candidate = Path(value)
                    candidate = candidate if candidate.is_absolute() else workspace / candidate
                    if not candidate.resolve().is_relative_to(workspace.resolve()):
                        self.current["denied_tools"].append(name + ":outside_workspace")
                        raise PermissionError("Evaluation path leaves synthetic workspace")
            result = execute(*args, **kwargs)
            self.current["tool_results"].append({"phase": self.phase, "run_id": context.run_id,
                "session_id": context.session_id, "tool_name": name, "input": inputs,
                "result": result.model_dump(mode="json")})
            return result

        runtime.tool_executor.execute = confined
        return runtime

    def snapshot(self):
        from contextlib import closing
        with closing(self.runtime._conn()) as conn:
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            selected = [n for n in names if n.replace("_", "").isalpha() and
                        (n.startswith(("manager_", "managed_", "agent_run", "agent_session"))
                         or n in {"background_jobs", "agent_tool_invocations", "management_commands"})]
            return {name: [dict(r) for r in conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid LIMIT 1000')]
                    for name in selected}

    def checkpoint(self, label):
        state = self.snapshot()
        save(self.directory / (label + ".json"), {"state": state, "tool_results": self.current["tool_results"]})
        self.persist()
        return state

    def require(self, name, condition):
        key = self.phase + ":" + name
        self.current["checks"][key] = bool(condition)
        if not condition:
            raise AssertionError(key)

    def turn(self, parent, prompt):
        from app.core.safety import SafetyReviewMode
        self.current["prompts"].append({"phase": self.phase, "text": prompt})
        result = self.runtime.run_agent_turn(session_id=parent, user_input=prompt, resumable=True,
            safety_review_mode=SafetyReviewMode(self.config.safety.tool_review_mode))
        self.current.setdefault("foreground_runs", []).append(result.model_dump(mode="json"))
        self.require("foreground_run_completed",
                     self.runtime.agent_run_manager.get_run(result.run_id).status.value == "completed")

    def drive(self, predicate):
        # Never advance the clock or make a future timer due artificially.
        for _ in range(self.max_steps - len(self.current["steps"])):
            tick = self.runtime.management_coordinator.tick()
            bound = self.runtime.management_coordinator.run_one()
            executed = self.runtime.agent_dispatch.run_one()
            self.current["steps"].append({"phase": self.phase, "tick": tick,
                                          "bound": bool(bound), "executed": bool(executed)})
            state = self.checkpoint("latest")
            if predicate(state):
                return state
            if not bound and not executed:
                break
        return self.snapshot()

    def successful(self, name, *, phase=None, session=None):
        return [r for r in self.current["tool_results"] if r["tool_name"] == name
                and r["result"].get("status") == "completed"
                and (phase is None or r["phase"] == phase)
                and (session is None or r["session_id"] == session)]

    def run_case(self, name):
        if name == "central_sweep":
            return self.run_central_sweep()
        import secrets
        self.directory = self.output / name
        workspace = self.directory / "workspace"
        workspace.mkdir(parents=True)
        path = workspace / "release.json"
        source = {"release_id": secrets.token_hex(12), "revision": 1, "package_ready": False,
                  "validation_passed": None, "approved_revision": None,
                  "build_status": "automated build and validation pending; no user action needed"}
        save(path, source)
        save(self.directory / "source_revision_1.json", source)
        save(self.directory / "oracle.json", {"completion": "package_ready=true AND validation_passed=true AND approved_revision=revision",
             "initial_ready": False, "updated_ready": True, "cancel": "same responsibility stopped; no future timer/task wait"})
        self.current = {"name": name, "checks": {}, "prompts": [], "tool_results": [],
                        "steps": [], "denied_tools": [], "passed": False}
        self.report["cases"].append(self.current)
        self.phase = "formation"
        try:
            self.open_runtime(self.directory)
            parent = self.runtime.management_service.create(title="Synthetic release responsibility")["session"]["session_id"]
            self.runtime.management_service.update(parent, expected_revision=1, status="active")
            self.current["parent_session_id"] = parent
            self.turn(parent, f"请负责核查 {path} 中这次发布是否具备交付条件。请核对发布包、验证结果和版本批准是否相互一致；"
                      "只有 package_ready 和 validation_passed 都为 true，且 approved_revision 等于 revision 才算办妥。"
                      "请安排执行会话核查并保留来源依据。没办妥就继续负责跟进，只有需要我作实质决定时才问我。")
            initial = self.checkpoint("formation")
            goals = initial.get("manager_goals", [])
            self.require("spontaneous_model_goal", len(goals) == 1 and bool(self.successful("management.set_goal", session=parent)))
            goal = goals[0]
            gid = goal["goal_id"]
            self.current["original_goal_id"] = gid
            self.require("actionable_initial_continuation", live_continuation(goal, owned_runs(initial, parent)))
            self.runtime.stop()
            self.open_runtime(self.directory)
            reopened = self.checkpoint("reconstructed_before_dispatch")
            self.require("durable_before_dispatch", reopened.get("manager_goals") == goals)
            self.phase = "unresolved_review"
            def reviewed(state):
                return background_reviewed(state, parent, self.current["tool_results"])
            state = self.drive(reviewed)
            tasks = [r for r in initial.get("agent_sessions", []) if r.get("kind") == "ordinary"]
            self.current["task_supervision_exercised"] = bool(tasks)
            self.require("substantive_task_dispatched", bool(tasks) and bool(self.successful("management.start_task", session=parent)))
            task_ids = {t["session_id"] for t in tasks}
            self.require("task_actually_read_partial_source", any(r["session_id"] in task_ids and source_read(r, source, path)
                         for r in self.current["tool_results"]))
            self.require("terminal_child_triggered_background_publication", reviewed(state) and
                         any(e.get("kind") == "task_terminal" and e.get("consumed_execution_id") not in {None, "obsolete"}
                             for e in state.get("manager_inbox", [])))
            self.require("background_read_owned_result", reviewed(state))
            current = next((g for g in state.get("manager_goals", []) if g["goal_id"] == gid), {})
            self.require("same_unresolved_goal_with_live_continuation", live_continuation(current, owned_runs(state, parent)))
            self.checkpoint("unresolved_review")
            self.phase = "cancel" if name == "cancel_followup" else "updated_source"
            if name == "cancel_followup":
                self.turn(parent, "这次发布的整项工作请到此停止，后续也不要再继续核查或向我提醒。")
            else:
                source = {**source, "revision": 2, "package_ready": True, "validation_passed": True,
                          "approved_revision": 2, "build_status": "complete"}
                save(path, source)
                save(self.directory / "source_revision_2.json", source)
                self.turn(parent, "发布来源已经更新，请接着处理这次发布核查。")
            before_drive = self.checkpoint("after_user_update")
            state = before_drive if name == "cancel_followup" else self.drive(lambda s: any(g["goal_id"] == gid and closed_goal(g, cancelled=name == "cancel_followup")
                                           for g in s.get("manager_goals", [])))
            goal = next((g for g in state.get("manager_goals", []) if g["goal_id"] == gid), {})
            self.require("same_goal_closed_and_waits_cleared", closed_goal(goal, cancelled=name == "cancel_followup"))
            self.require("no_replacement_goals", {g["goal_id"] for g in state.get("manager_goals", [])} == {gid})
            if name == "implicit_followup":
                self.require("fresh_exact_completed_source", ready(source) and any(
                    source_read(r, source, path) for r in self.current["tool_results"] if r["phase"] == self.phase))
                self.require("grounded_closure_after_fresh_read", grounded_completion(
                    [r for r in self.current["tool_results"] if r["phase"] == self.phase], parent, gid, source, path))
                self.require("reused_task_or_direct_simple_check", any(r["input"].get("session_id") in task_ids
                    for r in self.successful("management.send_instruction", phase=self.phase, session=parent))
                    or any(source_read(r, source, path) for r in self.successful("filesystem.read_file", phase=self.phase, session=parent)))
            else:
                self.runtime.stop()
                self.open_runtime(self.directory)
                self.phase = "cancel_reconstruction"
                resumed = self.drive(lambda _: False)
                self.require("no_new_work_or_delivery_after_reconstruction", resumed.get("manager_deliveries", []) == state.get("manager_deliveries", [])
                    and resumed.get("agent_runs", []) == state.get("agent_runs", [])
                    and not any(s["executed"] or s["bound"] for s in self.current["steps"] if s["phase"] == self.phase))
                self.require("cancel_does_not_resume", not any(r.get("status") not in TERMINAL for r in self.snapshot().get("agent_runs", [])))
            self.current["passed"] = not self.current["denied_tools"]
            self.require("tool_confinement", self.current["passed"])
        except Exception as exc:  # noqa: BLE001 -- Persist unexpected provider/runtime failures.
            self.current.update(passed=False, failure_phase=self.phase, error_type=type(exc).__name__, error=str(exc))
        finally:
            if self.runtime is not None:
                try:
                    self.checkpoint("final")
                finally:
                    self.runtime.stop()
                    self.runtime = None
            self.persist()


    def drive_central(self, predicate):
        import time
        for _ in range(self.max_steps - len(self.current["steps"])):
            if time.monotonic() >= self.central_deadline:
                break
            tick = self.runtime.management_coordinator.tick()
            bound = self.runtime.management_coordinator.run_one()
            executed = self.runtime.agent_dispatch.run_one()
            self.current["steps"].append({"phase": self.phase, "tick": tick,
                                          "bound": bool(bound), "executed": bool(executed)})
            state = self.checkpoint("latest")
            if predicate(state):
                return state
            if not bound and not executed:
                time.sleep(min(1.0, max(0, self.central_deadline - time.monotonic())))
        return self.snapshot()

    def run_central_sweep(self):
        import secrets
        import time
        self.directory = self.output / "central_sweep"
        workspace = self.directory / "workspace"
        workspace.mkdir(parents=True)
        paths = [workspace / "release_a.json", workspace / "release_b.json"]
        sources = [{"release_id": "release_" + label + "_" + secrets.token_hex(8),
                    "revision": 1, "package_ready": False, "validation_passed": False,
                    "approved_revision": 1, "build_status": "automated build pending; no user action needed"}
                   for label in ("a", "b")]
        for index, (path, source) in enumerate(zip(paths, sources, strict=True)):
            save(path, source)
            save(self.directory / f"source_{index}_revision_1.json", source)
        save(self.directory / "oracle.json", {"initial_ready": [False, False],
            "changed_A": "package/validation ready at revision2, approval stale at revision1; decision needed",
            "B": "unchanged automated work pending; retain responsibility",
            "central_trigger_required": True, "user_update_after_source_change": False,
            "notification": "meaningful changed-source decision once, unchanged sweep silent"})
        self.current = {"name": "central_sweep", "checks": {}, "prompts": [], "tool_results": [],
                        "steps": [], "denied_tools": [], "passed": False,
                        "configuration_override": {"attention_sweep_interval_seconds": 1,
                                                   "min_wake_interval_seconds": 0},
                        "real_wait_ceiling_seconds": 300}
        self.report["cases"].append(self.current)
        self.phase = "central_formation"
        self.central_deadline = time.monotonic() + 300
        try:
            self.open_runtime(self.directory)
            parent = self.runtime.management_service.create(title="Synthetic two release responsibilities")["session"]["session_id"]
            self.runtime.management_service.update(parent, expected_revision=1, status="active")
            self.current["parent_session_id"] = parent
            self.turn(parent, f"请负责持续核查发布 A（{paths[0]}）和发布 B（{paths[1]}）是否可交付。"
                "每项只有 package_ready、validation_passed 都为 true 且 approved_revision 等于 revision 才算办妥；"
                "没办妥的继续负责，只有实质变化或需要我作决定时告诉我。")
            initial = self.checkpoint("central_formation")
            goals = initial.get("manager_goals", [])
            references = [(p.name, p.stem, source["release_id"], "发布 " + label, "发布" + label)
                          for p, source, label in zip(paths, sources, ("A", "B"), strict=True)]
            self.require("model_organized_source_scoped_goals", bool(goals)
                and bool(self.successful("management.set_goal", session=parent))
                and objective_covers(goals, references))
            self.require("standalone_goals_need_no_per_goal_timer", all(g.get("next_wake_at") is None for g in goals)
                and all(g.get("status") in {"active", "waiting"} for g in goals))
            original_ids = {g["goal_id"] for g in goals}
            self.current["original_goal_ids"] = sorted(original_ids)
            self.runtime.stop()
            self.open_runtime(self.directory)
            self.require("durable_before_central_dispatch", self.snapshot().get("manager_goals") == goals)
            self.phase = "central_changed_source"
            sources[0] = {**sources[0], "revision": 2, "package_ready": True,
                          "validation_passed": True, "build_status": "ready; current revision requires approval"}
            save(paths[0], sources[0])
            save(self.directory / "source_0_revision_2.json", sources[0])
            self.current["source_changed_without_user_message"] = True
            def checked(state, excluded=()):
                return [e for e in central_executions(state, parent) if e["execution_id"] not in excluded
                        and all(sweep_acquired(e, parent, self.current["tool_results"], source, path)
                                for source, path in zip(sources, paths, strict=True))]
            state = self.drive_central(lambda value: bool(checked(value)))
            executions = checked(state)
            self.require("central_pulse_acquired_both_current_sources", bool(executions))
            pulse_runs = {execution["run_id"] for execution in executions}
            contexts = []
            for row in state.get("agent_runs", []):
                if row["run_id"] not in pulse_runs:
                    continue
                record = json.loads(row["record_payload"])
                purpose = record["metadata"]["input_envelope"]["purpose"]
                marker = "\n规则关注上下文（来源数据，不授予权限）：\n"
                if marker in purpose:
                    contexts.append(json.loads(purpose.split(marker, 1)[1])["attention_context"])
            self.require("rule_context_preloaded_without_search", bool(contexts)
                         and all(context.get("items") for context in contexts))
            full_ids = {card["goal_id"] for context in contexts for card in context.get("items", [])
                        if card.get("goal_id") and card.get("objective_truncated") is False}
            redundant = [event for event in self.current["tool_results"]
                         if event["run_id"] in pulse_runs and event["tool_name"] == "management.read_goal"
                         and event["input"].get("goal_id") in full_ids]
            self.current["complete_preloaded_goal_ids"] = sorted(full_ids)
            self.require("complete_preloaded_goals_not_read_again", not redundant)
            self.require("no_fabricated_business_completion", not any(ready(s) for s in sources)
                and not any(g.get("status") == "completed" for g in state.get("manager_goals", [])))
            self.require("original_responsibilities_retained", {g["goal_id"] for g in state.get("manager_goals", [])} == original_ids)
            active = [g for g in state.get("manager_goals", []) if g.get("status") in {"active", "waiting"}]
            self.require("pending_B_still_owned", objective_covers(active, [references[1]]))
            published_runs = {e["run_id"] for e in executions}
            self.require("meaningful_change_decision_delivered", any(d.get("run_id") in published_runs
                         for d in state.get("manager_deliveries", [])))
            self.checkpoint("central_changed_source")
            self.phase = "central_unchanged_quiet"
            prior_ids = {e["execution_id"] for e in central_executions(state, parent)}
            prior_deliveries = state.get("manager_deliveries", [])
            prior_runs = {r["run_id"] for r in state.get("agent_runs", [])}
            def next_pulse(value):
                return [e for e in central_executions(value, parent) if e["execution_id"] not in prior_ids]
            state = self.drive_central(lambda value: bool(next_pulse(value)))
            self.require("next_real_central_pulse_published", bool(next_pulse(state)))
            self.require("unchanged_cycle_no_duplicate_delivery", state.get("manager_deliveries", []) == prior_deliveries)
            self.current["quiet_source_check_task_runs"] = [r["run_id"] for r in state.get("agent_runs", [])
                if r["run_id"] not in prior_runs and r.get("session_id") != parent]
            self.checkpoint("central_unchanged_quiet")
            self.phase = "central_cancel"
            self.turn(parent, "这两项发布核查都到此停止，后续不要继续处理或提醒我。")
            cancelled = self.checkpoint("central_cancel")
            self.require("all_responsibilities_stopped", {g["goal_id"] for g in cancelled.get("manager_goals", [])} == original_ids
                and all(closed_goal(g, cancelled=True) for g in cancelled.get("manager_goals", [])))
            self.require("no_owned_execution_left_active", not any(r.get("status") in LIVE for r in owned_runs(cancelled, parent)))
            self.runtime.stop()
            self.open_runtime(self.directory)
            self.phase = "central_cancel_reconstruction"
            wait_until = time.monotonic() + 1.1
            while time.monotonic() < wait_until:
                time.sleep(max(0, min(0.2, wait_until - time.monotonic())))
            state = self.drive_central(lambda _: True)
            self.require("no_revival_after_real_pulse_interval", state.get("agent_runs", []) == cancelled.get("agent_runs", [])
                and state.get("manager_deliveries", []) == cancelled.get("manager_deliveries", []))
            self.require("tool_confinement", not self.current["denied_tools"])
            self.current["passed"] = True
        except Exception as exc:  # noqa: BLE001 -- Preserve real runtime/provider failures.
            self.current.update(passed=False, failure_phase=self.phase, error_type=type(exc).__name__, error=str(exc))
        finally:
            if self.runtime is not None:
                try:
                    self.checkpoint("final")
                finally:
                    self.runtime.stop()
                    self.runtime = None
            self.persist()


def run_live(args, names):
    import os
    import urllib.request
    from urllib.parse import urlsplit

    from app.core.local_config import load_local_config
    from evals.lka_evals.management_runtime import bounded_budget, configured_evaluation

    previous, original = dict(os.environ), urllib.request.urlopen
    try:
        for key in tuple(os.environ):
            if key.startswith("LKA_"):
                os.environ.pop(key)
        config, client, protected = configured_evaluation(args.local_config, args.env)
        config.agent = load_local_config(args.local_config).agent.model_copy(deep=True)
        config.agent.orchestrator, config.agent.checkpoint_backend = "langgraph", "sqlite"
        config.agent.unified_entry_enabled, config.agent.multi_agent_planning_enabled = True, False
        config.management.autonomous_enabled = True
        config.management.min_wake_interval_seconds = 0
        config.management.attention_sweep_interval_seconds = 1 if "central_sweep" in names else 0
        client.timeout_seconds = 60
        config.llm.clients = [client]
        endpoint = urlsplit(client.base_url)
        if endpoint.scheme != "https":
            raise ValueError("Configured model endpoint must use HTTPS")
        def model_only(request, *positional, **keywords):
            target = urlsplit(request.full_url if hasattr(request, "full_url") else str(request))
            if (target.scheme != "https" or target.netloc != endpoint.netloc
                    or target.path != endpoint.path.rstrip("/") + "/chat/completions"):
                raise PermissionError("Only configured model endpoint allowed; no search")
            keywords["timeout"] = 60
            return original(request, *positional, **keywords)
        args.output.mkdir(parents=True, exist_ok=False)
        budget = bounded_budget(args.ledger or args.output / "budget.sqlite3",
                                usd_limit=args.usd_limit, call_limit=args.call_limit)
        evaluation = Evaluation(args.output.resolve(), config, budget, protected, max_steps=args.max_steps)
        urllib.request.urlopen = model_only
        for name in names:
            evaluation.run_case(name)
        evaluation.report["passed"] = all(c["passed"] for c in evaluation.report["cases"])
        evaluation.persist()
        print(json.dumps({"passed": evaluation.report["passed"], "report": str(args.output / "report.json")}))
        return 0 if evaluation.report["passed"] else 1
    finally:
        urllib.request.urlopen = original
        os.environ.clear()
        os.environ.update(previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--llm-mode", choices=("offline", "real"), default="offline")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--local-config", type=Path, default=Path("config/local.toml"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--call-limit", type=int, default=80)
    parser.add_argument("--usd-limit", type=float, default=1.50)
    parser.add_argument("--max-steps", type=int, default=20)
    args = parser.parse_args(argv)
    names = args.case or list(CASES)
    if len(set(names)) != len(names):
        parser.error("Select each case at most once")
    if not (1 <= args.call_limit <= 80 and math.isfinite(args.usd_limit) and 0 < args.usd_limit <= 1.50
            and 1 <= args.max_steps <= 20):
        parser.error("Ceilings: 80 calls, $1.50, 20 driver iterations")
    if not args.run:
        print(json.dumps({"run": False, "cases": names, "call_limit": args.call_limit,
                          "usd_limit": args.usd_limit, "search_limit": 0, "max_output_tokens": 8192}))
        return 0
    if args.llm_mode != "real" or not args.case:
        parser.error("Dispatch requires --run --llm-mode real and explicit --case")
    if args.output is None or args.output.exists():
        parser.error("--output must name a new artifact directory")
    return run_live(args, names)


if __name__ == "__main__":
    raise SystemExit(main())
