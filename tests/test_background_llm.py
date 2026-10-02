from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.api.main import create_app
from app.core.background_llm import BatchedExtractionClient
from app.core.config import get_settings
from app.core.llm_workloads import (
    LLMWorkloadController,
    UsageTicket,
    Workload,
    current_workload,
    workload_scope,
)
from app.core.memory_extraction import extract_user_memories


def test_batch_drops_evidence_that_matches_multiple_original_messages():
    messages = [
        "写代码时我倾向先给补丁，之后解释原理",
        "审阅合同我倾向先给补丁，再列出风险",
    ]

    class Client:
        calls = 0

        def complete_text(self, **kwargs):
            self.calls += 1
            batch = json.loads(kwargs["user_prompt"])
            assert batch["user_messages"] == messages
            return SimpleNamespace(content=json.dumps({"candidates": [{
                # This common substring drops both task qualifiers. It must not
                # be attributed to both sources by substring matching.
                "claim": "先给补丁",
                "evidence": "我倾向先给补丁",
                "kind": "preference",
                "explicit": False,
                "confidence": 0.9,
            }]}, ensure_ascii=False))

    client = Client()
    batch = BatchedExtractionClient(client, messages)
    extracted = [
        extract_user_memories(
            source_id=f"source-{index}", content=message,
            llm_client=batch, allow_remote=True,
        )
        for index, message in enumerate(messages)
    ]

    assert extracted == [[], []]
    assert client.calls == 1


def test_batch_awaits_async_provider_and_reuses_partitioned_results():
    messages = [
        "写代码时我倾向先给补丁，之后解释原理",
        "审阅合同我倾向先列风险，再解释细节",
    ]
    claims = ["我倾向先给补丁", "我倾向先列风险"]

    class Client:
        calls = 0

        async def complete_text(self, **kwargs):
            self.calls += 1
            assert current_workload().task_id == "async-batch-job"
            assert json.loads(kwargs["user_prompt"])["user_messages"] == messages
            return SimpleNamespace(content=json.dumps({"candidates": [
                {"claim": claim, "evidence": claim, "kind": "preference",
                 "explicit": False, "confidence": 0.9}
                for claim in claims
            ]}, ensure_ascii=False))

    client = Client()
    batch = BatchedExtractionClient(client, messages)
    with workload_scope("background_memory", task_id="async-batch-job", max_tokens=10000):
        extracted = [
            extract_user_memories(
                source_id=f"source-{index}", content=message,
                llm_client=batch, allow_remote=True,
            )
            for index, message in enumerate(messages)
        ]

    assert [[candidate.claim for candidate in candidates] for candidates in extracted] == [
        [claim] for claim in claims
    ]
    assert client.calls == 1


def test_oversized_persisted_user_message_is_bounded_and_never_sent_to_remote(
    tmp_path, monkeypatch,
):
    config = tmp_path / "local.toml"
    config.write_text(
        "[memory]\nenabled=true\nbackground_enabled=true\n"
        "allow_remote_extraction=true\nextraction_debounce_seconds=0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    get_settings.cache_clear()
    runtime = create_app().state.runtime

    session_id = "large-background-message"
    runtime.session_service.ensure_session(session_id=session_id)
    body = "请帮我总结这份资料：" + "背景材料" * 30_000
    run = runtime.agent_run_manager.create_run(session_id=session_id, user_input="large")
    runtime.agent_run_manager.mark_running(run.run_id)
    user = runtime.session_service.append_message(
        session_id=session_id, role="user", content=body,
        payload={"trace_id": run.trace_id},
    )
    answer = runtime.session_service.append_message(
        session_id=session_id, role="agent", content="已完成",
        payload={"trace_id": run.trace_id, "run_id": run.run_id},
    )
    runtime.agent_run_manager.complete_run(run.run_id, result_snapshot={"answer": "已完成"})

    class Client:
        calls = 0

        def complete_text(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(content='{"candidates":[]}')

    client = Client()
    coordinator = runtime.memory_background
    coordinator.llm_client = client
    coordinator.allow_remote_extraction = True

    # Observe the text boundary at both the pre-batch scan and the final
    # extractor call; SQLite should never materialize the complete large row.
    import app.core.memory_background as memory_background_module

    real_extract = memory_background_module.extract_user_memories
    observed_lengths: list[int] = []

    def observe_extract(**kwargs):
        observed_lengths.append(len(kwargs["content"]))
        return real_extract(**kwargs)

    monkeypatch.setattr(memory_background_module, "extract_user_memories", observe_extract)
    coordinator._extract({
        "job_id": "large-job", "lease_owner": "test-worker", "lease_epoch": 1,
        "scope_id": session_id, "payload": {"message_ids": [answer.message_id]},
    })

    assert len(body) > 20_000
    assert observed_lengths and max(observed_lengths) <= 20_001
    assert user.message_id
    assert client.calls == 0


def test_usage_ledger_prunes_only_nonreserved_rows_older_than_90_days(tmp_path):
    controller = LLMWorkloadController(tmp_path / "usage.sqlite3")
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    live_reservation_expiry = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    with sqlite3.connect(controller.db_path) as conn:
        for call_id, status, expires_at in (
            ("old-completed", "completed", old),
            ("old-failed", "failed", old),
            ("old-estimated", "estimated", old),
            ("old-reserved", "reserved", live_reservation_expiry),
        ):
            conn.execute(
                "INSERT INTO llm_workload_usage VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, "background_memory", "old-task", old, expires_at, status,
                 10, 5, 0.0, 1.0, "test"),
            )

    ticket = UsageTicket(
        call_id="fresh-reservation", workload=Workload("background_memory"),
        input_tokens=2, output_tokens=3,
    )
    controller._reserve(ticket)
    with sqlite3.connect(controller.db_path) as conn:
        rows = dict(conn.execute(
            "SELECT call_id,status FROM llm_workload_usage"
        ).fetchall())
    assert rows == {
        "old-reserved": "reserved",
        "fresh-reservation": "reserved",
    }
    controller._finish(ticket)
