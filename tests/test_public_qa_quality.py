"""No-API regressions for the bounded public direct-service QA diagnostic."""

import asyncio
import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.llm import LLMResponse, LLMService
from app.core.llm.models import LLMResponseMode
from app.core.llm.registry import LLMClientRegistry
from app.core.local_config import LLMClientConfig, LLMProviderConfig, LocalAppConfig
from evals.lka_evals import public_qa
from evals.lka_evals.live_budget import LiveBudget, LiveBudgetExceeded
from evals.lka_evals.public_datasets import PublicDataset, PublicDocument, PublicQuery
from evals.lka_evals.public_retrieval import evaluate_dataset


class FakeClient:
    name = "fake"
    default_model = "deepseek-flash"

    def __init__(self, *, status="completed", finish_reason="stop", partial=False,
                 content=None, delay=0):
        self.requests = []
        self.status, self.finish_reason, self.partial = status, finish_reason, partial
        self.content, self.delay = content, delay

    async def complete(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        payload = json.loads(request.messages[1].content)
        refs = [payload["evidence"][0]["source_ref"]] if payload["evidence"] else []
        return LLMResponse(provider="fake", client_name=self.name, model=self.default_model,
            status=self.status, finish_reason=self.finish_reason, partial=self.partial,
            prompt_summary=request.prompt_summary,
            content=self.content if self.content is not None else json.dumps({
                "answer": "Paris", "citations": refs, "abstain": False}),
            usage={"prompt_tokens": 500, "completion_tokens": 20})


def setup(tmp_path, client=None, protected_values=None):
    client = client or FakeClient()
    config = LLMProviderConfig(default_client="fake", clients=[LLMClientConfig(
        name="fake", provider="openai_compatible", default_model=client.default_model,
        context_window_tokens=148000,
    )])
    registry = LLMClientRegistry()
    registry.register_client(client)
    service = LLMService(config=config, registry=registry)
    budget = public_qa.RunBudget(LiveBudget(tmp_path / "shared-budget.sqlite3", usd_limit=50),
                                source_id="test-rag")
    if protected_values is not None:
        from evals.lka_evals.live_budget import MeteredClient
        registry.register_client(MeteredClient(client, budget, allowed_model=client.default_model,
                                              protected_values=protected_values))
    return client, service, budget


def pairs(count=1):
    return [{"dataset": "hotpotqa" if i < 5 else "2wiki",
             "query": PublicQuery(str(i), f"What is city {i}?", "ORACLE_ONLY_NOT_EVIDENCE", ["doc"]),
             "evidence": [{"source_ref": "public/ref", "title": "City", "text": "Paris is a city."}]}
            for i in range(count)]


def evaluate(tmp_path, client=None, count=1, **kwargs):
    client, service, budget = setup(tmp_path, client)
    result = asyncio.run(public_qa.evaluate_pairs(pairs(count), service=service, budget=budget,
        client_name="fake", model=client.default_model, **kwargs))
    return client, budget, result


def test_sampling_is_shared_random_25_and_five_pairs_with_fixed_seed():
    queries = [PublicQuery(str(i), f"Question {i}", "answer") for i in range(200)]
    selected, paired = public_qa.sample_queries(queries, 20261005)
    assert len(selected) == 25 and len(paired) == 5 and paired == selected[:5]
    assert public_qa.sample_queries(queries, 20261005) == (selected, paired)
    assert public_qa.sample_queries(queries, 17)[0] != selected


def test_twenty_real_boundary_dispatches_same_parameters_no_oracle_or_tools(tmp_path):
    client, budget, result = evaluate(tmp_path, count=10)
    assert len(client.requests) == 20
    assert budget.snapshot()["groups"]["llm"]["calls"] == 20
    for i in range(0, 20, 2):
        closed, rag = client.requests[i:i + 2]
        assert closed.max_output_tokens == rag.max_output_tokens == 4096
        assert closed.metadata == rag.metadata == {"network_timeout_seconds": 30}
        assert closed.temperature == rag.temperature == 0
        assert closed.response_mode == rag.response_mode == LLMResponseMode.JSON
        assert closed.tools == rag.tools == []
        assert closed.messages[0].content == rag.messages[0].content
        assert "ORACLE_ONLY_NOT_EVIDENCE" not in closed.model_dump_json() + rag.model_dump_json()
        assert json.loads(closed.messages[1].content)["evidence"] == []
        assert json.loads(rag.messages[1].content)["evidence"][0]["text"] == "Paris is a city."
    assert result["summary"]["arms"]["retrieval"]["failures"] == 0
    assert set(result["summary"]["by_dataset"]) == {"hotpotqa", "2wiki"}
    # Reusing the ledger does not allow a 21st dispatch or rewrap/double-charge it.
    service = LLMService(config=LLMProviderConfig(default_client="fake", clients=[LLMClientConfig(
        name="fake", default_model="deepseek-flash", context_window_tokens=148000)]),
        registry=LLMClientRegistry())
    service.registry.register_client(client)
    repeated = asyncio.run(public_qa.evaluate_pairs(pairs(), service=service, budget=budget,
        client_name="fake", model="deepseek-flash"))
    assert len(client.requests) == 20
    assert all(row["status"] == "failure" for row in repeated["rows"])


@pytest.mark.parametrize("kwargs", [
    {"status": "incomplete"}, {"status": "failed"}, {"finish_reason": "length"},
    {"partial": True}, {"finish_reason": "content_filter"}, {"content": ""},
    {"content": '{"answer":"Par'}, {"content": '{"answer":"","citations":[],"abstain":false}'},
])
def test_incomplete_or_malformed_generation_is_failure_not_abstention(tmp_path, kwargs):
    client, budget, result = evaluate(tmp_path, FakeClient(**kwargs))
    assert len(client.requests) == 2
    assert budget.snapshot()["groups"]["llm"]["calls"] == 2
    assert all(row["status"] == "failure" and not row["abstain"]
               and row["metrics"] == {"exact_match": 0, "f1": 0} for row in result["rows"])


def test_timeout_no_retry_keeps_unknown_usage_reservation(tmp_path):
    client, budget, result = evaluate(tmp_path, FakeClient(delay=.1), timeout_seconds=.005)
    assert len(client.requests) == 2
    assert all(row["error_type"] == "TimeoutError" for row in result["rows"])
    usage = budget.snapshot()["groups"]["llm"]
    assert usage["calls"] == 2 and usage["known_usage_calls"] == 0 and usage["charged_usd"] > 0


def test_credential_guard_before_dispatch_and_reservation(tmp_path):
    client, service, budget = setup(tmp_path, protected_values=("synthetic-env-secret-983",))
    selected = pairs()
    selected[0]["query"] = PublicQuery("q", "synthetic-env-secret-983", "gold")
    result = asyncio.run(public_qa.evaluate_pairs(selected, service=service, budget=budget,
        client_name="fake", model="deepseek-flash"))
    assert client.requests == [] and budget.snapshot()["groups"] == {}
    assert all(row["error_type"] == "ProtectedEvaluationContent" for row in result["rows"])


def test_capacity_preflight_does_not_drop_evidence_or_send_partial_context(tmp_path):
    client, service, budget = setup(tmp_path)
    selected = pairs()
    selected[0]["evidence"][0]["text"] = "x" * 148000
    result = asyncio.run(public_qa.evaluate_pairs(selected, service=service, budget=budget,
        client_name="fake", model="deepseek-flash"))
    assert len(client.requests) == 1  # Closed book fits; actual RAG context fails closed.
    assert result["rows"][1]["status"] == "failure"
    assert len(result["rows"][1]["prompt"]["evidence"][0]["text"]) == 148000


def test_short_answer_scoring_citations_and_literal_checks_are_separate():
    assert public_qa.score_answer("PARIS!", "Paris") == {"exact_match": 1, "f1": 1}
    assert public_qa.score_answer("Paris city", "Paris")["f1"] == pytest.approx(.6667)
    answer = public_qa.parse_answer('{"answer":"Paris","citations":["ref"],"abstain":false}')
    check = public_qa.context_checks(answer, [{"source_ref": "ref", "text": "Paris is a city."}], "Paris")
    assert check["citation_validity"] and check["answer_literal_in_cited_context"]
    answer["citations"] = ["foreign"]
    check = public_qa.context_checks(answer, [{"source_ref": "ref", "text": "Paris"}], "Paris")
    assert not check["citation_validity"] and check["unsupported_citation_count"] == 1
    assert not check["answer_literal_in_cited_context"]
    answer["answer"] = "yes"
    assert public_qa.context_checks(answer, [], "yes")["answer_literal_in_cited_context"] is None


def test_explicit_abstention_is_not_transport_failure(tmp_path):
    _, _, result = evaluate(tmp_path, FakeClient(content='{"answer":"","citations":[],"abstain":true}'))
    assert all(row["status"] == "completed" and row["abstain"] for row in result["rows"])


def test_private_raw_checkpoints_and_stdout_summary_excludes_raw(tmp_path, monkeypatch, capsys):
    _, _, result = evaluate(tmp_path, output_dir=tmp_path)
    path = tmp_path / "qa_raw.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "ORACLE_ONLY_NOT_EVIDENCE" in path.read_text()
    monkeypatch.setattr(public_qa, "run_diagnostic", lambda **_: result["summary"])
    monkeypatch.setattr("sys.argv", ["public_qa", "--output-dir", str(tmp_path)])
    public_qa.main()
    text = capsys.readouterr().out
    assert "ORACLE_ONLY_NOT_EVIDENCE" not in text and "Paris" not in text
    assert json.loads(text)["label"] == public_qa.LABEL


def test_optional_retrieval_capture_uses_actual_top10_chunks_and_does_not_change_metrics(tmp_path):
    dataset = PublicDataset("tiny", [PublicDocument(str(i), f"Title {i}", f"shared phrase fact{i}")
                                     for i in range(12)],
                            [PublicQuery("q", "shared phrase", "unused gold", ["0"])])
    captured = []
    report = evaluate_dataset(dataset, mode="keyword", top_k=10, sample_limit=0, seed=1,
        data_dir=tmp_path / "capture", query_details=captured, capture_evidence_ids={"q"})
    baseline = evaluate_dataset(dataset, mode="keyword", top_k=10, sample_limit=0, seed=1,
        data_dir=tmp_path / "baseline")
    assert report["metrics"] == baseline["metrics"]
    assert len(captured[0]["search"]["results"]) == len(captured[0]["evidence"]) == 10
    assert [row["chunk_id"] for row in captured[0]["evidence"]] == [
        row["chunk_id"] for row in captured[0]["search"]["results"]]
    assert report["setup_timing_ms"]["total_cpu"] >= 0


def test_excess_pair_count_or_dispatch_limit_rejected_before_call(tmp_path):
    client, service, budget = setup(tmp_path)
    with pytest.raises(ValueError, match="20-dispatch"):
        asyncio.run(public_qa.evaluate_pairs(pairs(11), service=service, budget=budget,
            client_name="fake", model="deepseek-flash"))
    assert client.requests == []


def test_remote_requires_explicit_shared_ledger_before_any_work(tmp_path, monkeypatch):
    touched = []
    monkeypatch.setattr(public_qa, "load_local_config", lambda _: touched.append(True))
    with pytest.raises(ValueError, match="budget-ledger"):
        public_qa.run_diagnostic(config_path=tmp_path / "config.toml",
                                 output_dir=tmp_path / "out", remote=True)
    assert touched == [] and not (tmp_path / "out").exists()


def test_shared_exhaustion_cannot_be_bypassed_by_local_twenty_cap(tmp_path):
    client, service, budget = setup(tmp_path)
    shared = LiveBudget(tmp_path / "shared.sqlite3", usd_limit=50)
    shared.reserve(kind="llm", stage="other-worker", incoming=62_490_000)
    budget = public_qa.RunBudget(shared, source_id="test-rag", limit=20)
    selected = pairs()
    selected[0]["query"] = PublicQuery("q", "Q" * 10000, "oracle")
    result = asyncio.run(public_qa.evaluate_pairs(selected, service=service, budget=budget,
        client_name="fake", model="deepseek-flash"))
    assert client.requests == []
    assert shared.snapshot()["groups"]["llm"]["calls"] == 1
    assert budget.snapshot()["run_budget"]["dispatches"] == 0
    assert all(row["error_type"] == "LiveBudgetExceeded" for row in result["rows"])


@pytest.mark.parametrize("unavailable", [False, True])
@pytest.mark.parametrize("remote", [False, True])
def test_whole_runner_reuses_same_ids_and_fails_before_paid_when_local_model_missing(
    tmp_path, monkeypatch, unavailable, remote,
):
    client, service, budget = setup(tmp_path)
    config = LocalAppConfig(llm=service.config)
    monkeypatch.setattr(public_qa, "load_local_config", lambda _: config)
    monkeypatch.setattr(public_qa, "build_llm_service", lambda _: service)
    monkeypatch.setattr(public_qa, "DATASET_DIR", tmp_path)
    for name in ("hotpotqa_200", "2wikimultihopqa_200"):
        PublicDataset(name, [PublicDocument("doc", "City", "Paris is a city.")],
                      [PublicQuery(str(i), f"City question {i}", "Paris", ["doc"])
                       for i in range(200)]).to_jsonl(tmp_path / f"{name}.jsonl")
    selections = {}

    def fake_retrieval(dataset, *, mode, sample_limit, seed, query_details, capture_evidence_ids, **kwargs):
        assert sample_limit == 25 and seed == 20261005 and kwargs["top_k"] == 10
        selected, _ = public_qa.sample_queries(dataset.queries, seed)
        selections.setdefault(dataset.dataset, []).append([query.query_id for query in selected])
        missing = unavailable and mode == "hybrid_rerank"
        if not missing:
            for query in selected:
                row = {"query_id": query.query_id, "failed": False, "fallback": 0}
                if capture_evidence_ids and query.query_id in capture_evidence_ids:
                    row["evidence"] = [{"source_ref": "ref", "title": "City", "text": "Paris is a city."}]
                query_details.append(row)
        return {"mode_status": "unavailable" if missing else "available", "query_count": 25,
                "failure_count": 0, "fallback_rate": 0, "mode": mode, "dataset": dataset.dataset}

    monkeypatch.setattr(public_qa, "evaluate_dataset", fake_retrieval)
    output = tmp_path / "private-output"
    if unavailable:
        with pytest.raises(ValueError, match="no generation dispatched"):
            public_qa.run_diagnostic(config_path=tmp_path / "config.toml", output_dir=output,
                                     remote=remote, budget_ledger=budget.ledger.path)
        assert client.requests == []
    else:
        result = public_qa.run_diagnostic(config_path=tmp_path / "config.toml", output_dir=output,
                                          remote=remote, budget_ledger=budget.ledger.path)
        assert result["unique_retrieval_questions"] == 50
        assert len(client.requests) == (20 if remote else 0)
        if remote:
            assert result["qa"]["arms"]["retrieval"]["exact_match"] == 1
            assert result["qa"]["ledger"]["usd_limit"] == 50
            assert result["qa"]["ledger"]["run_budget"]["dispatches"] == 20
            assert result["qa"]["ledger"]["ledger_path"] == str(budget.ledger.path.resolve())
        else:
            assert result["qa"]["generation_attempts"] == 0
        assert stat.S_IMODE(output.stat().st_mode) == 0o700
        assert not (output / "budget.sqlite3").exists()
        summary = (output / "summary.json").read_text()
        assert "Paris" not in summary and '"prompt"' not in summary
    assert all(len(modes) == 3 and modes[0] == modes[1] == modes[2] for modes in selections.values())


def test_cli_remote_without_shared_ledger_is_rejected(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(public_qa, "run_diagnostic", lambda **_: called.append(True))
    monkeypatch.setattr("sys.argv", ["public_qa", "--remote", "--output-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        public_qa.main()
    assert exc.value.code == 2 and called == []


def test_shared_prior_calls_do_not_consume_local_allowance_but_are_audited(tmp_path):
    client, service, budget = setup(tmp_path)
    previous = budget.ledger.reserve(kind="llm", stage="other-worker", incoming=1, outgoing=1)
    result = asyncio.run(public_qa.evaluate_pairs(pairs(), service=service, budget=budget,
        client_name="fake", model="deepseek-flash"))
    assert len(client.requests) == 2
    snapshot = result["summary"]["ledger"]
    assert snapshot["groups"]["llm"]["calls"] == 3
    assert snapshot["run_budget"]["dispatches"] == 2
    assert previous not in snapshot["run_budget"]["call_ids"]
    with budget.ledger.connect() as conn:
        stages = conn.execute("SELECT stage FROM calls WHERE id != ?", (previous,)).fetchall()
    assert all(stage[0].startswith("test-rag:") for stage in stages)


def test_evidence_loading_failure_does_not_double_count_retrieval_metrics(tmp_path, monkeypatch):
    from app.domains.knowledge import KnowledgeService

    def fail_load(*args, **kwargs):
        raise RuntimeError("injected context load failure")

    monkeypatch.setattr(KnowledgeService, "load_chunks", fail_load)
    dataset = PublicDataset("tiny", [PublicDocument("a", "City", "Paris city")],
                            [PublicQuery("q", "Paris", "Paris", ["a"])])
    captured = []
    result = evaluate_dataset(dataset, mode="keyword", top_k=10, sample_limit=0, seed=1,
        data_dir=tmp_path, query_details=captured, capture_evidence_ids={"q"})
    assert result["query_count"] == 1 and result["failure_count"] == 0
    assert result["metrics"]["recall_at_10"] == 1 and len(captured) == 1
    assert captured[0]["evidence_error_type"] == "RuntimeError"


def test_concurrent_local_allowance_never_dispatches_twenty_first(tmp_path):
    _, _, budget = setup(tmp_path)

    def reserve(i):
        try:
            return budget.reserve(kind="llm", stage=f"attempt-{i}", incoming=10, outgoing=10)
        except LiveBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(reserve, range(30)))
    accepted = [value for value in ids if value is not None]
    assert len(accepted) == len(set(accepted)) == 20
    assert ids.count(None) == 10
    snapshot = budget.snapshot()
    assert snapshot["call_limit"] == 2000  # Shared cap is not replaced by the local cap.
    assert snapshot["groups"]["llm"]["calls"] == snapshot["run_budget"]["dispatches"] == 20
    assert snapshot["groups"]["llm"]["known_usage_calls"] == 0
    assert snapshot["groups"]["llm"]["charged_usd"] > 0


@pytest.mark.parametrize("remote", [False, True])
def test_cli_defaults_offline_and_requires_both_explicit_remote_flags(tmp_path, monkeypatch, remote):
    from pathlib import Path

    called = []
    monkeypatch.setattr(public_qa, "run_diagnostic", lambda **kw: called.append(kw) or {})
    ledger = tmp_path / "shared.sqlite3"
    ledger.touch()
    argv = ["public_qa", "--output-dir", str(tmp_path / "out")]
    if remote:
        argv += ["--remote", "--budget-ledger", str(ledger)]
    monkeypatch.setattr("sys.argv", argv)
    public_qa.main()
    assert called[0]["remote"] is remote
    assert called[0]["budget_ledger"] == (Path(ledger) if remote else None)


def test_context_summary_distinguishes_requested_and_historical_effective_clamp():
    detail = {"search": {"results": [{}, {}, {}]}, "evidence_filtered_count": 1,
              "evidence": [{"text": "x" * 1197 + "...", "char_count": 1554},
                           {"text": "short", "char_count": 5}]}
    result = public_qa.summarize_context_delivery([detail])
    assert result["requested_chunks"] == 3
    assert result["loaded_chunks"] == 2 and result["filtered_chunks"] == 1
    assert result["requested_max_chars_per_chunk"] == 1800
    assert result["effective_text_char_limit_observed"] == 1200
    assert result["loader_char_cap_hits"] == 0
    assert result["loader_1200_clamp_chunks"] == result["declared_char_count_text_shortfalls"] == 1
    assert "NOT full" in result["context_unit"]
    assert "not measured" in result["full_document_coverage"]
    unknown = public_qa.summarize_context_delivery([{"evidence": []}])
    assert unknown["requested_chunks"] is unknown["filtered_chunks"] is None
    assert unknown["effective_text_char_limit_observed"] is None


def test_existing_raw_analysis_is_local_only_overlapping_not_semantic_and_preserves_originals(
    tmp_path, monkeypatch, capsys,
):
    def forbidden(*args, **kwargs):
        pytest.fail("Saved-raw analysis must not initialize retrieval, models, config or a ledger")

    for name in ("evaluate_dataset", "build_llm_service", "load_local_config", "LiveBudget"):
        monkeypatch.setattr(public_qa, name, forbidden)
    entries = [{"dataset": "2wiki", "path": "/public/2wiki.jsonl", "paired_query_ids": ["a", "b", "c"]}]
    clamped = {"source_ref": "ref", "text": "Danish " + "x" * 1190 + "...", "char_count": 1554}
    short = {"source_ref": "ref", "text": "British", "char_count": 7}
    details = [{"query_id": "a", "search": {"results": [{}]}, "evidence": [clamped],
                "evidence_filtered_count": 0, "metrics": {"all_hop_recall": 0}},
               {"query_id": "b", "search": {"results": [{}]}, "evidence": [short],
                "evidence_filtered_count": 0, "metrics": {"all_hop_recall": 0}},
               {"query_id": "c", "search": {"results": [{}]}, "evidence": [short],
                "evidence_filtered_count": 0, "metrics": {"all_hop_recall": 1}}]
    rows = [{"dataset": "2wiki", "query_id": "a", "arm": "retrieval", "status": "completed",
             "gold": "Danish", "abstain": False, "prompt": {"evidence": [clamped]},
             "context_checks": {"citation_validity": True}, "metrics": {"exact_match": 0, "f1": 0}},
            {"dataset": "2wiki", "query_id": "b", "arm": "retrieval", "status": "completed",
             "gold": "British", "abstain": True, "prompt": {"evidence": [short]},
             "context_checks": {"citation_validity": False}, "metrics": {"exact_match": 0, "f1": 0}},
            {"dataset": "2wiki", "query_id": "c", "arm": "retrieval", "status": "failure",
             "gold": "yes", "abstain": False,
             "prompt": {"evidence": [{"source_ref": "ref", "text": "Bri"}]},
             "metrics": {"exact_match": 0, "f1": 0}}]
    original = {"manifest.json": entries, "2wiki_hybrid_rerank_raw.json": details,
                "qa_raw.json": rows, "retrieval_metrics.json": {"unchanged": True}}
    for name, data in original.items():
        public_qa._save_private(tmp_path / name, data)
    public_qa._save_private(tmp_path / "summary.json", {"qa": {"ledger": {"usd_limit": 2}}})
    (tmp_path / "budget.sqlite3").write_bytes(b"preserve reconciled historical source")
    originals = {name: (tmp_path / name).read_bytes() for name in (*original, "budget.sqlite3")}
    monkeypatch.setattr("sys.argv", ["public_qa", "--analyze-existing", str(tmp_path)])
    public_qa.main()
    printed = json.loads(capsys.readouterr().out)
    assert "cases" not in printed and "Danish" not in json.dumps(printed)
    assert printed["generation_outcomes"] == {"retrieval_rows": 3, "failures": 1, "abstentions": 1}
    counts = printed["category_counts"]
    assert counts["missing_hop"] == 2
    assert counts["abstained_despite_gold_literal"] == 1
    assert counts["citation_answer_gold_f1_zero"] == 1
    assert counts["rag_span_truncated_after_loading"] == 1
    assert counts["loader_1200_clamp_evidence"] == 1
    assert "NOT necessarily a wrong answer" in printed["limits"]
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["qa"]["ledger"] == {"usd_limit": 2}  # Do not relabel or reconcile it.
    assert all((tmp_path / name).read_bytes() == data for name, data in originals.items())
    assert stat.S_IMODE((tmp_path / "failure_analysis.json").stat().st_mode) == 0o600
    cases = json.loads((tmp_path / "failure_analysis.json").read_text())["cases"]
    assert cases[2]["flags"]["literal_gold_absent"] is None


def test_analysis_cli_cannot_accidentally_enable_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(public_qa, "analyze_saved_raw", lambda _: pytest.fail("Must reject before work"))
    monkeypatch.setattr("sys.argv", ["public_qa", "--analyze-existing", str(tmp_path), "--remote"])
    with pytest.raises(SystemExit) as exc:
        public_qa.main()
    assert exc.value.code == 2
