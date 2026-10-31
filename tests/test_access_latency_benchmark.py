"""Measurement semantics: internal progress is not user-visible answer latency."""
from scripts.benchmark_access_latency import answer_delta, stats


def test_first_text_does_not_count_hidden_drafts_or_control_events():
    assert not answer_delta("run_started", {})
    assert not answer_delta("final_answer", {"message": "complete"})
    assert not answer_delta("llm_delta", {"payload": {
        "content_role": "route_decision", "delta": "internal process text",
    }})
    assert not answer_delta("llm_delta", {"payload": {
        "content_role": "answer_draft", "delta": "unreviewed text",
    }})


def test_first_text_requires_a_nonempty_visible_answer_delta():
    assert answer_delta("llm_delta", {"payload": {
        "content_role": "final_answer", "delta": "approved text",
    }})
    assert not answer_delta("llm_delta", {"payload": {
        "content_role": "final_answer", "delta": "",
    }})


def test_small_sample_percentiles_do_not_interpolate_beyond_observed_tail():
    values = list(range(24))
    report = stats(values)
    assert report == {"n": 24, "p50_ms": 11, "p95_ms": 22, "p99_ms": 23, "max_ms": 23}
    assert stats([]) == {"n": 0}
