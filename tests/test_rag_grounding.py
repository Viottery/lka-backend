from evals.lka_evals.rag_grounding import evaluate_grounding, extract_citations


def test_grounding_metric_uses_only_successful_tool_evidence():
    from evals.lka_evals.metrics import evaluate_case
    from evals.lka_evals.subject import EvalRunArtifact

    artifact = EvalRunArtifact(
        suite_id="rag", case_id="citation", subject="runtime", request={},
        result={
            "answer": "The policy requires approval [policy#chunk=0].",
            "tool_events": [
                {"tool_name": "knowledge.search", "result": {
                    "status": "completed", "output": {"results": [
                        {"source_ref": "policy#chunk=0", "policy_decision": "allowed"},
                    ]},
                }},
                {"tool_name": "knowledge.load_chunks", "result": {
                    "status": "rejected", "output": {"chunks": [
                        {"source_ref": "untrusted#chunk=0", "policy_decision": "allowed"},
                    ]},
                }},
            ],
        },
    )
    metrics = evaluate_case({"expect": {"knowledge_grounding": {
        "evidence_sufficient": True,
    }}}, artifact)
    check = next(item for item in metrics if item.name == "knowledge_grounding")

    assert check.passed is True
    artifact.result["answer"] = "Unsupported [untrusted#chunk=0]."
    failed = evaluate_case({"expect": {"knowledge_grounding": {
        "evidence_sufficient": True,
    }}}, artifact)
    check = next(item for item in failed if item.name == "knowledge_grounding")
    assert check.passed is False
    assert check.details["unsupported_citation_count"] == 1


def test_extract_citations_deduplicates_in_first_seen_order():
    assert extract_citations("Claim [doc-a], then [doc-b], again [doc-a].") == (
        "doc-a", "doc-b"
    )


def test_grounding_checks_present_valid_citations_and_supported_answer():
    result = evaluate_grounding(
        "The policy says two reviewers are needed [policy.md].",
        permitted_source_refs={"policy.md"}, evidence_sufficient=True,
    )

    assert result.citation_presence is True
    assert result.citation_validity is True
    assert result.expected_abstention is True
    assert result.unsupported_citation_count == 0


def test_grounding_counts_unsupported_refs_and_requires_citation():
    result = evaluate_grounding(
        "A claim cites [allowed] and [unknown].",
        permitted_source_refs={"allowed"}, evidence_sufficient=True,
    )
    no_citation = evaluate_grounding(
        "A plain answer.", permitted_source_refs={"allowed"}, evidence_sufficient=True,
    )

    assert result.citation_validity is False
    assert result.unsupported_citation_count == 1
    assert result.unsupported_citations == ("unknown",)
    assert no_citation.citation_presence is False
    assert no_citation.citation_validity is False


def test_expected_abstention_uses_explicit_literal_markers():
    insufficient = evaluate_grounding(
        "证据不足，无法回答。", permitted_source_refs=(), evidence_sufficient=False,
    )
    unsupported_answer = evaluate_grounding(
        "The answer is 42.", permitted_source_refs=(), evidence_sufficient=False,
    )
    unnecessary_abstention = evaluate_grounding(
        "Insufficient evidence to answer.", permitted_source_refs={"doc"},
        evidence_sufficient=True,
    )

    assert insufficient.expected_abstention is True
    assert unsupported_answer.expected_abstention is False
    assert unnecessary_abstention.expected_abstention is False


def test_suite_grounding_metric_flags_fixture_annotated_unsupported_claim():
    from evals.lka_evals.metrics import evaluate_case
    from evals.lka_evals.subject import EvalRunArtifact

    artifact = EvalRunArtifact(
        suite_id="rag-grounding", case_id="unsupported-claim", subject="runtime", request={},
        result={"answer": "The policy requires manager approval [policy#approval].",
                "tool_events": [{"tool_name": "knowledge.search", "result": {
                    "status": "completed", "output": {"results": [{
                        "source_ref": "policy#approval", "policy_decision": "allowed",
                        "text": "The policy requires employee approval.",
                    }]}}}]},
    )
    checks = evaluate_case({"expect": {"knowledge_grounding": {
        "evidence_sufficient": True,
        "claims": [{"text": "manager approval", "supported": False}],
    }}}, artifact)
    grounding = next(item for item in checks if item.name == "knowledge_grounding")
    assert not grounding.passed
    assert grounding.details["citation_validity"] is True
    assert grounding.details["unsupported_claims"] == ("manager approval",)


def test_literal_citation_validity_is_not_semantic_claim_correctness():
    result = evaluate_grounding(
        "The policy requires manager approval [policy#approval].",
        permitted_source_refs={"policy#approval"}, evidence_sufficient=True,
        claim_oracle=[{"text": "manager approval", "supported": False}],
    )
    assert result.citation_validity is True
    assert result.unsupported_claims == ("manager approval",)
