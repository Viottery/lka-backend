"""Deterministic citation and abstention checks for RAG answer evaluations.

These checks validate references against supplied evidence. They do not assess
whether answer text is factually or semantically correct.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass

_BRACKET_CITATION = re.compile(r"\[([^\[\]]+)\]")


@dataclass(frozen=True)
class GroundingResult:
    citation_presence: bool
    citation_validity: bool
    expected_abstention: bool
    unsupported_citation_count: int
    citations: tuple[str, ...]
    unsupported_citations: tuple[str, ...]
    unsupported_claims: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable result."""
        return asdict(self)


def extract_citations(answer: str) -> tuple[str, ...]:
    """Extract unique ``[source-ref]`` citations in first-seen order."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _BRACKET_CITATION.finditer(answer):
        citation = match.group(1).strip()
        if citation and citation not in seen:
            found.append(citation)
            seen.add(citation)
    return tuple(found)


def evaluate_grounding(
    answer: str,
    *,
    permitted_source_refs: Iterable[str],
    evidence_sufficient: bool,
    abstention_markers: Iterable[str] = ("insufficient evidence", "无法从现有资料", "证据不足"),
    claim_oracle: Iterable[dict[str, object]] = (),
) -> GroundingResult:
    """Check citations and expected abstention against explicit case evidence.

    Citation syntax is ``[source-ref]``. Abstention is detected by a
    case-insensitive literal marker supplied by the evaluator; it is not a
    semantic judgment about the answer.
    """
    citations = extract_citations(answer)
    permitted = {str(ref).strip() for ref in permitted_source_refs if str(ref).strip()}
    unsupported = tuple(citation for citation in citations if citation not in permitted)
    markers = tuple(marker.casefold() for marker in abstention_markers if marker)
    abstained = any(marker in answer.casefold() for marker in markers)
    expected_abstention = (not evidence_sufficient) == abstained
    # These are explicit fixture annotations, not automatically inferred facts.
    # A claim is a violation only when the exact literal claim appears in answer.
    unsupported_claims = tuple(
        str(item.get("text", "")) for item in claim_oracle
        if item.get("supported") is False
        and str(item.get("text", ""))
        and str(item["text"]).casefold() in answer.casefold()
    )
    return GroundingResult(
        citation_presence=bool(citations),
        citation_validity=bool(citations) and not unsupported,
        expected_abstention=expected_abstention,
        unsupported_citation_count=len(unsupported),
        citations=citations,
        unsupported_citations=unsupported,
        unsupported_claims=unsupported_claims,
    )
