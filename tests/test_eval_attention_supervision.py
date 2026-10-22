"""Offline acceptance predicates and dispatch gates; never instantiate a runtime."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evals.lka_evals import attention_supervision as evaluation


def test_dry_plan_never_dispatches_or_creates_output(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dry plan dispatched live evaluation")
    monkeypatch.setattr(evaluation, "run_live", forbidden)
    missing = tmp_path / "missing-private-config.toml"
    output = tmp_path / "new-output"
    assert evaluation.main(["--llm-mode", "real", "--local-config", str(missing),
                            "--output", str(output)]) == 0
    assert not output.exists()
    assert '"run": false' in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [
    ["--run"], ["--run", "--case", "implicit_followup"],
    ["--run", "--llm-mode", "real"],
    ["--call-limit", "81"], ["--usd-limit", "1.51"], ["--usd-limit", "nan"],
    ["--max-steps", "21"], ["--case", "implicit_followup", "--case", "implicit_followup"],
])
def test_invalid_dispatch_gates_fail_before_loading_configuration(arguments, monkeypatch):
    monkeypatch.setattr(evaluation, "run_live", lambda *args: pytest.fail("Gate dispatched"))
    with pytest.raises(SystemExit) as error:
        evaluation.main(arguments)
    assert error.value.code == 2


def test_explicit_paid_dispatch_passes_selected_cases_and_fixed_ceilings(monkeypatch, tmp_path):
    captured = {}
    def dispatch(args, names):
        captured.update(names=names, calls=args.call_limit, usd=args.usd_limit, steps=args.max_steps)
        return 7
    monkeypatch.setattr(evaluation, "run_live", dispatch)
    assert evaluation.main(["--run", "--llm-mode", "real", "--case", "cancel_followup",
                            "--output", str(tmp_path / "new")]) == 7
    assert captured == {"names": ["cancel_followup"], "calls": 80, "usd": 1.50, "steps": 20}


def test_existing_output_is_preserved(monkeypatch, tmp_path):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("prior evidence")
    monkeypatch.setattr(evaluation, "run_live", lambda *args: pytest.fail("Gate dispatched"))
    with pytest.raises(SystemExit):
        evaluation.main(["--run", "--llm-mode", "real", "--case", "implicit_followup", "--output", str(tmp_path)])
    assert sentinel.read_text() == "prior evidence"


def test_continuation_requires_future_timer_or_nonterminal_wait():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    goal = {"status": "waiting", "wait_run_id": "child"}
    assert evaluation.live_continuation(goal, [{"run_id": "child", "status": "queued"}], now=now)
    assert not evaluation.live_continuation(goal, [{"run_id": "child", "status": "completed"}], now=now)
    assert not evaluation.live_continuation(goal, [{"run_id": "other", "status": "running"}], now=now)
    goal["next_wake_at"] = (now + timedelta(minutes=5)).isoformat()
    assert evaluation.live_continuation(goal, [], now=now)
    goal["next_wake_at"] = now.isoformat()
    assert not evaluation.live_continuation(goal, [], now=now)
    goal["next_wake_at"] = "2026-01-02T00:00:00"
    assert not evaluation.live_continuation(goal, [], now=now)
    goal.update(status="completed", next_wake_at=(now + timedelta(days=1)).isoformat())
    assert not evaluation.live_continuation(goal, [], now=now)


def test_completion_and_cancellation_clear_continuations():
    assert evaluation.closed_goal({"status": "completed", "wait_run_id": None, "next_wake_at": None})
    assert not evaluation.closed_goal({"status": "completed", "wait_run_id": "terminal-child"})
    assert not evaluation.closed_goal({"status": "cancelled", "next_wake_at": "future"}, cancelled=True)
    assert not evaluation.closed_goal({"status": "waiting"}, cancelled=True)
    assert evaluation.closed_goal({"status": "paused"}, cancelled=True)
    assert evaluation.closed_goal({"status": "cancelled"}, cancelled=True)


def test_read_provenance_rejects_mentions_stale_sources_and_partial_reads():
    path = Path("/disposable/workspace/release.json")
    source = {"release_id": "opaque", "revision": 2, "package_ready": True,
              "validation_passed": True, "approved_revision": 2}
    import json
    event = {"tool_name": "filesystem.read_file", "result": {"status": "completed", "output": {
        "resolved_path": str(path), "content": json.dumps(source), "truncated": False}}}
    assert evaluation.source_read(event, source, path)
    assert not evaluation.source_read(event, {**source, "revision": 3}, path)
    assert not evaluation.source_read(event, source, Path("/another/release.json"))
    event["result"]["output"]["truncated"] = True
    assert not evaluation.source_read(event, source, path)
    assert not evaluation.source_read({"tool_name": "management.read_task_result", "result": event["result"]}, source, path)
    assert not evaluation.source_read({"tool_name": "filesystem.read_file", "result": {
        "status": "completed", "output": {"path": str(path), "answer": json.dumps(source)}}}, source, path)


def test_business_completion_requires_current_approval_and_exact_booleans():
    source = {"revision": 2, "approved_revision": 2, "package_ready": True, "validation_passed": True}
    assert evaluation.ready(source)
    for patch in ({"approved_revision": 1}, {"validation_passed": None}, {"package_ready": 1},
                  {"revision": True, "approved_revision": True}, {"approved_revision": "2"}):
        assert not evaluation.ready({**source, **patch})


def test_silent_publication_is_review_and_foreign_events_are_not():
    state = {"managed_session_links": [{"manager_session_id": "parent", "run_id": "task"}],
             "agent_runs": [{"run_id": "task", "status": "completed"}],
             "manager_executions": [{"execution_id": "exec", "session_id": "parent",
                                      "run_id": "review", "status": "completed"}],
             "manager_inbox": [{"session_id": "parent", "kind": "task_terminal",
                                "payload": '{"run_id":"task"}', "consumed_execution_id": "exec"}],
             "manager_deliveries": []}
    results = [{"run_id": "review", "session_id": "parent", "tool_name": "management.read_task_result",
                "input": {"run_id": "task"},
                "result": {"status": "completed", "output": {"run": {"run_id": "task"}}}}]
    assert evaluation.background_reviewed(state, "parent", results)
    assert not evaluation.background_reviewed(state, "parent", [])
    results[0]["input"]["run_id"] = "other-task"
    assert not evaluation.background_reviewed(state, "parent", results)
    results[0]["input"]["run_id"] = "task"
    state["manager_inbox"][0]["payload"] = '{"run_id":"foreign"}'
    assert not evaluation.background_reviewed(state, "parent", results)
    state["manager_inbox"][0]["payload"] = '{"run_id":"task"}'
    state["manager_executions"][0]["status"] = "failed"
    assert not evaluation.background_reviewed(state, "parent", results)


def test_foreign_and_missing_status_runs_do_not_supply_continuation():
    goal = {"status": "waiting", "wait_run_id": "foreign"}
    state = {"managed_session_links": [{"manager_session_id": "other", "run_id": "foreign"}],
             "agent_runs": [{"run_id": "foreign", "status": "queued"}]}
    assert not evaluation.live_continuation(goal, evaluation.owned_runs(state, "parent"))
    assert not evaluation.live_continuation(goal, [{"run_id": "foreign"}])


def test_fresh_read_after_premature_closure_is_not_grounded_completion():
    import json
    source = {"revision": 2}
    path = Path("/synthetic/release.json")
    read = {"session_id": "task", "run_id": "task-run", "tool_name": "filesystem.read_file", "result": {
        "status": "completed", "output": {"resolved_path": str(path), "truncated": False,
                                             "content": json.dumps(source)}}}
    review = {"session_id": "parent", "tool_name": "management.read_task_result",
              "input": {"run_id": "task-run"}, "result": {"status": "completed",
              "output": {"run": {"run_id": "task-run"}}}}
    close = {"session_id": "parent", "tool_name": "management.set_goal", "input": {
        "goal_id": "original", "status": "completed"}, "result": {"status": "completed"}}
    assert evaluation.grounded_completion([read, review, close], "parent", "original", source, path)
    assert not evaluation.grounded_completion([close, read, review], "parent", "original", source, path)
    assert not evaluation.grounded_completion([read, close], "parent", "original", source, path)
    assert not evaluation.grounded_completion([read, review, close], "parent", "replacement", source, path)
    review["input"]["run_id"] = "unrelated-run"
    assert not evaluation.grounded_completion([read, review, close], "parent", "original", source, path)


def test_central_pulse_requires_matching_consumption_and_completed_execution():
    state = {"manager_inbox": [{"event_id": 7, "session_id": "parent", "kind": "timer",
        "goal_id": "attention", "payload": '{"attention_sweep":true,"generation":1}',
        "consumed_execution_id": "sweep"}], "manager_executions": [{
        "execution_id": "sweep", "session_id": "parent", "run_id": "manager-run",
        "event_ids": "[7]", "status": "completed"}], "manager_deliveries": []}
    assert evaluation.central_executions(state, "parent") == state["manager_executions"]
    assert not evaluation.central_executions(state, "foreign")
    state["manager_inbox"][0]["consumed_execution_id"] = "another"
    assert not evaluation.central_executions(state, "parent")
    state["manager_inbox"][0]["consumed_execution_id"] = "sweep"
    state["manager_inbox"][0]["payload"] = '{"timer_generation":1}'
    assert not evaluation.central_executions(state, "parent")
    state["manager_inbox"][0]["payload"] = '{"attention_sweep":true}'
    state["manager_executions"][0]["status"] = "bound"
    assert not evaluation.central_executions(state, "parent")


def test_central_fresh_source_must_belong_to_pulse_or_reviewed_child():
    import json
    source, path = {"revision": 2, "approved_revision": 1}, Path("/synthetic/release_a.json")
    execution = {"run_id": "central-manager"}
    read = {"session_id": "parent", "run_id": "central-manager", "tool_name": "filesystem.read_file",
        "result": {"status": "completed", "output": {"resolved_path": str(path),
                    "truncated": False, "content": json.dumps(source)}}}
    assert evaluation.sweep_acquired(execution, "parent", [read], source, path)
    read.update(session_id="task", run_id="task-run")
    assert not evaluation.sweep_acquired(execution, "parent", [read], source, path)
    review = {"session_id": "parent", "run_id": "central-manager", "tool_name": "management.read_task_result",
        "input": {"run_id": "task-run"}, "result": {"status": "completed", "output": {"run": {"run_id": "task-run"}}}}
    assert evaluation.sweep_acquired(execution, "parent", [read, review], source, path)
    review["run_id"] = "old-manager"
    assert not evaluation.sweep_acquired(execution, "parent", [read, review], source, path)
    assert not evaluation.ready(source)


def test_source_scope_accepts_aggregate_goal_and_rejects_unrelated_goal():
    refs = [("release_a.json", "发布 A"), ("release_b.json", "发布 B")]
    assert evaluation.objective_covers([{"objective": "持续负责发布 A 和发布 B 的验收"}], refs)
    assert evaluation.objective_covers([{"objective": "负责 release_a.json"}, {"objective": "负责 release_b.json"}], refs)
    assert not evaluation.objective_covers([{"objective": "负责另一个发布"}], refs)
    assert not evaluation.objective_covers([{"objective": "只负责发布 A"}], refs)


def test_new_central_case_is_dry_by_default(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(evaluation, "run_live", lambda *args: pytest.fail("Dry central plan dispatched"))
    assert evaluation.main(["--case", "central_sweep", "--local-config", str(tmp_path / "absent.toml")]) == 0
    assert '"central_sweep"' in capsys.readouterr().out
