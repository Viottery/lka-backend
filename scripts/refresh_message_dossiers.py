"""Refresh private dossiers from a finished immutable history run; never calls a model."""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from collections import Counter
from pathlib import Path

from app.core.local_config import load_local_config
from app.core.prompt_tokens import PromptTokenCounter
from app.domains.message_history_tracking import HistoryTracker
from app.domains.message_profile_documents import MessageProfileDocumentStore
from app.domains.message_reading_replay import canonical_json, digest, load_snapshot, validate_grant
from app.tool_packages.message_history_analysis import SYSTEM_PROMPT, Analysis, parse_sections
from scripts.analyze_message_history import (
    ALLOWED_GROUPS,
    PRIOR_TOKENS,
    ROOT,
    bounded_repair,
    build_plan,
    immutable_json,
    immutable_markdown,
    prompt,
    run_lock,
    stable_profile_inputs,
    validate_application_grant,
)
from scripts.replay_message_reading import _current_policies, _safe_destination, _selection


def latest_report(directory: Path) -> tuple[Path, dict]:
    paths = sorted(
        directory.glob("report-*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name)
    )
    if not paths:
        raise ValueError("completed_history_report_required")
    path = paths[-1]
    value = json.loads(path.read_text(encoding="utf-8"))
    if path.name != "report-" + digest(value)[:16] + ".json":
        raise ValueError("report_content_digest_mismatch")
    if not path.with_suffix(".md").is_file():
        raise ValueError("completed_markdown_report_required")
    return path, value


def reconstruct_chunk(
    chunk: dict, prior: list[dict], directory: Path, plan: dict, counter
) -> tuple[dict | None, list[dict]]:
    """Reproduce exactly the runner's chosen valid attempt or bounded first-attempt salvage."""
    repair = None
    salvage = None
    salvage_rejections = []
    for attempt in range(2):
        call_id = chunk["chunk_id"] + f"-a{attempt}"
        result_path = directory / "calls" / (call_id + ".result.json")
        intent_path = directory / "calls" / (call_id + ".intent.json")
        if not result_path.exists():
            break
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        user = prompt(chunk, prior, repair)
        expected = {
            "plan_digest": digest(plan),
            "chunk_id": chunk["chunk_id"],
            "attempt": attempt,
            "input_digest": digest(user),
            "input_tokens": counter.count_request(SYSTEM_PROMPT, user).count,
            "thinking_enabled": intent.get("thinking_enabled"),
        }
        if intent != expected:
            raise ValueError("reconstruction_intent_mismatch")
        record = json.loads(result_path.read_text(encoding="utf-8"))
        if record.get("intent_digest") != digest(intent):
            raise ValueError("reconstruction_result_mismatch")
        if record.get("state") != "received":
            break
        try:
            if record.get("partial") or record.get("finish_reason") in {"length", "max_tokens"}:
                raise ValueError("incomplete_generation")
            parsed, rejected = parse_sections(json.loads(record["raw"]), chunk["messages"], prior)
            if not rejected or attempt == 1:
                return parsed.model_dump(), rejected
            salvage, salvage_rejections = parsed.model_dump(), rejected
        except (ValueError, TypeError):
            pass
        diagnostic = json.loads(
            (directory / "calls" / (call_id + ".validation.json")).read_text(encoding="utf-8")
        )
        if diagnostic.get("intent_digest") != digest(intent) or not isinstance(
            diagnostic.get("codes"), list
        ):
            raise ValueError("reconstruction_diagnostic_mismatch")
        repair = bounded_repair(
            canonical_json({"validation_codes": diagnostic["codes"], "output": record["raw"]}),
            counter,
        )
    return salvage, salvage_rejections


def reconstruct_results(
    plan: dict, chunks: list[dict], report: dict, directory: Path, messages: dict, counter
) -> tuple[dict, list[tuple[dict, dict]], Counter]:
    if (
        report.get("plan_digest") != digest(plan)
        or report.get("snapshot_id") != plan["snapshot_id"]
    ):
        raise ValueError("report_plan_mismatch")
    coverage = report.get("coverage", {}).get("chunks", [])
    if (
        len(coverage) != len(chunks)
        or report["coverage"].get("message_count") != plan["message_count"]
    ):
        raise ValueError("report_coverage_mismatch")
    trackers = {alias: HistoryTracker(alias, rows) for alias, rows in messages.items()}
    results = []
    rejected_counts = Counter()
    for chunk, covered in zip(chunks, coverage, strict=True):
        if (
            covered.get("chunk_id") != chunk["chunk_id"]
            or covered.get("offsets") != chunk["offsets"]
            or covered.get("conversation") != chunk["conversation"]
            or covered.get("message_count") != len(chunk["messages"])
        ):
            raise ValueError("report_range_mismatch")
        prior = trackers[chunk["conversation"]].prior(counter, PRIOR_TOKENS - 100)
        accepted, rejected = reconstruct_chunk(chunk, prior, directory, plan, counter)
        state = "completed_with_rejected_items" if rejected else "completed"
        if accepted is not None:
            if covered.get("state") != state or covered.get("rejected_items", []) != rejected:
                raise ValueError("report_selected_attempt_mismatch")
            trackers[chunk["conversation"]].observe(chunk, accepted)
            results.append((chunk, accepted))
            rejected_counts.update(code for item in rejected for code in item["codes"])
        elif covered.get("state") in {"completed", "completed_with_rejected_items"}:
            raise ValueError("completed_chunk_has_no_valid_record")
    groups = {alias: tracker.report() for alias, tracker in trackers.items()}
    if groups != report.get("groups"):
        raise ValueError("report_group_reconstruction_mismatch")
    return groups, results, rejected_counts


def safe(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", html.escape(str(value)))


def user_index(
    report_path: Path, report: dict, groups: dict, mapping: dict, exports: dict, summaries: dict
) -> str:
    lines = [
        "# 三群历史阅读与人物观察",
        "",
        "本地私密。画像是可修订观察，不是核实事实。",
        "人物档案中，已接纳的来源支持观察与待核实笔记分开显示；待核实笔记不代表已通过画像门槛。",
        "只读取已有模型结果，本次整理没有模型调用、上传或生产分析开关变更。",
        "",
        f"已记录 {report['coverage']['message_count']} 条，模型完成读取 {report['coverage']['completed_messages']} 条；采集缺口未知，附件内容未知。",
        "语义候选仍有拒绝项，逐批状态和完整来源见报告。",
        "",
        "[完整群话题与事件报告]("
        + str(report_path.with_suffix(".md"))
        + ") · [结构化覆盖报告]("
        + str(report_path)
        + ")",
        "",
        "调用与消耗：" + canonical_json(report["usage"]),
        "",
        "原授权累计（包含前轮尝试）：" + canonical_json(report.get("aggregate_allowance", {})),
        "",
        "金额缺少定价时为未知，不按免费解释。",
    ]
    for alias, group in groups.items():
        scope = mapping[alias]
        source = scope["source"]
        export = exports[alias]
        lines += [
            "",
            "## 群 " + str(source["group_id"]) + "（" + alias + "）",
            "",
            f"已完成 {group['completed_messages']}/{group['message_count']} 条；未归入话题 {len(group['unassigned_completed_ids'])} 条。",
            "",
            "[本群人物档案](" + export["markdown"] + ")",
            "",
            "画像入库与待核实笔记：" + canonical_json(summaries[alias]),
            "",
            "### 活跃话题（前十）",
            "",
        ]
        for topic in group["topics_by_activity"][:10]:
            lines += [
                "- "
                + safe(topic["title"])
                + f"：{topic['message_count']}条 / {topic['participant_count']}人；"
                + safe(topic["summary"])
            ]
        counts = Counter(row["sender"] for row in mapping[alias].get("_rows", []))
        native_ids = {sender: native for native, sender in scope["users"].items()}
        lines += ["", "### 发言较多的参与者（前十）", ""]
        for sender, count in counts.most_common(10):
            native = native_ids.get(sender, sender)
            name = scope.get("display_names", {}).get(sender) or sender
            person = export.get("participants", {}).get(native)
            if person:
                lines += [
                    "- ["
                    + safe(name)
                    + "]("
                    + person["markdown"]
                    + f")：{count}条；本地身份 {safe(native)}。"
                ]
            else:
                lines += ["- " + safe(name) + f"：{count}条；没有可展示档案。"]
            authored_ids = {
                row["id"] for row in mapping[alias].get("_rows", []) if row["sender"] == sender
            }
            participation = sorted(
                (
                    (len(authored_ids.intersection(topic["member_ids"])), topic["title"])
                    for topic in group["topics_by_activity"]
                ),
                reverse=True,
            )
            discussed = [safe(title) + f"（{total}条）" for total, title in participation if total][
                :3
            ]
            if discussed:
                lines += [
                    "  参与话题："
                    + "、".join(discussed)
                    + "；这是来源关联的讨论记录，不代表个人偏好。"
                ]
    return "\n".join(lines) + "\n"


def refresh(args) -> dict:
    snapshot = _safe_destination(args.snapshot)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,60}", args.run_name):
        raise ValueError("invalid_run_name")
    directory = _safe_destination(str(snapshot / args.run_name))
    report_path, report = latest_report(directory)
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    grant = json.loads((directory / "grant.json").read_text(encoding="utf-8"))
    manifest, messages = load_snapshot(snapshot)
    mapping = json.loads((snapshot / "identity_mapping.json").read_text(encoding="utf-8"))
    if {str(value["source"]["group_id"]) for value in mapping.values()} != ALLOWED_GROUPS:
        raise ValueError("exact_authorized_group_scope_required")
    config = load_local_config(Path(args.config))
    name, model, provider = _selection(config)
    resolved = config.llm.resolve_model_config(name, model)
    counter = PromptTokenCounter(resolved.tokenizer_json_path if resolved else None)
    rebuilt, chunks = build_plan(
        manifest,
        messages,
        mapping,
        counter,
        provider=provider,
        chunk_size=plan["chunk_size"],
        max_input=plan["max_input_tokens"],
        output_tokens=plan["output_tokens"],
        workload=plan["workload"],
        parent_contract=plan.get("parent_contract"),
    )
    if rebuilt != plan or plan["schema_digest"] != digest(Analysis.model_json_schema()):
        raise ValueError("frozen_run_contract_changed")

    def live():
        current_manifest, current_messages = load_snapshot(snapshot)
        current_mapping = json.loads(
            (snapshot / "identity_mapping.json").read_text(encoding="utf-8")
        )
        current_grant = json.loads((directory / "grant.json").read_text(encoding="utf-8"))
        _, _, current_provider = _selection(load_local_config(Path(args.config)))
        if (
            current_manifest != manifest
            or current_mapping != mapping
            or digest(current_messages) != digest(messages)
            or current_grant != grant
            or json.loads((directory / "plan.json").read_text()) != plan
            or json.loads(report_path.read_text()) != report
        ):
            raise ValueError("offline_refresh_identity_changed")
        policies = _current_policies(Path(args.authority), args.authority_python)
        validate_application_grant(
            current_grant, plan, manifest, mapping, current_provider, policies, int(time.time())
        )
        parent = plan.get("parent_contract")
        if parent:
            parent_grant = json.loads((snapshot / parent["run_name"] / "grant.json").read_text())
            if digest(parent_grant) != parent["grant_digest"]:
                raise ValueError("parent_grant_changed")
            validate_grant(
                parent_grant, manifest, current_provider, policies, mapping, now=int(time.time())
            )

    profile_root = ROOT / "data/message_replay/living_profiles"
    profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with run_lock(profile_root), run_lock(directory):
        live()
        groups, results, rejected = reconstruct_results(
            plan, chunks, report, directory, messages, counter
        )
        store = MessageProfileDocumentStore(profile_root, retain_unverified_notes=True)
        summaries = {
            alias: {"accepted": 0, "rejected": Counter(), "unverified_notes": 0}
            for alias in messages
        }
        for chunk, result in results:
            live()
            alias = chunk["conversation"]
            scope = mapping[alias]
            rows, claims = stable_profile_inputs(chunk["messages"], result["claims"], scope)
            outcome = store.ingest(
                scope["source"]["conversation_key"],
                claims,
                rows,
                max(row["received_at"] for row in rows),
                capture_epoch=scope["source"]["capture_epoch"],
            )
            summaries[alias]["accepted"] += outcome["accepted"]
            summaries[alias]["rejected"].update(item["reason"] for item in outcome["rejected"])
            summaries[alias]["unverified_notes"] += outcome.get("retained_notes", 0)
        exports = {}
        for alias, scope in mapping.items():
            live()
            names = {
                native: scope.get("display_names", {}).get(sender) or sender
                for native, sender in scope["users"].items()
            }
            exports[alias] = store.export(
                scope["source"]["conversation_key"],
                now=int(time.time()),
                capture_epoch=scope["source"]["capture_epoch"],
                display_names=names,
            )
            summaries[alias]["rejected"] = dict(summaries[alias]["rejected"])
            snapshot_value = store.snapshot(
                scope["source"]["conversation_key"],
                now=int(time.time()),
                capture_epoch=scope["source"]["capture_epoch"],
            )
            summaries[alias]["accepted_observations_in_store"] = sum(
                len(person["claims"]) for person in snapshot_value["participants"]
            )
            summaries[alias]["unverified_notes_in_store"] = sum(
                len(person.get("machine_notes", [])) for person in snapshot_value["participants"]
            )
        metadata = {
            "source_report_digest": digest(report),
            "plan_digest": digest(plan),
            "exports": exports,
            "profile_ingest": summaries,
            "analysis_item_rejection_counts": dict(rejected),
            "remote_calls": 0,
        }
        destination = directory / ("dossiers-" + digest(metadata)[:16])
        destination.mkdir(exist_ok=True, mode=0o700)
        live()
        immutable_json(destination, "metadata.json", metadata)
        index_mapping = {
            alias: {**scope, "_rows": messages[alias]} for alias, scope in mapping.items()
        }
        live()
        immutable_markdown(
            destination / "README.md",
            user_index(report_path.resolve(), report, groups, index_mapping, exports, summaries),
        )
        return {
            "index": str((destination / "README.md").relative_to(ROOT)),
            "remote_calls": 0,
            "profile_ingest": summaries,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--run-name", default="history-v4")
    parser.add_argument("--config", default="config/local.toml")
    parser.add_argument("--authority", required=True)
    parser.add_argument("--authority-python")
    args = parser.parse_args()
    try:
        print(canonical_json(refresh(args)))
    except Exception as exc:  # noqa: BLE001 - never expose private exception bodies
        print(canonical_json({"state": "stopped", "error_class": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
