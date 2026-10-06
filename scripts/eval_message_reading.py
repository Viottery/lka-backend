"""Bounded synthetic reading evaluation; real provider use requires --remote.

Never opens the user's history database. The checked-in fixture is the only
message source. Fake output measures engineering, not semantic model quality.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from app.core.background_jobs import BackgroundJobStore
from app.core.local_config import MessageHistoryConfig
from app.core.message_analysis import MessageAnalysisCoordinator
from app.domains.message_history import MessageHistoryService

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/message_reading_cases.json"


def replay(service):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for room, kind in (("busy-group", "group"), ("quiet-group", "group"), ("private-friend", "private")):
        service.set_policy({"platform": "synthetic", "account_id": "self", "conversation_type": kind,
                            "conversation_id": room, "record_enabled": True, "analysis_enabled": False})
    service.set_reading_profile({"self_ids": {"synthetic": ["self"]}, "keywords": ["发布", "服务器"]}, expected_revision=0)
    messages = fixture["messages"]
    unique = service.import_messages(messages, schema_version=2)
    duplicates = service.import_messages(messages[:25], schema_version=2)
    rejected = service.import_messages(fixture["rejected_messages"], schema_version=2)
    service.scan_local_signals(limit=1000)
    conversations = service.list_conversations()["conversations"]
    actual = {}
    all_ids = set()
    for conversation in conversations:
        key = conversation["conversation_key"]
        cursor = None
        count = 0
        while True:
            page = service.history(key, limit=100, before_seq=cursor)
            count += len(page["messages"])
            all_ids.update(m["provider_message_id"] for m in page["messages"])
            cursor = page.get("next_before_seq")
            if cursor is None:
                break
        actual[conversation["conversation_id"]] = count
    local = service.list_insights(limit=100)["insights"]
    checks = {"accepted_310": len(unique["acknowledged"]) == 310,
              "duplicate_replay_25": len(duplicates["acknowledged"]) == 25,
              "rejected_3": len(rejected["rejected"]) == 3,
              "unique_count_unchanged": len(all_ids) == 310,
              "conversation_counts": actual == fixture["expectations"]["conversation_counts"],
              "native_direct_signal": any("direct_mention" in i["reason_codes"] for i in local),
              "native_group_signal": any("group_mention" in i["reason_codes"] for i in local),
              "no_analysis_jobs": not service.jobs.list(kind="message_analysis")}
    return {"fixture": str(FIXTURE.relative_to(ROOT)), "checks": checks,
            "passed": all(checks.values()), "counts": actual, "local_candidates": len(local),
            "semantic_quality_evaluated": False}


class SyntheticClient:
    def complete_text(self, **kwargs):
        return SimpleNamespace(content=json.dumps({"schema_version": 2, "topic_updates": [],
            "highlights": [], "importance_findings": [], "facts": [], "warnings": []}),
            usage={"total_tokens": 100}, finish_reason="stop", client_name="synthetic", model="synthetic")


def evaluate(*, remote=False, config_path=None):
    with tempfile.TemporaryDirectory(prefix="lka-reading-eval-") as directory:
        path = Path(directory) / "synthetic.sqlite"
        jobs = BackgroundJobStore(path)
        jobs.ensure_schema()
        history = MessageHistoryService(path, jobs)
        history.ensure_schema()
        report = replay(history)
        if remote:
            from app.core.llm import build_llm_service
            from app.core.local_config import load_local_config
            local = load_local_config(config_path or ROOT / "config/local.toml")
            client = build_llm_service(local.llm.model_copy(update={"timeout_seconds": 30}))
            if client is None:
                raise RuntimeError("Configured provider unavailable")
        else:
            client = SyntheticClient()
        selection = ({"background_client_name": local.message_history.background_client_name,
                      "background_model": local.message_history.background_model,
                      "model_prices": local.message_history.model_prices} if remote else {})
        config = MessageHistoryConfig(max_job_tokens=65536, max_job_calls=4, service_hourly_call_limit=4,
                                     service_daily_call_limit=4, yield_delay_seconds=0, **selection)
        coordinator = MessageAnalysisCoordinator(service=history, store=jobs, config=config, llm_client=client)
        # One small separate synthetic conversation; never enable the 310-row scopes.
        identity = {"platform": "synthetic", "account_id": "self", "conversation_type": "group", "conversation_id": "quality-small"}
        policy = history.set_policy({**identity, "record_enabled": True, "analysis_enabled": True,
                                     "auto_analyze": False, "min_interval_seconds": 0, "timezone": "Asia/Shanghai"})
        rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["messages"]
        sample = [{**rows[i], **identity, "message_id": "quality-" + rows[i]["message_id"], "capture_epoch": policy["capture_epoch"]} for i in (0, 3, 9, 17, 51)]
        history.import_messages(sample, schema_version=2)
        history.schedule_pending(policy["conversation_key"], force=True)
        for _ in range(8):
            if not coordinator.worker.run_one():
                break
        work = jobs.list(kind="message_analysis")
        budget = coordinator.controller.quota_usage("message_reading")
        coverage = history.coverage(policy["conversation_key"])
        findings = history.list_insights(conversation_key=policy["conversation_key"])["insights"]
        raw = history.history(policy["conversation_key"], limit=10)["messages"]
        providers = {row["message_id"]: row["provider_message_id"] for row in raw}
        expected = {"quality-case-004", "quality-case-010", "quality-case-018", "quality-case-052"}
        model_findings = [i for i in findings if "model" in i.get("detectors", [])]
        detected = {providers[s] for i in model_findings if i["kind"] in {"importance", "correction"}
                    and i["importance"] != "ordinary" for s in i["source_message_ids"] if s in providers}
        correct = detected & expected
        deadlines = [i for i in model_findings if "quality-case-018" in
                     {providers.get(s) for s in i["source_message_ids"]} and i.get("due_at")]
        target_date = datetime.fromisoformat("2026-10-05T15:00:00+08:00")
        report["semantic_probes"] = {
            "measurement": "small human-labelled synthetic evidence-level probes, not event-level quality proof",
            "importance_recall": len(correct) / len(expected) if remote else None,
            "importance_precision": len(correct) / len(detected) if remote and detected else None,
            "deadline_correction_exact": any(datetime.fromisoformat(i["due_at"]) == target_date for i in deadlines) if remote else None,
            "missing_expected_ids": sorted(expected - detected) if remote else [],
            "topic_stability": "measured by deterministic revision tests; requires multi-batch human semantic evaluation",
        }
        ledger_rows = coordinator.controller.health()["last_24h"]
        costs = [row["estimated_cost"] for row in ledger_rows]
        report["model_probe"] = {"mode": "real" if remote else "fake-engineering-only",
            "calls": budget["total_calls"], "tokens": budget["total_tokens"],
            "call_cap": 4, "job_states": [{"status": j["status"], "error_class": j["error_class"]} for j in work],
            "samples": 5, "topics": history.list_topics(conversation_key=policy["conversation_key"])["topics"],
            "coverage": coverage,
            "insights": findings,
            "calls_per_1000_sample_messages": budget["total_calls"] * 1000 / 5,
            "tokens_per_1000_sample_messages": budget["total_tokens"] * 1000 / 5,
            "estimated_cost": sum(costs) if costs and all(cost is not None for cost in costs) else None,
            "semantic_quality_requires_human_review": True}
        report["semantic_quality_evaluated"] = remote
        report["passed"] = report["passed"] and budget["total_calls"] <= 4 and coverage.get("analysis_covered_seq") == 5 and any(j["status"] == "succeeded" for j in work)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true", help="Explicitly authorize at most four synthetic-data provider calls")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(remote=args.remote, config_path=args.config)
    except Exception as exc:  # noqa: BLE001 - do not print providers, keys or input on failure.
        result = {"passed": False, "error_class": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
