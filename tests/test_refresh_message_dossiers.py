"""Offline dossier refresh uses synthetic finished artifacts; no LLM or native services."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from app.core.prompt_tokens import PromptTokenCounter
from app.domains.message_history_tracking import HistoryTracker
from app.domains.message_reading_replay import canonical_json, digest
from scripts import analyze_message_history as runner
from scripts import refresh_message_dossiers as refresh


def artifacts(tmp_path):
    snapshot = tmp_path / "snapshot"
    directory = snapshot / "run"
    calls = directory / "calls"
    calls.mkdir(parents=True)
    messages = {}
    mapping = {}
    for index, group in enumerate(sorted(runner.ALLOWED_GROUPS), 1):
        alias = f"c{index}"
        row = {
            "id": alias + "m00001",
            "sender": alias + "p0001",
            "seq": 1,
            "sent_at": int(time.time()) - 10,
            "received_at": int(time.time()) - 9,
            "text": "请明天提交计划",
            "kind": "text",
            "mentions": [],
            "reply": None,
            "parts": [],
            "thread": None,
            "timestamp_quality": "provider",
            "capabilities": {},
        }
        messages[alias] = [row]
        mapping[alias] = {
            "source": {
                "group_id": group,
                "conversation_key": "qq:group:" + group,
                "capture_epoch": 1,
            },
            "users": {"native-person": row["sender"]},
            "messages": {"native-" + row["id"]: row["id"]},
            "display_names": {row["sender"]: "测试作者"},
        }
    manifest = {
        "snapshot_id": "fixture",
        "projection_digest": digest(messages),
        "mapping_digest": digest(mapping),
    }
    for name, value in (
        ("manifest.json", manifest),
        ("messages.json", messages),
        ("identity_mapping.json", mapping),
    ):
        runner._artifact(snapshot, name, value)
    counter = PromptTokenCounter()
    plan, chunks = runner.build_plan(
        manifest,
        messages,
        mapping,
        counter,
        provider="fake",
        chunk_size=100,
        max_input=12000,
        output_tokens=3500,
    )
    runner._artifact(directory, "plan.json", plan)
    grant = {
        "active": True,
        "purpose": "full_history_application",
        "snapshot_id": "fixture",
        "manifest_digest": digest(manifest),
        "mapping_digest": digest(mapping),
        "provider_hash": "fake",
        "plan_digest": digest(plan),
        "run_name": "run",
        "scope_id": "full-history:fixture:run",
        "expires_at": int(time.time()) + 3600,
        "max_calls": 3,
        "max_tokens": 50000,
        "conversation_keys": [value["source"]["conversation_key"] for value in mapping.values()],
    }
    runner._artifact(directory, "grant.json", grant)
    trackers = {alias: HistoryTracker(alias, rows) for alias, rows in messages.items()}
    coverage = []
    for chunk in chunks:
        row = chunk["messages"][0]
        value = {
            "schema_version": 2,
            "topics": [
                {
                    "local_key": "t1",
                    "existing_key": None,
                    "continuity_source_id": None,
                    "title": "计划",
                    "summary": "提交计划",
                    "source_ids": [row["id"]],
                    "member_ids": [row["id"]],
                }
            ],
            "highlights": [],
            "important": [],
            "claims": [
                {
                    "sender": row["sender"],
                    "kind": "preference",
                    "text": "近期讨论计划",
                    "source_ids": [row["id"]],
                    "quote": row["text"],
                    "basis": "explicit",
                    "valid_until": None,
                    "facet": None,
                    "evidence_quotes": {row["id"]: row["text"]},
                }
            ],
            "group_summary": "讨论计划",
            "warnings": [],
        }
        intent = {
            "plan_digest": digest(plan),
            "chunk_id": chunk["chunk_id"],
            "attempt": 0,
            "input_digest": digest(runner.prompt(chunk, [])),
            "input_tokens": chunk["base_input_tokens"],
            "thinking_enabled": False,
        }
        runner._artifact(calls, chunk["chunk_id"] + "-a0.intent.json", intent)
        runner._artifact(
            calls,
            chunk["chunk_id"] + "-a0.result.json",
            {
                "intent_digest": digest(intent),
                "state": "received",
                "raw": canonical_json(value),
                "partial": False,
                "finish_reason": "stop",
            },
        )
        trackers[chunk["conversation"]].observe(chunk, value)
        coverage.append(
            {
                "chunk_id": chunk["chunk_id"],
                "conversation": chunk["conversation"],
                "offsets": chunk["offsets"],
                "message_count": 1,
                "state": "completed",
                "rejected_items": [],
            }
        )
    report = {
        "plan_digest": digest(plan),
        "snapshot_id": "fixture",
        "coverage": {"message_count": 3, "completed_messages": 3, "chunks": coverage},
        "groups": {alias: tracker.report() for alias, tracker in trackers.items()},
        "usage": {"durable_usage": {"total_calls": 3, "total_tokens": 2000}, "cost": None},
        "aggregate_allowance": {"total_calls": 3, "total_tokens": 2000},
    }
    report_path = directory / ("report-" + digest(report)[:16] + ".json")
    runner._artifact(directory, report_path.name, report)
    runner.immutable_markdown(report_path.with_suffix(".md"), "# 已完成报告\n")
    return snapshot, directory, manifest, messages, mapping, plan, chunks, report, counter


def test_refresh_has_no_llm_and_publishes_private_index(tmp_path, monkeypatch):
    snapshot, directory, _, _, mapping, _, _, _, _ = artifacts(tmp_path)
    args = SimpleNamespace(
        snapshot=str(snapshot),
        run_name="run",
        config="fake",
        authority="fake",
        authority_python=None,
    )
    config = SimpleNamespace(
        llm=SimpleNamespace(
            resolve_model_config=lambda *args: SimpleNamespace(tokenizer_json_path=None)
        )
    )
    monkeypatch.setattr(refresh, "ROOT", tmp_path)
    monkeypatch.setattr(refresh, "_safe_destination", lambda value: refresh.Path(value).resolve())
    monkeypatch.setattr(refresh, "load_local_config", lambda path: config)
    monkeypatch.setattr(refresh, "_selection", lambda config: ("fake", "fake", "fake"))
    policies = [
        {
            "conversation_key": value["source"]["conversation_key"],
            "record_enabled": True,
            "capture_epoch": 1,
        }
        for value in mapping.values()
    ]
    monkeypatch.setattr(refresh, "_current_policies", lambda *args: policies)

    def forbidden(*args, **kwargs):
        raise AssertionError("No model service may be constructed")

    monkeypatch.setattr("app.core.llm.build_llm_service", forbidden)
    result = refresh.refresh(args)
    assert result["remote_calls"] == 0
    index = tmp_path / result["index"]
    assert index.is_file()
    text = index.read_text()
    assert "测试作者" in text and "参与话题" in text
    assert "待核实笔记" in text
    assert all(
        summary["unverified_notes_in_store"] == 1 for summary in result["profile_ingest"].values()
    )
    policies[0]["capture_epoch"] = 2
    with pytest.raises(ValueError):
        refresh.refresh(args)
    assert len(list(directory.glob("dossiers-*"))) == 1


def test_report_required_and_reconstruction_detects_tampering(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="required"):
        refresh.latest_report(empty)
    _, directory, _, messages, _, plan, chunks, report, counter = artifacts(tmp_path)
    groups, results, rejected = refresh.reconstruct_results(
        plan, chunks, report, directory, messages, counter
    )
    assert groups == report["groups"] and len(results) == 3 and not rejected
    path = directory / "calls" / (chunks[0]["chunk_id"] + "-a0.intent.json")
    intent = json.loads(path.read_text())
    intent["input_digest"] = "modified"
    runner.immutable_json(directory, "unrelated-test-artifact.json", intent)
    # Tampering is supplied through a mocked read, preserving original immutable fixture files.
    original = refresh.Path.read_text

    def altered(self, *args, **kwargs):
        return canonical_json(intent) if self == path else original(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(refresh.Path, "read_text", altered)
        with pytest.raises(ValueError, match="intent_mismatch"):
            refresh.reconstruct_results(plan, chunks, report, directory, messages, counter)
