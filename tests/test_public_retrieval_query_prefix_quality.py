"""Explicit benchmark query transforms; no model loading or semantic claims."""

import json
import sys

import numpy as np
import pytest

from evals.lka_evals import public_retrieval as bench
from evals.lka_evals.public_datasets import PublicDataset, PublicDocument, PublicQuery


@pytest.fixture
def encoder(monkeypatch):
    class CapturingEncoder:
        def __init__(self):
            self.inputs = []

        def embed(self, documents, batch_size):
            values = list(documents)
            self.inputs.extend(values)
            return iter(np.array([1.0, 0.0], dtype=np.float32) for _ in values)

        def query_embed(self, *_args, **_kwargs):
            pytest.fail("Adapter must not add a second query-transform layer.")

    model = CapturingEncoder()
    monkeypatch.setattr(bench.FastEmbedEmbeddingProvider, "_get_model", lambda _self: model)
    return model


def _dataset():
    return PublicDataset("prefix_fixture", [PublicDocument("a", "Alpha", "literal passage")],
                         [PublicQuery("q", "literal question", "", ["a"])])


@pytest.mark.parametrize("prefix", ["", "PREFIX:: ", "检索表示："])
def test_evaluate_dataset_delivers_literal_prefix_once_to_embed(tmp_path, encoder, prefix):
    result = bench.evaluate_dataset(
        _dataset(), mode="semantic", top_k=1, sample_limit=1, seed=7, data_dir=tmp_path,
        embedding_model="fixture-model", embedding_dimensions=2, query_prefix=prefix,
    )
    assert result["mode_status"] == "available" and result["failure_count"] == 0
    assert encoder.inputs == ["literal passage", prefix + "literal question"]
    profile = result["runtime_models"]["embedding"]
    assert profile["model_name"] == "fixture-model" and profile["dimensions"] == 2
    assert profile["query_prefix"] == prefix
    assert type(profile["threads"]) is int and profile["threads"] > 0
    assert profile["local_files_only"] is True


def test_new_service_does_not_prefix_passages_or_read_runtime_config(tmp_path, encoder, monkeypatch):
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "nonexistent-runtime.toml"))
    service = bench._new_service(tmp_path, embedding_model="fixture-model",
                                 embedding_dimensions=2, query_prefix="custom:")
    service._embedding_provider.embed_passages(["body"])
    service._embedding_provider.embed_query("query")
    assert encoder.inputs == ["body", "custom:query"]


def test_run_benchmark_records_explicit_independent_profile(tmp_path, encoder):
    path = tmp_path / "fixture.jsonl"
    _dataset().to_jsonl(path)
    report = bench.run_benchmark(
        datasets=[path], modes=["semantic"], top_k=1, sample_limit=1, seed=7,
        embedding_model="fixture-model", embedding_dimensions=2, query_prefix="custom:",
    )
    assert encoder.inputs == ["literal passage", "custom:literal question"]
    config = report["configuration"]
    assert config["query_prefix"] == "custom:"
    assert config["embedding_model"] == "fixture-model" and config["embedding_dimensions"] == 2
    assert config["profile_source"] == "independent_benchmark_parameters"
    assert config["query_transform_layer"] == "adapter_literal_prefix_then_fastembed_embed"
    assert report["results"][0]["runtime_models"]["embedding"]["query_prefix"] == "custom:"


def test_omitted_prefix_remains_empty_without_a_config_default(tmp_path, encoder, monkeypatch):
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    path = tmp_path / "fixture.jsonl"
    _dataset().to_jsonl(path)
    report = bench.run_benchmark(datasets=[path], modes=["semantic"], top_k=1,
                                sample_limit=1, seed=7, embedding_model="fixture-model",
                                embedding_dimensions=2)
    assert report["configuration"]["query_prefix"] == ""
    assert encoder.inputs == ["literal passage", "literal question"]


def test_cli_passes_prefix_without_normalization_or_model_loading(monkeypatch, capsys):
    captured = {}

    def run(**kwargs):
        captured.update(kwargs)
        return {"configuration": {"query_prefix": kwargs["query_prefix"]}}

    monkeypatch.setattr(bench, "run_benchmark", run)
    monkeypatch.setattr(sys, "argv", ["public-retrieval", "--dataset", "fixture.jsonl",
                                      "--mode", "semantic", "--query-prefix", " prefix:: "])
    bench.main()
    assert captured["query_prefix"] == " prefix:: "
    assert json.loads(capsys.readouterr().out)["configuration"]["query_prefix"] == " prefix:: "


def test_keyword_mode_reports_no_embedding_runtime_even_if_model_was_provided(tmp_path):
    result = bench.evaluate_dataset(_dataset(), mode="keyword", top_k=1,
                                    sample_limit=1, seed=7, data_dir=tmp_path,
                                    embedding_model="not-loaded", query_prefix="not-used")
    assert result["runtime_models"]["embedding"] is None


def test_unavailable_model_is_not_recorded_as_an_instantiated_runtime(tmp_path):
    result = bench.evaluate_dataset(_dataset(), mode="semantic", top_k=1,
                                    sample_limit=1, seed=7, data_dir=tmp_path,
                                    query_prefix="provided-but-not-applied")
    assert result["mode_status"] == "unavailable"
    assert result["runtime_models"]["embedding"] is None
