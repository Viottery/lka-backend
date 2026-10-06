"""Tiny synthetic review-gate tests; no model, source or live database."""

import copy

import pytest

from app.domains.message_reading_metrics import score_reviews
from app.domains.message_reading_replay import digest


def sample():
    windows = [
        {
            "window_id": "c1w01",
            "digest": "a" * 64,
            "split": "development",
            "messages": [{"id": "m1"}],
        }
    ]
    result = {
        "window": "c1w01",
        "state": "completed",
        "strategy": "compact",
        "prompt_version": "v3",
        "result": {"highlights": [{"quote": "hello"}]},
        "participant_candidates": [{"claims": [{"status": "candidate"}]}],
    }
    labels = {
        "schema_version": 1,
        "snapshot_id": "b" * 64,
        "windows_digest": digest(windows),
        "reviewer_kind": "human",
        "review_complete": True,
        "reviewer_id": "local-user",
        "reviewed_at": "2026-10-05T14:00:00+08:00",
        "windows": {"c1w01": {"input_digest": "a" * 64, "events": [], "topic_ids": []}},
        "judgements": [
            {
                "window_id": "c1w01",
                "artifact_id": "c" * 64,
                "output_digest": digest(result),
                "useful_highlight_indices": [0],
                "accepted_claim_indices": [0],
            }
        ],
    }
    return labels, {"snapshot_id": "b" * 64}, windows, {"c" * 64: result}


def test_human_scores_keep_empty_event_recall_unknown_and_never_activate():
    report = score_reviews(*sample())
    assert report["gold_status"] == "human_reviewed"
    assert report["scores"][0]["event_recall"] is None
    assert report["scores"][0]["profile_candidate_precision"] == 1
    assert report["production_approval"] is False


def test_agent_review_cannot_become_human_gold():
    args = sample()
    args[0]["reviewer_kind"] = "agent"
    assert score_reviews(*args)["gold_status"] == "provisional"
    args[0]["review_complete"] = False
    with pytest.raises(ValueError, match="not_complete"):
        score_reviews(*args)


def test_changed_dataset_or_output_is_rejected():
    args = sample()
    args[0]["windows_digest"] = "f" * 64
    with pytest.raises(ValueError, match="dataset_changed"):
        score_reviews(*args)
    args = sample()
    args[3]["c" * 64]["result"] = {}
    with pytest.raises(ValueError, match="output_changed"):
        score_reviews(*args)


def test_critical_correction_missing_is_explicit_not_hidden_in_average():
    args = sample()
    args[0]["windows"]["c1w01"]["events"] = [
        {"event_id": "deadline-correction", "required": True, "source_ids": ["m1"]}
    ]
    report = score_reviews(*args)
    assert report["scores"][0]["event_recall"] == 0
    assert report["scores"][0]["missed_required_event_ids"] == ["deadline-correction"]


def test_failed_output_cannot_claim_hits_and_duplicate_review_is_rejected():
    args = sample()
    args[3]["c" * 64]["state"] = "evidence_rejected"
    args[0]["judgements"][0]["output_digest"] = digest(args[3]["c" * 64])
    with pytest.raises(ValueError, match="invalid_output"):
        score_reviews(*args)
    args = sample()
    args[0]["judgements"].append(copy.deepcopy(args[0]["judgements"][0]))
    with pytest.raises(ValueError, match="duplicate_artifact"):
        score_reviews(*args)
