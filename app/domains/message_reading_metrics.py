"""Deterministic scoring of explicit output reviews, never model self-grading.

Local files declare reviewer provenance; they are not authenticated production
approval records. Scores cannot enable analysis, approve matters or grant access.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domains.message_reading_replay import digest

Identity = Annotated[str, Field(min_length=1, max_length=120)]
Index = Annotated[int, Field(ge=0, le=100)]


class EventGold(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: Identity
    required: bool = False
    source_ids: list[Identity] = Field(min_length=1, max_length=30)


class WindowGold(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    events: list[EventGold] = Field(default_factory=list, max_length=30)
    topic_ids: list[Identity] = Field(default_factory=list, max_length=30)


class OutputJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    window_id: Identity
    artifact_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    matched_event_ids: list[Identity] = Field(default_factory=list, max_length=30)
    matched_topic_ids: list[Identity] = Field(default_factory=list, max_length=30)
    useful_highlight_indices: list[Index] = Field(default_factory=list, max_length=5)
    accepted_claim_indices: list[Index] = Field(default_factory=list, max_length=30)
    supported_output_items: int = Field(default=0, ge=0, le=1000)
    examined_output_items: int = Field(default=0, ge=0, le=1000)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def consistent_counts(self):
        if self.supported_output_items > self.examined_output_items:
            raise ValueError("supported_count_exceeds_examined")
        for values in (
            self.matched_event_ids,
            self.matched_topic_ids,
            self.useful_highlight_indices,
            self.accepted_claim_indices,
        ):
            if len(values) != len(set(values)):
                raise ValueError("duplicate_review_items")
        return self


class ReadingLabels(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    windows_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer_kind: Literal["human", "agent", "model"]
    review_complete: bool
    reviewer_id: str = Field(min_length=1, max_length=120)
    reviewed_at: str = Field(min_length=1, max_length=80)
    windows: dict[Identity, WindowGold] = Field(max_length=100)
    judgements: list[OutputJudgement] = Field(max_length=100)


def score_reviews(
    labels: dict, manifest: dict, windows: list[dict], artifacts: dict[str, dict]
) -> dict:
    review = ReadingLabels.model_validate(labels)
    if not review.review_complete:
        raise ValueError("manual_review_not_complete")
    if review.snapshot_id != manifest["snapshot_id"] or review.windows_digest != digest(windows):
        raise ValueError("labels_dataset_changed")
    supplied = {window["window_id"]: window for window in windows}
    for window_id, gold in review.windows.items():
        window = supplied.get(window_id)
        if window is None or gold.input_digest != window["digest"]:
            raise ValueError("labels_window_changed")
        source_ids = {row["id"] for row in window["messages"]}
        if any(set(event.source_ids) - source_ids for event in gold.events):
            raise ValueError("gold_source_outside_window")
        if len({event.event_id for event in gold.events}) != len(gold.events):
            raise ValueError("duplicate_gold_event")
        if len(set(gold.topic_ids)) != len(gold.topic_ids):
            raise ValueError("duplicate_gold_topic")
    scored, seen = [], set()
    for judgment in review.judgements:
        if judgment.artifact_id in seen:
            raise ValueError("duplicate_artifact_review")
        seen.add(judgment.artifact_id)
        record = artifacts.get(judgment.artifact_id)
        gold = review.windows.get(judgment.window_id)
        if (
            record is None
            or gold is None
            or record["window"] != judgment.window_id
            or digest(record) != judgment.output_digest
        ):
            raise ValueError("reviewed_output_changed")
        event_ids = {event.event_id for event in gold.events}
        required = {event.event_id for event in gold.events if event.required}
        if set(judgment.matched_event_ids) - event_ids or set(judgment.matched_topic_ids) - set(
            gold.topic_ids
        ):
            raise ValueError("unknown_gold_match")
        result = record.get("result") or {}
        highlights = result.get("highlights", [])[:5]
        claims = [
            claim
            for person in record.get("participant_candidates", [])
            for claim in person.get("claims", [])
            if claim.get("status") != "stale"
        ]
        valid = record["state"] == "completed"
        if (
            any(index >= len(highlights) for index in judgment.useful_highlight_indices)
            or any(index >= len(claims) for index in judgment.accepted_claim_indices)
            or not valid
            and (
                judgment.matched_event_ids
                or judgment.matched_topic_ids
                or judgment.useful_highlight_indices
                or judgment.accepted_claim_indices
            )
        ):
            raise ValueError("review_matches_invalid_output")
        scored.append(
            {
                "artifact_id": judgment.artifact_id,
                "window": judgment.window_id,
                "strategy": record["strategy"],
                "prompt_version": record["prompt_version"],
                "split": supplied[judgment.window_id]["split"],
                "analysis_completed": valid,
                "event_hits": len(judgment.matched_event_ids),
                "event_total": len(event_ids),
                "event_recall": len(judgment.matched_event_ids) / len(event_ids)
                if event_ids
                else None,
                "missed_event_ids": sorted(event_ids - set(judgment.matched_event_ids)),
                "missed_required_event_ids": sorted(required - set(judgment.matched_event_ids)),
                "topic_hits": len(judgment.matched_topic_ids),
                "topic_total": len(gold.topic_ids),
                "top5_useful": len(judgment.useful_highlight_indices),
                "top5_total": len(highlights),
                "top5_usefulness": len(judgment.useful_highlight_indices) / len(highlights)
                if highlights
                else None,
                "accepted_claims": len(judgment.accepted_claim_indices),
                "candidate_claims": len(claims),
                "profile_candidate_precision": len(judgment.accepted_claim_indices) / len(claims)
                if claims
                else None,
                "source_support": judgment.supported_output_items / judgment.examined_output_items
                if judgment.examined_output_items
                else None,
            }
        )
    human = review.reviewer_kind == "human"
    return {
        "snapshot_id": review.snapshot_id,
        "labels_digest": digest(labels),
        "reviewer_kind": review.reviewer_kind,
        "human_review_declared": human,
        "production_approval": False,
        "gold_status": "human_reviewed" if human else "provisional",
        "reviewed_outputs": len(scored),
        "unreviewed_outputs": len(artifacts) - len(seen),
        "unlabelled_windows": sorted(set(supplied) - set(review.windows)),
        "scores": scored,
        "limitation": "Manual output judgements; reviewer identity is not authenticated by a local file.",
    }
