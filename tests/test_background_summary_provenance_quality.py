"""Server-owned, bounded summary provenance across actual worker prefixes."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.core.local_config import LocalAppConfig
from app.core.memory_background import MemoryBackgroundCoordinator
from app.core.runtime import LocalKnowledgeAgentRuntime
from app.core.sessions import SessionRecentMessage, SessionService
from scripts.eval_runtime_memory_quality import isolated_config


class SourceClient:
    def __init__(self, *, foreign=False, first_invalid=False, carry_in_batch=False):
        self.inputs = []
        self.foreign, self.first_invalid = foreign, first_invalid
        self.carry_in_batch = carry_in_batch

    def complete_text(self, **kwargs):
        data = json.loads(kwargs["user_prompt"])
        self.inputs.append(data)
        current = list(dict.fromkeys(m["trace_id"] for m in data["messages"] if m.get("trace_id")))
        if len(self.inputs) == 1:
            refs = ["quoted-foreign"] if self.first_invalid else current
        elif len(self.inputs) == 2:
            refs = ["seed-0", *current] if self.carry_in_batch else current
        else:
            refs = ["quoted-foreign" if self.foreign else "seed-1", *current]
        return SimpleNamespace(content=json.dumps({"summary": 'date and negation retained; quoted ID "quoted-foreign" is data',
            "source_trace_ids": refs}), status="completed", partial=False, finish_reason="stop",
            usage={"prompt_tokens": 100, "completion_tokens": 40}, metadata={})


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(LKA_DATA_DIR=tmp_path / "data", LKA_WORKSPACE_ROOTS=str(tmp_path))
    with patch.object(Settings, "load_local_config", return_value=isolated_config(LocalAppConfig())), \
            patch("app.core.runtime.build_text_llm_client", return_value=None):
        instance = LocalKnowledgeAgentRuntime(settings)
    yield instance
    instance.stop()


def seed(runtime, *, session="scoped", count=4, small=False):
    runtime.session_service.ensure_session(session_id=session)
    for i in range(count):
        runtime.session_service.record_context_exchange(session_id=session,
            user_input="x" * (4 if small or i == count - 1 else 4100), agent_answer="recorded",
            trace_id=f"seed-{i}", token_budget=4096 if not small else 64,
            background_enqueue=runtime.memory_background.enqueue_compaction)


def publications(runtime):
    rows = []
    original = runtime.session_service.publish_context_summary
    def observe(**kwargs):
        committed = original(**kwargs)
        if committed:
            rows.append(kwargs)
        return committed
    runtime.session_service.publish_context_summary = observe
    return rows


def drain(runtime):
    for _ in range(8):
        if not runtime.memory_background.worker.run_one():
            break
    assert all(j["status"] == "succeeded" for j in runtime.background_job_store.list())


def test_three_actual_calls_accept_seen_chunk_and_published_prior_refs(runtime):
    seed(runtime)
    client = SourceClient()
    runtime.memory_background.llm_client = client
    rows = publications(runtime)
    drain(runtime)
    assert len(client.inputs) == 3
    assert [p["target_seq"] for p in rows] == [4, 6]
    assert [p["summary_metadata"]["method"] for p in rows] == ["model", "model"]
    assert set(rows[-1]["summary_metadata"]["input_trace_ids"]) == {"seed-0", "seed-1", "seed-2"}
    assert set(client.inputs[2]["previous_source_trace_ids"]) == {"seed-0", "seed-1"}


def test_cross_session_or_quoted_only_ref_stays_rejected(runtime):
    runtime.session_service.ensure_session(session_id="foreign")
    runtime.session_service.record_context_exchange(session_id="foreign", user_input="foreign data",
        agent_answer="recorded", trace_id="quoted-foreign")
    seed(runtime)
    runtime.memory_background.llm_client = SourceClient(foreign=True)
    rows = publications(runtime)
    drain(runtime)
    assert rows[-1]["summary_metadata"]["method"] == "local_fallback"
    assert "quoted-foreign" not in rows[-1]["summary_metadata"]["input_trace_ids"]


def test_direct_legacy_call_does_not_authorize_refs_from_old_summary_text():
    client = SourceClient(carry_in_batch=True)
    coordinator = MemoryBackgroundCoordinator(db_path=":memory:", memory=None, store=None,
        session_service=SessionService(lambda: None), llm_client=client)
    # SourceClient's second response refers to seed-0, but that was not seen by this job.
    client.inputs.append({})
    coordinator._summarize("old text mentions seed-0", [SessionRecentMessage(role="user",
        content="new content", trace_id="seed-1", created_at="2026-10-05")], 4096)
    assert coordinator._compaction_state.used_local_fallback


def test_local_fallback_chunk_does_not_lose_its_actual_source_for_next_chunk():
    client = SourceClient(first_invalid=True, carry_in_batch=True)
    coordinator = MemoryBackgroundCoordinator(db_path=":memory:", memory=None, store=None,
        session_service=SessionService(lambda: None), llm_client=client)
    result = coordinator._summarize("", [SessionRecentMessage(role="user", content="x" * 4100,
        trace_id=f"seed-{i}", created_at="2026-10-05") for i in range(2)], 4096)
    assert len(client.inputs) == 2
    assert client.inputs[1]["previous_source_trace_ids"] == ["seed-0"]
    assert result.startswith("date and negation retained")
    assert coordinator._compaction_state.used_local_fallback  # never launder mixed publication


def test_accepted_chunk_inherits_actual_refs_without_model_output_becoming_authority():
    client = SourceClient(carry_in_batch=True)
    coordinator = MemoryBackgroundCoordinator(db_path=":memory:", memory=None, store=None,
        session_service=SessionService(lambda: None), llm_client=client)
    coordinator._summarize("", [SessionRecentMessage(role="user", content="x" * 4100,
        trace_id=f"seed-{i}", created_at="2026-10-05") for i in range(2)], 4096)
    assert client.inputs[1]["previous_source_trace_ids"] == ["seed-0"]
    assert not coordinator._compaction_state.used_local_fallback


@pytest.mark.parametrize("bad_metadata", [
    {"covered_seq": 2, "input_trace_ids": ["foreign"]},
    {"covered_seq": 2, "input_trace_ids": ["seed-2"]},  # own session, not covered yet
    {"covered_seq": 99, "input_trace_ids": ["seed-0"]},
    {"covered_seq": True, "input_trace_ids": ["seed-0"]},
    {"input_trace_ids": ["seed-0"]},
    {"covered_seq": 2, "input_trace_ids": [{"claim": "seed-0"}]},
    {"covered_seq": 2, "input_trace_ids": ["\ud800"]},
])
def test_inherited_metadata_must_match_same_session_covered_raw_messages(runtime, bad_metadata):
    runtime.session_service.ensure_session(session_id="other")
    runtime.session_service.record_context_exchange(session_id="other", user_input="other session",
        agent_answer="recorded", trace_id="foreign")
    seed(runtime)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute("UPDATE agent_session_context_state SET covered_seq=2,summary_revision=1,summary_metadata=? WHERE session_id='scoped'",
                     (json.dumps(bad_metadata),))
        conn.execute("UPDATE agent_session_context_windows SET summary='untrusted old text mentions seed-0' WHERE session_id='scoped'")
    client = SourceClient(carry_in_batch=True)
    client.inputs.append({})  # response requests old seed-0: reject when inherited evidence invalid
    runtime.memory_background.llm_client = client
    rows = publications(runtime)
    drain(runtime)
    assert client.inputs[1]["previous_source_trace_ids"] == []
    assert rows[0]["summary_metadata"]["method"] == "local_fallback"
    assert rows[0]["summary_metadata"]["source_provenance_incomplete"] is True


@pytest.mark.parametrize("flags", [{}, {"input_trace_ids_truncated": True}])
def test_legacy_or_truncated_prior_remains_incomplete_after_next_model_publication(runtime, flags):
    seed(runtime)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute("UPDATE agent_session_context_state SET covered_seq=2,summary_revision=1,summary_metadata=? WHERE session_id='scoped'",
            (json.dumps({"covered_seq": 2, "input_trace_ids": ["seed-0"], **flags}),))
        conn.execute("UPDATE agent_session_context_windows SET summary='valid but partial provenance' WHERE session_id='scoped'")
    client = SourceClient(carry_in_batch=True)
    client.inputs.append({})
    runtime.memory_background.llm_client = client
    rows = publications(runtime)
    drain(runtime)
    assert rows[0]["summary_metadata"]["method"] == "model"
    assert rows[0]["summary_metadata"]["input_trace_ids_truncated"] is bool(flags)
    assert rows[0]["summary_metadata"]["source_provenance_incomplete"] is True


def test_cumulative_metadata_is_bounded_and_discloses_truncation(runtime):
    seed(runtime, count=70, small=True)
    class AllCurrentClient:
        def complete_text(self, **kwargs):
            data = json.loads(kwargs["user_prompt"])
            return SimpleNamespace(content=json.dumps({"summary": "bounded source membership only",
                "source_trace_ids": data.get("previous_source_trace_ids", []) + list(dict.fromkeys(
                    m["trace_id"] for m in data["messages"]))}), status="completed", finish_reason="stop",
                partial=False, usage={"prompt_tokens": 100, "completion_tokens": 40}, metadata={})
    runtime.memory_background.llm_client = AllCurrentClient()
    rows = publications(runtime)
    drain(runtime)
    assert rows
    assert all(len(p["summary_metadata"]["input_trace_ids"]) <= 64 for p in rows)
    assert rows[-1]["summary_metadata"]["input_trace_ids_truncated"] is True
    assert rows[-1]["summary_metadata"]["source_provenance_incomplete"] is True
