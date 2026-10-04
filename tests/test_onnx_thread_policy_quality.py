"""No-model, no-download tests for bounded local ONNX constructor threads."""

import os
import sys
from types import SimpleNamespace

import pytest

from app.integrations.local_reranker import FastEmbedCrossEncoderReranker
from app.integrations.local_semantic import FastEmbedEmbeddingProvider


def affinity(monkeypatch, cpus):
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(cpus)), raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 64)


@pytest.mark.parametrize("cpus,expected", [(1, 1), (2, 2), (3, 3), (4, 4), (14, 4)])
def test_auto_uses_affinity_and_caps_at_four(monkeypatch, cpus, expected):
    from app.integrations.onnx_runtime_policy import resolve_onnx_threads

    affinity(monkeypatch, cpus)
    assert resolve_onnx_threads(None) == expected


@pytest.mark.parametrize("cpu_count,expected", [(None, 1), (0, 1), (1, 1), (2, 2), (16, 4)])
@pytest.mark.parametrize("mode", ["missing", "unavailable", "unsupported", "empty"])
def test_native_windows_and_unavailable_affinity_have_safe_fallback(monkeypatch, cpu_count, expected, mode):
    from app.integrations.onnx_runtime_policy import resolve_onnx_threads

    if mode == "missing":
        monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    elif mode == "empty":
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(), raising=False)
    else:
        def unavailable(_pid):
            if mode == "unsupported":
                raise NotImplementedError("Affinity unsupported")
            raise OSError("Affinity unavailable")
        monkeypatch.setattr(os, "sched_getaffinity", unavailable, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: cpu_count)
    assert resolve_onnx_threads(None) == expected


@pytest.mark.parametrize("threads", [1, 2, 4, 8])
def test_explicit_positive_override_is_preserved_without_cpu_probe(monkeypatch, threads):
    from app.integrations.onnx_runtime_policy import resolve_onnx_threads

    monkeypatch.setattr(os, "sched_getaffinity", lambda _: pytest.fail("Explicit override must not probe"), raising=False)
    assert resolve_onnx_threads(threads) == threads


@pytest.mark.parametrize("threads", [0, -1, True, False, 1.5, "4"])
def test_invalid_override_is_rejected(threads):
    from app.integrations.onnx_runtime_policy import resolve_onnx_threads

    with pytest.raises(ValueError, match="threads"):
        resolve_onnx_threads(threads)


@pytest.mark.parametrize("threads,expected", [(None, 4), (1, 1), (2, 2), (4, 4)])
def test_embedding_lazy_kwargs_and_normalization_remain_compatible(monkeypatch, threads, expected):
    affinity(monkeypatch, 14)
    made, calls = [], []

    def factory(**kwargs):
        made.append(kwargs)

        def embed(values, **kwargs):
            calls.append((list(values), kwargs))
            return [SimpleNamespace(tolist=lambda: [3.0, 4.0]) for _ in values]

        return SimpleNamespace(embed=embed)

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=factory))
    provider = FastEmbedEmbeddingProvider(model_name="local/embed", dimensions=2,
        cache_dir="/cached", batch_size=16, query_prefix="query: ", threads=threads)
    assert made == []
    assert provider.embed_query("question") == pytest.approx([.6, .8])
    assert provider.embed_passages(["candidate"])[0] == pytest.approx([.6, .8])
    assert made == [{"model_name": "local/embed", "cache_dir": "/cached", "threads": expected,
                     "lazy_load": True, "local_files_only": True}]
    assert calls == [(["query: question"], {"batch_size": 16}),
                     (["candidate"], {"batch_size": 16})]


@pytest.mark.parametrize("threads,expected", [(None, 4), (1, 1), (2, 2), (4, 4)])
def test_cross_encoder_lazy_kwargs_and_score_order_remain_compatible(monkeypatch, threads, expected):
    affinity(monkeypatch, 14)
    made = []

    def factory(**kwargs):
        made.append(kwargs)
        return SimpleNamespace(rerank=lambda **_: [.25, .75])

    reranker = FastEmbedCrossEncoderReranker(model_name="local/rerank", cache_dir="/cached",
                                           model_factory=factory, threads=threads)
    assert made == [] and reranker.score("question", []) == []
    assert reranker.score("question", ["a", "b"]) == [.25, .75]
    assert made == [{"model_name": "local/rerank", "cache_dir": "/cached", "threads": expected,
                     "lazy_load": True, "local_files_only": True}]


@pytest.mark.parametrize("threads", [0, -1, True, False, 1.5, "4"])
@pytest.mark.parametrize("kind", ["embedding", "reranker"])
def test_constructor_rejects_invalid_threads_before_model_initialization(threads, kind):
    with pytest.raises(ValueError, match="threads"):
        if kind == "embedding":
            FastEmbedEmbeddingProvider(model_name="local/embed", dimensions=2,
                                       cache_dir="/cached", batch_size=16, threads=threads)
        else:
            FastEmbedCrossEncoderReranker(model_name="local/rerank", threads=threads,
                                         model_factory=lambda **_: pytest.fail("Must stay lazy"))
