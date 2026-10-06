"""Bounded history application tests use synthetic snapshots and fake LLM only."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.llm_workloads import current_workload
from app.core.prompt_tokens import PromptTokenCounter
from app.domains.message_history_tracking import (
    HistoryTracker,
    analysis_projection,
    decode_history,
    encode_history,
)
from app.domains.message_reading_replay import canonical_json, digest
from app.tool_packages.message_history_analysis import Analysis, parse_sections, validate_evidence
from scripts import analyze_message_history as runner


def row(number=1, alias="c1", text="请明天提交计划"):
    return {
        "id": f"{alias}m{number:05d}",
        "sender": alias + "p0001",
        "seq": number,
        "sent_at": 1700000000 + number,
        "received_at": 1700000001 + number,
        "text": text,
        "kind": "text",
        "mentions": [],
        "reply": None,
        "timestamp_quality": "provider",
        "thread": None,
        "capabilities": {},
        "parts": [{"kind": "text", "text": text}],
    }


def answer(rows):
    return {
        "schema_version": 2,
        "topics": [
            {
                "local_key": "plan",
                "existing_key": None,
                "continuity_source_id": None,
                "title": "计划",
                "summary": "提交计划",
                "source_ids": [rows[0]["id"]],
                "member_ids": [r["id"] for r in rows],
            }
        ],
        "highlights": [],
        "important": [
            {
                "text": "提交计划",
                "source_ids": [rows[0]["id"]],
                "quote": "请明天提交计划",
                "reason": "期限",
            }
        ],
        "claims": [],
        "group_summary": "讨论计划",
        "warnings": [],
    }


def fixture_snapshot(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    messages = {f"c{index}": [row(n, f"c{index}") for n in range(1, 4)] for index in range(1, 4)}
    mapping = {
        alias: {
            "source": {
                "group_id": group,
                "conversation_key": "qq:group:" + group,
                "capture_epoch": 1,
            },
            "users": {"native-user": alias + "p0001"},
            "messages": {"native-" + r["id"]: r["id"] for r in rows},
            "display_names": {alias + "p0001": "测试人物"},
        }
        for (alias, rows), group in zip(
            messages.items(), sorted(runner.ALLOWED_GROUPS), strict=True
        )
    }
    manifest = {
        "snapshot_id": "synthetic",
        "projection_digest": digest(messages),
        "mapping_digest": digest(mapping),
    }
    for name, value in (
        ("manifest.json", manifest),
        ("messages.json", messages),
        ("identity_mapping.json", mapping),
    ):
        runner._artifact(snapshot, name, value)
    return snapshot, manifest, messages, mapping


def test_minimal_codec_preserves_all_analysis_semantics():
    rows = [
        row(),
        {
            **row(2, text=""),
            "sent_at": None,
            "kind": "image",
            "parts": [{"kind": "unsupported"}],
            "mentions": ["all"],
            "reply": "unresolved",
            "thread": "thread1",
            "timestamp_quality": "unknown",
        },
    ]
    assert decode_history(encode_history(rows)) == [analysis_projection(r) for r in rows]


def test_plan_covers_every_row_and_rejects_oversize(tmp_path):
    _, manifest, messages, mapping = fixture_snapshot(tmp_path)
    plan, chunks = runner.build_plan(
        manifest,
        messages,
        mapping,
        PromptTokenCounter(),
        provider="fake",
        chunk_size=2,
        max_input=12000,
        output_tokens=3500,
    )
    assert plan["message_count"] == 9
    for alias, rows in messages.items():
        assert [r for c in chunks if c["conversation"] == alias for r in c["messages"]] == rows
    messages["c1"][0]["text"] = "极长" * 20000
    with pytest.raises(ValueError, match="single_message"):
        runner.build_plan(
            manifest,
            messages,
            mapping,
            PromptTokenCounter(),
            provider="fake",
            chunk_size=100,
            max_input=12000,
            output_tokens=3500,
        )


def test_schema_evidence_and_semantic_membership():
    rows = [row(), row(2), row(3)]
    value = answer(rows[:2])
    parsed = Analysis.model_validate(value)
    assert validate_evidence(parsed, rows, []) == []
    tracker = HistoryTracker("c1", rows)
    tracker.observe({"chunk_id": "b1", "messages": rows}, value)
    report = tracker.report()
    assert report["topics_by_activity"][0]["message_count"] == 2
    assert report["unassigned_completed_ids"] == [rows[2]["id"]]
    value["topics"][0]["existing_key"] = "unknown"
    assert "unsupported_topic_continuity" in validate_evidence(
        Analysis.model_validate(value), rows, []
    )


def test_reverse_mapping_stays_local(tmp_path):
    _, _, messages, mapping = fixture_snapshot(tmp_path)
    rows = messages["c1"]
    claims = [
        {
            "sender": "c1p0001",
            "source_ids": [rows[0]["id"]],
            "evidence_quotes": {rows[0]["id"]: "请明天"},
        }
    ]
    native_rows, native_claims = runner.stable_profile_inputs(rows, claims, mapping["c1"])
    assert native_rows[0]["sender"] == "native-user"
    assert native_claims[0]["evidence_quotes"] == {"native-c1m00001": "请明天"}
    assert "native-user" not in runner.prompt({"conversation": "c1", "messages": rows}, [])
    assert rows[0]["sender"] == "c1p0001"


@pytest.mark.parametrize("case", ["normal", "repair", "failure", "revoke", "crash", "successor"])
def test_fake_execution_resume_and_fencing(tmp_path, monkeypatch, case):
    snapshot, manifest, messages, mapping = fixture_snapshot(tmp_path)
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
    directory = snapshot / "run"
    directory.mkdir()
    runner._artifact(directory, "plan.json", plan)
    config = SimpleNamespace(
        llm=SimpleNamespace(
            timeout_seconds=30,
            resolve_model_config=lambda *args: SimpleNamespace(context_window_tokens=32000),
        ),
        background=SimpleNamespace(
            hourly_token_limit=1000000, daily_token_limit=2000000, daily_cost_limit=0
        ),
        message_history=SimpleNamespace(model_prices=[]),
    )
    args = SimpleNamespace(
        snapshot=str(snapshot),
        config="fake",
        authorize_remote=True,
        max_calls=6,
        max_tokens=200000,
        run_name="run",
        authority="fake",
        authority_python=None,
    )
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "_selection", lambda config: ("fake", "fake", "fake"))
    monkeypatch.setattr("app.core.local_config.load_local_config", lambda path: config)
    policies = [
        {
            "conversation_key": value["source"]["conversation_key"],
            "capture_epoch": 1,
            "record_enabled": True,
        }
        for value in mapping.values()
    ]
    monkeypatch.setattr(runner, "_current_policies", lambda *args: policies)

    class FakeService:
        calls = 0

        def supports_thinking_control(self, **kwargs):
            return True

        async def complete_text(self, **kwargs):
            self.calls += 1
            await current_workload().check_dispatch()
            async with self.workloads.admit(input_tokens=100, output_tokens=3500) as ticket:
                ticket.dispatched = True
                ticket.usage = {"prompt_tokens": 100, "completion_tokens": 100}
                if case == "crash":
                    raise asyncio.CancelledError
                if case == "failure":
                    raise RuntimeError("secret chat should never appear")
                if case == "revoke":
                    policies[0]["capture_epoch"] = 2
                value = json.loads(kwargs["user_prompt"])
                rows = decode_history(value["messages"])
                raw = (
                    "bad JSON"
                    if case == "repair" and self.calls == 1
                    else canonical_json(answer(rows))
                )
                return SimpleNamespace(
                    content=raw, usage=ticket.usage, partial=False, finish_reason="stop"
                )

    service = FakeService()
    monkeypatch.setattr("app.core.llm.build_llm_service", lambda _: service)
    invoke = lambda: asyncio.run(
        runner.execute(args, directory, plan, chunks, manifest, messages, mapping, config, counter)
    )
    if case == "revoke":
        with pytest.raises(ValueError):
            invoke()
        assert not list(directory.glob("report*"))
        assert not list((directory / "calls").glob("*.result.json"))
        return
    if case == "crash":
        with pytest.raises(asyncio.CancelledError):
            invoke()
        case = "normal"
        result = invoke()
        assert result["state"] == "incomplete"
        assert result["completed_messages"] == 6
        assert service.calls == 3  # crashed first chunk is not called again
        return
    result = invoke()
    assert result["state"] == ("incomplete" if case == "failure" else "completed")
    count = service.calls
    result2 = invoke()
    assert service.calls == count
    assert result2["usage"]["durable_usage"]["total_calls"] == count
    assert count == (4 if case == "repair" else 3)
    assert result2["usage"]["cost"] is None
    reports = [json.loads(path.read_text()) for path in directory.glob("report-*.json")]
    assert reports[0]["coverage"]["completed_messages"] == (0 if case == "failure" else 9)
    if case == "successor":
        child = snapshot / "child"
        child.mkdir()
        child_plan, child_chunks = runner.build_plan(
            manifest,
            messages,
            mapping,
            counter,
            provider="fake",
            chunk_size=100,
            max_input=12000,
            output_tokens=3500,
            parent_contract=runner.parent_contract(snapshot, "run"),
        )
        runner._artifact(child, "plan.json", child_plan)
        args.run_name = "child"
        args.max_calls = 3
        args.max_tokens = 100000
        child_result = asyncio.run(
            runner.execute(
                args, child, child_plan, child_chunks, manifest, messages, mapping, config, counter
            )
        )
        assert child_result["state"] == "completed"
        child_report = json.loads(next(child.glob("report-*.json")).read_text())
        assert child_report["usage"]["durable_usage"]["total_calls"] == 3
        assert child_report["aggregate_allowance"]["total_calls"] == 6
        too_large = snapshot / "too-large"
        too_large.mkdir()
        runner._artifact(too_large, "plan.json", child_plan)
        args.run_name = "too-large"
        with pytest.raises(ValueError, match="remaining_allowance"):
            asyncio.run(
                runner.execute(
                    args,
                    too_large,
                    child_plan,
                    child_chunks,
                    manifest,
                    messages,
                    mapping,
                    config,
                    counter,
                )
            )
        assert not (too_large / "grant.json").exists()


def test_grant_scope_epoch_and_immutable_finite_budget(tmp_path):
    _, manifest, messages, mapping = fixture_snapshot(tmp_path)
    plan, _ = runner.build_plan(
        manifest,
        messages,
        mapping,
        PromptTokenCounter(),
        provider="fake",
        chunk_size=2,
        max_input=12000,
        output_tokens=3500,
    )
    grant = {
        "purpose": "full_history_application",
        "active": True,
        "snapshot_id": "synthetic",
        "provider_hash": "fake",
        "manifest_digest": digest(manifest),
        "mapping_digest": digest(mapping),
        "expires_at": 100,
        "run_name": "run",
        "scope_id": "full-history:synthetic:run",
        "plan_digest": digest(plan),
        "max_calls": 6,
        "max_tokens": 100000,
        "conversation_keys": [r["source"]["conversation_key"] for r in mapping.values()],
    }
    policies = [
        {
            "conversation_key": r["source"]["conversation_key"],
            "capture_epoch": 1,
            "record_enabled": True,
        }
        for r in mapping.values()
    ]
    runner.validate_application_grant(grant, plan, manifest, mapping, "fake", policies, 1)
    policies[0]["capture_epoch"] = 2
    with pytest.raises(ValueError):
        runner.validate_application_grant(grant, plan, manifest, mapping, "fake", policies, 1)
    runner.immutable_json(tmp_path, "immutable.json", {"key": 1})
    with pytest.raises(ValueError):
        runner.immutable_json(tmp_path, "immutable.json", {"key": 2})


def test_recursive_native_evidence_survives_alias_shift():
    versions = []
    for number in (1, 5):
        alias = f"c1p{number:04d}"
        message = {
            **row(),
            "sender": alias,
            "text": f"@{alias} 回复 c1m00001",
            "mentions": [alias],
            "reply": "c1m00001",
            "parts": [
                {"kind": "text", "text": f"@{alias} 回复 c1m00001"},
                {"kind": "mention", "target": alias},
            ],
        }
        claim = {
            "sender": alias,
            "text": f"@{alias}",
            "quote": f"@{alias}",
            "source_ids": ["c1m00001"],
            "evidence_quotes": {"c1m00001": f"@{alias}"},
        }
        mapping = {"users": {"native-person": alias}, "messages": {"native-message": "c1m00001"}}
        versions.append(runner.stable_profile_inputs([message], [claim], mapping))
    assert versions[0] == versions[1]
    assert versions[0][0][0]["parts"][1]["target"] == "native-person"
    assert versions[0][1][0]["evidence_quotes"] == {"native-message": "@native-person"}


def test_atomic_immutable_markdown_and_shared_lock(tmp_path):
    path = tmp_path / "report.md"
    runner.immutable_markdown(path, "正文\n")
    runner.immutable_markdown(path, "正文\n")
    with pytest.raises(ValueError):
        runner.immutable_markdown(path, "changed\n")
    assert path.read_text() == "正文\n"
    assert not list(tmp_path.glob(".report-*"))
    with runner.run_lock(tmp_path), pytest.raises(OSError), runner.run_lock(tmp_path):
        pass
    with runner.run_lock(tmp_path):
        pass


def test_style_requires_exact_multiple_authored_evidence():
    rows = [row(), row(2)]
    value = answer(rows)
    value["claims"] = [
        {
            "sender": "c1p0001",
            "kind": "communication_style",
            "text": "询问细节",
            "quote": "请明天",
            "source_ids": [rows[0]["id"]],
            "basis": "observed",
        }
    ]
    assert "insufficient_style_evidence" in validate_evidence(
        Analysis.model_validate(value), rows, []
    )
    value["claims"][0]["source_ids"].append(rows[1]["id"])
    value["claims"][0]["evidence_quotes"] = {r["id"]: "请明天" for r in rows}
    assert validate_evidence(Analysis.model_validate(value), rows, []) == []


def test_itemwise_salvage_preserves_valid_items_without_fixing_ids_or_quotes():
    rows = [row(), row(2)]
    value = answer(rows)
    value["claims"] = [
        {
            "sender": "c1p9999",
            "kind": "need",
            "text": "假候选",
            "quote": "不在原文",
            "source_ids": ["unknown"],
            "basis": "uncertain",
            "evidence_quotes": ["不合法数组"],
        }
    ]
    value["topics"].append({**value["topics"][0], "local_key": "bad", "member_ids": ["c1p0001"]})
    accepted, rejected = parse_sections(value, rows, [])
    assert accepted.topics[0].member_ids == [r["id"] for r in rows]
    assert len(accepted.topics) == 1
    assert len(accepted.important) == 1
    assert accepted.claims == []
    assert {r["section"] for r in rejected} == {"topics", "claims"}
