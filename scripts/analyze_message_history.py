"""Explicit full recorded group-history application; private artifacts only.

Plan: python -m scripts.analyze_message_history --snapshot ... --plan
Execute the reviewed plan with --remote --authorize-remote --max-calls N
--max-tokens N --authority ... . This never enables production analysis.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import html
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domains.message_history_tracking import (
    APPLICATION_CODEC,
    HistoryTracker,
    analysis_projection,
    decode_history,
    encode_history,
)
from app.domains.message_reading_replay import canonical_json, digest, load_snapshot, validate_grant
from app.tool_packages.message_history_analysis import (
    SYSTEM_PROMPT,
    VERSION,
    Analysis,
    parse_sections,
)
from scripts.replay_message_reading import (
    ROOT,
    _artifact,
    _current_policies,
    _safe_destination,
    _selection,
)

ALLOWED_GROUPS = {"1036840759", "1046158144", "376882037"}
PRIOR_TOKENS = 1800
REPAIR_TOKENS = 1200


def prompt(chunk: dict, prior: list[dict], repair: str | None = None) -> str:
    value = {
        "timezone": "Asia/Shanghai",
        "conversation": chunk["conversation"],
        "coverage": "all_recorded_projected_messages_contiguous",
        "attachment_content": "unknown_not_uploaded",
        "prior_topics": prior,
        "messages": encode_history(chunk["messages"]),
    }
    if repair is not None:
        value["repair"] = {
            "instruction": "修复上一输出的结构或证据错误，重新输出完整JSON。",
            "previous_output_bounded": repair,
        }
    return canonical_json(value)


def build_plan(
    manifest: dict,
    messages: dict,
    mapping: dict,
    counter,
    *,
    provider: str,
    chunk_size: int,
    max_input: int,
    output_tokens: int,
    workload: str = "background_memory",
    parent_contract: dict | None = None,
) -> tuple[dict, list[dict]]:
    """All rows appear once; oversized single rows stop planning rather than truncate."""
    if not 1 <= chunk_size <= 200 or not 1024 <= output_tokens <= 12000:
        raise ValueError("invalid_chunk_or_output_limit")
    if max_input <= PRIOR_TOKENS + REPAIR_TOKENS + 1024:
        raise ValueError("invalid_input_limit")
    if workload not in {"interactive", "background_memory"}:
        raise ValueError("invalid_workload")
    groups = {str(value["source"]["group_id"]) for value in mapping.values()}
    if groups != ALLOWED_GROUPS or set(mapping) != set(messages):
        raise ValueError("history_scope_must_be_exact_authorized_three_groups")
    chunks = []
    for conversation, rows in sorted(messages.items()):
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError("duplicate_source_id")
        if [row["seq"] for row in rows] != sorted({row["seq"] for row in rows}):
            raise ValueError("snapshot_not_ordered")
        start = 0
        while start < len(rows):
            end = min(start + chunk_size, len(rows))
            while end > start:
                chunk = {"conversation": conversation, "messages": rows[start:end]}
                count = counter.count_request(SYSTEM_PROMPT, prompt(chunk, [])).count
                if count + PRIOR_TOKENS + REPAIR_TOKENS <= max_input:
                    break
                end -= 1
            if end == start:
                raise ValueError("single_message_exceeds_input_limit_no_truncation_allowed")
            if decode_history(encode_history(chunk["messages"])) != [
                analysis_projection(row) for row in chunk["messages"]
            ]:
                raise ValueError("codec_not_lossless")
            chunk.update(
                chunk_id=f"{conversation}b{len(chunks) + 1:05d}",
                offsets=[start, end],
                digest=digest(chunk["messages"]),
                base_input_tokens=count,
            )
            chunks.append(chunk)
            start = end
    entries = [
        {key: value for key, value in chunk.items() if key != "messages"} for chunk in chunks
    ]
    plan = {
        "version": VERSION,
        "snapshot_id": manifest["snapshot_id"],
        "manifest_digest": digest(manifest),
        "mapping_digest": digest(mapping),
        "projection_digest": manifest["projection_digest"],
        "provider_hash": provider,
        "prompt_digest": digest(SYSTEM_PROMPT),
        "schema_digest": digest(Analysis.model_json_schema()),
        "codec_version": APPLICATION_CODEC,
        "chunk_size": chunk_size,
        "max_input_tokens": max_input,
        "output_tokens": output_tokens,
        "workload": workload,
        "parent_contract": parent_contract,
        "workload_purpose": "user_requested_foreground_history"
        if workload == "interactive"
        else "background_history",
        "prior_token_reserve": PRIOR_TOKENS,
        "repair_token_reserve": REPAIR_TOKENS,
        "max_attempts_per_chunk": 2,
        "message_count": sum(map(len, messages.values())),
        "chunks": entries,
        "counter_method": counter.count_text("").method,
        "counter_conservative": counter.count_text("").conservative,
        "base_input_tokens": sum(chunk["base_input_tokens"] for chunk in chunks),
        "one_pass_call_count": len(chunks),
        "maximum_call_count": 2 * len(chunks),
        "one_pass_token_upper_bound": sum(
            chunk["base_input_tokens"] + PRIOR_TOKENS + output_tokens for chunk in chunks
        ),
        "with_repairs_token_upper_bound": 2 * len(chunks) * (max_input + output_tokens),
        "input_projection": "all_text_senders_sent_times_native_mentions_replies_parts_threads_time_quality",
        "local_only_fields": ["seq", "received_at"],
        "media_uploaded": False,
        "production_analysis_enabled": False,
    }
    return plan, chunks


def immutable_json(directory: Path, name: str, value: dict) -> None:
    path = directory / name
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("immutable_artifact_mismatch")
        return
    _artifact(directory, name, value)


def immutable_markdown(path: Path, body: str) -> None:
    if path.exists():
        if path.read_text(encoding="utf-8") != body:
            raise ValueError("immutable_markdown_mismatch")
        return
    fd, temporary = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def parent_contract(snapshot: Path, name: str | None) -> dict | None:
    if name is None:
        return None
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,60}", name):
        raise ValueError("invalid_parent_run_name")
    grant = json.loads((snapshot / name / "grant.json").read_text(encoding="utf-8"))
    if grant.get("parent_contract"):
        raise ValueError("nested_successor_not_supported")
    return {
        "run_name": name,
        "grant_digest": digest(grant),
        "scope_id": grant["scope_id"],
        "max_calls": grant["max_calls"],
        "max_tokens": grant["max_tokens"],
    }


def validate_application_grant(
    grant: dict,
    plan: dict,
    manifest: dict,
    mapping: dict,
    provider: str,
    policies: list[dict],
    now: int,
) -> None:
    validate_grant(grant, manifest, provider, policies, mapping, now=now)
    if (
        grant.get("purpose") != "full_history_application"
        or grant.get("plan_digest") != digest(plan)
        or grant.get("scope_id")
        != "full-history:" + manifest["snapshot_id"] + ":" + grant.get("run_name", "")
        or type(grant.get("max_calls")) is not int
        or grant["max_calls"] <= 0
        or type(grant.get("max_tokens")) is not int
        or grant["max_tokens"] <= 0
    ):
        raise ValueError("application_grant_mismatch")


@contextmanager
def run_lock(directory: Path):
    """Kernel advisory lock recovers after process death; never deletes artifacts."""
    path = directory / "run.lock"
    with path.open("a+b") as handle:
        path.chmod(0o600)
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def stable_profile_inputs(
    rows: list[dict], claims: list[dict], mapping: dict
) -> tuple[list[dict], list[dict]]:
    """Reverse aliases ONLY for local dossiers; never part of the model prompt."""
    people = {alias: native for native, alias in mapping["users"].items()}
    ids = {alias: native for native, alias in mapping["messages"].items()}
    replacements = {**people, **ids}
    pattern = re.compile(
        r"(?<![A-Za-z0-9])("
        + "|".join(re.escape(alias) for alias in sorted(replacements, key=len, reverse=True))
        + r")(?![A-Za-z0-9])"
    )

    def normalize(value):
        if isinstance(value, str):
            return pattern.sub(lambda match: replacements[match.group()], value)
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {replacements.get(key, key): normalize(item) for key, item in value.items()}
        return value

    # All local text, quoted evidence, part targets, mentions and evidence-map keys
    # share stable identities across snapshots; the external prompt stays aliased.
    return normalize(copy.deepcopy(rows)), normalize(copy.deepcopy(claims))


def bounded_repair(raw: str, counter) -> str:
    # Only previous generated output is shortened; the message projection is intact.
    value = raw[:4000]
    while counter.count_text(canonical_json({"repair": value})).count > REPAIR_TOKENS - 200:
        value = value[: len(value) // 2]
    return value


def ledger_summary(controller, scope_id: str) -> dict:
    with sqlite3.connect(controller.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT u.* FROM llm_workload_usage u JOIN llm_workload_quota_usage q "
                "ON q.call_id=u.call_id WHERE q.scope_id=? ORDER BY u.created_at",
                (scope_id,),
            )
        ]
    return {
        "durable_usage": controller.quota_usage(scope_id),
        "provider_reported_tokens": sum(
            row["input_tokens"] + row["output_tokens"]
            for row in rows
            if row["count_method"] == "provider_usage"
        ),
        "provider_usage_calls": sum(row["count_method"] == "provider_usage" for row in rows),
        "estimated_usage_calls": sum(row["count_method"] != "provider_usage" for row in rows),
        "cost": sum(row["cost"] for row in rows)
        if rows and all(row["cost_known"] for row in rows)
        else None,
        "cost_unknown_is_not_zero": True,
    }


def timestamp(value: int | None) -> str:
    if value is None:
        return "发送时间未知"
    return datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai")).isoformat()


def markdown(report: dict, messages: dict, mapping: dict) -> str:
    def safe(value: str) -> str:
        return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", html.escape(value))

    lines = [
        "# 三群历史消息分析",
        "",
        "本地私密。以下均为来源支持的观察，不是核实事实。附件内容未知。",
        "统计只覆盖已记录且脱敏投影的历史；采集缺口未知。",
        "",
        "## 覆盖与消耗",
        "",
        canonical_json(report["coverage"]),
        "",
        canonical_json(report["usage"]),
        "",
        "费用未知不表示免费；发送时间缺失保持未知。",
    ]
    for alias, group in report["groups"].items():
        rows = {row["id"]: row for row in messages[alias]}
        source = mapping[alias]["source"]
        lines += [
            "",
            "## 群 " + str(source["group_id"]) + "（" + alias + "）",
            "",
            (
                f"记录 {group['message_count']} 条，成功分析 {group['completed_messages']} 条；"
                f"成功但未归入话题 {len(group['unassigned_completed_ids'])} 条；"
                f"未处理 {len(group['unprocessed_ids'])} 条。"
            ),
            "",
            "### 话题与热点（参与人数、时段与有限消息量评分）",
        ]
        profile_path = report["profile_exports"].get(alias, {}).get("markdown")
        if profile_path:
            lines += ["", "人物档案：[本群人物观察](" + str(profile_path) + ")。"]
        for item in group["topics_by_activity"]:
            lines += [
                "",
                "#### " + safe(item["title"]),
                "",
                safe(item["summary"]),
                "",
                f"{item['message_count']} 条 / {item['participant_count']} 人；主题 {item['key']}。",
                "时间范围："
                + (
                    " → ".join(timestamp(t) for t in item["sent_time_span"])
                    if item["sent_time_span"]
                    else "未知"
                ),
            ]
            for change in item["changes"]:
                lines += [
                    "",
                    "- " + safe(change["summary"]) + "（" + ", ".join(change["source_ids"]) + "）",
                ]
            for mid in item["source_ids"][:8]:
                row = rows[mid]
                lines += [
                    "",
                    f"来源 {mid} / "
                    + safe(
                        mapping[alias].get("display_names", {}).get(row["sender"], row["sender"])
                    )
                    + f"（{row['sender']}） / {timestamp(row['sent_at'])}：",
                    "",
                    "> " + safe(row["text"]).replace("\n", "\n> "),
                ]
        for kind, label in (("important", "需要关注的事项"), ("highlights", "信息与趣味亮点")):
            lines += ["", "### " + label]
            if not group[kind]:
                lines += ["", "本次没有通过证据校验的候选。"]
            for item in group[kind]:
                mid = next(
                    mid
                    for mid in item["source_ids"]
                    if any(
                        item["quote"] in text
                        for text in [
                            rows[mid]["text"],
                            *[
                                part.get("text", "")
                                for part in rows[mid].get("parts", [])
                                if part.get("kind") == "text"
                            ],
                        ]
                    )
                )
                lines += [
                    "",
                    "- " + safe(item["text"]) + "；" + safe(item["reason"]),
                    "",
                    "  来源 "
                    + ", ".join(item["source_ids"])
                    + " / "
                    + timestamp(rows[mid]["sent_at"]),
                    "",
                    "> " + safe(item["quote"]).replace("\n", "\n> "),
                ]
        lines += ["", "### 群侧重点（逐批观察）", ""]
        lines.extend(
            "- " + safe(item["summary"])
            for item in group["batch_group_summaries"]
            if item["summary"]
        )
        lines += [
            "",
            "### 限制",
            "",
            "既有话题上下文有界，长期话题可能分段；无人工语义评分。",
            "未处理来源（前20条；完整列表见配套JSON）：" + ", ".join(group["unprocessed_ids"][:20]),
            "未分配来源（前20条；完整列表见配套JSON）："
            + ", ".join(group["unassigned_completed_ids"][:20]),
        ]
        lines.extend("- " + safe(warning) for warning in group["warnings"])
    return "\n".join(lines) + "\n"


async def execute(
    args,
    directory: Path,
    plan: dict,
    chunks: list[dict],
    manifest: dict,
    messages: dict,
    mapping: dict,
    config,
    counter,
) -> dict:
    from app.core.llm import build_llm_service
    from app.core.llm_workloads import (
        BudgetPricing,
        BudgetQuota,
        LLMWorkloadController,
        workload_scope,
    )
    from app.core.local_config import load_local_config
    from app.domains.message_profile_documents import MessageProfileDocumentStore

    name, model, provider = _selection(config)
    resolved = config.llm.resolve_model_config(name, model)
    capacity = resolved.context_window_tokens if resolved else None
    if capacity is None or plan["max_input_tokens"] + plan["output_tokens"] + 4096 > capacity:
        raise ValueError("configured_context_capacity_insufficient")
    grant_path = directory / "grant.json"
    ledger = ROOT / "data/message_replay/usage.sqlite3"
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    controller = LLMWorkloadController(
        ledger,
        hourly_tokens=config.background.hourly_token_limit,
        daily_tokens=config.background.daily_token_limit,
        daily_cost_limit=config.background.daily_cost_limit,
    )
    parent = plan.get("parent_contract")
    parent_quota = (
        BudgetQuota(
            parent["scope_id"], max_calls=parent["max_calls"], max_tokens=parent["max_tokens"]
        )
        if parent
        else None
    )
    if not grant_path.exists():
        if not args.authorize_remote or not args.max_calls or not args.max_tokens:
            raise ValueError("new_application_grant_requires_explicit_finite_authorization")
        if parent:
            previous = controller.quota_usage(parent["scope_id"])
            if (
                args.max_calls > parent["max_calls"] - previous["total_calls"]
                or args.max_tokens > parent["max_tokens"] - previous["total_tokens"]
            ):
                raise ValueError("successor_cap_exceeds_original_remaining_allowance")
        _artifact(
            directory,
            "grant.json",
            {
                "purpose": "full_history_application",
                "active": True,
                "run_name": args.run_name,
                "scope_id": "full-history:" + manifest["snapshot_id"] + ":" + args.run_name,
                "snapshot_id": manifest["snapshot_id"],
                "provider_hash": provider,
                "manifest_digest": digest(manifest),
                "mapping_digest": digest(mapping),
                "plan_digest": digest(plan),
                "client_name": name,
                "model": model,
                "expires_at": int(time.time()) + 86400,
                "conversation_keys": [
                    row["source"]["conversation_key"] for row in mapping.values()
                ],
                "max_calls": args.max_calls,
                "max_tokens": args.max_tokens,
                "parent_contract": parent,
            },
        )
    original_grant = json.loads(grant_path.read_text(encoding="utf-8"))
    if args.max_calls is not None and args.max_calls != original_grant["max_calls"]:
        raise ValueError("existing_grant_is_immutable")
    if args.max_tokens is not None and args.max_tokens != original_grant["max_tokens"]:
        raise ValueError("existing_grant_is_immutable")
    service = build_llm_service(config.llm)
    if service is None:
        raise ValueError("real_provider_unavailable")
    service.workloads = controller
    service.background_timeout_seconds = min(90, config.llm.timeout_seconds)
    thinking = False if service.supports_thinking_control(client_name=name) else None
    quota = BudgetQuota(
        original_grant["scope_id"],
        max_calls=original_grant["max_calls"],
        max_tokens=original_grant["max_tokens"],
    )
    price = next(
        (
            row
            for row in config.message_history.model_prices
            if (row.client_name, row.model) == (name, model)
        ),
        None,
    )
    pricing = BudgetPricing(**price.model_dump()) if price else None

    def live():
        # Read all identity artifacts again. An immutable snapshot is not permanent consent.
        current_manifest, current_messages = load_snapshot(Path(args.snapshot))
        current_mapping = json.loads(
            (Path(args.snapshot) / "identity_mapping.json").read_text(encoding="utf-8")
        )
        current_grant = json.loads(grant_path.read_text(encoding="utf-8"))
        if (
            current_grant != original_grant
            or current_manifest != manifest
            or digest(current_messages) != digest(messages)
            or current_mapping != mapping
            or json.loads((directory / "plan.json").read_text(encoding="utf-8")) != plan
        ):
            raise ValueError("application_identity_changed")
        _, _, current_provider = _selection(load_local_config(Path(args.config)))
        validate_application_grant(
            current_grant,
            plan,
            manifest,
            mapping,
            current_provider,
            _current_policies(Path(args.authority), args.authority_python),
            int(time.time()),
        )
        if parent:
            parent_grant = json.loads(
                (Path(args.snapshot) / parent["run_name"] / "grant.json").read_text(
                    encoding="utf-8"
                )
            )
            if (
                digest(parent_grant) != parent["grant_digest"]
                or current_grant.get("parent_contract") != parent
            ):
                raise ValueError("parent_authorization_changed")
            validate_grant(
                parent_grant,
                manifest,
                current_provider,
                _current_policies(Path(args.authority), args.authority_python),
                mapping,
                now=int(time.time()),
            )
        usage = controller.quota_usage(quota.scope_id)
        if usage["total_calls"] > quota.max_calls or usage["total_tokens"] > quota.max_tokens:
            raise ValueError("application_budget_exceeded")
        return current_grant

    live()
    records_dir = directory / "calls"
    records_dir.mkdir(exist_ok=True, mode=0o700)
    if not records_dir.resolve().is_relative_to(directory.resolve()):
        raise ValueError("artifact_path_escape")
    trackers = {alias: HistoryTracker(alias, rows) for alias, rows in messages.items()}
    profiles = MessageProfileDocumentStore(ROOT / "data/message_replay/living_profiles")
    profile_ingest = {}
    coverage = []
    stopping = False
    for chunk in chunks:
        tracker = trackers[chunk["conversation"]]
        prior = tracker.prior(counter, PRIOR_TOKENS - 100)
        accepted = None
        accepted_rejections = []
        salvage_candidate = None
        salvage_rejections = []
        repair = None
        final_state = "unprocessed_budget_stop" if stopping else "failed"
        for attempt in range(2):
            user = prompt(chunk, prior, repair)
            incoming = counter.count_request(SYSTEM_PROMPT, user).count
            if incoming > plan["max_input_tokens"]:
                raise ValueError("full_prompt_limit_exceeded")
            call_id = chunk["chunk_id"] + f"-a{attempt}"
            intent_name = call_id + ".intent.json"
            result_name = call_id + ".result.json"
            intent = {
                "plan_digest": digest(plan),
                "chunk_id": chunk["chunk_id"],
                "attempt": attempt,
                "input_digest": digest(user),
                "input_tokens": incoming,
                "thinking_enabled": thinking,
            }
            result_path = records_dir / result_name
            if result_path.exists():
                immutable_json(records_dir, intent_name, intent)
                record = json.loads(result_path.read_text(encoding="utf-8"))
                if record.get("intent_digest") != digest(intent):
                    raise ValueError("checkpoint_identity_mismatch")
            elif (records_dir / intent_name).exists():
                # Unknown in-flight outcomes consume their durable admission and never auto-retry.
                immutable_json(records_dir, intent_name, intent)
                final_state = "interrupted_call_no_retry"
                break
            elif stopping:
                break
            else:
                live()
                immutable_json(records_dir, intent_name, intent)
                response = None
                try:
                    with workload_scope(
                        plan["workload"],
                        task_id=digest([quota.scope_id, call_id]),
                        max_tokens=plan["max_input_tokens"] + plan["output_tokens"],
                        quotas=(quota, parent_quota) if parent_quota else (quota,),
                        pricing=pricing,
                        before_dispatch=live,
                    ):
                        response = await service.complete_text(
                            system_prompt=SYSTEM_PROMPT,
                            user_prompt=user,
                            prompt_summary="Authorized full recorded group history application",
                            require_json=True,
                            temperature=0,
                            max_output_tokens=plan["output_tokens"],
                            thinking_enabled=thinking,
                            client_name=name,
                            model=model,
                        )
                    live()
                    record = {
                        "intent_digest": digest(intent),
                        "state": "received",
                        "raw": response.content,
                        "usage": response.usage,
                        "finish_reason": response.finish_reason,
                        "partial": response.partial,
                    }
                    if len(response.content.encode()) > 131072:
                        record = {"intent_digest": digest(intent), "state": "output_size_limit"}
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - private exception boundary
                    live()  # Revocation aborts publication, including failure artifacts.
                    record = {
                        "intent_digest": digest(intent),
                        "state": "provider_failed",
                        "error_class": type(exc).__name__,
                        "error_category": getattr(exc, "category", "unknown"),
                    }
                    if type(exc).__name__ in {
                        "WorkBudgetExceeded",
                        "BackgroundBudgetDeferred",
                        "BackgroundTaskBudgetExceeded",
                    }:
                        stopping = True
                live()
                immutable_json(records_dir, result_name, record)
            final_state = record["state"]
            if record["state"] != "received":
                break  # Provider failures are terminal, no provider retries or repair calls.
            try:
                if record.get("partial") or record.get("finish_reason") in {"length", "max_tokens"}:
                    raise ValueError("incomplete_generation")
                parsed, rejected = parse_sections(
                    json.loads(record["raw"]), chunk["messages"], prior
                )
                if rejected and attempt == 0:
                    salvage_candidate = parsed.model_dump()
                    salvage_rejections = rejected
                    raise ValueError("rejected_items:" + canonical_json(rejected))
                accepted = parsed.model_dump()
                accepted_rejections = rejected
                final_state = "completed_with_rejected_items" if rejected else "completed"
                break
            except (ValueError, TypeError) as exc:
                final_state = "invalid_output"
                from pydantic import ValidationError

                if isinstance(exc, ValidationError):
                    codes = [
                        str(error["loc"]) + ":" + error["type"]
                        for error in exc.errors(include_input=False)
                    ]
                elif type(exc) is ValueError and str(exc).startswith(
                    ("invalid_evidence", "incomplete_generation", "rejected_items")
                ):
                    codes = [str(exc)]
                else:
                    codes = ["invalid_json_or_type"]
                live()
                immutable_json(
                    records_dir,
                    call_id + ".validation.json",
                    {"intent_digest": digest(intent), "codes": codes},
                )
                repair = bounded_repair(
                    canonical_json({"validation_codes": codes, "output": record["raw"]}), counter
                )
        if accepted is None and salvage_candidate is not None:
            accepted = salvage_candidate
            accepted_rejections = salvage_rejections
            final_state = "completed_with_rejected_items"
        if accepted is not None:
            live()
            tracker.observe(chunk, accepted)
            scope = mapping[chunk["conversation"]]
            rows, claims = stable_profile_inputs(chunk["messages"], accepted["claims"], scope)
            outcome = profiles.ingest(
                scope["source"]["conversation_key"],
                claims,
                rows,
                max(row["received_at"] for row in rows),
                capture_epoch=scope["source"]["capture_epoch"],
            )
            profile_ingest[chunk["chunk_id"]] = outcome
        coverage.append(
            {
                "chunk_id": chunk["chunk_id"],
                "conversation": chunk["conversation"],
                "offsets": chunk["offsets"],
                "message_count": len(chunk["messages"]),
                "state": final_state,
                "rejected_items": accepted_rejections,
                "semantic_item_coverage_complete": final_state == "completed",
            }
        )
        print(canonical_json({"chunk_id": chunk["chunk_id"], "state": final_state}), flush=True)
    live()
    profile_exports = {}
    for alias, scope in mapping.items():
        live()
        names = {
            native: scope.get("display_names", {}).get(sender, "")
            for native, sender in scope["users"].items()
        }
        profile_exports[alias] = profiles.export(
            scope["source"]["conversation_key"],
            now=int(time.time()),
            capture_epoch=scope["source"]["capture_epoch"],
            display_names=names,
        )
    report = {
        "plan_digest": digest(plan),
        "snapshot_id": manifest["snapshot_id"],
        "coverage": {
            "message_count": plan["message_count"],
            "chunks": coverage,
            "completed_messages": sum(len(t.completed) for t in trackers.values()),
            "capture_gaps": "unknown",
            "attachment_contents": "unknown",
            "all_recorded_rows_planned": True,
        },
        "groups": {alias: tracker.report() for alias, tracker in trackers.items()},
        "usage": ledger_summary(controller, quota.scope_id),
        "aggregate_allowance": controller.quota_usage(
            parent["scope_id"] if parent else quota.scope_id
        ),
        "parent_contract": parent,
        "profile_exports": profile_exports,
        "profile_ingest": profile_ingest,
        "workload": plan["workload"],
        "workload_purpose": plan["workload_purpose"],
        "observations_not_verified_facts": True,
    }
    live()
    # Each publication revision is immutable, including partial coverage reports.
    publication_id = digest(report)[:16]
    immutable_json(directory, "report-" + publication_id + ".json", report)
    output = directory / ("report-" + publication_id + ".md")
    live()
    immutable_markdown(output, markdown(report, messages, mapping))
    return {
        "state": "completed"
        if all(row["state"] == "completed" for row in coverage)
        else "incomplete",
        "completed_messages": report["coverage"]["completed_messages"],
        "report": str(output.relative_to(ROOT)),
        "usage": report["usage"],
    }


def main() -> None:
    from app.core.local_config import load_local_config
    from app.core.prompt_tokens import PromptTokenCounter

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--run-name", default="history-v1")
    parser.add_argument("--parent-run")
    parser.add_argument("--config", default="config/local.toml")
    parser.add_argument("--chunk-size", type=int, default=100)
    parser.add_argument("--max-input-tokens", type=int, default=12000)
    parser.add_argument("--output-tokens", type=int, default=5000)
    parser.add_argument(
        "--workload", choices=["interactive", "background_memory"], default="background_memory"
    )
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--authorize-remote", action="store_true")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--authority")
    parser.add_argument("--authority-python")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,60}", args.run_name):
            raise ValueError("invalid_run_name")
        if args.plan == args.remote or (args.remote and not args.authority):
            raise ValueError("choose_plan_or_remote_with_authority")
        if any(value is not None and value <= 0 for value in (args.max_calls, args.max_tokens)):
            raise ValueError("authorization_limits_must_be_positive")
        snapshot = _safe_destination(args.snapshot)
        if args.parent_run == args.run_name:
            raise ValueError("successor_cannot_be_own_parent")
        manifest, messages = load_snapshot(snapshot)
        mapping = json.loads((snapshot / "identity_mapping.json").read_text(encoding="utf-8"))
        config = load_local_config(Path(args.config))
        name, model, provider = _selection(config)
        resolved = config.llm.resolve_model_config(name, model)
        counter = PromptTokenCounter(resolved.tokenizer_json_path if resolved else None)
        plan, chunks = build_plan(
            manifest,
            messages,
            mapping,
            counter,
            provider=provider,
            chunk_size=args.chunk_size,
            max_input=args.max_input_tokens,
            output_tokens=args.output_tokens,
            workload=args.workload,
            parent_contract=parent_contract(snapshot, args.parent_run),
        )
        directory = _safe_destination(str(snapshot / args.run_name))
        directory.mkdir(exist_ok=True, mode=0o700)
        immutable_json(directory, "plan.json", plan)
        if args.plan:
            print(canonical_json({key: value for key, value in plan.items() if key != "chunks"}))
        else:
            profile_root = ROOT / "data/message_replay/living_profiles"
            profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with run_lock(profile_root), run_lock(directory):
                print(
                    canonical_json(
                        asyncio.run(
                            execute(
                                args,
                                directory,
                                plan,
                                chunks,
                                manifest,
                                messages,
                                mapping,
                                config,
                                counter,
                            )
                        )
                    )
                )
    except KeyboardInterrupt:
        print(
            canonical_json({"state": "interrupted", "retry": "no_free_retry_check_private_intents"})
        )
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001 - no exception-carried secrets/chat
        print(canonical_json({"state": "stopped", "error_class": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
