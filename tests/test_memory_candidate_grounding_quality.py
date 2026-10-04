"""Adversarial model candidates must keep their source's semantic limits."""

import json
from types import SimpleNamespace

import pytest

from app.core.memory_extraction import (
    MEMORY_EXTRACTION_SYSTEM_PROMPT,
    extract_user_memories,
    memory_candidate_rejection_reasons,
)
from scripts.eval_personal_assistant_memory import (
    _candidate_rejection_reasons,
    _CountingClient,
    _force_model_extract,
)


class Client:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    def complete_text(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            content=json.dumps({"candidates": self.candidates}),
            finish_reason="stop", usage={},
        )


def _candidate(claim, evidence, *, kind="preference"):
    return {"claim": claim, "evidence": evidence, "kind": kind,
            "confidence": 0.95, "explicit": True}


def _extract(path, message, candidates):
    client = Client(candidates)
    if path == "production":
        result = extract_user_memories(
            source_id="grounding-review", content=message, llm_client=client, allow_remote=True,
        )
    else:
        result = _force_model_extract(
            "grounding-review", message, _CountingClient(client, 1, diagnostics=True),
        )
    return result, client


@pytest.mark.parametrize("path", ["production", "forced"])
@pytest.mark.parametrize("message,claim,evidence", [
    ("I don't prefer tables.", "prefer tables", "I don't prefer tables."),
    ("I don't prefer tables.", "prefer tables", "prefer tables"),
    ("Correction: I no longer prefer long summaries.", "prefer long summaries",
     "Correction: I no longer prefer long summaries."),
    ("For this answer only, I prefer bullet points.", "I prefer bullet points",
     "For this answer only, I prefer bullet points."),
    ("When reading unfamiliar material, I prefer a short summary.",
     "I prefer a short summary", "I prefer a short summary"),
    ("From now on, I prefer brief replies.", "I prefer brief replies",
     "From now on, I prefer brief replies."),
    ("我不希望用表格。", "用表格", "我不希望用表格。"),
    ("更正：我不再喜欢长篇回答。", "喜欢长篇回答", "更正：我不再喜欢长篇回答。"),
    ("仅这次回答，我偏好短段落。", "我偏好短段落", "我偏好短段落"),
    ("我希望不使用表格。", "偏好使用表格", "我希望不使用表格"),
    ("如果阅读陌生材料，我倾向简洁摘要。", "我倾向简洁摘要", "我倾向简洁摘要"),
    ("I prefer prose. Today only, I prefer prose.", "I prefer prose", "I prefer prose"),
])
def test_model_cannot_cut_negation_or_scope_out_of_source_clause(path, message, claim, evidence):
    item = _candidate(claim, evidence)
    result, _ = _extract(path, message, [item])
    assert result == []
    assert any(reason.startswith("claim_omits_")
               for reason in _candidate_rejection_reasons(item, message))


@pytest.mark.parametrize("path", ["production", "forced"])
@pytest.mark.parametrize("message,claim,evidence", [
    ("I prefer not using tables.", "I prefer not using tables", "I prefer not using tables."),
    ("I don't prefer tables.", "I don't prefer tables", "I don't prefer tables."),
    ("I don't prefer tables, but I prefer prose.", "I prefer prose",
     "I don't prefer tables, but I prefer prose."),
    ("我偏好简短的摘要。", "偏好简短摘要", "我偏好简短的摘要"),
    ("我倾向不使用表格。", "偏好不使用表格", "我倾向不使用表格"),
    ("我希望以后默认使用中文。", "以后默认使用中文", "我希望以后默认使用中文"),
    ("When reading unfamiliar material, I prefer a short summary.",
     "When reading unfamiliar material, I prefer a short summary",
     "When reading unfamiliar material, I prefer a short summary."),
    ("如果阅读陌生材料，我倾向简洁摘要。", "如果阅读陌生材料，我倾向简洁摘要",
     "如果阅读陌生材料，我倾向简洁摘要。"),
    ("我不希望用表格，但是我希望用短段落。", "我希望用短段落",
     "我不希望用表格，但是我希望用短段落。"),
])
def test_source_faithful_negation_conditions_and_positive_paraphrase_remain_valid(
    path, message, claim, evidence,
):
    item = _candidate(claim, evidence)
    result, _ = _extract(path, message, [item])
    assert result
    # Local deterministic extraction retains its existing canonical wording.
    if path == "forced":
        assert result[0].claim == claim
    assert _candidate_rejection_reasons(item, message) == []


@pytest.mark.parametrize("path", ["production", "forced"])
@pytest.mark.parametrize("kind", [[], {}, None, 1])
def test_malformed_kind_skips_only_bad_candidate_not_valid_peer(path, kind):
    message = "A durable statement: I prefer brief replies."
    bad = _candidate("I prefer brief replies", message, kind=kind)
    good = _candidate("I prefer brief replies", message)
    result, client = _extract(path, message, [bad, good])
    assert len(result) == 1
    assert result[0].claim == good["claim"]
    assert client.calls == 1
    assert _candidate_rejection_reasons(bad, message) == ["invalid_kind"]


def test_existing_local_correction_and_negation_paths_are_unchanged():
    client = Client([])
    negative = extract_user_memories(source_id="local-negative", content="我不喜欢简洁回答")
    corrected = extract_user_memories(
        source_id="local-correction", content="请改为以后用短段落说明。", llm_client=client,
        allow_remote=True,
    )
    assert negative == []
    assert [item.claim for item in corrected] == ["以后用短段落说明"]
    assert client.calls == 0


def test_prompt_keeps_enduring_negative_preferences_distinct_from_retractions():
    assert "Retain an enduring negative preference only with its negation intact" in MEMORY_EXTRACTION_SYSTEM_PROMPT
    assert "Ignore corrections and retractions" in MEMORY_EXTRACTION_SYSTEM_PROMPT


def test_diagnostics_and_forced_eval_use_the_production_validator():
    assert _candidate_rejection_reasons is memory_candidate_rejection_reasons
    message = "A durable statement: I prefer brief replies."
    bad = _candidate("I prefer brief replies", message, kind={})
    good = _candidate("I prefer brief replies", message)
    client = _CountingClient(Client([bad, good]), 1, diagnostics=True)
    result = _force_model_extract("malformed-diagnostic", message, client)
    assert [candidate.claim for candidate in result] == [good["claim"]]
    assert [item["rejection_reasons"] for item in client.responses[0]["candidate_validation"]] == [
        ["invalid_kind"], [],
    ]


def test_repeated_short_evidence_scans_its_source_clause_only_once(monkeypatch):
    from app.core import memory_extraction

    class Pattern:
        calls = 0

        def finditer(self, clause):
            self.calls += 1
            return iter(())

    pattern = Pattern()
    monkeypatch.setattr(memory_extraction, "_GROUNDING_QUALIFIERS", (("unused", pattern),))
    assert memory_candidate_rejection_reasons(_candidate("z", "z"), "z" * 20_000) == []
    assert pattern.calls == 1
