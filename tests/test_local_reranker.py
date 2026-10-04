from dataclasses import dataclass

import pytest

from app.integrations.local_reranker import FastEmbedCrossEncoderReranker, LocalRerankerError


@dataclass
class Result:
    index: int
    score: float


class FakeCrossEncoder:
    def __init__(self, results=None):
        self.results = results
        self.calls = []

    def rerank(self, **kwargs):
        self.calls.append(kwargs)
        return self.results if self.results is not None else [
            Result(index=i, score=float(i)) for i in reversed(range(len(kwargs["documents"])))
        ]


def test_lazy_local_initialization_and_scores_are_input_aligned():
    made = []

    def factory(**kwargs):
        made.append(kwargs)
        return FakeCrossEncoder()

    reranker = FastEmbedCrossEncoderReranker(
        model_name="local/model", cache_dir="/models", model_factory=factory
    )
    assert made == []
    assert reranker.score("query", ["a", "b", "c"]) == [0.0, 1.0, 2.0]
    assert made == [{
        "model_name": "local/model",
        "threads": reranker.threads,
        "lazy_load": True,
        "local_files_only": True,
        "cache_dir": "/models",
    }]


def test_empty_candidates_do_not_initialize_model():
    def factory(**kwargs):
        pytest.fail("factory should remain lazy")

    assert FastEmbedCrossEncoderReranker(
        model_name="local/model", model_factory=factory
    ).score("query", []) == []


def test_candidate_count_and_text_lengths_are_bounded():
    model = FakeCrossEncoder()
    reranker = FastEmbedCrossEncoderReranker(
        model_name="local/model",
        model_factory=lambda **kwargs: model,
        max_candidates=2,
        max_query_chars=3,
        max_candidate_chars=2,
    )
    assert len(reranker.score("query", ["abcd", "efgh", "ignored"])) == 2
    assert model.calls[0]["query"] == "que"
    assert model.calls[0]["documents"] == ["ab", "ef"]


@pytest.mark.parametrize(
    "results",
    [
        [Result(0, float("nan"))],
        [Result(1, 0.5)],
        [Result(0, 0.2), Result(0, 0.3)],
        [],
    ],
)
def test_invalid_scores_raise_recoverable_error(results):
    reranker = FastEmbedCrossEncoderReranker(
        model_name="local/model",
        model_factory=lambda **kwargs: FakeCrossEncoder(results),
    )
    with pytest.raises(LocalRerankerError):
        reranker.score("query", ["candidate"] * max(1, len(results)))


def test_missing_fastembed_is_reported_as_recoverable(monkeypatch):
    def missing_import(*args, **kwargs):
        raise ImportError("FastEmbed unavailable")

    reranker = FastEmbedCrossEncoderReranker(
        model_name="local/model", model_factory=missing_import
    )
    with pytest.raises(LocalRerankerError, match="could not initialize"):
        reranker.score("query", ["candidate"])


def test_inference_failure_is_reported_as_recoverable():
    class BrokenModel:
        def rerank(self, **kwargs):
            raise OSError("model files missing")

    reranker = FastEmbedCrossEncoderReranker(
        model_name="local/model", model_factory=lambda **kwargs: BrokenModel()
    )
    with pytest.raises(LocalRerankerError, match="scoring failed"):
        reranker.score("query", ["candidate"])


def test_busy_local_model_fails_fast_for_fusion_fallback():
    reranker = FastEmbedCrossEncoderReranker(
        model_name="local/model", queue_timeout_ms=0,
        model_factory=lambda **kwargs: FakeCrossEncoder([0.5]),
    )
    assert reranker._inference_slots.acquire(blocking=False)
    try:
        with pytest.raises(LocalRerankerError, match="busy"):
            reranker.score("query", ["candidate"])
    finally:
        reranker._inference_slots.release()
