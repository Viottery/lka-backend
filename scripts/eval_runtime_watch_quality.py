"""Private three-slot mail-watch replay through the real scheduler/child pipeline.

Slot dates are compressed fixture replay, not three naturally elapsed days.
No background services are started. Paid dispatch requires explicit --remote.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.watch_scheduler import WatchScheduler
from app.domains.mail import MailAccountInput, MailMessageInput
from app.domains.watch import WatchInput
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded, instrument_service
from scripts.eval_runtime_memory_quality import (
    LEDGER,
    MODEL,
    OUTPUT,
    RecordedClient,
    isolated_config,
)

MAX_CALLS = 18
CALLS_PER_OCCURRENCE = 6
DISTRACTOR = "PRIVATE-DISTRACTOR-UNAUTHORIZED-739"


class WatchBudget:
    """Attempt limits supplement, never replace, production child admission."""

    def __init__(self, ledger, tag):
        self.ledger, self.tag = ledger, tag
        self.ids, self.counts = [], [0, 0, 0]
        self.occurrence = None
        self.lock = threading.Lock()

    def reserve(self, **kwargs):
        with self.lock:
            index = self.occurrence
            if (kwargs["kind"] != "llm" or index not in range(3)
                    or len(self.ids) >= MAX_CALLS
                    or self.counts[index] >= CALLS_PER_OCCURRENCE):
                raise LiveBudgetExceeded("watch replay attempt allowance exhausted")
            call_id = self.ledger.reserve(**{**kwargs,
                "stage": f"{self.tag}:occurrence{index + 1}:" + kwargs["stage"]})
            self.ids.append(call_id)
            self.counts[index] += 1
            return call_id

    def finish(self, *args, **kwargs):
        self.ledger.finish(*args, **kwargs)


def mail_fingerprint(runtime):
    """Read-only fixture integrity, excluding ordinary run/session writes."""
    with sqlite3.connect(runtime.db_path) as conn:
        rows = {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("mail_accounts", "mail_messages")}
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()


def run_probe(root, ledger, *, config=None, injected_service=None):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    workspace = root / "workspace"
    workspace.mkdir()
    settings = Settings(LKA_DATA_DIR=root / "data", LKA_WORKSPACE_ROOTS=str(workspace))
    config = isolated_config(config or settings.load_local_config())
    config = config.model_copy(update={"memory": config.memory.model_copy(
        update={"enabled": False, "background_enabled": False})})
    if injected_service is None and (config.llm.is_disabled() or config.llm.model != MODEL):
        raise ValueError("watch probe requires configured, priced deepseek-flash")
    with patch.object(Settings, "load_local_config", return_value=config):
        runtime = LocalKnowledgeAgentRuntime(settings)
    if injected_service is not None:
        runtime.agent_llm_client = injected_service
        runtime.agent_turn_loop.llm_client = injected_service
        injected_service.workloads = runtime.llm_workloads
    service = runtime.agent_llm_client
    service.background_timeout_seconds = 30
    budget = WatchBudget(ledger, root.name)
    records, lock = [], threading.Lock()
    for provider in service.registry.list_clients():
        service.registry.register_client(RecordedClient(provider, records, lock))
    instrument_service(service, budget, allowed_model=MODEL)
    report = {"mode": "offline_scripted" if injected_service else "remote",
        "model": MODEL, "slot_mode": "compressed_fixture_replay_not_natural_days",
        "call_limit": MAX_CALLS, "per_occurrence_call_limit": CALLS_PER_OCCURRENCE,
        "production_child_limits": {"tokens": 40000, "calls": 6, "tools": 8, "seconds": 120},
        "private_artifacts": str(root), "occurrences": [], "views": [], "mechanical_checks": {},
        "semantic_review": {"status": "pending_root_review", "independent": True}}
    actual_client = service.config.default_client or service.registry.list_clients()[0].name
    selected = service.config.resolve_model_config(actual_client, MODEL)
    report["inference_config"] = {"actual_client": actual_client,
        "tokenizer_json_path": str(selected.tokenizer_json_path) if selected else None,
        "context_window_tokens": selected.context_window_tokens if selected else None,
        "output_reserve_tokens": selected.output_reserve_tokens if selected else None,
        "profile_selection": "unchanged_production_watch_default"}
    original_child = runtime.run_child_agent_async

    async def capture_child(**kwargs):
        view = kwargs["views"].tool
        before = view.model_dump(mode="json")
        row = {"before": before}
        report["views"].append(row)
        try:
            result = await original_child(**kwargs)
            row["task_status"] = result.status.value
            row["missing_requirements"] = list(result.missing_requirements)
            return result
        finally:
            row["after"] = view.model_dump(mode="json")

    runtime.run_child_agent_async = capture_child
    now = datetime.now(UTC)
    account = MailAccountInput(provider="local_json", email_address="watch@example.test")

    def import_update(index):
        return runtime.import_mail(account=account, messages=[MailMessageInput(
            external_id=f"river-739-{index}", subject="RIVER-739项目评审更新",
            sender="updates@example.test", to=[account.email_address],
            received_at=(now + timedelta(minutes=index)).isoformat(),
            body_text="RIVER-739项目评审地点为南楼。" if index == 0
            else "RIVER-739项目评审地点改为北楼。")])

    started = time.monotonic()
    try:
        imported = import_update(0)
        runtime.import_mail(account=MailAccountInput(provider="local_json",
            email_address="other@example.test"), messages=[MailMessageInput(
                external_id="decoy", subject="RIVER-739项目评审更新", sender="other@example.test",
                received_at=now.isoformat(), body_text=DISTRACTOR)])
        source_id = runtime.mail_knowledge_mirror.source_id_for_account(imported.account_id)
        watch = runtime.watch_service.create(WatchInput(title="项目评审动态",
            goal="关注RIVER-739项目评审的最新地点变化，并说明负责人是否已经确定。",
            timezone="UTC", daily_time="08:00", categories=["mail"],
            scope={"source_ids": [source_id], "account_ids": [imported.account_id],
                "web_enabled": False, "matter_enabled": False, "knowledge_enabled": False}))
        report["authorized_scope"] = watch["scope"]
        scheduler = WatchScheduler(runtime, runtime.watch_service)
        for index in range(3):
            if index == 2:
                import_update(1)
            slot = now - timedelta(days=3 - index)
            runtime.watch_service.create_occurrence(watch["watch_id"], slot)
            before = mail_fingerprint(runtime)
            budget.occurrence = index
            tick = time.monotonic()
            scheduler.run_one(owner="isolated-watch-quality")
            budget.occurrence = None
            occurrence = runtime.watch_service.get_occurrence(watch["watch_id"], slot)
            briefing = next((b for b in runtime.watch_service.list_briefings(
                watch_id=watch["watch_id"]) if b["occurrence_id"] == occurrence["occurrence_id"]), None)
            report["occurrences"].append({"occurrence": occurrence, "briefing": briefing,
                "seconds": time.monotonic() - tick, "calls": budget.counts[index],
                "mail_immutable": before == mail_fingerprint(runtime),
                "workspace_empty": not any(workspace.rglob("*"))})
        rows = report["occurrences"]
        briefs = [r["briefing"] or {} for r in rows]
        changes = [b.get("changes", []) for b in briefs]
        with sqlite3.connect(runtime.db_path) as conn:
            authorized_ids = {row[0] for row in conn.execute(
                "SELECT message_id FROM mail_messages WHERE account_id=?", (imported.account_id,))}
        verified_items = [item for b in briefs for section in ("changes", "unchanged", "decisions")
            for item in b.get(section, [])]
        report["mechanical_checks"] = {
            "three_succeeded": all(r["occurrence"]["status"] == "succeeded" for r in rows),
            "fresh_sessions": len({r["occurrence"]["session_id"] for r in rows}) == 3,
            "mail_and_workspace_immutable": all(r["mail_immutable"] and r["workspace_empty"] for r in rows),
            "frozen_read_views": bool(report["views"]) and all(
                v["before"] == v["after"] and v["before"]["side_effect_level"] == "read"
                and v["before"]["allowed_source_ids"] == [source_id]
                and v["before"]["allowed_account_ids"] == [imported.account_id]
                and not v["before"]["full_workspace_authority"]
                for v in report["views"]),
            "unauthorized_distractor_absent": DISTRACTOR not in json.dumps(records, ensure_ascii=False),
            "fixture_first_literal": any(c.get("claim") == "RIVER-739项目评审地点为南楼。"
                and c.get("current_observation") == c["claim"]
                and c.get("evidence_check") == "excerpt_match" for c in changes[0]),
            "second_deduplicated": not changes[1] and bool(briefs[1].get("unchanged")),
            "fixture_third_literal_same_identity": any(c.get("claim") == "RIVER-739项目评审地点改为北楼。"
                and c.get("current_observation") == c["claim"]
                and c.get("evidence_check") == "excerpt_match"
                and c.get("event_key") in {old.get("event_key") for old in changes[0]}
                for c in changes[2]),
            "unconfirmed_section_present": all(bool(b.get("unconfirmed")) for b in briefs),
            "fixture_verified_citations_authorized": bool(verified_items) and all(
                bool(item.get("evidence_refs")) and all(
                    str(ref).removeprefix("mail_message:").split("#")[0] in authorized_ids
                    for ref in item["evidence_refs"]) for item in verified_items),
        }
    except Exception as exc:  # noqa: BLE001 - private diagnostic, no whole-probe retry
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        budget.occurrence = None
        runtime.stop()
        report["seconds"] = time.monotonic() - started
        report["calls"] = len(budget.ids)
        report["provider_records"] = records
        selections = {}
        for record in records:
            request, response = record["request"], record.get("response", {})
            name = response.get("client_name") or request.get("client_name") or actual_client
            model = response.get("model") or request.get("model") or MODEL
            resolved = service.config.resolve_model_config(name, model)
            selections[(name, model)] = {"actual_client": name, "actual_model": model,
                "tokenizer_json_path": str(resolved.tokenizer_json_path) if resolved and resolved.tokenizer_json_path else None,
                "context_window_tokens": resolved.context_window_tokens if resolved else None,
                "output_reserve_tokens": resolved.output_reserve_tokens if resolved else None,
                "profile_selection": "unchanged_production_watch_default"}
        report["inference_configs"] = list(selections.values())
        if report["inference_configs"]:
            report["inference_config"] = report["inference_configs"][0]
        run_ids = {r["occurrence"].get("run_id") for r in report["occurrences"]}
        run_ids.update(v["before"].get("child_run_id") for v in report["views"])
        report["runs"] = [{"run": runtime.agent_run_manager.get_run(r).model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in runtime.agent_run_manager.list_events(r)]}
            for r in run_ids if r and runtime.agent_run_manager.get_run(r)]
        with ledger.connect() as conn:
            report["ledger_calls"] = [dict(zip(("stage", "status", "charged", "input_tokens", "output_tokens"), row, strict=True))
                for call_id in budget.ids for row in conn.execute(
                    "SELECT stage,status,charged,input_tokens,output_tokens FROM calls WHERE id=?", (call_id,))]
        for index, row in enumerate(report["occurrences"]):
            calls = [c for c in report["ledger_calls"]
                if c["stage"].startswith(f"{root.name}:occurrence{index + 1}:")]
            row["usage"] = {key: sum(c[key] or 0 for c in calls)
                for key in ("input_tokens", "output_tokens", "charged")}
            row["classification_counts"] = {key: len((row["briefing"] or {}).get(key, []))
                for key in ("changes", "unchanged", "unconfirmed", "decisions")}
            if index < len(report["views"]):
                row["child_task_status"] = report["views"][index].get("task_status")
                row["missing_requirements"] = report["views"][index].get("missing_requirements", [])
        report["mechanical_checks_pass"] = len(report["mechanical_checks"]) == 10 and all(report["mechanical_checks"].values())
        report["mechanical_pass"] = all(report["mechanical_checks"].get(k, False) for k in (
            "three_succeeded", "fresh_sessions", "mail_and_workspace_immutable",
            "frozen_read_views", "unauthorized_distractor_absent"))
        path = root / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    if not parser.parse_args().remote:
        parser.error("explicit --remote required; Root review/GO required before paid execution")
    root = OUTPUT / ("runtime_watch_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"))
    report = run_probe(root, LiveBudget(LEDGER, usd_limit=50))
    metrics = {k: report[k] for k in ("mode", "seconds", "calls", "call_limit", "mechanical_checks", "mechanical_checks_pass", "semantic_review", "private_artifacts")}
    for key in ("input_tokens", "output_tokens", "charged"):
        metrics[key] = sum(row[key] or 0 for row in report["ledger_calls"])
    metrics["error_type"] = report.get("error", {}).get("type")
    print(json.dumps(metrics, ensure_ascii=False), flush=True)
    if not report["mechanical_checks_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
