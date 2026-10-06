"""Explicit local snapshot preparation and bounded real text-model experiments.

Run `python -m scripts.replay_message_reading --help`. No production writes,
automatic remote calls, or user chat in console output. Artifacts stay in data/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from app.domains.message_reading_replay import (
    build_windows,
    canonical_json,
    create_snapshot,
    digest,
    load_snapshot,
    validate_grant,
    validate_windows,
)

ROOT = Path(__file__).resolve().parents[1]


def _artifact(directory: Path, name: str, value: object) -> None:
    path = directory / name
    with path.open("x", encoding="utf-8") as output:
        output.write(canonical_json(value) + "\n")
    path.chmod(0o600)


def audit_history(messages: dict, counter) -> dict:
    """Local whole-snapshot engineering replay, never semantic model evaluation."""
    from app.domains.message_participant_profiles import DAY, ParticipantProfileIndex
    from app.domains.message_reading_codec import CODEC_VERSION, decode_messages, encode_messages

    groups = {}
    for alias, rows in sorted(messages.items()):
        index = ParticipantProfileIndex(alias)
        ordered = sorted(rows, key=lambda row: (row["received_at"], row["seq"], row["id"]))
        now = max((row["received_at"] for row in ordered), default=0)
        for row in ordered:
            index.observe([row], row["received_at"])
            index.hot_profiles(row["received_at"])
        index._last_recompute = None
        hot = index.hot_profiles(now)
        # Duplicate delivery is idempotent within retained history.
        before = {sender: index.profile(sender, now)["score"] for sender in index._people}
        index.observe(ordered, now)
        after = {sender: index.profile(sender, now)["score"] for sender in index._people}
        if before != after:
            raise ValueError("historical_activity_not_idempotent")
        payload_tokens = {"plain": 0, "compact": 0, "body": 0}
        for offset in range(0, len(rows), 200):
            batch = rows[offset:offset + 200]
            encoded = encode_messages(batch)
            if decode_messages(encoded) != batch:
                raise ValueError("historical_codec_roundtrip_failed")
            payload_tokens["plain"] += counter.count_text(canonical_json(batch)).count
            payload_tokens["compact"] += counter.count_text(canonical_json(encoded)).count
            payload_tokens["body"] += sum(counter.count_text(row["text"]).count for row in batch)
        simulations = {}
        for days in (7, 14, 31):
            index.cleanup(now + days * DAY)
            active = index.hot_profiles(now + days * DAY)
            simulations[str(days)] = {"remaining_activity_people": len(index._people),
                                       "hot_people": len(active)}
        groups[alias] = {"message_count": len(rows), "sender_count": len({r["sender"] for r in rows}),
                        "hot_people": len(hot), "pool_capacity": index.capacity,
                        "top_participants": [{"sender": r["sender"], "score": r["score"],
                                              "status": r["status"], "missing_components": r["missing_components"]}
                                             for r in hot[:10]],
                        "idempotent": True, "codec_roundtrip": True,
                        "projected_payload_tokens": payload_tokens, "simulated_days_without_new_input": simulations}
    totals = {key: sum(g["projected_payload_tokens"][key] for g in groups.values())
              for key in ("plain", "compact", "body")}
    return {"codec_version": CODEC_VERSION, "counter_method": counter.count_text("").method,
            "conservative_count": counter.count_text("").conservative, "groups": groups,
            "projected_payload_tokens": totals,
            "compact_payload_reduction": 1 - totals["compact"] / totals["plain"] if totals["plain"] else None,
            "token_scope": "payload_only_excludes_system_old_context_output_and_retries",
            "semantic_quality_verified": False, "human_gold_available": False,
            "remote_calls": 0, "production_approval": False}


def local_audit(args) -> None:
    from app.core.local_config import load_local_config
    from app.core.prompt_tokens import PromptTokenCounter

    directory = _safe_destination(args.snapshot)
    manifest, messages = load_snapshot(directory)
    if Path(args.output_name).name != args.output_name or not args.output_name.endswith(".json"):
        raise ValueError("audit_output_must_be_local_json")
    config = load_local_config(Path(args.config))
    name, model, _ = _selection(config)
    resolved = config.llm.resolve_model_config(name, model)
    counter = PromptTokenCounter(resolved.tokenizer_json_path if resolved else None)
    value = {"snapshot_id": manifest["snapshot_id"], **audit_history(messages, counter)}
    _artifact(directory, args.output_name, value)
    print(canonical_json({key: value[key] for key in (
        "snapshot_id", "codec_version", "counter_method", "projected_payload_tokens",
        "compact_payload_reduction", "remote_calls", "production_approval")}))


def _report_summary(records: list[dict], ledger: list[dict]) -> dict:
    """Do not equate a successful provider request with valid analysis/gold."""
    known_usage = [row for row in ledger if row["count_method"] == "provider_usage"]
    return {
        "admitted_calls": len(ledger),
        "recorded_responses": len(records),
        "provider_usage_calls": len(known_usage),
        "provider_reported_tokens": sum(
            row["input_tokens"] + row["output_tokens"] for row in known_usage
        ),
        "ledger_accounted_tokens": sum(
            row["input_tokens"] + row["output_tokens"] for row in ledger
        ),
        "estimated_usage_calls": len(ledger) - len(known_usage),
        "cost": sum(row["cost"] for row in ledger)
        if ledger and all(row["cost_known"] for row in ledger)
        else None,
        "structurally_completed": sum(row["state"] == "completed" for row in records),
        "gold_status": "unlabelled",
        "semantic_quality_verified": False,
        "critical_event_recall": None,
        "top5_usefulness": None,
        "profile_candidate_precision": None,
    }


def _json_fence(value: object) -> str:
    body = json.dumps(value, ensure_ascii=False, indent=2)
    width = max((len(run) for run in re.findall(r"`+", body)), default=0) + 1
    fence = "`" * max(3, width)
    return fence + "json\n" + body + "\n" + fence


def _authorized_failure_response(response, authorize) -> tuple[str | None, bool]:
    try:
        authorize()
    except Exception:  # noqa: BLE001 - fail closed without leaking exception bodies
        return None, False
    return response.content if response else None, True


def report(args) -> None:
    """Offline private case export; no model or production database access."""
    directory = _safe_destination(args.snapshot)
    manifest, messages = load_snapshot(directory)
    if Path(args.output_name).name != args.output_name or not args.output_name.endswith(".md"):
        raise ValueError("report_must_be_local_markdown")
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((directory / "results").glob("*.json"))
    ]
    mapping = json.loads((directory / "identity_mapping.json").read_text(encoding="utf-8"))
    grant = json.loads((directory / "grant.json").read_text(encoding="utf-8"))
    windows = json.loads((directory / "windows.json").read_text(encoding="utf-8"))
    validate_windows(windows, messages, grant.get("window_sets", {}).get("windows.json", ""))
    notes_path = directory / "review_notes.json"
    notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else {}
    with sqlite3.connect(
        (ROOT / "data/message_replay/usage.sqlite3").as_uri() + "?mode=ro", uri=True
    ) as conn:
        conn.row_factory = sqlite3.Row
        ledger = [
            dict(row)
            for row in conn.execute(
                "SELECT u.* FROM llm_workload_usage u "
                "JOIN llm_workload_quota_usage q ON q.call_id=u.call_id "
                "WHERE q.scope_id=? ORDER BY u.created_at",
                ("replay:" + manifest["snapshot_id"],),
            )
        ]
    summary = _report_summary(records, ledger)
    lines = [
        "# 消息阅读首轮回放：本地私密 case",
        "",
        "仅供本地人工审阅，不进 Git。正文为脱敏后的测试投影，不是完整原始聊天导出。",
        "开发集回放不代表 holdout 或生产验收；结构校验通过也不代表语义正确。",
        "预期和观察来自编码 Agent 的暂定审阅，不是用户标注的 gold。",
        "",
        "## 调用与费用",
        "",
        _json_fence({"snapshot_id": manifest["snapshot_id"], **summary}),
        "",
        "金额未配置时为 null，不按零费用解释。被取消的在途请求仍计入额度，",
        "无 provider usage 的调用按账本估算，不冒充真实消费量。",
        "关键事件召回、Top5 有用率和画像精度均未建立人工 gold，不能给出达标比例。",
    ]
    for window in windows:
        results = [row for row in records if row["window"] == window["window_id"]]
        if not results:
            continue
        source = mapping[window["conversation"]]["source"]
        lines += [
            "",
            "## " + window["window_id"],
            "",
            _json_fence({"source": source, "seq_range": window["range"], "split": window["split"]}),
            "",
            "### 暂定预期／观察（非人工 gold）",
            "",
            _json_fence(notes.get(window["window_id"], {"status": "unlabelled"})),
            "",
            "### 输入消息",
            "",
            _json_fence(window["messages"]),
        ]
        for row in sorted(
            results,
            key=lambda row: (
                row["prompt_version"],
                row["strategy"],
                row.get("selector_version") or "",
            ),
        ):
            lines += [
                "",
                "### " + row["prompt_version"] + " / " + row["strategy"],
                "",
                _json_fence(
                    {key: value for key, value in row.items() if key != "participant_profiles"}
                ),
            ]
    path = directory / args.output_name
    with path.open("x", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    path.chmod(0o600)
    _artifact(directory, Path(args.output_name).stem + ".json", summary)
    print(canonical_json({"report": str(path.relative_to(ROOT)), **summary}))


def label_or_score(args) -> None:
    """Explicit local review workflow; never fabricate human labels."""
    from app.domains.message_reading_metrics import score_reviews

    directory = _safe_destination(args.snapshot)
    manifest, messages = load_snapshot(directory)
    windows = json.loads((directory / "windows.json").read_text(encoding="utf-8"))
    grant = json.loads((directory / "grant.json").read_text(encoding="utf-8"))
    validate_windows(windows, messages, grant.get("window_sets", {}).get("windows.json", ""))
    if Path(args.output_name).name != args.output_name or not args.output_name.endswith(".json"):
        raise ValueError("review_output_must_be_local_json")
    artifacts = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((directory / "results").glob("*.json"))
    }
    if args.command == "label-template":
        value = {
            "schema_version": 1,
            "snapshot_id": manifest["snapshot_id"],
            "windows_digest": digest(windows),
            "reviewer_kind": "human",
            "review_complete": False,
            "reviewer_id": "",
            "reviewed_at": "",
            "windows": {
                row["window_id"]: {"input_digest": row["digest"], "events": [], "topic_ids": []}
                for row in windows
            },
            "judgements": [
                {
                    "window_id": row["window"],
                    "artifact_id": key,
                    "output_digest": digest(row),
                    "matched_event_ids": [],
                    "matched_topic_ids": [],
                    "useful_highlight_indices": [],
                    "accepted_claim_indices": [],
                    "supported_output_items": 0,
                    "examined_output_items": 0,
                    "note": "",
                }
                for key, row in artifacts.items()
            ],
        }
    else:
        if Path(args.labels_file).name != args.labels_file:
            raise ValueError("labels_file_must_be_local")
        labels = json.loads((directory / args.labels_file).read_text(encoding="utf-8"))
        value = score_reviews(labels, manifest, windows, artifacts)
    _artifact(directory, args.output_name, value)
    print(
        canonical_json(
            {
                "command": args.command,
                "output": args.output_name,
                "remote_calls": 0,
                "production_approval": False,
                "gold_status": value.get("gold_status", "unlabelled"),
            }
        )
    )


def _safe_destination(value: str) -> Path:
    path = Path(value).resolve()
    base = (ROOT / "data/message_replay").resolve()
    if not path.is_relative_to(base) or path == base:
        raise ValueError("output_must_be_inside_private_message_replay_directory")
    return path


def _selection(config):
    clients = config.llm.client_configs()
    name = config.message_history.background_client_name or config.llm.default_client
    if name is None and len(clients) == 1:
        name = clients[0].name
    client = next((row for row in clients if row.name == name), None)
    if client is None:
        raise ValueError("configured_background_client_unresolved")
    model = config.message_history.background_model or client.default_model
    provider_hash = digest(
        {"name": name, "provider": client.provider, "base_url": client.base_url, "model": model}
    )
    return name, model, provider_hash


def _current_policies(source: Path, windows_python: str | None = None) -> list[dict]:
    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", str(source)):
        if not windows_python:
            raise ValueError("native_authority_python_required")
        # Do not open a live Windows WAL database from WSL SQLite. The trusted
        # native reader returns permission counters only, never message bodies.
        code = (
            "import json,sqlite3,sys;from pathlib import Path;"
            "c=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+'?mode=ro',uri=True);"
            "c.row_factory=sqlite3.Row;"
            "print(json.dumps([dict(r) for r in c.execute('SELECT conversation_key,record_enabled,capture_epoch FROM message_history_policies')]));c.close()"
        )

        def quote(value):
            return "'" + value.replace("'", "''") + "'"

        command = "& " + " ".join(
            quote(value) for value in (windows_python, "-c", code, str(source))
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return json.loads(result.stdout)
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT conversation_key,record_enabled,capture_epoch FROM message_history_policies"
            )
        ]


def prepare(args) -> None:
    from app.core.local_config import load_local_config

    output = _safe_destination(args.output)
    manifest = create_snapshot(
        Path(args.source),
        output,
        platform=args.platform,
        account_id=args.account,
        group_ids=args.groups,
        until=int(datetime.fromisoformat(args.until).timestamp()),
        consent_at=datetime.now(UTC).isoformat(),
    )
    _, messages = load_snapshot(output)
    windows = build_windows(messages, size=args.window_size)
    _artifact(output, "windows.json", windows)
    _artifact(
        output,
        "labels_template.json",
        {
            row["window_id"]: {
                "status": "unlabelled",
                "events": [],
                "topics": [],
                "accepted_claims": [],
            }
            for row in windows
        },
    )
    name, model, provider = _selection(load_local_config(Path(args.config)))
    mapping = json.loads((output / "identity_mapping.json").read_text(encoding="utf-8"))
    if args.consent_remote:
        _artifact(
            output,
            "grant.json",
            {
                "active": True,
                "snapshot_id": manifest["snapshot_id"],
                "provider_hash": provider,
                "manifest_digest": digest(manifest),
                "mapping_digest": digest(mapping),
                "window_sets": {"windows.json": digest(windows)},
                "client_name": name,
                "model": model,
                "expires_at": int(time.time()) + 86400,
                "conversation_keys": [
                    value["source"]["conversation_key"] for value in mapping.values()
                ],
                "max_calls": 12,
                "max_tokens": 100000,
            },
        )
    print(
        canonical_json(
            {
                "snapshot_id": manifest["snapshot_id"],
                "messages": manifest["count"],
                "conversations": manifest["conversations"],
                "windows": len(windows),
                "remote_grant_created": args.consent_remote,
            }
        )
    )


async def evaluate(args) -> None:
    from app.core.llm import build_llm_service
    from app.core.llm_workloads import (
        BudgetPricing,
        BudgetQuota,
        LLMWorkloadController,
        workload_scope,
    )
    from app.core.local_config import load_local_config
    from app.core.prompt_tokens import PromptTokenCounter
    from app.domains.message_participant_profiles import (
        ConversationFocusTracker,
        ParticipantProfileIndex,
    )
    from app.domains.message_reading_codec import (
        CODEC_VERSION,
        SELECTOR_VERSION,
        decode_messages,
        encode_messages,
        select_messages,
    )
    from app.tool_packages.message_replay_analysis import (
        PROMPT_VERSION,
        SYSTEM_PROMPT,
        ReplayAnalysis,
        validate_evidence,
    )

    directory = _safe_destination(args.snapshot)
    manifest, snapshot_messages = load_snapshot(directory)
    mapping = json.loads((directory / "identity_mapping.json").read_text(encoding="utf-8"))
    if Path(args.windows_file).name != args.windows_file:
        raise ValueError("window_file_must_be_local")
    windows = json.loads((directory / args.windows_file).read_text(encoding="utf-8"))
    requested = set(args.windows or [])
    if requested - {window["window_id"] for window in windows}:
        raise ValueError("unknown_window")
    chosen = [window for window in windows if window["window_id"] in requested] if requested else []
    if args.remote and not chosen:
        raise ValueError("remote_requires_explicit_window_ids")
    if args.remote and any(window["split"] != "development" for window in chosen):
        raise ValueError("holdout_not_available_for_development_iterations")
    config = load_local_config(Path(args.config))
    name, model, provider_hash = _selection(config)
    resolved = config.llm.resolve_model_config(name, model)
    counter = PromptTokenCounter(resolved.tokenizer_json_path if resolved else None)
    service = build_llm_service(config.llm) if args.remote else None
    if args.remote and service is None:
        raise ValueError("configured_real_provider_unavailable")
    # This is a separate local evaluation ledger, not production worker state.
    # Existing authoritative admission primitives enforce the run lifetime cap.
    controller = LLMWorkloadController(
        ROOT / "data/message_replay/usage.sqlite3",
        hourly_tokens=config.background.hourly_token_limit,
        daily_tokens=config.background.daily_token_limit,
        daily_cost_limit=config.background.daily_cost_limit,
    )
    if service:
        service.workloads = controller
        service.background_timeout_seconds = min(60, config.llm.timeout_seconds)
    # Strict, short extraction does not need an unbounded reasoning transcript.
    # Unsupported providers receive no vendor-specific field.
    thinking_enabled = (
        False if service and service.supports_thinking_control(client_name=name) else None
    )
    grant_path = directory / "grant.json"

    def live():
        grant = json.loads(grant_path.read_text(encoding="utf-8"))
        # Reread processing identity, rather than permanently trusting startup.
        _, _, current_provider = _selection(load_local_config(Path(args.config)))
        validate_grant(
            grant,
            manifest,
            current_provider,
            _current_policies(Path(args.authority), args.authority_python),
            mapping,
            now=int(time.time()),
        )
        if current_provider != provider_hash:
            raise ValueError("evaluation_provider_changed")
        validate_windows(
            windows, snapshot_messages, grant.get("window_sets", {}).get(args.windows_file, "")
        )
        current_usage = controller.quota_usage("replay:" + manifest["snapshot_id"])
        if current_usage["total_calls"] > min(12, grant["max_calls"]) or current_usage[
            "total_tokens"
        ] > min(100000, grant["max_tokens"]):
            raise ValueError("evaluation_limits_tightened")
        return grant

    pricing_row = next(
        (
            row
            for row in config.message_history.model_prices
            if (row.client_name, row.model) == (name, model)
        ),
        None,
    )
    pricing = BudgetPricing(**pricing_row.model_dump()) if pricing_row else None
    results_directory = directory / "results"
    results_directory.mkdir(exist_ok=True)
    if not results_directory.resolve().is_relative_to(directory):
        raise ValueError("results_directory_outside_snapshot")
    results_directory.chmod(0o700)
    output_limit = min(args.output_tokens, config.message_history.generation_output_tokens)
    if output_limit < 256:
        raise ValueError("invalid_output_token_limit")
    for window in chosen or windows:
        if digest(window["messages"]) != window["digest"]:
            raise ValueError("window_digest_mismatch")
        authorized = {row["id"]: row for row in snapshot_messages.get(window["conversation"], [])}
        if not window["messages"] or any(
            authorized.get(row["id"]) != row for row in window["messages"]
        ):
            raise ValueError("window_outside_snapshot")
        for strategy in args.strategies:
            original = window["messages"]
            selection = select_messages(
                original, max_messages=args.selected_count, seed=manifest["snapshot_id"]
            )
            rows = selection["messages"] if strategy == "selected" else original
            payload = rows if strategy == "plain" else encode_messages(rows)
            if strategy != "plain" and decode_messages(payload) != rows:
                raise ValueError("codec_roundtrip_failed")
            user_prompt = canonical_json(
                {
                    "timezone": "Asia/Shanghai",
                    "conversation": window["conversation"],
                    "coverage_mode": selection["coverage_mode"]
                    if strategy == "selected"
                    else "full_text",
                    "messages": payload,
                }
            )
            incoming = counter.count_request(SYSTEM_PROMPT, user_prompt)
            call_key = digest(
                {
                    "snapshot": manifest["snapshot_id"],
                    "window": window["digest"],
                    "strategy": strategy,
                    "prompt": PROMPT_VERSION,
                    "input": user_prompt,
                    "provider": provider_hash,
                    "output_tokens": output_limit,
                    "thinking_enabled": thinking_enabled,
                    "codec_version": CODEC_VERSION,
                    "selector_version": SELECTOR_VERSION if strategy == "selected" else None,
                }
            )
            artifact = results_directory / (call_key + ".json")
            if artifact.exists():
                print(
                    canonical_json(
                        {
                            "window": window["window_id"],
                            "strategy": strategy,
                            "state": "already_recorded",
                        }
                    ),
                    flush=True,
                )
                continue
            summary = {
                "window": window["window_id"],
                "strategy": strategy,
                "input_estimate": incoming.count,
                "count_method": incoming.method,
                "original_messages": len(original),
                "model_messages": len(rows),
                "prompt_version": PROMPT_VERSION,
                "gold_status": "unlabelled",
                "thinking_enabled": thinking_enabled,
                "codec_version": CODEC_VERSION,
                "selector_version": SELECTOR_VERSION if strategy == "selected" else None,
            }
            if incoming.count > config.message_history.max_input_tokens:
                print(canonical_json({**summary, "state": "input_too_large"}), flush=True)
                continue
            if not args.remote:
                print(canonical_json({**summary, "state": "local_preflight_only"}), flush=True)
                continue
            grant = live()
            quota = BudgetQuota(
                "replay:" + manifest["snapshot_id"],
                max_calls=min(12, grant["max_calls"]),
                max_tokens=min(100000, grant["max_tokens"]),
            )
            start = time.perf_counter()
            response = None
            permission_valid = False
            try:
                with workload_scope(
                    "background_memory",
                    task_id=call_key,
                    max_tokens=32768,
                    quotas=(quota,),
                    pricing=pricing,
                    before_dispatch=live,
                ):
                    response = await service.complete_text(
                        system_prompt=SYSTEM_PROMPT,
                        user_prompt=user_prompt,
                        prompt_summary="Isolated authorized reading replay",
                        require_json=True,
                        temperature=0,
                        max_output_tokens=output_limit,
                        thinking_enabled=thinking_enabled,
                        client_name=name,
                        model=model,
                    )
                live()
                permission_valid = True
                if response.partial or response.finish_reason in {"length", "max_tokens"}:
                    raise ValueError("incomplete_generation")
                if len(response.content.encode("utf-8")) > 65536:
                    raise ValueError("model_output_byte_limit")
                parsed = ReplayAnalysis.model_validate(json.loads(response.content))
                errors = validate_evidence(parsed, rows)
                state = "completed" if not errors else "evidence_rejected"
                now = max(row["received_at"] for row in original)
                people = ParticipantProfileIndex(window["conversation"])
                people.observe(
                    [
                        row
                        for row in snapshot_messages[window["conversation"]]
                        if row["seq"] <= window["range"][1]
                    ],
                    now,
                )
                rejected_claims = []
                if not errors:
                    for claim in parsed.participant_claim_candidates:
                        if not people.add_claim(claim.model_dump(), rows, now):
                            rejected_claims.append(
                                {"sender": claim.sender, "reason": people.last_rejection_reason}
                            )
                focus = ConversationFocusTracker(window["conversation"])
                if not errors:
                    focus.observe(
                        [candidate.model_dump() for candidate in parsed.focus_candidates], rows, now
                    )
                record = {
                    **summary,
                    "state": state,
                    "result": parsed.model_dump(),
                    "errors": errors,
                    "input_digest": digest(user_prompt),
                    "usage": response.usage,
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "selection": selection if strategy == "selected" else None,
                    "participant_profiles": people.hot_profiles(now),
                    "participant_candidates": people.candidates(now),
                    "rejected_claims": rejected_claims,
                    "focus": focus.merge(now),
                    "semantic_quality_verified": False,
                }
                permission_valid = False
                live()
                permission_valid = True
                _artifact(results_directory, artifact.name, record)
                print(
                    canonical_json(
                        {
                            **summary,
                            "state": state,
                            "errors": errors,
                            "usage": response.usage,
                            "elapsed_seconds": record["elapsed_seconds"],
                        }
                    ),
                    flush=True,
                )
            except asyncio.CancelledError:
                _artifact(
                    results_directory,
                    artifact.name,
                    {
                        **summary,
                        "state": "cancelled",
                        "usage": response.usage if response else None,
                        "elapsed_seconds": round(time.perf_counter() - start, 3),
                    },
                )
                raise
            except Exception as exc:  # noqa: BLE001 - private-data terminal boundary
                # Pydantic/provider exceptions may embed input bodies/URLs. Do
                # not expose their text to terminal or normal logs.
                state = "failed:" + type(exc).__name__
                # A parse failure can race permission revocation too. Never
                # retain model text without a fresh authoritative check.
                raw_response, permission_valid = _authorized_failure_response(response, live)
                _artifact(
                    results_directory,
                    artifact.name,
                    {
                        **summary,
                        "state": state,
                        "raw_response": raw_response,
                        "usage": response.usage if response else None,
                        "failure_code": str(exc)
                        if type(exc) is ValueError
                        and str(exc) in {"incomplete_generation", "model_output_byte_limit"}
                        else None,
                        "finish_reason": response.finish_reason if response else None,
                        "elapsed_seconds": round(time.perf_counter() - start, 3),
                    },
                )
                print(canonical_json({**summary, "state": state}), flush=True)
                if response and not permission_valid:
                    return
                if type(exc).__name__ in {"WorkBudgetExceeded", "BackgroundBudgetDeferred"}:
                    return


def main() -> None:
    os.chdir(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/local.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--source", required=True)
    snapshot.add_argument("--output", required=True)
    snapshot.add_argument("--platform", required=True)
    snapshot.add_argument("--account", required=True)
    snapshot.add_argument("--groups", nargs="+", required=True)
    snapshot.add_argument("--until", required=True)
    snapshot.add_argument("--window-size", type=int, default=24)
    snapshot.add_argument("--consent-remote", action="store_true")
    rebuild = commands.add_parser("windows")
    rebuild.add_argument("--snapshot", required=True)
    rebuild.add_argument("--window-size", type=int, default=12)
    rebuild.add_argument("--windows-file", default="windows-v2.json")
    replay = commands.add_parser("evaluate")
    replay.add_argument("--snapshot", required=True)
    replay.add_argument("--authority", required=True)
    replay.add_argument("--authority-python")
    replay.add_argument("--windows", nargs="+")
    replay.add_argument("--windows-file", default="windows.json")
    replay.add_argument(
        "--strategies",
        nargs="+",
        choices=["plain", "compact", "selected"],
        default=["plain", "compact", "selected"],
    )
    replay.add_argument("--selected-count", type=int, default=16)
    replay.add_argument("--output-tokens", type=int, default=2048)
    replay.add_argument("--remote", action="store_true")
    export = commands.add_parser("report")
    export.add_argument("--snapshot", required=True)
    export.add_argument("--output-name", default="cases_iteration1.md")
    labels = commands.add_parser("label-template")
    labels.add_argument("--snapshot", required=True)
    labels.add_argument("--output-name", default="human_labels_template.json")
    scoring = commands.add_parser("score")
    scoring.add_argument("--snapshot", required=True)
    scoring.add_argument("--labels-file", required=True)
    scoring.add_argument("--output-name", default="review_scores.json")
    audit = commands.add_parser("audit")
    audit.add_argument("--snapshot", required=True)
    audit.add_argument("--output-name", default="historical_engineering_audit.json")
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            prepare(args)
        elif args.command == "windows":
            directory = _safe_destination(args.snapshot)
            if Path(args.windows_file).name != args.windows_file:
                raise ValueError("window_file_must_be_local")
            _, messages = load_snapshot(directory)
            windows = build_windows(messages, size=args.window_size)
            _artifact(directory, args.windows_file, windows)
            print(
                canonical_json(
                    {
                        "windows": len(windows),
                        "window_file": args.windows_file,
                        "development": sum(window["split"] == "development" for window in windows),
                    }
                )
            )
        elif args.command == "report":
            report(args)
        elif args.command in {"label-template", "score"}:
            label_or_score(args)
        elif args.command == "audit":
            local_audit(args)
        else:
            asyncio.run(evaluate(args))
    except KeyboardInterrupt:
        print(canonical_json({"state": "cancelled", "usage": "check_durable_ledger"}))
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001 - never expose exception-carried secrets/chat
        print(canonical_json({"state": "stopped", "error_class": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
