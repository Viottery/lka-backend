"""Optional local FastEmbed cross-encoder reranking adapter."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Sequence
from typing import Any


class LocalRerankerError(RuntimeError):
    """Recoverable adapter error; callers can continue with the original ranking."""


ModelFactory = Callable[..., Any]


class FastEmbedCrossEncoderReranker:
    """Score query/candidate pairs with a lazily loaded, local FastEmbed model.

    ``score`` returns one finite float per retained candidate, aligned with the
    input order. This synchronous adapter does not move inference off the caller's
    thread; async callers should run it in their own worker thread.
    """

    def __init__(
        self,
        *,
        model_name: str,
        cache_dir: str | None = None,
        batch_size: int = 16,
        max_candidates: int = 50,
        max_query_chars: int = 4000,
        max_candidate_chars: int = 8000,
        max_concurrent_inferences: int = 1,
        queue_timeout_ms: int = 25,
        local_files_only: bool = True,
        model_factory: ModelFactory | None = None,
    ) -> None:
        if not model_name:
            raise ValueError("model_name must not be empty")
        if batch_size < 1 or max_candidates < 1:
            raise ValueError("batch_size and max_candidates must be positive")
        if max_query_chars < 1 or max_candidate_chars < 1:
            raise ValueError("text limits must be positive")
        if max_concurrent_inferences < 1 or queue_timeout_ms < 0:
            raise ValueError("inference concurrency must be positive and queue timeout non-negative")
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.batch_size = batch_size
        self.max_candidates = max_candidates
        self.max_query_chars = max_query_chars
        self.max_candidate_chars = max_candidate_chars
        self.local_files_only = local_files_only
        self.queue_timeout_ms = queue_timeout_ms
        self._model_factory = model_factory
        self._model: Any | None = None
        self._load_error: LocalRerankerError | None = None
        self._load_lock = threading.Lock()
        self._inference_slots = threading.BoundedSemaphore(max_concurrent_inferences)

    def score(self, query: str, candidates: Sequence[str]) -> list[float]:
        """Return finite scores aligned to up to ``max_candidates`` inputs.

        Candidate and query text is deterministically truncated before inference.
        An empty candidate sequence returns immediately without importing/loading
        FastEmbed.
        """
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if not candidates:
            return []
        if any(not isinstance(text, str) for text in candidates):
            raise ValueError("all candidates must be strings")

        texts = [text[: self.max_candidate_chars] for text in candidates[: self.max_candidates]]
        bounded_query = query[: self.max_query_chars]
        if not self._inference_slots.acquire(timeout=self.queue_timeout_ms / 1000):
            raise LocalRerankerError("local cross-encoder is busy; fusion ranking was retained")
        try:
            ranked = self._get_model().rerank(
                query=bounded_query,
                documents=texts,
                batch_size=self.batch_size,
            )
            scores = self._normalize_scores(ranked, len(texts))
        except LocalRerankerError:
            raise
        except Exception as exc:
            raise LocalRerankerError(f"local cross-encoder scoring failed: {exc}") from exc
        finally:
            self._inference_slots.release()
        return scores

    def _get_model(self) -> Any:
        if self._load_error is not None:
            raise self._load_error
        if self._model is None:
            with self._load_lock:
                if self._load_error is not None:
                    raise self._load_error
                if self._model is None:
                    try:
                        factory = self._model_factory
                        if factory is None:
                            from fastembed.rerank.cross_encoder import TextCrossEncoder

                            factory = TextCrossEncoder
                        kwargs: dict[str, Any] = {
                            "model_name": self.model_name,
                            "lazy_load": True,
                            "local_files_only": self.local_files_only,
                        }
                        if self.cache_dir is not None:
                            kwargs["cache_dir"] = self.cache_dir
                        self._model = factory(**kwargs)
                    except Exception as exc:
                        self._load_error = LocalRerankerError(
                            f"could not initialize local FastEmbed cross-encoder: {exc}"
                        )
                        raise self._load_error from exc
        return self._model

    @staticmethod
    def _normalize_scores(ranked: Any, expected_count: int) -> list[float]:
        try:
            results = list(ranked)
            if len(results) != expected_count:
                raise ValueError(
                    f"expected {expected_count} reranker results, received {len(results)}"
                )
            scores: list[float | None] = [None] * expected_count
            for position, item in enumerate(results):
                index = getattr(item, "index", position)
                raw_score = getattr(item, "score", item)
                index = int(index)
                score = float(raw_score)
                if index < 0 or index >= expected_count or scores[index] is not None:
                    raise ValueError("reranker returned invalid or duplicate candidate index")
                if not math.isfinite(score):
                    raise ValueError("reranker returned a non-finite score")
                scores[index] = score
            if any(score is None for score in scores):
                raise ValueError("reranker omitted one or more candidate scores")
            return [float(score) for score in scores if score is not None]
        except LocalRerankerError:
            raise
        except Exception as exc:
            raise LocalRerankerError(f"invalid cross-encoder results: {exc}") from exc
